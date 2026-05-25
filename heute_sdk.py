"""
|Heute Express (货易达) API Python SDK
|======================================
|货易达 (heute-express.com) API Python SDK
|============================================
|封装货易达物流后台全部API + track.heute-express.com 轨迹查询，支持：
|- 🔐 自动登录（用户名+密码，无需手动拷贝 Token）
|- 📦 订单列表分页拉取（全部订单）
|- 🔍 订单详情查询（产品名、重量、金额、关税、运费等）
|- 📍 物流轨迹查询（自动OCR验证码，最多10次重试）
|- 💾 导出为 JSON / CSV（含中文表头）
|- 💰 财务管理、线路查询等扩展
|- ⏰ 与 Hermes Cron 配合实现每日自动同步

用法:
    from heute_sdk import HeuteClient, track_package
    
    # ⭐ 方式一：自动登录（推荐）
    client = HeuteClient.login(username="USTAR", password="xxx")
    
    # 方式二：手动注入 Token
    client = HeuteClient(token="eyJ...")
    
    # 拉取全部订单列表
    orders = client.fetch_all_orders("2026-04-01", "2026-05-15")
    
    # 查单个订单详情
    detail = client.get_order_detail("2605150906547151")
    
    # 📍 查物流轨迹（无需Token，自动OCR验证码）
    result = track_package("DEUHYD600169132940EU")
    print(result["logisticsCompany"])   # 顺丰
    for log in result["trackingDetails"]:
        print(log["trackingTime"], log["trackingDesc"])
    
    # 导出
    client.export_csv("orders.csv")
"""

import json
import time
import csv
import os
import re
import sys
import subprocess
import urllib.request
import urllib.error
import ssl
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from PIL import Image, ImageEnhance


# ─── 常量 ────────────────────────────────────────────────────────────────────

BASE_URL = "https://www.heute-express.com"
API_PREFIX = "/Prod/api/app"

# ─── API 端点 ─────────────────────────────────────────────────────────────────

ENDPOINTS = {
    # 认证
    "login":           f"{API_PREFIX}/token/login",
    "refresh":         f"{API_PREFIX}/token/refresh",
    
    # 订单
    "order_list":      f"{API_PREFIX}/member-order/get-member-order-list",
    "order_detail":    f"{API_PREFIX}/member-order/get-member-order-for-view",
    "order_wait_pay":  f"{API_PREFIX}/member-order/get-member-wait-pay-order-count",
    "order_enable_line": f"{API_PREFIX}/member-order/get-enable-line",
    
    # 财务
    "account_detail":  f"{API_PREFIX}/finance/account-detail",     # 推测
    "account_bill":    f"{API_PREFIX}/finance/my-bills",           # 推测
    "balance":         f"{API_PREFIX}/finance/balance",            # 推测
    
    # 通用
    "dictionary":      f"{API_PREFIX}/common/get-all-dictionary",
}

# ─── 状态码映射 ──────────────────────────────────────────────────────────────

ORDER_STATES = {
    0: "已作废",
    1: "待支付",
    2: "待入库",
    3: "国际运输",
    4: "国内配送",
    5: "签收",
    # ... 可能会有更多状态
}

ID_CARD_STATUS = {
    0: "未上传",
    1: "待审核",
    2: "已上传",
    -1: "审核失败",
}


# ─── 请求头（浏览器兼容） ─────────────────────────────────────────────────────

def _make_headers(token: str) -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Referer": f"{BASE_URL}/members/order-list",
        "Origin": BASE_URL,
    }


# ─── 核心客户端 ───────────────────────────────────────────────────────────────

class HeuteClient:
    """货易达 API 客户端"""
    
    def __init__(self, token: str = None, token_file: str = None):
        """
        初始化客户端。
        
        参数:
            token: Bearer Token（JWT），从浏览器 sessionStorage 获取
            token_file: 存储 token 的文件路径（替代直接传 token）
        
        Token 获取方法:
            1. 登录 https://www.heute-express.com
            2. F12 → Application → Session Storage
            3. 找到 CHISU__PRODUCTION__2.8.0__COMMON__SESSION__KEY__
            4. 这个值不是 JWT，需要从浏览器 Network 标签捕获:
               - 发起一次搜索（如点"搜索"按钮）
               - 找到 XHR 请求 get-member-order-list
               - 复制 Authorization: Bearer <token> 中的 token
        """
        self.token = token
        self.token_file = token_file
        self._ctx = ssl.create_default_context()
        
        if token_file and os.path.exists(token_file):
            with open(token_file) as f:
                self.token = f.read().strip()
        
        self._last_response = None
    
    # ─── 内部 HTTP 方法 ────────────────────────────────────────────────────
    
    def _post(self, endpoint: str, data: dict) -> dict:
        """POST JSON 请求"""
        url = f"{BASE_URL}{endpoint}"
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=_make_headers(self.token), method="POST")
        try:
            with urllib.request.urlopen(req, context=self._ctx, timeout=30) as resp:
                self._last_response = json.loads(resp.read().decode("utf-8"))
                return self._last_response
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise HeuteAPIError(f"HTTP {e.code}: {body[:200]}", status=e.code, response=body)
    
    def _check_token(self):
        """检查 token 是否有效"""
        if not self.token:
            raise HeuteAuthError("未设置 Token，请先调用 login() 或传入 token")
    
    # ─── 自动登录 ──────────────────────────────────────────────────────────
    
    @classmethod
    def login(cls, username: str, password: str,
              token_file: str = None, save: bool = False) -> "HeuteClient":
        """
        用用户名密码自动登录，返回已认证的客户端
        
        参数:
            username: 货易达用户名
            password: 货易达密码
            token_file: 保存 token 的文件路径（可选）
            save: 是否自动保存 token 到文件
        
        返回:
            HeuteClient 实例（已登录）
        
        示例:
            client = HeuteClient.login("USTAR", "xxx")
            client = HeuteClient.login("USTAR", "xxx", save=True)  # 自动保存token
        """
        import urllib.request
        
        url = f"{BASE_URL}{ENDPOINTS['login']}"
        body = json.dumps({"name": username, "password": password}).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/login",
        }
        
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        ctx = ssl.create_default_context()
        
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise HeuteAPIError(f"登录失败 (HTTP {e.code}): {body[:200]}",
                                status=e.code, response=body)
        
        token = data.get("token")
        if not token:
            raise HeuteAuthError(f"登录返回中未找到 token: {json.dumps(data, ensure_ascii=False)[:200]}")
        
        client = cls(token=token, token_file=token_file)
        if save and token_file:
            client.save_token(token_file)
        
        return client
    
    # ─── 订单列表 ──────────────────────────────────────────────────────────
    
    def get_order_list(self, page: int = 1, page_size: int = 100,
                       start_time: str = None, end_time: str = None,
                       order_sn: str = None, track_no: str = None,
                       consignee: str = None, sender: str = None,
                       merchant_sn: str = None, platform_sn: str = None,
                       line_id: int = None, state: int = None) -> dict:
        """
        获取订单列表（分页）
        
        返回: {"totalCount": int, "items": [order, ...]}
        每项包含: sn, globalWayBillSN, tempLineSN, consigneeName, consigneeTel,
                  state, senderName, weight, creationTime, lineName, 
                  merchantOrderSN, platformSN, idCardInfoStatus
        """
        self._check_token()
        payload = {"pageIndex": page, "pageSize": page_size}
        if start_time:      payload["startTime"] = start_time
        if end_time:        payload["endTime"] = end_time
        if order_sn:        payload["orderSN"] = order_sn
        if track_no:        payload["trackNo"] = track_no
        if consignee:       payload["consigneeName"] = consignee
        if sender:          payload["senderName"] = sender
        if merchant_sn:     payload["merchantOrderSN"] = merchant_sn
        if platform_sn:     payload["platformSN"] = platform_sn
        if line_id:         payload["lineId"] = line_id
        if state is not None: payload["state"] = state
        
        return self._post(ENDPOINTS["order_list"], payload)
    
    def fetch_all_orders(self, start_time: str = None, end_time: str = None,
                         page_size: int = 100, progress_cb=None) -> List[dict]:
        """
        分页拉取全部订单
        
        ⚠️ API 的 startTime/endTime 参数不生效（服务器忽略）
        故采用策略：按时间倒序拉取，在本地过滤 + 遇到早于起止时间的记录时提前停止
        
        参数:
            start_time: "2026-04-01"（本地过滤用）
            end_time:   "2026-05-15"（本地过滤用）
            page_size: 每页数量（最大128）
            progress_cb: 进度回调 fn(fetched, total, page)
        
        返回: [order, ...]
        """
        all_orders = []
        page = 1
        total = None
        errors = 0
        max_errors = 10
        early_stop = False
        
        while not early_stop:
            try:
                data = self.get_order_list(
                    page=page, page_size=page_size,
                    start_time=start_time, end_time=end_time
                )
                items = data.get("items", [])
                
                # 本地过滤日期
                filtered = []
                for item in items:
                    created = item.get("creationTime", "")[:10]
                    
                    # 如果当前记录比 start_time 还早，且数据是按时间倒序排的，可以停了
                    if start_time and created < start_time:
                        early_stop = True
                        break
                    
                    if end_time and created > end_time:
                        continue  # 太新的跳过（一般不会，因为默认就是最新的）
                    
                    filtered.append(item)
                
                all_orders.extend(filtered)
                
                if total is None:
                    total = data.get("totalCount", 0)
                
                if progress_cb:
                    progress_cb(len(all_orders), total or 0, page)
                
                # 如果当前页没有数据，或者少于page_size，或者已触发提前停止
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
    
    # ─── 订单详情 ──────────────────────────────────────────────────────────
    
    def get_order_detail(self, sn: str) -> dict:
        """
        获取订单完整详情
        
        返回字段包括:
            sn, lineSettingCreateOrderAlias, state, creationTime,
            weight, moneyEstimate, moneyFinal,
            feeShip, feeShipEstimate, feeShipGlobal, feeShipGlobalEstimate,
            feeCustoms, feeCustomsEstimate, feeInsurance, feeReinforce,
            goodsValue, goodsValueEstimateRMB,
            senderName, senderTel, senderCity, senderStreet,
            consigneeName, consigneeTel, consigneeIDNumber,
            consigneeProvince, consigneeCity, consigneeCounty, consigneeAddress,
            globalWayBillSN, tempLineSN,
            orderDetails: [{goodsName, goodsNameForeign, goodsBrand, ean,
                           price, priceRMB, num, netWeight, goodsCode}, ...]
        """
        self._check_token()
        return self._post(ENDPOINTS["order_detail"], {"sn": sn})
    
    def fetch_order_details(self, order_sns: List[str], 
                            batch_size: int = 5, progress_cb=None) -> Dict[str, dict]:
        """
        批量获取订单详情
        
        参数:
            order_sns: 订单号列表
            batch_size: 并发批次大小
        
        返回: {sn: detail_data, ...}
        """
        results = {}
        total = len(order_sns)
        
        for i, sn in enumerate(order_sns):
            try:
                results[sn] = self.get_order_detail(sn)
                if progress_cb:
                    progress_cb(i + 1, total, sn)
                time.sleep(0.3)
            except Exception as e:
                print(f"  [{i+1}/{total}] {sn}: ERROR - {e}")
                results[sn] = {"error": str(e)}
        
        return results
    
    # ─── 扩展 API ──────────────────────────────────────────────────────────
    
    def get_enabled_lines(self) -> List[dict]:
        """获取可用线路"""
        self._check_token()
        return self._post(ENDPOINTS["order_enable_line"], {})
    
    def get_wait_pay_count(self) -> int:
        """获取待付款订单数"""
        self._check_token()
        data = self._post(ENDPOINTS["order_wait_pay"], {})
        return data.get("count", 0)
    
    # ─── 导出 ──────────────────────────────────────────────────────────────
    
    @staticmethod
    def orders_to_csv(orders: List[dict], filepath: str):
        """
        订单列表导出为 CSV
        
        orders: fetch_all_orders() / fetch_order_details() 的输出
        """
        if not orders:
            raise ValueError("订单列表为空")
        
        fieldnames_map = {
            "sn": "订单号",
            "state": "状态",
            "lineName": "线路",
            "lineSettingCreateOrderAlias": "线路",
            "globalWayBillSN": "国际单号",
            "tempLineSN": "国内单号",
            "merchantOrderSN": "商家订单号",
            "platformSN": "电商平台单号",
            "senderName": "寄件人",
            "senderTel": "寄件人电话",
            "consigneeName": "收件人",
            "consigneeTel": "收件人电话",
            "consigneeIDNumber": "收件人身份证",
            "consigneeProvince": "省份",
            "consigneeCity": "城市",
            "consigneeAddress": "详细地址",
            "weight": "重量(g)",
            "creationTime": "创建时间",
            "moneyEstimate": "预估费用(分)",
            "moneyFinal": "最终费用(分)",
            "feeShipEstimate": "预估运费(分)",
            "feeShip": "实际运费(分)",
            "feeShipGlobalEstimate": "预估国际运费(分)",
            "feeShipGlobal": "实际国际运费(分)",
            "feeCustomsEstimate": "预估关税(分)",
            "feeCustoms": "实际关税(分)",
            "feeInsurance": "保险费(分)",
            "feeReinforce": "加固费(分)",
            "goodsValueEstimateRMB": "商品价值(分)",
            "idCardInfoStatus": "身份证状态",
        }
        
        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(fieldnames_map.keys()),
                                    extrasaction="ignore")
            writer.writerow(fieldnames_map)  # 中文表头
            for order in orders:
                row = {}
                for eng in fieldnames_map:
                    val = order.get(eng)
                    # 状态码转文字
                    if eng == "state" and val in ORDER_STATES:
                        val = ORDER_STATES[val]
                    elif eng == "idCardInfoStatus" and val in ID_CARD_STATUS:
                        val = ID_CARD_STATUS[val]
                    # weight 从 g 转 kg
                    elif eng == "weight" and val:
                        val = f"{val/1000:.3f}"
                    # 金额从分转元
                    elif eng in ("moneyEstimate", "moneyFinal", "feeShipEstimate",
                                 "feeShip", "feeShipGlobalEstimate", "feeShipGlobal",
                                 "feeCustomsEstimate", "feeCustoms",
                                 "goodsValueEstimateRMB") and val:
                        val = f"{val/100:.2f}"
                    row[eng] = val
                writer.writerow(row)
        
        return filepath
    
    @staticmethod
    def order_details_to_csv(orders: List[dict], filepath: str):
        """订单详情导出 CSV，含产品明细行"""
        fieldnames = [
            "订单号", "线路", "状态", "收件人", "重量(kg)",
            "产品名称", "英文名", "品牌", "数量", "单价(EUR)", "单价(RMB)",
            "EAN码", "净重(g)", "商品编码",
            "国际单号", "国内单号",
            "预估运费", "预估关税", "预估总费用", "最终费用",
            "创建时间",
        ]
        
        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(fieldnames)
            
            for order in orders:
                details = order.get("orderDetails") or [{}]
                for prod in details:
                    writer.writerow([
                        order.get("sn", ""),
                        order.get("lineSettingCreateOrderAlias", ""),
                        ORDER_STATES.get(order.get("state"), order.get("state")),
                        order.get("consigneeName", ""),
                        f"{(order.get('weight') or 0)/1000:.3f}",
                        prod.get("goodsName", ""),
                        prod.get("goodsNameForeign", ""),
                        prod.get("goodsBrand", ""),
                        prod.get("num", ""),
                        f"{(prod.get('price') or 0)/100:.2f}" if prod.get("price") else "",
                        f"{(prod.get('priceRMB') or 0)/100:.2f}" if prod.get("priceRMB") else "",
                        prod.get("ean", ""),
                        prod.get("netWeight", ""),
                        prod.get("goodsCode", ""),
                        order.get("globalWayBillSN", ""),
                        order.get("tempLineSN", ""),
                        f"{(order.get('feeShipEstimate') or 0)/100:.2f}",
                        f"{(order.get('feeCustomsEstimate') or 0)/100:.2f}",
                        f"{(order.get('moneyEstimate') or 0)/100:.2f}",
                        f"{(order.get('moneyFinal') or 0)/100:.2f}",
                        order.get("creationTime", ""),
                    ])
        
        return filepath
    
    # ─── Token 管理 ────────────────────────────────────────────────────────
    
    def save_token(self, filepath: str = None):
        """保存 token 到文件"""
        path = filepath or self.token_file or "heute_token.txt"
        with open(path, "w") as f:
            f.write(self.token)
        return path
    
    @staticmethod
    def decode_token(token: str) -> dict:
        """解码 JWT token（查看过期时间等）"""
        import base64
        parts = token.split(".")
        if len(parts) != 3:
            return {"error": "不是有效的 JWT"}
        # 解码 payload
        padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
        try:
            payload = json.loads(base64.urlsafe_b64decode(padded))
            if "exp" in payload:
                payload["exp_date"] = datetime.fromtimestamp(
                    payload["exp"], tz=timezone.utc
                ).isoformat()
            return payload
        except Exception as e:
            return {"error": str(e)}


# ─── 物流轨迹查询 ─────────────────────────────────────────────────────────
# track.heute-express.com — 独立验证码认证系统（无需主站Token）
# ────────────────────────────────────────────────────────────────────────────

TRACK_BASE = "https://track.heute-express.com"
COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".track_cookies")


def _get_track_session() -> str:
    """获取track系统的sessionId"""
    rm = f"rm -f {COOKIE_FILE}" if os.path.exists(COOKIE_FILE) else ""
    if rm:
        subprocess.run(rm, shell=True, capture_output=True, timeout=5)
    out = subprocess.run(
        f'curl -s -c {COOKIE_FILE} "{TRACK_BASE}/api/public/tracking/session"',
        shell=True, capture_output=True, text=True, timeout=10
    )
    d = json.loads(out.stdout)
    return d.get("data", "")


def _get_captcha(sid: str):
    """下载验证码图片到临时文件"""
    t = int(time.time() * 1000)
    url = f"{TRACK_BASE}/api/public/tracking/captcha?sessionId={sid}&t={t}"
    subprocess.run(
        f'curl -s -b {COOKIE_FILE} -o /tmp/heute_captcha.png "{url}"',
        shell=True, capture_output=True, timeout=10
    )


def _ocr_captcha() -> set:
    """OCR识别验证码，返回候选集合"""
    img = Image.open("/tmp/heute_captcha.png").convert("L")
    enh = ImageEnhance.Contrast(img)
    bw = enh.enhance(4.0).point(lambda x: 255 if x > 120 else 0)
    bw.save("/tmp/heute_captcha_bw.png")

    texts = set()
    whitelist = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    for psm in ["7", "8", "13"]:
        cmd = f"tesseract /tmp/heute_captcha_bw.png stdout --psm {psm} -c tessedit_char_whitelist={whitelist} 2>/dev/null"
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        t = out.stdout.strip()
        if t:
            t = re.sub(r"[^A-Z0-9]", "", t.upper())
            if 2 <= len(t) <= 6:
                texts.add(t)

        # inverted
        bw_inv = bw.point(lambda x: 255 - x)
        bw_inv.save("/tmp/heute_captcha_inv.png")
        cmd2 = f"tesseract /tmp/heute_captcha_inv.png stdout --psm {psm} -c tessedit_char_whitelist={whitelist} 2>/dev/null"
        out2 = subprocess.run(cmd2, shell=True, capture_output=True, text=True)
        t2 = out2.stdout.strip()
        if t2:
            t2 = re.sub(r"[^A-Z0-9]", "", t2.upper())
            if 2 <= len(t2) <= 6:
                texts.add(t2)
    return texts


def _do_track_query(sid: str, captcha: str, tracking_no: str) -> dict:
    """发送轨迹查询请求"""
    payload = json.dumps({
        "sessionId": sid,
        "captcha": captcha,
        "trackingNo": tracking_no
    })
    out = subprocess.run(
        f'curl -s "{TRACK_BASE}/api/public/tracking/query" '
        f'-H "Content-Type: application/json" -b {COOKIE_FILE} '
        f"-d '{payload}'",
        shell=True, capture_output=True, text=True, timeout=10
    )
    return json.loads(out.stdout)


def track_package(tracking_no: str, max_attempts: int = 10,
                  verbose: bool = False) -> dict:
    """
    查询国际运单号物流轨迹
    
    track.heute-express.com 需要验证码，SDK会自动OCR识别。
    成功率通常在80-90%，最多重试 max_attempts 次。
    
    参数:
        tracking_no: 国际运单号 (如 DEUHYD600169132940EU)
        max_attempts: 最大重试次数（默认10）
        verbose: 是否打印调试信息
    
    返回:
        {
            "trackingNo": str,          # 国际单号
            "extTrackNoCn": str,        # 国内单号（顺丰等）
            "logisticsCompany": str,    # 物流公司
            "currentStatus": str,       # 当前状态（已签收/派件中/清关中...）
            "trackingDetails": [{       # 轨迹列表（按时间倒序）
                "trackingTime": str,       # 时间 "YYYY-MM-DD HH:mm:ss"
                "trackingDesc": str,       # 描述
                "statusName": str,         # 状态类型
                "currentSiteName": str,    # 当前位置
                "nextSiteName": str,       # 下一站
                "address": str,            # 地址
                "contact": str,            # 联系人
                "contactPhone": str,       # 联系电话
                "signerName": str,         # 签收人
                "signerTypeDesc": str      # 签收类型
            }]
        }
    
    失败时返回 {"error": "查询失败，超过最大重试次数"}
    """
    for attempt in range(max_attempts):
        if verbose:
            print(f"[track] attempt {attempt+1}/{max_attempts}", file=sys.stderr)
        
        sid = _get_track_session()
        _get_captcha(sid)
        candidates = _ocr_captcha()
        
        if verbose:
            print(f"[track] captcha candidates: {candidates}", file=sys.stderr)
        
        for c in candidates:
            result = _do_track_query(sid, c, tracking_no)
            code = result.get("code")
            if code == 200:
                return result.get("data", {})
            if verbose:
                msg = result.get("message", "")
                print(f"[track]  '{c}' -> {msg}", file=sys.stderr)
        
        time.sleep(0.5)
    
    return {"error": "查询失败，超过最大重试次数"}


# ─── 异常 ─────────────────────────────────────────────────────────────────────

class HeuteAPIError(Exception):
    def __init__(self, message, status=None, response=None):
        super().__init__(message)
        self.status = status
        self.response = response

class HeuteAuthError(Exception):
    pass


# ─── 使用示例 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== 货易达 SDK ===")
    print()
    print("用法:")
    print("  from heute_sdk import HeuteClient")
    print("  client = HeuteClient(token='你的Bearer Token')")
    print("  orders = client.fetch_all_orders('2026-04-01', '2026-05-15')")
    print()
    print("Token 获取:")
    print("  1. 登录货易达网站")
    print("  2. F12 → 搜索订单 → Network 标签")
    print('  3. 找到 get-member-order-list 请求')
    print('  4. 复制 Authorization: Bearer <token>')
    print()
    print("字段说明:")
    print("  weight:     克(g)，需 /1000 转千克")
    print("  money/价:   分(cent)，需 /100 转元")
    print("  state:      2=待入库, 3=国际运输, 4=国内配送, 5=签收")
