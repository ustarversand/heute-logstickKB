#!/usr/bin/env python3
"""物流看板 — 身份证上传模块（通过直邮管家代理上传到CCS认证系统）"""
import os, json, uuid, io, logging, urllib.request, urllib.parse
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import policy

logger = logging.getLogger("idcard-upload")

ZYGJ_URL = "http://192.168.178.26:8899"
ZYGJ_ADMIN = "admin"
ZYGJ_PASS = "Hilden11031980"
UPLOAD_DIR = "/tmp/idcard_uploads"


def _build_multipart(fields: dict, files: dict) -> tuple:
    """构建multipart/form-data请求体，返回(body_bytes, content_type)"""
    boundary = f"----WebKitFormBoundary{uuid.uuid4().hex[:16]}"
    body_parts = []

    # 文本字段
    for key, value in fields.items():
        body_parts.append(f"--{boundary}\r\n")
        body_parts.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n')
        body_parts.append(f"{value}\r\n")

    # 文件字段
    for key, (filename, file_data) in files.items():
        body_parts.append(f"--{boundary}\r\n")
        body_parts.append(f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n')
        body_parts.append("Content-Type: image/jpeg\r\n\r\n")
        body_parts.append(file_data)
        body_parts.append(b"\r\n")

    body_parts.append(f"--{boundary}--\r\n")

    # 混合bytes和str
    body_bytes = b""
    for part in body_parts:
        if isinstance(part, str):
            body_bytes += part.encode("utf-8")
        else:
            body_bytes += part

    content_type = f"multipart/form-data; boundary={boundary}"
    return body_bytes, content_type


def _do_request(method, url, data=None, headers=None, content_type=None):
    """通用HTTP请求"""
    if headers is None:
        headers = {}
    if data is not None:
        if isinstance(data, bytes):
            if content_type:
                headers["Content-Type"] = content_type
            elif not headers.get("Content-Type"):
                headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif isinstance(data, dict):
            data = urllib.parse.urlencode(data).encode("utf-8")
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            cookies = resp.headers.get_all("Set-Cookie") if hasattr(resp.headers, "get_all") else []
            if not cookies:
                # Python 3.6+方式
                cookies = resp.headers.get_all("Set-Cookie") if hasattr(resp.headers, "get_all") else []
                if not cookies:
                    # 手动解析
                    set_cookie = resp.headers.get("Set-Cookie")
                    if set_cookie:
                        cookies = [set_cookie]
            return {
                "status": resp.status,
                "body": json.loads(body) if body else {},
                "cookies": cookies,
            }
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error(f"HTTP {e.code}: {body[:200]}")
        cookies = e.headers.get_all("Set-Cookie") if hasattr(e.headers, "get_all") else []
        if not cookies:
            set_cookie = e.headers.get("Set-Cookie")
            if set_cookie:
                cookies = [set_cookie]
        return {"status": e.code, "body": body, "cookies": cookies, "error": body}
    except urllib.error.URLError as e:
        logger.error(f"连接失败: {e}")
        return {"status": 0, "body": {"success": False, "msg": f"连接失败: {e.reason}"}, "error": str(e)}


def _parse_cookies(cookies_list):
    """从Set-Cookie列表提取session cookie"""
    session_cookie = ""
    for cookie_str in cookies_list:
        if cookie_str:
            parts = cookie_str.split(";")[0].strip()
            if parts.startswith("session="):
                session_cookie = parts
            elif parts:
                if not session_cookie:
                    session_cookie = parts
    return session_cookie


def upload(name: str, id_number: str, front_data: bytes, reverse_data: bytes) -> dict:
    """上传身份证正反面到认证系统（通过直邮管家中转）

    Args:
        name: 收件人姓名
        id_number: 身份证号
        front_data: 身份证正面图片二进制数据
        reverse_data: 身份证反面图片二进制数据

    Returns:
        {"success": True/False, "msg": "..."}
    """
    # Step 1: 登录直邮管家
    logger.info(f"[upload] 登录直邮管家 admin@{ZYGJ_URL}")
    login_data = json.dumps({"username": ZYGJ_ADMIN, "password": ZYGJ_PASS}).encode("utf-8")
    login_result = _do_request("POST", f"{ZYGJ_URL}/api/login",
                                data=login_data,
                                headers={"Content-Type": "application/json"})

    if login_result.get("status") != 200:
        err = login_result.get("body", {})
        if isinstance(err, dict):
            msg = err.get("msg", str(err))
        else:
            msg = str(err)
        return {"success": False, "msg": f"直邮管家登录失败: {msg}"}

    # Step 2: 提取session cookie
    session_cookie = _parse_cookies(login_result.get("cookies", []))
    if not session_cookie:
        # 尝试从JSON响应中找session
        login_body = login_result.get("body", {})
        if isinstance(login_body, dict) and login_body.get("success"):
            # 可能session在cookie中设置了但我们没抓到，尝试直接发请求
            session_cookie = "session=auto"
        else:
            return {"success": False, "msg": "直邮管家登录成功但未获取到session"}

    logger.info(f"[upload] 登录成功, cookie={session_cookie[:30]}...")

    # Step 3: 构建multipart上传
    fields = {
        "name": name,
        "id_number": id_number,
    }
    files = {
        "front": (f"{id_number}_front.jpg", front_data),
        "reverse": (f"{id_number}_reverse.jpg", reverse_data),
    }

    body_bytes, content_type = _build_multipart(fields, files)

    # Step 4: 上传到直邮管家
    headers = {
        "Cookie": session_cookie,
    }
    upload_result = _do_request("POST", f"{ZYGJ_URL}/api/idcard/upload",
                                 data=body_bytes,
                                 headers=headers,
                                 content_type=content_type)

    logger.info(f"[upload] 上传结果: status={upload_result.get('status')}")

    if upload_result.get("status") not in (200, 201):
        body = upload_result.get("body", "")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except:
                pass
        if isinstance(body, dict):
            msg = body.get("msg", str(body))
        else:
            msg = str(body)
        return {"success": False, "msg": f"上传失败: {msg}"}

    result_body = upload_result.get("body", {})
    if isinstance(result_body, str):
        try:
            result_body = json.loads(result_body)
        except:
            pass

    if isinstance(result_body, dict) and result_body.get("success"):
        # 上传成功后，通知清关看板更新认证状态
        try:
            verify_url = "http://192.168.178.26:18995/api/idcard/mark-verified"
            req = urllib.request.Request(
                f"{verify_url}?name={urllib.parse.quote(name)}&id_number={urllib.parse.quote(id_number)}",
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                verify_result = json.loads(resp.read().decode())
                if verify_result.get("verified"):
                    logger.info(f"[upload] 清关看板已更新认证状态: {id_number}")
                else:
                    logger.info(f"[upload] 清关看板: CCS尚未确认 {id_number}")
        except Exception as e:
            logger.warning(f"[upload] 通知清关看板失败 (不影响上传): {e}")
        return {"success": True, "msg": result_body.get("msg", "上传成功")}
    elif isinstance(result_body, dict):
        return {"success": False, "msg": result_body.get("msg", "上传返回异常")}
    else:
        return {"success": True, "msg": "上传完成"}
