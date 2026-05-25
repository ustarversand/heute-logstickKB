#!/usr/bin/env python3
"""
货易达物流轨迹查询 API 封装
=======================
track.heute-express.com 的 Python urllib 封装
- 自动登录 + Token 缓存（带过期检查）
- 批量多线程查询
- 进度回调支持
- 浏览器 User-Agent 规避 Cloudflare WAF
"""

import json
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

# ─── 配置 ───────────────────────────────────────────────────────────────────
BASE_API = 'https://track.heute-express.com/api'
LOGIN_USER = 'USTAR'
LOGIN_PASS = 'Hilden11031980!'
DEFAULT_TOKEN_FILE = '/tmp/heute_track_token.json'
DEFAULT_THREADS = 8
REQUEST_TIMEOUT = 15

# 浏览器头 — 防 Cloudflare WAF 拦截
BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Origin': 'https://track.heute-express.com',
    'Referer': 'https://track.heute-express.com/',
}


# ─── 登录 ───────────────────────────────────────────────────────────────────
def login(token_file: str = DEFAULT_TOKEN_FILE) -> str:
    """货易达 track API 登录，返回 accessToken（自动缓存）"""
    # 检查缓存
    if os.path.exists(token_file):
        try:
            with open(token_file) as f:
                cached = json.load(f)
            if cached.get('expires_at', 0) > time.time() + 300:
                return cached['token']
        except (json.JSONDecodeError, KeyError):
            pass

    # 登录请求
    data = json.dumps({'username': LOGIN_USER, 'password': LOGIN_PASS}).encode()
    req = urllib.request.Request(
        f'{BASE_API}/auth/login',
        data=data,
        headers={**BROWSER_HEADERS, 'Content-Type': 'application/json'},
    )
    resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
    result = json.loads(resp.read().decode())

    token = result['data']['accessToken']
    expires_in = result['data'].get('expiresIn', 7200)

    with open(token_file, 'w') as f:
        json.dump({'token': token, 'expires_at': time.time() + expires_in}, f)

    return token


# ─── 单条查询 ────────────────────────────────────────────────────────────────
def query(gw: str, token: Optional[str] = None) -> dict:
    """查单个国际运单号的轨迹，返回完整结果或空 dict"""
    if token is None:
        token = login()

    url = f'{BASE_API}/logistics-tracking?trackingNo={gw}&page=1&size=100'
    req = urllib.request.Request(url, headers={**BROWSER_HEADERS, 'Authorization': f'Bearer {token}'})
    try:
        resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
        data = json.loads(resp.read().decode())
        records = data.get('data', {}).get('records', [])
        if not records:
            return {}
        r = records[0]
        return {
            'trackingNo': r.get('trackingNo', ''),
            'extTrackNoCn': r.get('extTrackNoCn', ''),
            'logisticsCompany': r.get('cnLogisticsCompany', ''),
            'currentStatus': r.get('platformTrackingStatusName', ''),
            'latestDesc': (r.get('platformTrackingStatusText', '') or '')[:200],
            'latestTime': r.get('platformTrackingStatusTime', ''),
            'subscriptionSource': r.get('subscriptionSource', ''),
            'isSubscribed': r.get('isSubscribed', 0),
            'trackingDetails': r.get('trackingDetails', []),
        }
    except Exception:
        return {}


# ─── 批量查询 ────────────────────────────────────────────────────────────────
def batch_query(
    gw_list: list,
    threads: int = DEFAULT_THREADS,
    progress_cb: Optional[Callable] = None,
    token_file: str = DEFAULT_TOKEN_FILE,
) -> dict:
    """
    批量查询，返回 {gw: result_dict}
    progress_cb(current, total, ok_count, fail_count)
    """
    token = login(token_file)
    results: dict = {}
    ok_count = 0
    fail_count = 0
    total = len(gw_list)
    done = 0

    def _query_one(gw: str) -> tuple:
        nonlocal ok_count, fail_count
        try:
            r = query(gw, token)
            if r and r.get('trackingNo'):
                ok_count += 1
            else:
                fail_count += 1
            return gw, r, True
        except Exception:
            fail_count += 1
            return gw, {'tracking': {'error': str(Exception)}}, False

    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = {pool.submit(_query_one, gw): gw for gw in gw_list}
        for f in as_completed(futures):
            gw, result, _ = f.result()
            results[gw] = result
            done += 1
            if progress_cb and done % 10 == 0:
                progress_cb(done, total, ok_count, fail_count)

    if progress_cb:
        progress_cb(done, total, ok_count, fail_count)

    return results


# ─── 精简版：直接查一个运单并返回格式化结果 ──────────────────────────────────
def query_simple(gw: str) -> Optional[dict]:
    """简便调用：登录+查询一条，返回精简结果"""
    try:
        result = query(gw)
        if result:
            return {
                'tracking_no': result.get('trackingNo', ''),
                'status': result.get('currentStatus', ''),
                'desc': result.get('latestDesc', ''),
                'time': result.get('latestTime', ''),
                'company': result.get('logisticsCompany', ''),
            }
    except Exception:
        pass
    return None


# ─── 测试入口 ────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'batch':
        # 批量测试：python3 heute_track_api.py batch gw1,gw2,gw3
        gws = sys.argv[2].split(',') if len(sys.argv) > 2 else ['TEST']
        print(f'🔍 批量查询 {len(gws)} 条…')
        results = batch_query(gws, threads=4)
        for gw, r in results.items():
            t = r.get('tracking', r) if isinstance(r, dict) else {}
            print(f'  {gw}: {t.get("currentStatus", "查无结果")}')
    else:
        # 单条测试
        gw = sys.argv[1] if len(sys.argv) > 1 else ''
        if gw:
            print(f'🔍 查询 {gw}…')
            r = query(gw)
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            # 只测登录
            token = login()
            print(f'✅ 登录成功，Token: {token[:20]}...')
