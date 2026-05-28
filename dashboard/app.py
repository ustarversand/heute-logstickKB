#!/usr/bin/env python3
# 货易达物流看板 — FastAPI 后端（恢复版）
import json, os, sys, time, csv, hashlib, urllib, uvicorn, subprocess, shutil, ssl, threading, re, sqlite3
from datetime import datetime, timedelta
from collections import Counter, defaultdict
from fastapi import FastAPI, Query, HTTPException, Request, Body, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from heute_db import (
    init_db, DB, DB_PATH, get_orders, get_order_count, get_senders,
    get_tracking, get_tracking_status_dist, get_tracking_by_no, search_tracking,
    get_anomalies, get_anomaly_counts, count_anomalies_by_sender,
    get_month_overview, upsert_tracking, bulk_upsert_orders,
    store_apollo_order, get_apollo_orders, get_apollo_order_count, delete_apollo_order,
    create_after_sales, get_after_sales, update_after_sales, delete_after_sales,
    get_after_sales_stats, search_after_sales, lookup_order_by_tracking,
    set_manual_status, clear_manual_status, get_manual_statuses,
    get_finance, sync_finance,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, '..', 'data')
PROGRESS_FILE = os.path.join(DATA_DIR, '.track_progress.json')
_batch_lock = threading.Lock()
_batch_in_progress = False
ORDER_SYNC_FILE = os.path.join(DATA_DIR, '.order_sync_progress.json')
_order_sync_lock = threading.Lock()
_order_sync_in_progress = False

init_db()
app = FastAPI(title='货易达物流看板 (SQLite)', version='2.0')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])
app.add_middleware(SessionMiddleware, secret_key='heute-dashboard-secret-2026', max_age=86400)

USERS_PATH = os.path.join(DATA_DIR, 'users.json')
def load_users():
    if os.path.exists(USERS_PATH):
        try:
            with open(USERS_PATH, encoding='utf-8') as f: return json.load(f)
        except: return {}
    return {}
def save_users(users):
    with open(USERS_PATH, 'w', encoding='utf-8') as f: json.dump(users, f, ensure_ascii=False, indent=2)
def get_sender_from_session(request):
    role = request.session.get('role', '')
    return None if role == 'admin' else request.session.get('sender', None)
def format_state(s):
    return {0:'已作废',1:'待支付',2:'待入库',3:'国际运输',4:'国内配送',5:'签收',-6:'已撤销'}.get(s, f'状态{s}')

# ─── Auth ───
@app.get('/api/auth/me')
def auth_me(request: Request):
    return {'logged_in': bool(request.session.get('role')), 'username': request.session.get('username',''),
            'role': request.session.get('role',''), 'sender': request.session.get('sender','')}

@app.post('/api/auth/login')
def auth_login(request: Request, data: dict):
    u, p = data.get('username','').strip(), data.get('password','').strip()
    if not u or not p: raise HTTPException(400, '缺少用户名或密码')
    users = load_users()
    user = users.get(u)
    if not user or user.get('password') != p: raise HTTPException(401, '用户名或密码错误')
    role = user.get('role', 'sender')
    request.session['sender'] = u if role == 'sender' else None
    request.session['role'] = role
    request.session['username'] = u
    return {'ok': True, 'user': u, 'role': role}

@app.post('/api/auth/logout')
def auth_logout(request: Request):
    request.session.clear(); return {'ok': True}

@app.get('/api/senders')
def api_senders(): return {'senders': get_senders()}

# ─── Stats (with cache) ───
_STATS_CACHE = {}
_STATS_CACHE_TTL = 5  # seconds

def _get_stats_cached(month, sender):
    import hashlib
    key = hashlib.md5(f"{month}:{sender}".encode()).hexdigest()
    now = time.time()
    if key in _STATS_CACHE:
        ts, data = _STATS_CACHE[key]
        if now - ts < _STATS_CACHE_TTL:
            return data
    data = _compute_stats(month, sender)
    _STATS_CACHE[key] = (now, data)
    return data

def _apply_overrides_to_track_status(month, sender, track_status):
    """Overlay manual status overrides onto the track_status distribution dict"""
    try:
        import sqlite3
        from heute_db import DB_PATH
        db = sqlite3.connect(DB_PATH)
        if sender:
            rows = db.execute("""
                SELECT ms.manual_status, t.current_status
                FROM order_status_overrides ms
                JOIN orders o ON ms.sn = o.sn AND ms.month = o.month
                LEFT JOIN tracking t ON o.global_waybill_sn = t.tracking_no AND t.month = o.month
                WHERE ms.month=? AND o.sender_name=?
            """, (month, sender)).fetchall()
        else:
            rows = db.execute("""
                SELECT ms.manual_status, t.current_status
                FROM order_status_overrides ms
                JOIN orders o ON ms.sn = o.sn AND ms.month = o.month
                LEFT JOIN tracking t ON o.global_waybill_sn = t.tracking_no AND t.month = o.month
                WHERE ms.month=?
            """, (month,)).fetchall()
        db.close()
        for manual_status, old_status in rows:
            # Strip emoji prefix: "✅ 已签收" → "已签收"
            clean = manual_status.split(' ', 1)[-1] if ' ' in manual_status else manual_status
            # Decrement old tracking-based status (skip if no tracking record)
            if old_status:
                track_status[old_status] = max(0, track_status.get(old_status, 0) - 1)
            elif old_status is None:
                # No tracking record — nothing to decrement
                pass
            # Increment manual status
            track_status[clean] = track_status.get(clean, 0) + 1
    except Exception:
        import traceback; traceback.print_exc()

def _compute_stats(month, sender):
    from heute_db import get_orders, get_tracking_status_dist, get_anomaly_counts
    orders = get_orders(month, sender)
    state_dist = Counter()
    track_status = get_tracking_status_dist(month, sender)
    _apply_overrides_to_track_status(month, sender, track_status)
    for o in orders: state_dist[format_state(o.get('state'))] += 1
    cancelled_count = sum(1 for o in orders if o.get('state') == -6)
    signed_total = track_status.get('已签收', 0) + track_status.get('签收(国内确认)', 0)
    signed_domestic_only = track_status.get('签收(国内确认)', 0)
    anomalies = get_anomaly_counts(month, sender)
    abnormal_count = anomalies.get('severe', 0)
    tracked = sum(track_status.values())
    # Collect abnormal orders for the 异常/问题件 tab
    abnormal_orders = []
    for o in orders:
        st = o.get('state', 0)
        if st not in (5, -6, 0):  # not 签收/已撤销/已作废
            abnormal_orders.append({
                'sn': o.get('sn',''),
                'consignee': o.get('consignee_name',''),
                'state': format_state(st),
                'sender': o.get('sender_name',''),
                'tracking': o.get('global_waybill_sn','') or '',
                'created': (o.get('creation_time','') or '')[:19].replace('T',' ') if o.get('creation_time') else ''
            })
    # Also add orders from anomalies if available
    try:
        from heute_db import get_anomalies
        severe_orders = get_anomalies(month, match_filter='severe', sender=sender)
        existing_sns = {a['sn'] for a in abnormal_orders if 'sn' in a}
        for s in severe_orders:
            sn = s.get('order_sn') or s.get('sn') or ''
            if sn and sn not in existing_sns:
                abnormal_orders.append({
                    'sn': sn,
                    'consignee': s.get('consignee_name',''),
                    'state': '问题件',
                    'sender': s.get('sender_name',''),
                    'tracking': s.get('intl_tracking','') or '',
                    'created': ''
                })
    except: pass
    return {'total_orders': len(orders), 'tracked': tracked, 'untracked': len(orders) - tracked,
            'tracking_status': track_status, 'state_distribution': dict(state_dist),
            'cancelled_count': cancelled_count, 'signed_total': signed_total,
            'signed_domestic_only': signed_domestic_only, 'abnormal_count': abnormal_count,
            'abnormal_orders': abnormal_orders}

# ─── Stats ───
@app.get('/api/stats')
def get_stats(request: Request, month: str = 'may'):
    sender = get_sender_from_session(request)
    return _get_stats_cached(month, sender)

@app.get('/api/stats/force')
def get_stats_force(request: Request, month: str = 'may'):
    """强制刷新统计（跳过缓存）"""
    sender = get_sender_from_session(request)
    return _compute_stats(month, sender)

# ─── Orders ───
@app.get('/api/orders/recent')
def get_recent_orders(request: Request, limit: int = 50, month: str = 'may'):
    sender = get_sender_from_session(request)
    orders = get_orders(month, sender)
    return sorted(orders, key=lambda o: o.get('creation_time','') or '', reverse=True)[:limit]

@app.get("/api/orders/query")
def api_orders_query(request: Request, page: int = 1, per_page: int = 50,
                     q: str = "", status: str = "", sender: str = "",
                     date_from: str = "", date_to: str = "", month: str = "may",
                     sort_by: str = "creation_time", sort_order: str = "desc"):
    logged_sender = get_sender_from_session(request)
    try:
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        where_clauses = ["o.month=?"]
        params = [month]
        if logged_sender:
            where_clauses.append("o.sender_name=?")
            params.append(logged_sender)
        elif sender:
            where_clauses.append("o.sender_name=?")
            params.append(sender)
        if q:
            like_sql = "(o.sn LIKE ? OR o.consignee_name LIKE ? OR o.global_waybill_sn LIKE ?)"
            where_clauses.append(like_sql)
            kw = "%" + q + "%"
            params.extend([kw, kw, kw])
        state_map = {"作废":0,"待支付":1,"国际运输":3,"国内配送":4,"签收":5,"已签收":5,"已撤销":-6}
        track_status_map = {"清关中":["清关中","已清关"],"国际运输":["在途","离开","已揽收","快件航班批次已创建","国际运输"],"国内配送":["到达","转寄","派件中"],"问题件":["问题件"],"其他":["其他"]}
        if status:
            if status == "待入库":
                where_clauses.append("(o.global_waybill_sn IS NOT NULL AND o.global_waybill_sn != '' AND (t.current_status IS NULL OR t.current_status = '运单已经创建'))")
            elif status in track_status_map:
                ts = track_status_map[status]
                placeholders = ",".join(["?" for _ in ts])
                where_clauses.append("t.current_status IN (" + placeholders + ")")
                params.extend(ts)
            else:
                sv = state_map.get(status)
                if sv is not None:
                    where_clauses.append("o.state=?")
                    params.append(sv)
        if date_from:
            where_clauses.append("o.creation_time >= ?")
            params.append(date_from)
        if date_to:
            where_clauses.append("o.creation_time <= ?")
            params.append(date_to + " 23:59:59")
        where_sql = " AND ".join(where_clauses)
        sort_columns = {"sn":"o.sn","state":"o.state","sender_name":"o.sender_name",
                        "creation_time":"o.creation_time","consignee_name":"o.consignee_name",
                        "global_waybill_sn":"o.global_waybill_sn"}
        col = sort_columns.get(sort_by, "o.creation_time")
        ord_dir = "DESC" if sort_order == "desc" else "ASC"
        # Use LEFT JOIN in COUNT query too (for tracking status filters)
        has_track_filter = any("t." in w for w in where_clauses)
        from_sql = " FROM orders o LEFT JOIN tracking t ON o.global_waybill_sn = t.tracking_no AND t.month = o.month " if has_track_filter else " FROM orders o "
        count_row = db.execute("SELECT COUNT(*) as cnt" + from_sql + "WHERE " + where_sql, params).fetchone()
        total = count_row["cnt"] if count_row else 0
        offset = (page - 1) * per_page
        query = """SELECT o.sn, o.month, o.global_waybill_sn, o.temp_line_sn as domestic_no,
                    o.consignee_name, o.sender_name, o.state, o.weight,
                    o.creation_time, o.line_name, o.platform_sn,
                    t.current_status as track_status,
                    t.ext_track_no_cn as track_company,
                    t.latest_desc as latest_desc,
                    t.queried_at, t.tracking_json,
                    ms.manual_status
                    FROM orders o
                    LEFT JOIN tracking t ON o.global_waybill_sn = t.tracking_no AND t.month = o.month
                    LEFT JOIN order_status_overrides ms ON o.sn = ms.sn AND ms.month = o.month
                    WHERE """ + where_sql + """
                    ORDER BY """ + col + " " + ord_dir + """
                    LIMIT ? OFFSET ?"""
        params_ext = params + [per_page, offset]
        rows = db.execute(query, params_ext).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["state_name"] = format_state(d["state"])
            d["track_city"] = extract_city_from_desc(d.get("latest_desc", ""))
            tj = d.get("tracking_json", "[]")
            try:
                d["detail_count"] = len(json.loads(tj)) if tj else 0
            except:
                d["detail_count"] = 0
            if "tracking_json" in d:
                del d["tracking_json"]
            items.append(d)
        by_status = {}
        try:
            db2 = sqlite3.connect(DB_PATH)
            db2.row_factory = sqlite3.Row
            # 按轨迹状态分组统计
            # 待入库 = 有运单号(global_waybill_sn)但无轨迹数据(current_status IS NULL)
            track_map_sql = """
                CASE
                    WHEN o.state=-6 THEN '已撤销'
                    WHEN o.state=0 THEN '已作废'
                    WHEN t.current_status IN ('已签收','签收(国内确认)','签收') THEN '已签收'
                    WHEN t.current_status IN ('清关中','已清关') THEN '清关中'
                    /* 待入库：有运单号但物流还没开始走 */
                    WHEN t.current_status IN ('运单已经创建') THEN '待入库'
                    WHEN t.current_status IN ('在途','离开','已揽收','快件航班批次已创建','国际运输') THEN '国际运输'
                    WHEN t.current_status IN ('到达','转寄','派件中') THEN '国内配送'
                    WHEN t.current_status IN ('问题件') THEN '问题件'
                    WHEN t.current_status IN ('其他') THEN '其他'
                    /* 以下 current_status IS NULL */
                    WHEN o.global_waybill_sn IS NOT NULL AND o.global_waybill_sn != '' THEN '待入库'
                    WHEN o.state=1 THEN '待支付'
                    WHEN o.state=2 THEN '待入库'
                    WHEN o.state=3 THEN '国际运输'
                    WHEN o.state=4 THEN '国内配送'
                    WHEN o.state=5 THEN '已签收'
                    ELSE '其他'
                END
            """
            rows = db2.execute(
                "SELECT " + track_map_sql + " as status_group, COUNT(*) as cnt FROM orders o "
                "LEFT JOIN tracking t ON o.global_waybill_sn = t.tracking_no AND t.month = o.month "
                "WHERE " + where_sql + " GROUP BY status_group", params
            ).fetchall()
            for r in rows:
                by_status[r["status_group"]] = r["cnt"]
            db2.close()
        except:
            pass
        db.close()
        return {"items": items, "total": total, "page": page, "per_page": per_page, "by_status": by_status}
    except Exception as e:
        raise HTTPException(500, "查询失败: " + str(e))

@app.get("/api/orders/export")
def api_orders_export(request: Request, q: str = "", status: str = "",
                      sender: str = "", date_from: str = "", date_to: str = "",
                      month: str = "may", ids: str = ""):
    logged_sender = get_sender_from_session(request)
    try:
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        where_clauses = ["o.month=?"]
        params = [month]
        if logged_sender:
            where_clauses.append("o.sender_name=?")
            params.append(logged_sender)
        elif sender:
            where_clauses.append("o.sender_name=?")
            params.append(sender)
        if q:
            like_sql = "(o.sn LIKE ? OR o.consignee_name LIKE ? OR o.global_waybill_sn LIKE ?)"
            where_clauses.append(like_sql)
            kw = "%" + q + "%"
            params.extend([kw, kw, kw])
        state_map = {"作废":0,"待支付":1,"国际运输":3,"国内配送":4,"签收":5,"已签收":5,"已撤销":-6}
        if status:
            if status == "待入库":
                where_clauses.append("(o.global_waybill_sn IS NOT NULL AND o.global_waybill_sn != '' AND (t.current_status IS NULL OR t.current_status = '运单已经创建'))")
            else:
                sv = state_map.get(status)
                if sv is not None:
                    where_clauses.append("o.state=?")
                    params.append(sv)
        if ids:
            id_list = ids.split(",")
            placeholders = ",".join(["?" for _ in id_list])
            where_clauses.append("o.sn IN (" + placeholders + ")")
            params.extend(id_list)
        where_sql = " AND ".join(where_clauses)
        rows = db.execute("""SELECT o.sn, o.global_waybill_sn, o.temp_line_sn, o.consignee_name,
            o.sender_name, o.state, o.weight, o.creation_time, o.line_name, o.platform_sn,
            t.current_status, t.ext_track_no_cn, t.latest_desc
            FROM orders o LEFT JOIN tracking t ON o.global_waybill_sn=t.tracking_no AND t.month=o.month
            WHERE """ + where_sql + """ ORDER BY o.creation_time DESC""", params).fetchall()
        import io, csv
        output = io.StringIO()
        w = csv.writer(output)
        w.writerow(["订单号","国际单号","国内单号","收件人","寄件人","状态","重量","创建时间","线路","平台单号","轨迹状态","物流公司","最新轨迹","轨迹城市"])
        for r in rows:
            city = extract_city_from_desc(r["latest_desc"] or "")
            w.writerow([r["sn"], r["global_waybill_sn"], r["temp_line_sn"], r["consignee_name"],
                       r["sender_name"], format_state(r["state"]), r["weight"], r["creation_time"],
                       r["line_name"], r["platform_sn"], r["current_status"], r["ext_track_no_cn"],
                       (r["latest_desc"] or "")[:80], city])
        db.close()
        csv_data = output.getvalue()
        return Response(content=csv_data, media_type="text/csv",
                       headers={"Content-Disposition": "attachment; filename=orders_" + month + ".csv"})
    except Exception as e:
        raise HTTPException(500, "导出失败: " + str(e))


@app.get("/api/orders/{sn}/products")
def api_order_products(sn: str):
    """获取订单产品明细（从货易达API实时查）"""
    try:
        from heute_api import HeuteAPI
        api = HeuteAPI()
        detail = api.order.detail(sn)
        if not detail or not detail.get('sn'):
            return {"items": [], "error": "API未返回数据"}
        products = detail.get('orderDetails') or detail.get('orderDetailsList') or []
        return {"items": products, "sn": sn}
    except Exception as e:
        import traceback
        return {"items": [], "error": str(e), "trace": traceback.format_exc()}

@app.get('/api/orders/untracked')
def get_untracked_orders(request: Request, month: str = 'may'):
    sender = get_sender_from_session(request)
    try:
        db = sqlite3.connect(DB_PATH)
        q = "SELECT o.sn,o.consignee_name,o.sender_name,o.state,o.global_waybill_sn,o.temp_line_sn,o.creation_time,o.line_name FROM orders o LEFT JOIN tracking t ON o.global_waybill_sn=t.tracking_no AND t.month=? WHERE o.month=? AND o.state NOT IN (5,-6) AND t.tracking_no IS NULL"
        params = [month, month]
        if sender: q += " AND o.sender_name=?"; params.append(sender)
        q += " ORDER BY o.creation_time DESC"
        rows = db.execute(q, params).fetchall()
        db.close()
    except Exception as e: raise HTTPException(500, str(e))
    return [{'sn':r[0],'consignee':r[1],'sender':r[2],'state':format_state(r[3]),'tracking_no':r[4]or'',
             'domestic_no':r[5]or'','created':(r[6]or'')[:19].replace('T',' ')if r[6]else'','line':r[7]or''} for r in rows]

# ─── Tracking ───
@app.get('/api/tracking/results')
def get_tracking_results(request: Request, page: int = 1, per_page: int = 50, status_filter: str = None, month: str = 'may'):
    sender = get_sender_from_session(request)
    if sender:
        sender_gws = {o['global_waybill_sn'] for o in get_orders(month, sender)}
        all_items, _ = get_tracking(month, status_filter, 1, 999999)
        items = [i for i in all_items if i.get('tracking',{}).get('tracking_no','') in sender_gws]
        total = len(items)
    else:
        items, total = get_tracking(month, status_filter, page, per_page)
    page_start = (page - 1) * per_page
    page_items = items[page_start:page_start + per_page]
    return {'items': page_items, 'total': total, 'page': page, 'per_page': per_page, 'month': month}
# ─── Tracking Live ───
def extract_city_from_desc(desc):
    """从轨迹描述中提取城市名"""
    if not desc: return ''
    m = re.search(r'【([^】]+)】', desc)
    if m:
        loc = m.group(1)
        if not re.search(r'\d{11}', loc) and '电话' not in loc:
            city = re.search(r'([\u4e00-\u9fff]{2,4}(?:市|区|县|镇))', loc)
            if city: return city.group(1)
            simple = re.search(r'([\u4e00-\u9fff]{2,4})', loc)
            if simple: return simple.group(1)
    return ''

@app.get('/api/tracking/query-live')
def api_tracking_live_query(tracking_no: str = ''):
    tn = tracking_no.strip()
    if not tn: return {'found': False, 'error': '请输入运单号'}

    # ❶ 优先从 DB 缓存读取（已有 trackingDetails 就不用重新查）
    try:
        db = sqlite3.connect(DB_PATH)
        row = db.execute(
            "SELECT current_status, latest_desc, latest_time, ext_track_no_cn, tracking_json, source FROM tracking WHERE tracking_no=?",
            (tn,)
        ).fetchone()
        if row and row[4]:  # tracking_json 不为空
            details = json.loads(row[4]) if isinstance(row[4], str) else []
            if details and len(details) > 0:
                db.close()
                return {
                    'found': True, 'tracking_no': tn, 'cached': True,
                    'status_name': better_status(row[0] or '', row[1] or ''),
                    'status_text': row[1] or '',
                    'city': extract_city_from_desc(row[1] or ''),
                    'trackingDetails': details,
                    'extTrackNoCn': row[3] or '',
                }
        db.close()
    except:
        pass

    # ❷ 快速 Track API
    from heute_api import HeuteAPI
    try:
        api = HeuteAPI()
        result = api.track.query(tn)
    except:
        result = {}

    # 先从 HeuteAPI 提取trackingDetails保底（SDK OCR失败时还能有明细）
    tracking_details = []
    api_details = result.get('trackingDetails', [])
    if api_details:
        for d in api_details:
            ts = d.get('trackingTime', '') or d.get('time', '')
            desc = d.get('trackingDesc', '') or d.get('desc', '')
            site = d.get('currentSiteName', '') or d.get('address', '') or d.get('location', '')
            tracking_details.append({
                'time': ts, 'desc': desc, 'location': site,
                'trackingTime': ts, 'trackingDesc': desc, 'address': site,
                'currentSiteName': d.get('currentSiteName', ''),
                'nextSiteName': d.get('nextSiteName', ''),
                'statusName': d.get('statusName', ''),
                'signerName': d.get('signerName', ''),
                'signerTypeDesc': d.get('signerTypeDesc', ''),
                'contact': d.get('contact', ''),
                'contactPhone': d.get('contactPhone', ''),
            })
    current_status = result.get('currentStatus', '')
    latest_desc = result.get('latestDesc', '')
    ext_no = result.get('extTrackNoCn', '')
    logistics_co = result.get('logisticsCompany', '')

    # ❸ SDK 查完整轨迹（仅当 HeuteAPI 未返回数据时作为兜底）— 最多3次尝试+5秒超时
    sdk_error = ''
    if not tracking_details:
        try:
            from heute_sdk import track_package
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(track_package, tn, max_attempts=3, verbose=False)
                try:
                    pkg = fut.result(timeout=10)
                except concurrent.futures.TimeoutError:
                    sdk_error = 'SDK查询超时(10s)'
                    pkg = None
            if pkg and pkg.get('trackingNo') and 'error' not in pkg:
                details_raw = pkg.get('trackingDetails', [])
                if details_raw:
                    for d in details_raw:
                        ts = d.get('trackingTime', '')
                        desc = d.get('trackingDesc', '')
                        site = d.get('currentSiteName', '') or d.get('address', '')
                        entry = {
                            'time': ts, 'desc': desc,
                            'location': site,
                            'trackingTime': ts, 'trackingDesc': desc,
                            'address': site,
                            'currentSiteName': d.get('currentSiteName', ''),
                            'nextSiteName': d.get('nextSiteName', ''),
                            'statusName': d.get('statusName', ''),
                            'signerName': d.get('signerName', ''),
                            'signerTypeDesc': d.get('signerTypeDesc', ''),
                            'contact': d.get('contact', ''),
                            'contactPhone': d.get('contactPhone', ''),
                        }
                        tracking_details.append(entry)
                    current_status = pkg.get('currentStatus', current_status)
                    latest_desc = details_raw[0].get('trackingDesc', latest_desc) if details_raw else latest_desc
                    ext_no = pkg.get('extTrackNoCn', ext_no)
                    logistics_co = pkg.get('logisticsCompany', logistics_co)
        except Exception as e:
            sdk_error = str(e)[:200]
    
    # 保存到数据库（包含完整trackingDetails）
    try:
        db = sqlite3.connect(DB_PATH)
        from heute_db import upsert_tracking
        now = datetime.now()
        _MONTH_NAMES = {4:'apr',5:'may',6:'june',7:'july',8:'aug',9:'sep',10:'oct',11:'nov',12:'dec',1:'jan',2:'feb',3:'mar'}
        mo = _MONTH_NAMES.get(now.month, 'may')
        # 构造包含trackingDetails的完整结果
        full_result = {
            'trackingNo': tn,
            'currentStatus': current_status,
            'latestDesc': latest_desc,
            'latestTime': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'extTrackNoCn': ext_no,
            'logisticsCompany': logistics_co,
            'trackingDetails': tracking_details,
        }
        upsert_tracking(db, tn, mo, full_result)
        db.commit(); db.close()
    except:
        pass
    
    city = extract_city_from_desc(latest_desc)
    return {
        'found': True,
        'tracking_no': tn,
        'status_name': better_status(current_status, latest_desc),
        'status_text': latest_desc,
        'city': city,
        'trackingDetails': tracking_details,
        'extTrackNoCn': ext_no,
        'logisticsCompany': logistics_co,
        'sdk_error': sdk_error or None,
    }

@app.get('/api/tracking/{tracking_no}')
def query_tracking(tracking_no: str, month: str = None):
    for m in ([month] if month else ['may','apr']):
        r = get_tracking_by_no(tracking_no, m)
        if r: return r
    raise HTTPException(404, f'未找到 {tracking_no}')

# ─── Frontend ───
@app.get('/favicon.ico')
def favicon(): 
    from fastapi.responses import Response
    return Response(status_code=204)

@app.get('/')
def index():
    path = os.path.join(BASE_DIR, 'templates', 'index.html')
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return HTMLResponse(content=f.read(), headers={'Cache-Control': 'no-cache'})
    return HTMLResponse('<h1>404</h1>', status_code=404)

# ─── Main ───
if __name__ == '__main__':
    port = int(os.environ.get('DASHBOARD_PORT', '8892'))
    print(f'📊 货易达物流看板 (SQLite) — http://0.0.0.0:{port}')
    uvicorn.run('dashboard.app:app', host='0.0.0.0', port=port)

# ─── Status Mapping ───
BETTER_STATUS = {
    '已签收': '✅ 已签收', '签收(国内确认)': '✅ 已签收', '签收': '✅ 已签收',
    '清关中': '🛃 清关中',
}

def better_status(status_name, status_text=''):
    if status_name in BETTER_STATUS:
        return BETTER_STATUS[status_name]
    desc = (status_text or '').lower()
    if '清关' in desc or '海关' in desc or '口岸' in desc:
        return '🛃 清关中'
    if '派送' in desc or '配送' in desc or '派件' in desc or '快递员' in desc:
        return '🚚 国内配送'
    if '机场' in desc or '航班' in desc or '起飞' in desc or '国际' in desc:
        return '✈️ 国际运输'
    m = re.search(r'【(.+?)】', status_text or '')
    if m:
        loc = m.group(1)
        if not ('电话' in loc or '手机' in loc or re.search(r'\d{11}', loc)):
            return loc
    return status_name or '未知'

# ─── Anomalies ───
@app.get('/api/anomalies')
def get_anomalies_endpoint(request: Request, month: str = 'may', match_filter: str = None):
    sender = get_sender_from_session(request)
    anomalies = get_anomalies(month, match_filter=match_filter, sender=sender)
    counts = Counter(a.get('match') for a in anomalies)
    return {'total': len(anomalies), 'severe': counts.get('severe',0), 'warning': counts.get('warning',0),
            'ok': counts.get('ok',0), 'items': anomalies}

# ─── Tracking Search ───
@app.get('/api/tracking/search')
def search_tracking_api(request: Request, q: str = '', month: str = 'may'):
    sender = get_sender_from_session(request)
    results = search_tracking(q, month)
    if sender:
        sender_gws = {o['global_waybill_sn'] for o in get_orders(month, sender)}
        results = [r for r in results if r.get('tracking',{}).get('tracking_no','') in sender_gws]
    return [{'order_sn':r.get('tracking',{}).get('order',{}).get('sn',''),
             'consignee':r.get('tracking',{}).get('order',{}).get('consignee_name',''),
             'tracking_no':r.get('tracking',{}).get('tracking_no',''),
             'tracking_status':better_status(r.get('tracking',{}).get('currentStatus',r.get('tracking',{}).get('current_status','')))} for r in results[:100]]

# ─── Tracking Refresh ───
@app.post('/api/tracking/{tracking_no}/refresh')
def refresh_tracking(tracking_no: str, month: str = 'may'):
    from heute_api import HeuteAPI
    api = HeuteAPI()
    try:
        result = api.track.query(tracking_no)
    except Exception as e:
        raise HTTPException(502, f'查询失败: {e}')
    if not result or not result.get('trackingNo'):
        raise HTTPException(404, f'未返回轨迹: {tracking_no}')
    try:
        db = sqlite3.connect(DB_PATH)
        row = db.execute("SELECT sn FROM orders WHERE global_waybill_sn=?",(tracking_no,)).fetchone()
        order_sn = row[0] if row else ''
        db.close()
    except: order_sn = ''
    try:
        db = sqlite3.connect(DB_PATH)
        upsert_tracking(db, tracking_no, month, result, order_info={'sn': order_sn})
        db.commit(); db.close()
    except: pass
    return {'status': 'ok', 'tracking_no': tracking_no, 'current_status': result.get('currentStatus',''),
            'latest_desc': result.get('latestDesc','')}

# ─── Tracking Status ───
@app.post('/api/tracking/{tracking_no}/status')
def set_tracking_status(tracking_no: str, data: dict = Body(...)):
    month = data.get('month','may'); status = data.get('status','')
    if not status: raise HTTPException(400,'缺少status')
    with sqlite3.connect(DB_PATH) as db:
        db.execute("UPDATE tracking SET current_status=?, queried_at=? WHERE tracking_no=? AND month=?",
                   (status, datetime.now().isoformat()[:19], tracking_no, month))
        db.commit()
    return {'ok': True}

# ─── Untracked Batch ───
@app.post('/api/tracking/query-untracked')
def start_track_query(month: str = 'may'):
    """启动一键更新 - 从货易达API查询所有订单的最新轨迹"""
    with _batch_lock:
        global _batch_in_progress
        if _batch_in_progress:
            raise HTTPException(429, '已有批量查询任务运行中')
        _batch_in_progress = True
    def _run():
        import sqlite3, time
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from threading import Lock
        try:
            db = sqlite3.connect(DB_PATH, check_same_thread=False)
            orders_list = get_orders(month)
            total = len(orders_list)
            lock = Lock()
            queried = [0]
            skipped = [0]
            with open(PROGRESS_FILE, 'w') as f:
                json.dump({'running': True, 'done': False, 'percent': 0,
                           'message': f'准备查询 {total} 单…', 'phase': 'tracking'}, f)

            def _query_one(o):
                """查单个轨迹---每条线程独立api实例"""
                gws = o.get('global_waybill_sn', '')
                if not gws:
                    return None
                # 检查是否已签收 → 永久跳过
                existing = db.execute("SELECT current_status, queried_at FROM tracking WHERE tracking_no=? AND month=?",
                                     (gws, month)).fetchone()
                if existing:
                    cur_status = existing[0] or ''
                    if any(k in cur_status for k in ('已签收', '签收', '签收(国内确认)')):
                        with lock: skipped[0] += 1
                        return None
                    # 24小时内查过的跳过
                    qat = existing[1]
                    if qat and isinstance(qat, str) and len(qat) >= 19:
                        from datetime import datetime as dt2
                        try:
                            qtime = dt2.strptime(qat[:19], '%Y-%m-%d %H:%M:%S')
                            if (datetime.now() - qtime).total_seconds() < 21600:
                                with lock: skipped[0] += 1
                                return None
                        except: pass
                try:
                    from heute_api import HeuteAPI
                    api = HeuteAPI()
                    result = api.track.query(gws)
                    if result and result.get('trackingNo'):
                        tn = result.get('trackingNo')
                        tracking_data = {
                            'tracking_no': tn,
                            'current_status': result.get('currentStatus', result.get('current_status', '')),
                            'currentStatus': result.get('currentStatus', result.get('current_status', '')),
                            'latest_desc': result.get('latestDesc', ''),
                            'latest_time': result.get('latestTime', ''),
                            'ext_track_no_cn': result.get('extTrackNoCn', ''),
                            'trackingDetails': result.get('trackingDetails', []),
                            'queried_at': datetime.now().isoformat()[:19],
                        }
                        with lock:
                            upsert_tracking(db, tn, month, tracking_data, {'sn': o.get('sn','')})
                            # 自动标记：轨迹有位置且当前状态"其他"→设为国际运输
                            if tracking_data.get('trackingDetails'):
                                for _d in tracking_data['trackingDetails']:
                                    _loc = _d.get('currentSiteName','') or _d.get('address','')
                                    if _loc:
                                        _cur = db.execute("SELECT current_status FROM tracking WHERE tracking_no=? AND month=?", (tn, month)).fetchone()
                                        if _cur and (_cur[0] or '') == '其他':
                                            db.execute("UPDATE tracking SET current_status=? WHERE tracking_no=? AND month=?", ('国际运输', tn, month))
                                        break
                            queried[0] += 1
                        return tracking_data
                except:
                    pass
                return None

            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = {pool.submit(_query_one, o): i for i, o in enumerate(orders_list)}
                done_cnt = 0
                for f in as_completed(futures):
                    done_cnt += 1
                    if done_cnt % 20 == 0 or done_cnt >= total:
                        with lock:
                            db.commit()
                        pct = min(int(done_cnt / total * 95) + 5, 100)
                        with open(PROGRESS_FILE, 'w') as pf:
                            json.dump({'running': done_cnt < total, 'done': done_cnt >= total,
                                       'percent': pct,
                                       'message': f'查询{done_cnt}/{total} (新{queried[0]}/跳过{skipped[0]})',
                                       'phase': 'done' if done_cnt >= total else 'tracking'}, pf)

            db.commit(); db.close()
            with open(PROGRESS_FILE, 'w') as f:
                json.dump({'running': False, 'done': True, 'percent': 100,
                           'message': f'完成！新查 {queried[0]} 单，跳过 {skipped[0]} 单，共 {total} 单',
                           'phase': 'done'}, f)
        except Exception as e:
            with open(PROGRESS_FILE, 'w') as f:
                json.dump({'running': False, 'done': True, 'percent': 100,
                           'message': f'错误: {e}', 'phase': 'error'}, f)
        finally:
            with _batch_lock:
                global _batch_in_progress
                _batch_in_progress = False
    import threading
    threading.Thread(target=_run, daemon=True).start()
    return {'ok': True, 'message': '后台批量查询已启动'}

@app.get('/api/tracking/query-untracked/progress')
def get_track_progress():
    if os.path.exists(PROGRESS_FILE): return json.load(open(PROGRESS_FILE))
    return {'running': False, 'done': True}

@app.get('/api/tracking/query-untracked/status')
def get_track_status(): return {'busy': _batch_in_progress}

# ─── State Reconcile (状态同步) ───
_reconcile_lock = threading.Lock()
_reconcile_in_progress = False

@app.post('/api/tracking/reconcile-states')
def reconcile_states(month: str = 'all'):
    """根据轨迹状态批量更新 orders.state"""
    global _reconcile_in_progress
    with _reconcile_lock:
        if _reconcile_in_progress:
            raise HTTPException(429, '已有同步任务运行中')
        _reconcile_in_progress = True

    def _run():
        global _reconcile_in_progress
        try:
            import sqlite3
            db = sqlite3.connect(DB_PATH)
            from heute_db import _judge_state

            months = ['apr', 'may'] if month == 'all' else [month]
            total_updated = 0
            for mo in months:
                rows = db.execute("""
                    SELECT o.sn, o.state, o.global_waybill_sn,
                           t.current_status, t.ext_track_no_cn, t.latest_desc, t.tracking_json
                    FROM orders o
                    JOIN tracking t ON o.global_waybill_sn = t.tracking_no AND t.month = o.month
                    WHERE o.month=?
                """, (mo,)).fetchall()

                updated = 0
                for r in rows:
                    sn, cur_state, gws, cur_status, ext_no, desc, tj = r
                    details = []
                    if tj:
                        try:
                            details = json.loads(tj) if isinstance(tj, str) else []
                        except: pass

                    new_state = _judge_state(cur_status or '', ext_no or '', desc or '', details)
                    if new_state is None or new_state == cur_state:
                        continue
                    db.execute("UPDATE orders SET state=? WHERE sn=?", (new_state, sn))
                    updated += 1

                db.commit()
                total_updated += updated

            db.close()
            return {'ok': True, 'updated': total_updated}
        except Exception as e:
            return {'ok': False, 'error': str(e)}
        finally:
            _reconcile_in_progress = False

    threading.Thread(target=_run, daemon=True).start()
    return {'ok': True, 'message': '后台状态同步已启动'}

@app.get('/api/tracking/reconcile-states/status')
def get_reconcile_status():
    return {'busy': _reconcile_in_progress}

# ─── Order Sync (实时订单同步) ───
def _write_order_progress(data: dict):
    with open(ORDER_SYNC_FILE, 'w') as f:
        json.dump(data, f)

@app.post('/api/orders/sync')
def start_order_sync(days: int = 5):
    """从货易达API实时同步订单到本地DB（后台线程）"""
    global _order_sync_in_progress
    with _order_sync_lock:
        if _order_sync_in_progress:
            raise HTTPException(429, '已有订单同步任务运行中')
        _order_sync_in_progress = True

    def _run():
        global _order_sync_in_progress
        try:
            now = datetime.now()
            start = (now - timedelta(days=days)).strftime('%Y-%m-%d')
            end = now.strftime('%Y-%m-%d')
            _write_order_progress({
                'running': True, 'done': False, 'percent': 0,
                'message': f'正在拉取 {start}~{end} 订单…',
                'phase': 'syncing'
            })
            from heute_api import HeuteAPI
            api = HeuteAPI()
            orders = api.order.list_all(start_time=start, end_time=end)
            total = len(orders)
            _write_order_progress({
                'running': True, 'done': False, 'percent': 60,
                'message': f'拉取完成，共 {total} 单，正在写入DB…',
                'phase': 'preparing'
            })
            from heute_express_sync import dashboard_upsert_orders
            db_path = os.path.join(DATA_DIR, 'heute.db')
            dashboard_upsert_orders(db_path, orders)
            _write_order_progress({
                'running': False, 'done': True, 'percent': 100,
                'message': f'同步完成！共 {total} 单已写入DB',
                'phase': 'done'
            })
        except Exception as e:
            _write_order_progress({
                'running': False, 'done': True, 'percent': 100,
                'message': f'错误: {e}',
                'phase': 'error'
            })
        finally:
            with _order_sync_lock:
                _order_sync_in_progress = False

    import threading
    threading.Thread(target=_run, daemon=True).start()
    return {'ok': True, 'message': '订单同步已启动'}

@app.get('/api/orders/sync/progress')
def get_order_sync_progress():
    if os.path.exists(ORDER_SYNC_FILE):
        return json.load(open(ORDER_SYNC_FILE))
    return {'running': False, 'done': True}

@app.get('/api/orders/sync/status')
def get_order_sync_status():
    return {'busy': _order_sync_in_progress}

# ─── Order Detail ───
@app.get('/api/order/{sn}')
def get_order_detail(sn: str, month: str = 'may'):
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    row = db.execute("""
        SELECT o.*, ms.manual_status, ms.updated_at as manual_status_updated,
               t.current_status as track_status
        FROM orders o
        LEFT JOIN order_status_overrides ms ON o.sn = ms.sn AND ms.month = o.month
        LEFT JOIN tracking t ON o.global_waybill_sn = t.tracking_no AND t.month = o.month
        WHERE o.sn=? AND o.month=?
    """, (sn, month)).fetchone()
    db.close()
    if not row:
        raise HTTPException(404, f'订单 {sn} 未找到')
    result = dict(row)
    # Compute state_name from state integer
    state = result.get('state')
    result['state_name'] = format_state(state) if state is not None else None
    try:
        from heute_api import HeuteAPI
        detail = HeuteAPI().order.detail(sn)
        if detail and detail.get('sn'):
            result['_api_detail'] = True
    except: pass
    return result

# ─── Manual Status ───
@app.put('/api/orders/manual-status')
def set_order_manual_status(data: dict = Body(...)):
    sn = data.get('sn',''); status = data.get('status',''); month = data.get('month','may')
    if not sn: raise HTTPException(400,'参数 sn 必填')
    if status: set_manual_status(sn, status, month)
    else: clear_manual_status(sn, month)
    return {'ok': True, 'sn': sn, 'status': status, 'cleared': not bool(status)}

# ─── After Sales ───
@app.get('/api/after-sales/lookup-tracking')
def api_lookup_tracking(tracking_no: str = ''):
    match = lookup_order_by_tracking(tracking_no.strip())
    if match: return {'matched': True, 'intl_tracking_no': match['gw'], 'domestic_tracking_no': match['ts'],
                      'sender_name': match['sender'], 'order_sn': match['sn']}
    return {'matched': False}

@app.get('/api/after-sales')
def api_get_after_sales(request: Request, month: str = 'may', status: str = None,
                         sender: str = None, q: str = None, page: int = 1, per_page: int = 50):
    logged = get_sender_from_session(request)
    if q: items, total = search_after_sales(q.strip(), page=page, per_page=per_page, sender=logged or sender)
    else: items, total = get_after_sales(month, status, logged or sender, page, per_page)
    return {'items': items, 'total': total, 'page': page, 'per_page': per_page}

@app.get('/api/after-sales/stats')
def api_after_sales_stats(request: Request, month: str = 'may'):
    sender = get_sender_from_session(request)
    return get_after_sales_stats(month, DB_PATH)

@app.post('/api/after-sales')
def api_create_after_sales(data: dict = Body(...)):
    try:
        aid = create_after_sales(data)
        return {'ok': True, 'id': aid}
    except Exception as e: raise HTTPException(400, str(e))

@app.patch('/api/after-sales/{after_id}')
def api_update_after_sales(after_id: int, data: dict = Body(...)):
    ok = update_after_sales(after_id, data)
    if not ok: raise HTTPException(404, '售后记录不存在')
    return {'ok': True}

@app.delete('/api/after-sales/{after_id}')
def api_delete_after_sales(after_id: int):
    ok = delete_after_sales(after_id)
    if not ok: raise HTTPException(404, '售后记录不存在')
    return {'ok': True}

# ─── Finance ───
@app.get('/api/finance')
def api_get_finance(month: str = 'may'):
    return get_finance(month)

@app.get('/api/finance/daily-detail')
def get_finance_daily_detail(month: str = 'may', date: str = None):
    """获取某天的财务流水明细"""
    with DB(DB_PATH) as db:
        rows = db.execute(
            "SELECT order_sn, money_changed, description, creation_time FROM finance_logs WHERE month=? AND creation_time LIKE ? ORDER BY creation_time DESC",
            (month, f'{date}%' if date else '2026-05-%')
        ).fetchall()
        records = [
            {'order_sn': r[0], 'amount': r[1]/100, 'description': r[2], 'time': r[3][:19]}
            for r in rows
        ]
        return {
            'today': {'date': date, 'count': len(records), 'records': records[:50]} if date else None,
            'yesterday': None
        }

@app.get('/api/finance/by-product')
def get_finance_by_product(month: str = 'may'):
    """物流产品占比（按line_name统计，用于成本分析）"""
    with DB(DB_PATH) as db:
        rows = db.execute(
            "SELECT line_name, COUNT(*) as cnt FROM orders WHERE month=? AND state NOT IN (0,-6) AND line_name != '' GROUP BY line_name ORDER BY cnt DESC",
            (month,)
        ).fetchall()
        products = [{'name': r['line_name'], 'count': r['cnt']} for r in rows]
        total_orders = sum(r['cnt'] for r in rows)
        return {'products': products, 'cached': total_orders, 'total_orders': total_orders}

@app.get('/api/finance/overweight')
def get_overweight(month: str = 'may'):
    """超重产品统计 — 按线路聚合"""
    with DB(DB_PATH) as db:
        # 总订单数 per 线路（含无超重的订单）
        total_orders_map = {}
        for r in db.execute("""
            SELECT line_name, COUNT(*) as cnt
            FROM orders WHERE month=? AND line_name IS NOT NULL
            GROUP BY line_name
        """, (month,)).fetchall():
            total_orders_map[r[0]] = r[1]

        # 有超重扣款的订单数 per 线路 + 金额统计
        rows = db.execute("""
            SELECT
                COALESCE(o.line_name, '未知线路') as name,
                COUNT(DISTINCT f.order_sn) as surcharge_orders,
                COUNT(*) as total_records,
                SUM(f.money_changed) / 100.0 as total_surcharge,
                AVG(o.weight) / 1000.0 as avg_weight_kg,
                MAX(o.weight) / 1000.0 as max_weight_kg
            FROM finance_logs f
            LEFT JOIN orders o ON CAST(f.order_sn AS TEXT) = o.sn
            WHERE f.month=?
            GROUP BY name
            ORDER BY total_surcharge ASC
        """, (month,)).fetchall()

        products = []
        total_orders_all = 0
        total_surcharge_all = 0.0
        for r in rows:
            name = r[0] or '未知线路'
            surcharge_orders = r[1] or 0
            total_records = r[2] or 0
            surcharge_amt = float(r[3] or 0)
            avg_weight = float(r[4] or 0)
            max_weight = float(r[5] or 0)
            line_orders = total_orders_map.get(name, surcharge_orders)
            rate = round((surcharge_orders / line_orders * 100) if line_orders else 0, 1)

            # 打包建议
            suggestion = ''
            if rate >= 60:
                suggestion = '⚠️ 超重偏多，建议检查包装标准'
            elif rate >= 30:
                suggestion = '⚡ 超重较多，建议优化装箱方案'
            elif avg_weight > 3:
                suggestion = '📦 均重大，考虑按重量分段'
            elif rate <= 10:
                suggestion = '✅ 控制正常'
            elif max_weight > 5:
                suggestion = '📏 个别超重大单，注意抽检'

            products.append({
                'name': name,
                'order_count': line_orders,
                'surcharge_count': surcharge_orders,
                'surcharge_amount': round(surcharge_amt, 2),
                'avg_weight_kg': round(avg_weight, 2),
                'surcharge_rate': rate,
                'suggestion': suggestion
            })
            total_orders_all += line_orders
            total_surcharge_all += surcharge_amt

        return {
            'products': products,
            'summary': {
                'total_products': len(products),
                'total_orders': total_orders_all,
                'total_surcharge': round(total_surcharge_all, 2)
            }
        }

@app.get('/api/overweight/orders')
def get_overweight_orders(month: str = 'may', page: int = 1, per_page: int = 50):
    """超重订单列表 — 含产品/重量/收件人/线路"""
    with DB(DB_PATH) as db:
        offset = (page - 1) * per_page
        rows = db.execute("""
            SELECT f.order_sn, f.money_changed, f.description, f.creation_time,
                   o.consignee_name, o.line_name, o.weight, o.state,
                   o.sender_name
            FROM finance_logs f
            LEFT JOIN orders o ON CAST(f.order_sn AS TEXT) = o.sn
            WHERE f.month=? AND f.description LIKE '%称重%'
            ORDER BY f.creation_time DESC LIMIT ? OFFSET ?
        """, (month, per_page, offset)).fetchall()
        total = db.execute(
            "SELECT COUNT(*) FROM finance_logs WHERE month=? AND description LIKE '%称重%'",
            (month,)
        ).fetchone()[0]
        state_names = {2:'待入库',3:'国际运输',4:'国内配送',5:'已签收',-6:'已撤销',0:'已撤销'}
        items = []
        for r in rows:
            weight_g = r[6] or 0
            st = r[7]
            items.append({
                'order_sn': r[0],
                'total_surcharge': r[1]/100,
                'description': r[2],
                'time': r[3][:19] if r[3] else '',
                'product_name': r[5] or '',
                'consignee': r[4] or '',
                'line': r[5] or '',
                'weight_g': weight_g,
                'weight_kg': round(weight_g / 1000, 2) if weight_g else None,
                'state_name': state_names.get(st, f'状态{st}'),
                'sender_name': r[8] or '',
                'surcharge_count': 1,
                'has_surcharge': True,
            })
        return {'items': items, 'total': total}

@app.post('/api/finance/refresh')
def api_refresh_finance(month: str = 'may'):
    """从货易达API拉取财务数据"""
    import threading
    def _run():
        try:
            result = sync_finance(month)
            with open(PROGRESS_FILE, 'w') as f:
                json.dump({'running': False, 'done': True,
                           'message': f'财务更新完成：新插入 {result["inserted"]} 条',
                           'phase': 'done'}, f)
        except Exception as e:
            with open(PROGRESS_FILE, 'w') as f:
                json.dump({'running': False, 'done': True,
                           'message': f'财务更新失败: {e}', 'phase': 'error'}, f)
    threading.Thread(target=_run, daemon=True).start()
    return {'ok': True, 'message': '财务数据刷新已启动（后台拉取）'}

# ─── Todo（待办：未认证身份证）─────────────────────────────────────────────────

@app.get('/api/todo')
def api_todo(request: Request, month: str = None):
    """从清关看板拉取未认证身份证，按寄件人分组（按当前登录用户隔离）"""
    import urllib.request, json
    # 尝试多个地址（容器化部署下可能不同网络）
    urls = [
        'http://192.168.178.26:18995/api/missing/recent',
        'http://localhost:8895/api/missing/recent',
        'http://172.17.0.1:18995/api/missing/recent',
    ]
    last_error = ''
    for url in urls:
        try:
            req = urllib.request.Request(url, method='GET')
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
            break
        except Exception as e:
            last_error = str(e)
            continue
    else:
        return {'items': [], 'senders': {}, 'total': 0, 'sender_count': 0, 'error': last_error}
    
    items = data.get('items', [])
    by_sender = {}
    for o in items:
        s = o.get('sender_name', '未知')
        if s not in by_sender:
            by_sender[s] = []
        by_sender[s].append({
            'consignee': o.get('consignee_name', ''),
            'id_number': o.get('consignee_id_number', ''),
            'domestic_no': o.get('domestic_no', ''),
            'line_name': o.get('line_name', ''),
            'sn': o.get('sn', ''),
            'global_waybill': o.get('global_waybill', ''),
        })
    
    # ⭐ 按当前登录用户隔离寄件人数据
    sender = get_sender_from_session(request)
    if sender is not None:
        # 非管理员：只看自己的待办
        my_items = by_sender.get(sender, [])
        by_sender = {sender: my_items} if my_items else {}
    
    return {
        'items': items,
        'senders': dict(sorted(by_sender.items(), key=lambda x: -len(x[1]))),
        'total': sum(len(v) for v in by_sender.values()),
        'sender_count': len(by_sender),
    }

# ─── 身份证上传（通过直邮管家中转）───────────────────────────────────────────

import dashboard.idcard_upload as idcard_upload

@app.post('/api/todo/upload')
def api_todo_upload(request: Request,
                    name: str = Form(...),
                    id_number: str = Form(...),
                    front_image: UploadFile = File(...),
                    reverse_image: UploadFile = File(...)):
    """上传身份证正反面到认证系统"""
    # 读取上传的图片文件
    front_data = front_image.file.read()
    reverse_data = reverse_image.file.read()
    
    if not front_data or not reverse_data:
        raise HTTPException(400, '图片文件为空')
    
    if len(front_data) > 10 * 1024 * 1024 or len(reverse_data) > 10 * 1024 * 1024:
        raise HTTPException(400, '单张图片不能超过10MB')
    
    result = idcard_upload.upload(name, id_number, front_data, reverse_data)
    
    if result.get('success'):
        return {'ok': True, 'msg': result.get('msg', '上传成功')}
    else:
        return JSONResponse(status_code=502, content={'ok': False, 'msg': result.get('msg', '上传失败')})

# ─── Admin ───
@app.get('/api/admin/senders')
def admin_senders(request: Request):
    if request.session.get('role') != 'admin': raise HTTPException(403,'仅管理员可访问')
    return [{'name': n, 'total_orders': 0, 'tracked': 0, 'signed': 0, 'severe': 0} for n in get_senders()]

# ─── Token Login ───
@app.get('/api/auth/token-login')
def token_login(token: str = ''):
    return {'ok': False, 'error': 'Token登录仅完整版支持'}
