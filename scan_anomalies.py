#!/usr/bin/env python3
"""批量扫描在途国际单号 → 对比国内单号状态 → 写入 anomaly_comparison.json
   修复v2: 预加载缓存+反向索引，避免O(N²)重复IO"""
import json, os, sys, time, hashlib, urllib.request, urllib.parse
from datetime import datetime
from collections import defaultdict

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
TOGGLE_PATH = os.path.join(DATA_DIR, 'kuaidi100_toggle.json')
KD100_KEY = "tufqlXgA2928"
KD100_CUSTOMER = "E26E983AE77169477938606B043C5494"

STATE_MAP = {0:"在途",1:"揽收",2:"疑难",3:"签收",4:"退签",5:"派件",6:"退回",7:"清关",8:"拒签"}

# ─── 快递100开关检查 ──────────────────────────────────────────────────────

def is_kuaidi100_enabled() -> bool:
    """检查管理后台的快递100企业版开关状态"""
    if os.path.exists(TOGGLE_PATH):
        try:
            with open(TOGGLE_PATH) as f:
                data = json.load(f)
            return bool(data.get('enabled', False))
        except:
            pass
    return False

# ─── 预加载辅助 ───────────────────────────────────────────────────────────

def _load_tracking_cache(month: str) -> dict:
    """加载单月 tracking_results.json（如果文件存在且非空）"""
    path = os.path.join(DATA_DIR, f'{month}_tracking_results.json')
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

def _build_domestic_index(tracking: dict) -> dict[str, str]:
    """建立 extTrackNoCn → tracking_no 反向索引"""
    idx = {}
    for tn, entry in tracking.items():
        if not isinstance(entry, dict):
            continue
        t = entry.get('tracking', {})
        if not isinstance(t, dict):
            continue
        ext = (t.get('extTrackNoCn') or '').strip()
        if ext:
            idx[ext] = tn  # 国内单号 → 国际单号（反向映射）
    return idx

def _load_phone_map(month: str) -> dict:
    """从 orders 加载 国际单号→收件人手机号 映射（SF查询用）"""
    path = os.path.join(DATA_DIR, f'{month}_orders.json')
    phone_map = {}
    if not os.path.exists(path):
        return phone_map
    try:
        with open(path, 'r') as f:
            orders_data = json.load(f)
        for o in orders_data.get('items', []):
            gw = (o.get('globalWayBillSN') or '').strip()
            tel = (o.get('consigneeTel') or '').strip()
            if gw and tel:
                phone_map[gw] = tel
    except:
        pass
    return phone_map

# ─── API 查询 ──────────────────────────────────────────────────────────────

def _kuaidi100_enterprise(biz_no: str, com: str, phone: str = '') -> dict | None:
    """企业版API查询（签名=param+key+customer）"""
    try:
        params = {'com': com, 'num': biz_no, 'resultv2': '4'}
        if phone:
            params['phone'] = phone
        param = json.dumps(params, separators=(',', ':'))
        raw = param + KD100_KEY + KD100_CUSTOMER
        sign = hashlib.md5(raw.encode()).hexdigest().upper()
        data = urllib.parse.urlencode({'customer': KD100_CUSTOMER, 'sign': sign, 'param': param}).encode()
        req = urllib.request.Request('https://poll.kuaidi100.com/poll/query.do', data=data,
            headers={'Content-Type': 'application/x-www-form-urlencoded'})
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
        if result.get('returnCode'):
            return None
        state = int(result.get('state', 0))
        raw_data = result.get('data', [])
        detected_com = result.get('com', '')
        resp = {
            'status': STATE_MAP.get(state, f"状态{state}"),
            'state_code': state,
            'events': len(raw_data),
            'latest_time': (raw_data[0] if raw_data else {}).get('time', ''),
            'latest_desc': ((raw_data[0] if raw_data else {}).get('context', ''))[:200],
            'source': f'kuaidi100_enterprise({com})',
            'raw_data': raw_data,
        }
        if detected_com:
            resp['detected_com'] = detected_com
        return resp
    except Exception:
        return None

def _kuaidi100_public_sf(biz_no: str) -> dict | None:
    """公共API查询SF（5s超时）"""
    try:
        url = f"https://www.kuaidi100.com/query?type=shunfeng&postid={biz_no}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        if data.get("status") != "200":
            return None
        state = int(data.get("state", 0))
        if state >= 300:
            status_name = "签收"
        else:
            status_name = STATE_MAP.get(state, f"状态{state}")
        items = data.get("data", [])
        events = [i for i in items if i.get("context", "") != "查无结果"] if isinstance(items, list) else []
        return {
            'status': status_name,
            'state_code': state,
            'events': len(events),
            'latest_time': (events[0] if events else {}).get('time', ''),
            'latest_desc': ((events[0] if events else {}).get('context', ''))[:80],
            'source': 'kuaidi100_public(sf)',
        }
    except Exception:
        return None

def _kuaidi100_public_heute(tracking_no: str) -> dict | None:
    """免费API查询 type=heute（兜底用）"""
    try:
        url = f"https://www.kuaidi100.com/query?type=heute&postid={tracking_no}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        if data.get("status") not in ("200", "201") and not data.get("data"):
            return None
        ischeck = data.get("ischeck", "0")
        state_str = str(data.get("state", ""))
        items = data.get("data", [])
        if ischeck == "1":
            status_name = "签收"
        elif state_str in {"0","1","2","3","4","5","6","7","8"}:
            status_name = STATE_MAP.get(int(state_str), f"状态{state_str}")
        else:
            status_name = "在途"
        events = [i for i in items if i.get("context", "") != "查无结果"] if isinstance(items, list) else []
        return {
            'status': status_name,
            'state_code': int(state_str) if state_str.isdigit() else -1,
            'events': len(events),
            'latest_time': (events[0] if events else {}).get('ftime', events[0].get('time', '') if events else ''),
            'latest_desc': ((events[0] if events else {}).get('context', ''))[:200],
            'source': 'kuaidi100_public(heute)',
            'raw_data': items,
        }
    except Exception:
        return None

# ─── 查询函数（接收预加载缓存，不重复IO）──────────────────────────────────

def query_intl_from_cache(tracking_no: str, caches: dict[str, dict]) -> dict | None:
    """查国际单号：从预加载的缓存中查找"""
    for month, cache in caches.items():
        entry = cache.get(tracking_no)
        if entry:
            t = entry.get('tracking', {})
            if isinstance(t, dict) and 'currentStatus' in t:
                details = t.get('trackingDetails', [])
                return {
                    'status': {'已签收':'签收','运输中':'在途','派件中':'派件','揽件中':'揽收'}.get(
                        t.get('currentStatus',''), t.get('currentStatus','')),
                    'events': len(details) if isinstance(details, list) else 0,
                    'latest_time': t.get('latestTime', ''),
                    'latest_desc': t.get('latestDesc', '')[:200],
                    'source': 'cache',
                }
    # 兜底：免费版实时查（仅在开关开启时）
    if is_kuaidi100_enabled():
        r = _kuaidi100_public_heute(tracking_no)
        if r:
            return r
    return None

def query_domestic_from_cache(ext_no: str, dom_index: dict[str, str],
                               caches: dict[str, dict], phone: str = '') -> dict | None:
    """查国内单号：从预加载的反向索引+缓存中查找，企业版兜底"""
    if not ext_no:
        return None
    # 1) 从反向索引找 → O(1)
    if ext_no in dom_index:
        intl_tn = dom_index[ext_no]
        for month, cache in caches.items():
            entry = cache.get(intl_tn)
            if entry:
                t = entry.get('tracking', {})
                if isinstance(t, dict):
                    details = t.get('trackingDetails', [])
                    if isinstance(details, list) and len(details) >= 3:
                        return {
                            'status': {'已签收':'签收','运输中':'在途','派件中':'派件'}.get(
                                t.get('currentStatus',''), t.get('currentStatus','')),
                            'events': len(details),
                            'source': 'cache',
                        }
    # 2) 没有缓存 → 企业版实时查（仅在开关开启时）
    if not is_kuaidi100_enabled():
        return None
    prefix = ext_no[:2].upper()
    if prefix == 'JD':
        # 京东走免费版
        try:
            url = f"https://www.kuaidi100.com/query?type=jd&postid={ext_no}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read())
            if data.get("status") == "200" and data.get("data"):
                return {'status': '在途', 'events': len([i for i in data["data"] if i.get("context") != "查无结果"]),
                        'source': 'kuaidi100_public'}
        except:
            pass
    elif prefix == 'SF':
        # 顺丰：企业版优先（带phone），免费版兜底
        result = _kuaidi100_enterprise(ext_no, 'shunfeng', phone)
        if result:
            return result
        result = _kuaidi100_public_sf(ext_no)
        if result:
            return result
    else:
        # 其他类型走企业版auto
        result = _kuaidi100_enterprise(ext_no, 'auto')
        if result:
            return result
    return None

# ─── 对比分析 ──────────────────────────────────────────────────────────────

def _raw_to_tracking_details(raw_data: list) -> list:
    """API原始数据 → trackingDetails 格式"""
    if not raw_data:
        return []
    details = []
    for item in raw_data:
        if not isinstance(item, dict):
            continue
        ctx = item.get('context', '')
        if ctx == '查无结果':
            continue
        details.append({
            'trackingTime': item.get('ftime', item.get('time', '')),
            'trackingDesc': ctx,
            'statusName': item.get('status', ''),
            'currentSiteName': '',
            'nextSiteName': '',
            'address': item.get('areaName', '') or item.get('areaCode', '') or '',
            'contact': '',
            'contactPhone': '',
            'signerName': '',
            'signerTypeDesc': '',
        })
    return details

# 国际状态中"已落后"的标志：国内已签收但国际还未更新
_SEVERE_INTL = {'在途', '派件', '清关', '揽收', '其他', '离开', '到达', '未知', ''}

def compare(intl: dict, dom: dict) -> str:
    """对比分析 → ok / warning / severe"""
    is_intl = intl.get('status', '')
    ds = dom.get('status', '')
    if is_intl == ds:
        return 'ok'
    # 国内已签收 → 国际还在途中/未知/离开 → 严重异常
    if ds == '签收' and is_intl in _SEVERE_INTL:
        return 'severe'
    if is_intl in ('清关', '已清关', '在途') and ds in ('家签', '本人签收', '已签收'):
        return 'severe'
    if is_intl != ds:
        return 'warning'
    return 'ok'

# ─── 主流程 ────────────────────────────────────────────────────────────────

def scan_month(month: str, dry_run: bool = False, fix: bool = False):
    """扫描指定月份的在途单号"""
    tracking_file = os.path.join(DATA_DIR, f'{month}_tracking_results.json')
    output_file = os.path.join(DATA_DIR, f'anomaly_comparison_{month}.json')

    if not os.path.exists(tracking_file):
        print(f"❌ {tracking_file} not found")
        return

    print(f"📦 预加载缓存...")
    t0 = time.time()

    # ── 一次性加载所有月份缓存 ──
    caches = {}
    for m in ('april', 'may'):
        caches[m] = _load_tracking_cache(m)
    cur_tracking = caches.get(month, {})

    print(f"    loaded {sum(len(v) for v in caches.values())} entries from {len(caches)} months ({time.time()-t0:.1f}s)")

    # ── 建立反向索引（跨月份）──
    dom_index = {}  # extTrackNoCn → tracking_no
    for m, cache in caches.items():
        idx = _build_domestic_index(cache)
        dom_index.update(idx)
    print(f"    built {len(dom_index)} domestic reverse index entries")

    # ── 加载手机号 ──
    phone_map = _load_phone_map(month)

    print(f"📦 {month}: {len(cur_tracking)} entries in cache")

    # 找出需要扫描的单号
    targets = []
    for tn, entry in cur_tracking.items():
        if not isinstance(entry, dict):
            continue
        t = entry.get('tracking', {})
        if not isinstance(t, dict):
            continue
        ext = t.get('extTrackNoCn', '')
        status = t.get('currentStatus', '')
        if not ext:
            continue
        if status in ('签收', '已签收', '已撤销', '运单已经创建', '签收(国内确认)'):
            continue
        targets.append({
            'intl_tn': tn,
            'dom_tn': ext,
            'cached_status': status,
            'order_sn': entry.get('order', {}).get('sn', '') if isinstance(entry.get('order'), dict) else '',
        })

    print(f"🎯 {len(targets)} targets (non-签收 with domestic counterpart)")

    if dry_run:
        for t in targets[:10]:
            print(f"  {t['order_sn']:20s} {t['intl_tn'][:25]} → {t['dom_tn']} [{t['cached_status']}]")
        return

    # ── 批量查询 ──
    results = []
    severe_new = []

    for i, t in enumerate(targets):
        intl_tn = t['intl_tn']
        dom_tn = t['dom_tn']

        # 查询国际（从缓存/免费版）
        intl_result = query_intl_from_cache(intl_tn, caches)
        if not intl_result:
            intl_result = {'status': t['cached_status'], 'state_code': -1, 'events': 0,
                          'latest_time': '', 'latest_desc': '', 'source': 'cache'}

        # 查询国内（从反向索引/企业版）
        dom_result = query_domestic_from_cache(dom_tn, dom_index, caches, phone_map.get(intl_tn, ''))
        if not dom_result:
            continue

        match = compare(intl_result, dom_result)

        record = {
            'order_sn': t['order_sn'],
            'intl_tracking': intl_tn,
            'dom_tracking': dom_tn,
            'intl': intl_result,
            'dom': dom_result,
            'match': match,
            'scanned_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }

        # 时间差计算
        it = intl_result.get('latest_time', '')
        dt = dom_result.get('latest_time', '')
        if it and dt:
            try:
                t1 = datetime.strptime(it[:19], '%Y-%m-%d %H:%M:%S')
                t2 = datetime.strptime(dt[:19], '%Y-%m-%d %H:%M:%S')
                record['time_diff_hours'] = round(abs((t1 - t2).total_seconds()) / 3600, 1)
            except:
                pass

        results.append(record)

        tag = '🔴' if match == 'severe' else ('🟡' if match == 'warning' else '🟢')
        print(f"  {tag} [{i+1}/{len(targets)}] {intl_tn[:20]} {intl_result['status']} vs {dom_result['status']} ({match})")

        if match == 'severe':
            severe_new.append(record)

        # 增量保存每20条
        if (i + 1) % 20 == 0:
            with open(output_file, 'w') as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            print(f"  💾 saved {len(results)}/{len(targets)}")

    # 写最终结果（JSON + SQLite）
    with open(output_file, 'w') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from heute_db import bulk_upsert_anomalies
        bulk_upsert_anomalies(results, month)
    except Exception as e:
        print(f"  ⚠️ SQLite写入失败: {e}")

    # 统计
    counts = defaultdict(int)
    for r in results:
        counts[r['match']] += 1

    print(f"\n✅ {month} scan done: {len(results)} compared ({time.time()-t0:.1f}s total)")
    print(f"   🟢 ok: {counts['ok']}  🟡 warning: {counts['warning']}  🔴 severe: {counts['severe']}")

    if severe_new:
        print(f"\n🚨 {len(severe_new)} SEVERE anomalies:")
        for r in severe_new:
            print(f"   {r['order_sn']} {r['intl_tracking'][:25]} → {r['dom_tracking']}")

    # ── 可选：在t0记录耗时时顺便记录──
    elapsed = time.time() - t0

    # ── Fix: 补填缺少轨迹的条目 ──
    if fix:
        # 预先收集需要补填的列表
        to_fill = []
        for tn, entry in cur_tracking.items():
            if not isinstance(entry, dict):
                continue
            track_data = entry.get('tracking', {})
            if not isinstance(track_data, dict):
                continue
            ext = track_data.get('extTrackNoCn', '')
            if not ext:
                continue
            existing_details = track_data.get('trackingDetails', [])
            if isinstance(existing_details, list) and len(existing_details) >= 3:
                continue
            to_fill.append(tn)

        if to_fill:
            print(f"\n📡 补填轨迹: {len(to_fill)} entries with extTrackNoCn but <3 details")
            fill_count = 0
            for i, tn in enumerate(to_fill):
                entry = cur_tracking[tn]
                track_data = entry.get('tracking', {})
                if not isinstance(track_data, dict):
                    continue
                ext = track_data.get('extTrackNoCn', '')
                if not ext:
                    continue
                existing_details = track_data.get('trackingDetails', [])
                if isinstance(existing_details, list) and len(existing_details) >= 3:
                    continue

                intl_result = query_intl_from_cache(tn, caches)
                if not intl_result:
                    continue
                raw_data = intl_result.get('raw_data', [])
                if not raw_data:
                    continue
                new_details = _raw_to_tracking_details(raw_data)
                if new_details and len(new_details) > len(existing_details):
                    track_data['trackingDetails'] = new_details
                    track_data['trackingNo'] = tn
                    if intl_result.get('latest_time'):
                        track_data['latestTime'] = intl_result['latest_time']
                        track_data['latestDesc'] = intl_result.get('latest_desc', '')
                        track_data['currentStatus'] = intl_result['status']
                    fill_count += 1
                    print(f"  📥 [{fill_count}/{len(to_fill)}] {tn[:25]} → {len(new_details)}条轨迹 ({intl_result['status']})", flush=True)

                if (i + 1) % 20 == 0:
                    # 只存被修改过的
                    with open(tracking_file, 'w') as f:
                        json.dump(cur_tracking, f, ensure_ascii=False, indent=2)
                    # SQLite同步
                    try:
                        from heute_db import bulk_upsert_tracking
                        bulk_upsert_tracking(cur_tracking, month)
                    except: pass
                    print(f"  💾 saved tracking cache ({fill_count} filled so far)")

            with open(tracking_file, 'w') as f:
                json.dump(cur_tracking, f, ensure_ascii=False, indent=2)
            # SQLite同步
            try:
                from heute_db import bulk_upsert_tracking
                bulk_upsert_tracking(cur_tracking, month)
            except: pass
            print(f"\n📡 轨迹补填完成: {fill_count}/{len(to_fill)} filled")
        else:
            print(f"\n📡 轨迹补填: 无需补填（全部已有足够轨迹）")

    # ── Fix: 自动修正国内已签收的订单状态 ──
    if fix and severe_new:
        fixed_count = 0
        skipped_recent = 0
        for r in severe_new:
            intl_tn = r['intl_tracking']
            time_diff = r.get('time_diff_hours', 0)
            if time_diff < 2:
                skipped_recent += 1
                continue
            if intl_tn in cur_tracking and isinstance(cur_tracking[intl_tn], dict):
                track_data = cur_tracking[intl_tn].get('tracking', {})
                if isinstance(track_data, dict):
                    dom_time = r['dom'].get('latest_time', '')[:16]
                    track_data['currentStatus'] = '签收(国内确认)'
                    track_data['latestDesc'] = f'[国内{dom_time}确认签收] {r["dom"].get("latest_desc", "")}'
                    track_data['latestTime'] = r['dom'].get('latest_time', '')
                    track_data['_fix_source'] = 'auto-fix-by-scan'
                    fixed_count += 1

        if fixed_count > 0:
            with open(tracking_file, 'w') as f:
                json.dump(cur_tracking, f, ensure_ascii=False, indent=2)
            # SQLite同步
            try:
                from heute_db import bulk_upsert_tracking
                bulk_upsert_tracking(cur_tracking, month)
            except: pass
            print(f"\n🔧 FIXED: {fixed_count} orders updated to 签收(国内确认) in {tracking_file}")
        if skipped_recent > 0:
            print(f"⏭  SKIPPED: {skipped_recent} severe cases with time_diff<2h (may be fresh sync)")
        if fixed_count == 0 and skipped_recent == 0:
            print(f"\n⚠️  No fixes applied")

    return results, severe_new

if __name__ == '__main__':
    month = sys.argv[1] if len(sys.argv) > 1 else 'april'
    dry_run = '--dry' in sys.argv
    fix = '--fix' in sys.argv
    scan_month(month, dry_run=dry_run, fix=fix)
