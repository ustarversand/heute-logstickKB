#!/usr/bin/env python3
# 货易达物流看板 — FastAPI 后端（恢复版）
import json, os, sys, time, csv, hashlib, urllib, uvicorn, subprocess, shutil, ssl, threading, re, sqlite3
from datetime import datetime, timedelta
from collections import Counter, defaultdict
from fastapi import FastAPI, Query, HTTPException, Request, Body, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
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
    return {0:'已作废',1:'待支付',2:'待入库',3:'国际运输',4:'国内配送',5:'签收'}.get(s, f'状态{s}')

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

# ─── Stats ───
@app.get('/api/stats')
def get_stats(request: Request, month: str = 'may'):
    sender = get_sender_from_session(request)
    orders = get_orders(month, sender)
    state_dist = Counter()
    track_status = get_tracking_status_dist(month, sender)
    for o in orders: state_dist[format_state(o.get('state'))] += 1
    cancelled_count = sum(1 for o in orders if o.get('state') == -6)
    signed_total = track_status.get('已签收', 0) + track_status.get('签收(国内确认)', 0)
    signed_domestic_only = track_status.get('签收(国内确认)', 0)
    anomalies = get_anomaly_counts(month, sender)
    abnormal_count = anomalies.get('severe', 0)
    return {'total_orders': len(orders), 'tracked': sum(track_status.values()),
            'tracking_status': track_status, 'state_distribution': dict(state_dist),
            'cancelled_count': cancelled_count, 'signed_total': signed_total,
            'signed_domestic_only': signed_domestic_only, 'abnormal_count': abnormal_count}

# ─── Orders ───
@app.get('/api/orders/recent')
def get_recent_orders(request: Request, limit: int = 50, month: str = 'may'):
    sender = get_sender_from_session(request)
    orders = get_orders(month, sender)
    return sorted(orders, key=lambda o: o.get('creation_time','') or '', reverse=True)[:limit]

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
        start = (page-1)*per_page
        items = items[start:start+per_page]
    else:
        items, total = get_tracking(month, status_filter, page, per_page)
    # Build order lookup for consignee
    orders_list = get_orders(month)
    order_map = {}
    for o in orders_list:
        sn = o.get('sn')
        if sn: order_map[sn] = o
    def extract_location(desc):
        m = re.search(r'【(.+?)】', desc or '')
        return m.group(1) if m else ''
    def guess_carrier(source, tracking_no, domestic_no):
        if source: return source
        dn = domestic_no or tracking_no
        if dn.startswith('JDV'): return '京东快递'
        if dn.startswith('SF'): return '顺丰速运'
        return ''
    result_items = []
    for i in items:
        trk = i.get('tracking', {})
        tn = trk.get('tracking_no', '')
        order_sn = trk.get('order', {}).get('sn', '')
        order_info = order_map.get(order_sn, {})
        details = trk.get('trackingDetails', [])
        desc = trk.get('latest_desc', '')
        result_items.append({
            'tracking_no': tn,
            'order_sn': order_sn,
            'consignee': order_info.get('consignee_name', ''),
            'status': trk.get('currentStatus', trk.get('current_status', '')),
            'location': extract_location(desc),
            'company': guess_carrier(trk.get('source', ''), tn, trk.get('ext_track_no_cn', '')),
            'domestic_no': trk.get('ext_track_no_cn', '') or trk.get('domestic_no', ''),
            'detail_count': len(details),
            'details': details,
            'queried_at': trk.get('queried_at', ''),
        })
    return {'items': result_items, 'total': total, 'page': page, 'per_page': per_page}

@app.get('/api/tracking/{tracking_no}')
def query_tracking(tracking_no: str, month: str = None):
    for m in ([month] if month else ['may','april']):
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
def get_anomalies_endpoint(request: Request, month: str = 'april', match_filter: str = None):
    sender = get_sender_from_session(request)
    anomalies = get_anomalies(month, match_filter=match_filter, sender=sender)
    counts = Counter(a.get('match') for a in anomalies)
    return {'total': len(anomalies), 'severe': counts.get('severe',0), 'warning': counts.get('warning',0),
            'ok': counts.get('ok',0), 'items': anomalies}

# ─── Tracking Live ───
@app.get('/api/tracking/query-live')
def api_tracking_live_query(tracking_no: str = ''):
    tn = tracking_no.strip()
    if not tn: return {'found': False, 'error': '请输入运单号'}
    from heute_api import HeuteAPI
    try:
        api = HeuteAPI()
        result = api.track.query(tn)
        if result and result.get('trackingNo'):
            return {'found': True, 'tracking_no': result.get('trackingNo',tn),
                    'status_name': better_status(result.get('currentStatus',''), result.get('latestDesc','')),
                    'status_text': result.get('latestDesc','')}
        return {'found': False, 'error': '未找到此单号'}
    except Exception as e:
        return {'found': False, 'error': str(e)}

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
    orders = get_orders(month)
    for o in orders:
        if o.get('sn') == sn:
            result = dict(o)
            try:
                from heute_api import HeuteAPI
                detail = HeuteAPI().order.detail(sn)
                if detail and detail.get('sn'):
                    result['_api_detail'] = True
            except: pass
            return result
    raise HTTPException(404, f'订单 {sn} 未找到')

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
    return {'total': 0, 'by_status': {}, 'by_type': {}}

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
    """产品维度统计（从订单描述中提取产品名）"""
    with DB(DB_PATH) as db:
        rows = db.execute(
            "SELECT money_changed, description FROM finance_logs WHERE month=?",
            (month,)
        ).fetchall()
        products = []
        total_orders = len(rows)
        return {'products': products, 'cached': total_orders, 'total_orders': total_orders}

@app.get('/api/finance/overweight')
def get_overweight(month: str = 'may'):
    """超重产品统计"""
    with DB(DB_PATH) as db:
        rows = db.execute(
            "SELECT money_changed, description FROM finance_logs WHERE month=? AND description LIKE '%超重%'",
            (month,)
        ).fetchall()
        return {'products': [{'money_changed': r[0], 'description': r[1]} for r in rows]}

@app.get('/api/overweight/orders')
def get_overweight_orders(month: str = 'may', page: int = 1, per_page: int = 50):
    """超重订单列表"""
    with DB(DB_PATH) as db:
        offset = (page - 1) * per_page
        rows = db.execute(
            "SELECT order_sn, money_changed, description, creation_time FROM finance_logs WHERE month=? AND description LIKE '%超重%' ORDER BY creation_time DESC LIMIT ? OFFSET ?",
            (month, per_page, offset)
        ).fetchall()
        total = db.execute(
            "SELECT COUNT(*) FROM finance_logs WHERE month=? AND description LIKE '%超重%'",
            (month,)
        ).fetchone()[0]
        items = [{'order_sn': r[0], 'amount': r[1]/100, 'description': r[2], 'time': r[3][:19]} for r in rows]
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
