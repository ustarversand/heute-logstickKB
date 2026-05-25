#!/usr/bin/env python3
"""
🚀 轨迹批量查询 — 货易达track API（唯一来源）
货易达track API track.heute-express.com (8线程并发，百条自动保存)
"""
import json, os, sys, time
from datetime import datetime
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import ssl

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
LOG_FILE = '/tmp/batch_track_hybrid.log'

# 进度文件（由看板传入，前端轮询用）
_PROGRESS_FILE = None
_TOTAL_ESTIMATE = 0

def _write_pbar(current, total=None, msg=''):
    """写进度到文件供看板前端轮询"""
    if not _PROGRESS_FILE:
        return
    t = total or _TOTAL_ESTIMATE
    try:
        with open(_PROGRESS_FILE, 'w') as f:
            json.dump({
                'running': True, 'phase': 'tracking',
                'current': current, 'total': t,
                'percent': round(current / t * 100, 1) if t > 0 else 0,
                'message': msg or f'轨迹查询 {current}/{t}',
                'done': False,
            }, f)
    except Exception:
        pass

# 货易达API（唯一来源）
TOKEN_FILE = '/tmp/heute_track_token.json'
BASE_API = 'https://track.heute-express.com/api'

MONTH_FILES = {
    'april': {'orders': 'april_orders.json', 'tracking': 'april_tracking_results.json'},
    'may':   {'orders': 'may_orders.json',   'tracking': 'may_tracking_results.json'},
}

_CTX = ssl.create_default_context()


def log(msg):
    ts = datetime.now().isoformat()[:19]
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')


# ═════════════════════════════════════════════════════════════════════
#  货易达track API
# ═════════════════════════════════════════════════════════════════════

# ─── 从 heute_track_api 导入 ───────────────────────────────────────────
from heute_track_api import login as _hoje_login, query as _hoje_query

def _hoje_query(gw):
    """货易达查当前状态"""
    data = _hoje_query(gw)
    if not data:
        return None
    return {
        'trackingNo': data.get('trackingNo', ''),
        'extTrackNoCn': data.get('extTrackNoCn', ''),
        'logisticsCompany': data.get('logisticsCompany', ''),
        'currentStatus': data.get('currentStatus', ''),
        'latestDesc': (data.get('latestDesc', '') or '')[:200],
        'latestTime': data.get('latestTime', ''),
        'subscriptionSource': data.get('subscriptionSource', ''),
        'isSubscribed': data.get('isSubscribed', 0),
        'trackingDetails': data.get('trackingDetails', []),
    }


# ═════════════════════════════════════════════════════════════════════
#  公共
# ═════════════════════════════════════════════════════════════════════

def load_orders_and_existing(month):
    """加载订单和已有结果"""
    orders_file = os.path.join(DATA_DIR, MONTH_FILES[month]['orders'])
    tracking_file = os.path.join(DATA_DIR, MONTH_FILES[month]['tracking'])

    with open(orders_file) as f:
        raw = json.load(f)
    orders = raw.get('items', raw if isinstance(raw, list) else [])

    existing = {}
    if os.path.exists(tracking_file) and os.path.getsize(tracking_file) > 0:
        with open(tracking_file, 'rb') as f:
            existing = json.loads(f.read().decode('utf-8', errors='replace'))

    pending = []
    stats = {'total': 0, 'skip_signed_full': 0, 'skip_signed_short': 0,
             'skip_cancelled': 0, 'to_query': 0}
    for o in orders:
        gw = (o.get('globalWayBillSN') or '').strip()
        if not gw:
            continue
        if o.get('state') == -6:
            stats['skip_cancelled'] += 1
            continue
        stats['total'] += 1
        if gw in existing:
            t = existing[gw].get('tracking', {})
            if isinstance(t, dict) and t.get('currentStatus') in ('已签收', '已撤销'):
                tl = t.get('trackingDetails', [])
                tl_len = len(tl) if isinstance(tl, list) else 0
                if tl_len >= 10:
                    stats['skip_signed_full'] += 1
                    continue
                else:
                    stats['skip_signed_short'] += 1
                    continue
        pending.append(o)
        stats['to_query'] += 1
    return orders, pending, existing, stats


def save(month, results):
    """保存结果 + 摘要（JSON + SQLite双写）"""
    tracking_file = os.path.join(DATA_DIR, MONTH_FILES[month]['tracking'])
    with open(tracking_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, default=str)
    # 同步写入SQLite
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from heute_db import bulk_upsert_tracking
        bulk_upsert_tracking(results, month)
    except Exception as e:
        log(f"  ⚠️ SQLite同步失败: {e}")
    cnt = Counter()
    total_tl = 0
    for r in results.values():
        t = r.get('tracking', {})
        if isinstance(t, dict):
            s = t.get('currentStatus', '')
            cnt[s] += 1 if s else 0
            tl = t.get('trackingDetails', [])
            total_tl += len(tl) if isinstance(tl, list) else 0
            if 'error' in t:
                cnt['查询失败'] += 1
        else:
            cnt['未知'] += 1
    by_state = os.path.join(DATA_DIR, f'{month}_tracking_by_state.json')
    with open(by_state, 'w') as f:
        json.dump(dict(cnt.most_common()), f, ensure_ascii=False)
    log(f"  💾 已保存 {len(results)} 条, 状态: {dict(cnt.most_common(6))}, 轨迹总计: {total_tl}条")


def _fill_ext_tracking(result: dict, order_sn) -> None:
    """如果轨迹提取不到国内单号，直接用货易达的 tempLineSN 补上"""
    if isinstance(order_sn, str):
        return
    temp = (order_sn.get('tempLineSN') or '').strip()
    if not temp:
        return
    tracking = result.get('tracking', {})
    if isinstance(tracking, dict) and not tracking.get('extTrackNoCn', ''):
        tracking['extTrackNoCn'] = temp


def do_query_single(gw, order_sn=''):
    """单条查询: 货易达track API 唯一来源
    返回 (gw, result_dict, ok, source)
    """
    try:
        hoje = _hoje_query(gw)
        if hoje:
            result = {
                'order': {
                    'sn': order_sn,
                } if isinstance(order_sn, str) else order_sn,
                'tracking': hoje,
                'queried_at': datetime.now().isoformat()[:19],
            }
            _fill_ext_tracking(result, order_sn)
            return gw, result, True, 'hoje'
    except Exception:
        pass

    result = {
        'order': {'sn': order_sn} if isinstance(order_sn, str) else order_sn,
        'tracking': {'error': '查询失败'},
        'queried_at': datetime.now().isoformat()[:19],
    }
    _fill_ext_tracking(result, order_sn)
    return gw, result, False, ''


def run_month(month, force=False):
    log(f"\n{'='*55}")
    if force:
        log(f"🚀 全量刷新 {month}月（强制模式：包含短轨迹签收单）")
        orders, pending, existing, raw_stats = load_orders_and_existing(month)
        tracking_file = os.path.join(DATA_DIR, MONTH_FILES[month]['tracking'])
        existing_data = {}
        if os.path.exists(tracking_file) and os.path.getsize(tracking_file) > 0:
            with open(tracking_file, 'rb') as f:
                existing_data = json.loads(f.read().decode('utf-8', errors='replace'))

        pending = []
        stats = {'total': 0, 'skip_signed_full': 0, 'skip_signed_short': 0,
                 'skip_cancelled': 0, 'to_query': 0, 'force_refresh': 0}
        for o in orders:
            gw = (o.get('globalWayBillSN') or '').strip()
            if not gw:
                continue
            if o.get('state') == -6:
                stats['skip_cancelled'] += 1
                continue
            stats['total'] += 1
            if gw in existing_data:
                t = existing_data[gw].get('tracking', {})
                if isinstance(t, dict) and t.get('currentStatus') in ('已签收', '已撤销'):
                    tl = t.get('trackingDetails', [])
                    tl_len = len(tl) if isinstance(tl, list) else 0
                    if tl_len >= 10:
                        stats['skip_signed_full'] += 1
                        continue
                    else:
                        stats['force_refresh'] += 1
            pending.append(o)
            stats['to_query'] += 1

        log(f"📊 {month}月: 共{stats['total']}单, "
            f"✅丰满跳过{stats['skip_signed_full']}单, "
            f"🔄强制刷新{stats['force_refresh']}单, "
            f"待查{stats['to_query']}单")
    else:
        log(f"🚀 货易达track {month}月（货易达API独家）")
        orders, pending, existing, stats = load_orders_and_existing(month)
        log(f"📊 {month}月: 共{stats['total']}单, "
            f"✅丰满跳过{stats['skip_signed_full']}单, "
            f"🟡短轨迹跳过{stats['skip_signed_short']}单, "
            f"🗑撤销{stats['skip_cancelled']}单, "
            f"待查{stats['to_query']}单")

    if stats['to_query'] == 0:
        log("✅ 全部已完成")
        return

    results = existing.copy()
    total = len(pending)
    start = time.time()

    step1_ok = step1_err = 0
    cnt_hoje = 0

    # ═══════════════════════════════════════
    # 并行查询 (8线程)
    # ═══════════════════════════════════════
    log(f"\n⚡ 查询 {total}条, 8线程")

    def do_track(order):
        gw = (order.get('globalWayBillSN') or '').strip()
        return do_query_single(gw, {
            'sn': order.get('sn', ''),
            'consignee': order.get('consigneeName', ''),
            'state': order.get('state'),
            'lineName': order.get('lineName', ''),
            'created': (order.get('creationTime', '') or '')[:10],
            'senderName': order.get('senderName', ''),
            'tempLineSN': (order.get('tempLineSN') or '').strip(),
        })

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(do_track, o): o for o in pending}
        for i, future in enumerate(as_completed(futures)):
            gw, result, ok, source = future.result()
            results[gw] = result
            if ok:
                step1_ok += 1
                if source == 'hoje':
                    cnt_hoje += 1
            else:
                step1_err += 1

            done = i + 1
            if done % 100 == 0:
                el = time.time() - start
                rate = done / el if el > 0 else 0
                rem = (total - done) / rate if rate > 0 else 0
                log(f"  📊 [{done}/{total}] {step1_ok}成功/{step1_err}失败 | "
                    f"🏪货易达{cnt_hoje} | "
                    f"{rate:.0f}条/秒 | 预计剩余{rem:.0f}s")
                save(month, results)
                # 每100条刷新进度
                _write_pbar(done, total, f'轨迹查询 {done}/{total} ({step1_ok}成功)')

    # 最终保存
    save(month, results)
    el = time.time() - start
    total_tl = sum(
        len(r.get('tracking', {}).get('trackingDetails', []))
        if isinstance(r.get('tracking', {}), dict)
        and isinstance(r.get('tracking', {}).get('trackingDetails'), list) else 0
        for r in results.values()
    )
    log(f"\n{'='*55}")
    log(f"✅ {month}月完成！")
    log(f"  成功{step1_ok}条 | 货易达API{cnt_hoje} | 失败{step1_err}")
    log(f"  总轨迹数: {total_tl}条 | 总耗时: {el:.0f}秒 ({el/60:.1f}分钟)")
    # 进度完成
    _write_pbar(total, total, f'全部完成 {total}条 ({step1_ok}成功)')
    if _PROGRESS_FILE:
        try:
            with open(_PROGRESS_FILE, 'w') as f:
                json.dump({'running': False, 'phase': 'done', 'current': total, 'total': total,
                           'percent': 100, 'message': f'更新完成 {step1_ok}成功/{step1_err}失败',
                           'done': True}, f)
        except Exception:
            pass


if __name__ == '__main__':
    force = '--force' in sys.argv
    # 解析 --progress-file 和 --total
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    month = args[0] if args and args[0] in ('april', 'may') else 'april'
    for i, a in enumerate(sys.argv):
        if a == '--progress-file' and i + 1 < len(sys.argv):
            _PROGRESS_FILE = sys.argv[i + 1]
        if a == '--total' and i + 1 < len(sys.argv):
            try:
                _TOTAL_ESTIMATE = int(sys.argv[i + 1])
            except ValueError:
                pass
    if month not in ('april', 'may'):
        print("用法: python3 batch_track_hybrid.py [april|may] [--force] [--progress-file PATH] [--total N]")
        sys.exit(1)
    run_month(month, force=force)
