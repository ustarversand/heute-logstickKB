#!/usr/bin/env python3
"""
货易达统一 API 封装
=================
heute_api.py — 合并两个子系统

  🚚 TrackClient  → track.heute-express.com  物流轨迹查询
  📦 OrderClient  → www.heute-express.com    订单列表+明细

用法:
    from heute_api import HeuteAPI
    
    api = HeuteAPI()  # 自动用 USTAR 账号
    
    # 🚚 轨迹
    track = api.track.query("DEUHYD600169132940EU")
    
    # 📦 订单列表（最新100条）
    orders = api.order.list(page=1, page_size=100)
    
    # 📦 订单详情
    detail = api.order.detail("2605150906547151")

零外部依赖，只用 Python 标准库（urllib + json + os + time）
"""

import json
import os
import time
import urllib.request
import urllib.error
import ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional, List, Dict, Any


# ═══════════════════════════════════════════════════════════════════════════════
# 全局配置
# ═══════════════════════════════════════════════════════════════════════════════

LOGIN_USER = "USTAR"
LOGIN_PASS = "Hilden11031980!"
REQUEST_TIMEOUT = 15

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.heute-express.com",
    "Referer": "https://www.heute-express.com/",
}


# ═══════════════════════════════════════════════════════════════════════════════
# 🚚 TrackClient — 物流轨迹查询 (track.heute-express.com)
# ═══════════════════════════════════════════════════════════════════════════════

TRACK_BASE = "https://track.heute-express.com/api"
TRACK_TOKEN_FILE = "/tmp/heute_track_token.json"
TRACK_TOKEN_EXPIRES = 7200       # 秒
TRACK_DEFAULT_THREADS = 8

_TRACK_HEADERS = {
    "User-Agent": BROWSER_HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://track.heute-express.com",
    "Referer": "https://track.heute-express.com/",
}


class TrackClient:
    """🚚 物流轨迹查询 — track.heute-express.com"""

    def __init__(self, token_file: str = TRACK_TOKEN_FILE):
        self.token_file = token_file
        self._token: Optional[str] = None
        self._ctx = ssl.create_default_context()

    # ─── 登录 ────────────────────────────────────────────────────────────

    def login(self, force: bool = False) -> str:
        """
        登录轨迹系统，返回 accessToken（自动缓存）
        force=True 强制重新登录（忽略缓存）
        """
        # 检查缓存
        if not force and os.path.exists(self.token_file):
            try:
                with open(self.token_file) as f:
                    cached = json.load(f)
                if cached.get("expires_at", 0) > time.time() + 300:
                    self._token = cached["token"]
                    return self._token
            except (json.JSONDecodeError, KeyError):
                pass

        data = json.dumps({"username": LOGIN_USER, "password": LOGIN_PASS}).encode()
        req = urllib.request.Request(
            f"{TRACK_BASE}/auth/login",
            data=data,
            headers={**_TRACK_HEADERS, "Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=self._ctx)
        result = json.loads(resp.read().decode())

        self._token = result["data"]["accessToken"]
        expires_in = result["data"].get("expiresIn", TRACK_TOKEN_EXPIRES)

        with open(self.token_file, "w") as f:
            json.dump(
                {"token": self._token, "expires_at": time.time() + expires_in}, f
            )
        return self._token

    def _ensure_token(self):
        if not self._token:
            self.login()

    # ─── 单条查询 ────────────────────────────────────────────────────────

    def query(self, gw: str) -> dict:
        """查单个国际运单号的轨迹"""
        self._ensure_token()
        url = f"{TRACK_BASE}/tracking?trackingNo={gw}"
        req = urllib.request.Request(
            url,
            headers={**_TRACK_HEADERS, "Authorization": f"Bearer {self._token}"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=self._ctx)
            data = json.loads(resp.read().decode())
            records = data.get("data", {}).get("records", [])
            if not records:
                return {}
            r = records[0]
            return {
                "trackingNo": r.get("trackingNo", ""),
                "extTrackNoCn": r.get("extTrackNoCn", ""),
                "logisticsCompany": r.get("cnLogisticsCompany", r.get("logisticsCompany", "")),
                "currentStatus": r.get("platformTrackingStatusName", ""),
                "latestDesc": (r.get("platformTrackingStatusText", "") or "")[:200],
                "latestTime": r.get("platformTrackingStatusTime", ""),
                "subscriptionSource": r.get("subscriptionSource", ""),
                "isSubscribed": r.get("isSubscribed", 0),
                "trackingDetails": r.get("trackingDetails", []),
            }
        except Exception:
            return {}

    # ─── 批量查询（多线程） ──────────────────────────────────────────────

    def batch_query(
        self,
        gw_list: list,
        threads: int = TRACK_DEFAULT_THREADS,
        progress_cb: Optional[Callable] = None,
    ) -> dict:
        """批量查轨迹，返回 {gw: result_dict}"""
        self._ensure_token()
        results: dict = {}
        ok_count = 0
        fail_count = 0
        total = len(gw_list)
        done = 0

        def _query_one(gw: str) -> tuple:
            nonlocal ok_count, fail_count
            try:
                r = self.query(gw)
                if r and r.get("trackingNo"):
                    ok_count += 1
                else:
                    fail_count += 1
                return gw, r, True
            except Exception:
                fail_count += 1
                return gw, {}, False

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

    # ─── 简便调用 ────────────────────────────────────────────────────────

    def query_simple(self, gw: str) -> Optional[dict]:
        """查一条返回精简结果"""
        try:
            result = self.query(gw)
            if result:
                return {
                    "tracking_no": result.get("trackingNo", ""),
                    "status": result.get("currentStatus", ""),
                    "desc": result.get("latestDesc", ""),
                    "time": result.get("latestTime", ""),
                    "company": result.get("logisticsCompany", ""),
                }
        except Exception:
            pass
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# 📦 OrderClient — 订单查询 (www.heute-express.com)
# ═══════════════════════════════════════════════════════════════════════════════

ORDER_BASE = "https://www.heute-express.com"
ORDER_TOKEN_FILE = "/tmp/heute_order_token.txt"

ORDER_API = {
    "login":     "/Prod/api/app/token/login",
    "list":      "/Prod/api/app/member-order/get-member-order-list",
    "detail":    "/Prod/api/app/member-order/get-member-order-for-view",
    "lines":     "/Prod/api/app/member-order/get-enable-line",
    "wait_pay":  "/Prod/api/app/member-order/get-member-wait-pay-order-count",
}

ORDER_STATES = {
    0: "已作废", 1: "待支付", 2: "待入库",
    3: "国际运输", 4: "国内配送", 5: "签收",
}


class OrderClient:
    """📦 订单查询 — www.heute-express.com"""

    def __init__(self, token_file: str = ORDER_TOKEN_FILE):
        self.token_file = token_file
        self._token: Optional[str] = None
        self._ctx = ssl.create_default_context()

    # ─── 登录 ────────────────────────────────────────────────────────────

    def login(self, force: bool = False) -> str:
        """登录订单系统，返回 Token（自动缓存）"""
        if not force and self._token:
            return self._token
        if not force and os.path.exists(self.token_file):
            with open(self.token_file) as f:
                self._token = f.read().strip()
            if self._token:
                return self._token

        url = f"{ORDER_BASE}{ORDER_API['login']}"
        body = json.dumps({"name": LOGIN_USER, "password": LOGIN_PASS}).encode()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": BROWSER_HEADERS["User-Agent"],
            "Origin": ORDER_BASE,
            "Referer": f"{ORDER_BASE}/login",
        }
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=self._ctx)
        data = json.loads(resp.read().decode())

        self._token = data.get("token")
        if not self._token:
            raise Exception(f"登录失败，返回未包含 token: {json.dumps(data, ensure_ascii=False)[:200]}")

        with open(self.token_file, "w") as f:
            f.write(self._token)
        return self._token

    def _ensure_token(self):
        if not self._token:
            self.login()

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": BROWSER_HEADERS["User-Agent"],
            "Referer": f"{ORDER_BASE}/members/order-list",
            "Origin": ORDER_BASE,
        }

    def _post(self, endpoint: str, data: dict) -> dict:
        url = f"{ORDER_BASE}{endpoint}"
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers=self._headers(), method="POST"
        )
        try:
            with urllib.request.urlopen(req, context=self._ctx, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise Exception(f"订单API HTTP {e.code}: {err_body[:200]}")

    # ─── 订单列表 ────────────────────────────────────────────────────────

    def list(
        self,
        page: int = 1,
        page_size: int = 100,
        start_time: str = None,
        end_time: str = None,
        order_sn: str = None,
        track_no: str = None,
        consignee: str = None,
        sender: str = None,
        state: int = None,
    ) -> dict:
        """获取订单列表（分页）
        
        返回: {"totalCount": int, "items": [order, ...]}
        """
        self._ensure_token()
        payload = {"pageIndex": page, "pageSize": page_size}
        if start_time:   payload["startTime"] = start_time
        if end_time:     payload["endTime"] = end_time
        if order_sn:     payload["orderSN"] = order_sn
        if track_no:     payload["trackNo"] = track_no
        if consignee:    payload["consigneeName"] = consignee
        if sender:       payload["senderName"] = sender
        if state is not None: payload["state"] = state

        return self._post(ORDER_API["list"], payload)

    def list_all(
        self,
        start_time: str = None,
        end_time: str = None,
        page_size: int = 100,
        progress_cb: Callable = None,
    ) -> List[dict]:
        """分页拉取全部订单
        
        ⚠️ API 的 startTime/endTime 参数可能不生效（服务器忽略）
        策略：分页拉取，本地按日期过滤 + 遇到早于起始时间时提前停止
        """
        self._ensure_token()
        all_orders = []
        page = 1
        total = None
        errors = 0
        max_errors = 10
        early_stop = False

        while not early_stop:
            try:
                data = self.list(
                    page=page, page_size=page_size,
                    start_time=start_time, end_time=end_time,
                )
                items = data.get("items", [])

                filtered = []
                for item in items:
                    created = (item.get("creationTime") or "")[:10]
                    if start_time and created < start_time:
                        early_stop = True
                        break
                    if end_time and created > end_time:
                        continue
                    filtered.append(item)

                all_orders.extend(filtered)

                if total is None:
                    total = data.get("totalCount", 0)

                if progress_cb:
                    progress_cb(len(all_orders), total or 0, page)

                if len(items) < page_size or early_stop:
                    break

                page += 1
                errors = 0
                time.sleep(0.3)

            except Exception as e:
                errors += 1
                if errors >= max_errors:
                    raise RuntimeError(f"连续 {errors} 次错误，停止拉取") from e
                time.sleep(2)

        return all_orders

    # ─── 订单详情 ────────────────────────────────────────────────────────

    def detail(self, sn: str) -> dict:
        """获取单个订单完整详情"""
        self._ensure_token()
        return self._post(ORDER_API["detail"], {"sn": sn})

    def fetch_details(
        self,
        sn_list: List[str],
        batch_size: int = 5,
        progress_cb: Callable = None,
    ) -> Dict[str, dict]:
        """批量获取订单详情"""
        self._ensure_token()
        results = {}
        total = len(sn_list)
        for i, sn in enumerate(sn_list):
            try:
                results[sn] = self.detail(sn)
                if progress_cb:
                    progress_cb(i + 1, total, sn)
                time.sleep(0.3)
            except Exception as e:
                results[sn] = {"error": str(e)}
        return results

    # ─── 辅助 ────────────────────────────────────────────────────────────

    def get_lines(self) -> List[dict]:
        """获取可用线路"""
        self._ensure_token()
        return self._post(ORDER_API["lines"], {})

    def get_wait_pay_count(self) -> int:
        """获取待付款订单数"""
        self._ensure_token()
        data = self._post(ORDER_API["wait_pay"], {})
        return data.get("count", 0)

    @staticmethod
    def state_name(state: int) -> str:
        return ORDER_STATES.get(state, str(state))


# ═══════════════════════════════════════════════════════════════════════════════
# 💰 FinanceClient — 财务数据 (www.heute-express.com)
# ═══════════════════════════════════════════════════════════════════════════════

class FinanceClient:
    """💰 称重补款 / 财务流水 — 共享 OrderClient 的 Token"""

    MONEY_API = "/Prod/api/app/member-center/get-member-money-logs"

    def __init__(self, order_client: OrderClient):
        self._order = order_client

    def fetch_money_logs(
        self, start_date: str, end_date: str, type_val: int = -2,
    ) -> list[dict]:
        """拉取财务流水（自动翻页）"""
        self._order._ensure_token()
        all_items = []
        page = 1
        while True:
            payload = {
                "pageIndex": page,
                "pageSize": 200,
                "startTime": start_date,
                "endTime": end_date,
                "type": type_val,
                "orderSn": None,
            }
            url = f"{ORDER_BASE}{self.MONEY_API}"
            body = json.dumps(payload).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._order._token}",
                "User-Agent": BROWSER_HEADERS["User-Agent"],
                "Referer": f"{ORDER_BASE}/members/member-money-log",
                "Origin": ORDER_BASE,
            }
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, context=self._order._ctx, timeout=REQUEST_TIMEOUT) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace")
                raise Exception(f"财务API HTTP {e.code}: {err_body[:200]}")

            items = data.get("items", [])
            if not items:
                break
            all_items.extend(items)
            total = data.get("totalCount", 0)
            if len(all_items) >= total:
                break
            page += 1
            time.sleep(0.3)

        return all_items

    @staticmethod
    def generate_surcharge_csv(items: list[dict], csv_path: str, month_prefix: str = "2026-05"):
        """从财务流水生成称重补款 CSV"""
        import csv
        month_items = [i for i in items if i.get("creationTime", "").startswith(month_prefix)]
        os.makedirs(os.path.dirname(csv_path), exist_ok=True)
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["时间", "订单号", "金额(元)", "描述"])
            for item in month_items:
                t = (item.get("creationTime") or "")[:19]
                oid = item.get("orderSN", "")
                amt = item.get("moneyChanged", 0) / 100
                desc = item.get("description", "")
                w.writerow([t, oid, f"{amt:.2f}", desc])
        return month_items


# ═══════════════════════════════════════════════════════════════════════════════
# 🏭 HeuteAPI — 统一入口
# ═══════════════════════════════════════════════════════════════════════════════

class HeuteAPI:
    """货易达统一 API 入口
    
    用法:
        api = HeuteAPI()
        
        # 🚚 轨迹
        t = api.track.query("DEUHYD600169132940EU")
        
        # 📦 订单列表
        result = api.order.list(page=1, page_size=100)
        orders = result.get("items", [])
        
        # 📦 订单详情
        detail = api.order.detail("2605150906547151")
    """

    def __init__(
        self,
        track_token_file: str = TRACK_TOKEN_FILE,
        order_token_file: str = ORDER_TOKEN_FILE,
    ):
        self.track = TrackClient(token_file=track_token_file)
        self.order = OrderClient(token_file=order_token_file)
        self.finance = FinanceClient(self.order)

    def login_all(self, force: bool = False):
        """同时登录两个系统"""
        self.track.login(force=force)
        self.order.login(force=force)


# ═══════════════════════════════════════════════════════════════════════════════
# 兼容旧接口
# ═══════════════════════════════════════════════════════════════════════════════

# 保持与 heute_track_api.py 一致的函数签名
track_login = TrackClient().login
track_query = TrackClient().query
track_batch_query = TrackClient().batch_query
track_query_simple = TrackClient().query_simple


# ═══════════════════════════════════════════════════════════════════════════════
# 命令行入口
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    api = HeuteAPI()
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""

    if cmd == "login":
        api.login_all()
        print("✅ 轨迹系统登录成功")
        print("✅ 订单系统登录成功")

    elif cmd == "track" and len(sys.argv) > 2:
        gw = sys.argv[2]
        r = api.track.query(gw)
        if r:
            print(f"📦 {r['trackingNo']}")
            print(f"   状态: {r['currentStatus']}")
            print(f"   国内单号: {r.get('extTrackNoCn', '-')}")
            print(f"   物流公司: {r.get('logisticsCompany', '-')}")
            print(f"   最新时间: {r.get('latestTime', '-')}")
            print(f"   描述: {r.get('latestDesc', '-')}")
        else:
            print(f"❌ 未查到轨迹: {gw}")

    elif cmd == "track-batch" and len(sys.argv) > 2:
        gws = sys.argv[2].split(",")
        print(f"🔍 批量查询 {len(gws)} 条轨迹…")
        results = api.track.batch_query(gws, threads=4)
        ok = sum(1 for r in results.values() if r.get("trackingNo"))
        print(f"✅ 成功 {ok}/{len(gws)}")

    elif cmd == "orders" or cmd == "list":
        page = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        page_size = int(sys.argv[3]) if len(sys.argv) > 3 else 20
        result = api.order.list(page=page, page_size=page_size)
        items = result.get("items", [])
        total = result.get("totalCount", 0)
        print(f"📦 共 {total} 条订单（第{page}页，{len(items)}条）:")
        for o in items:
            state = OrderClient.state_name(o.get("state"))
            print(f"  {o['sn']} | {state} | {o.get('consigneeName','-')} | {o.get('creationTime','')}")

    elif cmd == "detail" and len(sys.argv) > 2:
        sn = sys.argv[2]
        d = api.order.detail(sn)
        print(json.dumps(d, ensure_ascii=False, indent=2)[:3000])

    else:
        print("用法:")
        print("  python3 heute_api.py login                   # 同时登录两个系统")
        print("  python3 heute_api.py track <运单号>          # 查轨迹")
        print("  python3 heute_api.py track-batch gw1,gw2     # 批量查轨迹")
        print("  python3 heute_api.py list [页] [每页条数]    # 订单列表")
        print("  python3 heute_api.py detail <订单号>         # 订单详情")
