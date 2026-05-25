#!/usr/bin/env python3
"""
货易达订单同步 + 卖家备注自动回写脚本
=======================================
功能:
  1. sync      — 从货易达拉取订单 → 存入 SQLite
  2. write-remarks — 从 SQLite 读取 → 自动写卖家备注到聚水潭
  3. stats     — 查看同步统计

双账号:
  --profile default  → USTAR / lebenswelle (默认)
  --profile mscj     → MSCJ

用法:
  # 同步最近3天
  python3 heute_express_sync.py sync

  # 同步全部历史
  python3 heute_express_sync.py sync --full

  # 同步指定范围
  python3 heute_express_sync.py sync --start 2026-01-01 --end 2026-05-15

  # 写卖家备注（只写未写的）
  python3 heute_express_sync.py write-remarks

  # 强制重写全部
  python3 heute_express_sync.py write-remarks --force

  # 测试用（只写前10条）
  python3 heute_express_sync.py write-remarks --limit 10

  # MSCJ 账号
  python3 heute_express_sync.py --profile mscj sync
  python3 heute_express_sync.py --profile mscj write-remarks

  # 查看统计
  python3 heute_express_sync.py stats
"""

import sys
import os
import json
import time
import sqlite3
import hashlib
import argparse
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple

# ─── 路径 ────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

# ─── 日志 ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("heute_sync")

# ─── 账号配置（支持环境变量覆盖）────────────────────────────────────────────
PROFILES = {
    "default": {
        "username": os.environ.get('HEUTE_USERNAME', "USTAR"),
        "password": os.environ.get('HEUTE_PASSWORD', "Hilden11031980!"),
        "db_name": "heute_express_ustar.db",
        "sender": None,
    },
    "mscj": {
        "username": os.environ.get('MSCJ_USERNAME', "MSCJ"),
        "password": os.environ.get('MSCJ_PASSWORD', "Mt123456789!"),
        "db_name": "heute_express_mscj.db",
        "sender": None,
    },
}

# 快递公司映射（商业单号前缀 → 名称）
COURIER_MAP = {
    "JDV": "京东快递",
    "JD": "京东快递",
    "SF": "顺丰速运",
}


def get_courier(biz_no: str) -> str:
    """根据商业单号前缀判断快递公司"""
    if not biz_no:
        return "国内快递"
    for prefix, name in COURIER_MAP.items():
        if biz_no.upper().startswith(prefix):
            return name
    return "国内快递"


def get_db_path(profile: str) -> str:
    """获取数据库路径"""
    db_name = PROFILES.get(profile, PROFILES["default"])["db_name"]
    return os.path.join(SCRIPT_DIR, "data", db_name)


# ═══════════════════════════════════════════════════════════════════════════
# 数据库操作
# ═══════════════════════════════════════════════════════════════════════════

def init_db(db_path: str):
    """初始化数据库表结构"""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            sn TEXT PRIMARY KEY,
            global_waybill TEXT,
            temp_line_sn TEXT,
            consignee_name TEXT,
            consignee_tel TEXT,
            state INTEGER,
            sender_name TEXT,
            weight REAL,
            creation_time TEXT,
            line_name TEXT,
            line_id INTEGER,
            merchant_order_sn TEXT,
            platform_sn TEXT,
            idcard_info_status INTEGER,
            seller_remark TEXT,        -- 已写入的卖家备注内容（空=未写）
            seller_remark_written_at TEXT,  -- 写入时间
            synced_at TEXT             -- 同步时间
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_platform ON orders(platform_sn)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_merchant ON orders(merchant_order_sn)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_remark ON orders(seller_remark)")
    conn.commit()
    conn.close()


def upsert_orders(db_path: str, orders: List[dict]):
    """批量插入/更新订单"""
    conn = sqlite3.connect(db_path)
    now = datetime.now(timezone.utc).isoformat()

    for o in orders:
        sn = o.get("sn", "")
        if not sn:
            continue

        # 检查是否已存在 → 保留已有的 seller_remark
        existing = conn.execute(
            "SELECT seller_remark FROM orders WHERE sn=?", (sn,)
        ).fetchone()
        existing_remark = existing[0] if existing and existing[0] else None

        conn.execute("""
            INSERT OR REPLACE INTO orders
            (sn, global_waybill, temp_line_sn, consignee_name, consignee_tel,
             state, sender_name, weight, creation_time, line_name, line_id,
             merchant_order_sn, platform_sn, idcard_info_status,
             seller_remark, seller_remark_written_at, synced_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            sn,
            o.get("globalWayBillSN", ""),
            o.get("tempLineSN", ""),
            o.get("consigneeName", ""),
            o.get("consigneeTel", ""),
            o.get("state", 0),
            o.get("senderName", ""),
            o.get("weight", 0),
            o.get("creationTime", ""),
            o.get("lineName", ""),
            o.get("lineId", 0),
            o.get("merchantOrderSN", ""),
            o.get("platformSN", ""),
            o.get("idCardInfoStatus", 0),
            existing_remark,  # 保留已有备注
            None if not existing_remark else None,
            now,
        ))

    conn.commit()
    conn.close()


def get_month_label(creation_time: str) -> str:
    """从创建时间提取月份标签 (may/jun 等)"""
    m = creation_time[:7] if len(creation_time) >= 7 else ""
    month_map = {
        "2026-04": "apr", "2026-05": "may", "2026-06": "jun",
        "2026-07": "jul", "2026-08": "aug", "2026-09": "sep",
        "2026-10": "oct", "2026-11": "nov", "2026-12": "dec",
        "2027-01": "jan", "2027-02": "feb", "2027-03": "mar",
    }
    return month_map.get(m, m.replace("-", "") if m else "may")


def dashboard_upsert_orders(db_path: str, orders: List[dict]):
    """写入看板数据库 (heute.db) — 带 month 字段，按 creation_time 自动标记月份"""
    import sqlite3
    conn = sqlite3.connect(db_path)
    # 确保表存在（heute_db.py 的 schema，但只建 orders 和 tracking）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            sn          TEXT NOT NULL,
            month       TEXT NOT NULL DEFAULT 'may',
            global_waybill_sn TEXT DEFAULT '',
            temp_line_sn      TEXT DEFAULT '',
            consignee_name    TEXT DEFAULT '',
            consignee_tel     TEXT DEFAULT '',
            sender_name       TEXT DEFAULT '',
            state       INTEGER DEFAULT 0,
            weight      REAL DEFAULT 0,
            creation_time     TEXT DEFAULT '',
            line_name   TEXT DEFAULT '',
            line_id     INTEGER DEFAULT 0,
            merchant_order_sn  TEXT DEFAULT '',
            platform_sn       TEXT DEFAULT '',
            id_card_info_status INTEGER DEFAULT 0,
            PRIMARY KEY (sn, month)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tracking (
            tracking_no     TEXT NOT NULL,
            month           TEXT NOT NULL DEFAULT 'may',
            current_status  TEXT DEFAULT '',
            latest_desc     TEXT DEFAULT '',
            latest_time     TEXT DEFAULT '',
            ext_track_no_cn TEXT DEFAULT '',
            tracking_json   TEXT DEFAULT '[]',
            order_json      TEXT DEFAULT '{}',
            source          TEXT DEFAULT '',
            queried_at      TEXT DEFAULT '',
            PRIMARY KEY (tracking_no, month)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_month ON orders(month)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_sender ON orders(sender_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_gw ON orders(global_waybill_sn)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tracking_month ON tracking(month)")
    conn.commit()

    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    for o in orders:
        sn = o.get("sn", "")
        if not sn:
            continue
        ct = o.get("creationTime", "")
        month = get_month_label(ct)
        conn.execute("""
            INSERT OR REPLACE INTO orders
            (sn, month, global_waybill_sn, temp_line_sn,
             consignee_name, consignee_tel, state, sender_name,
             weight, creation_time, line_name, line_id,
             merchant_order_sn, platform_sn, id_card_info_status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            sn, month,
            o.get("globalWayBillSN", ""),
            o.get("tempLineSN", ""),
            o.get("consigneeName", ""),
            o.get("consigneeTel", ""),
            o.get("state", 0),
            o.get("senderName", ""),
            o.get("weight", 0),
            ct,
            o.get("lineName", ""),
            o.get("lineId", 0),
            o.get("merchantOrderSN", ""),
            o.get("platformSN", ""),
            o.get("idCardInfoStatus", 0),
        ))
        inserted += 1
    conn.commit()
    conn.close()
    logger.info(f"💾 看板数据库写入完成: {inserted} 条 → {db_path}")


def get_unwritten_orders(db_path: str, limit: Optional[int] = None,
                         force: bool = False) -> List[dict]:
    """获取待写入卖家备注的订单"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    if force:
        query = "SELECT * FROM orders WHERE merchant_order_sn IS NOT NULL AND merchant_order_sn != ''"
    else:
        query = ("SELECT * FROM orders WHERE merchant_order_sn IS NOT NULL "
                 "AND merchant_order_sn != '' "
                 "AND (seller_remark IS NULL OR seller_remark = '')")

    query += " ORDER BY creation_time DESC"

    if limit:
        query += f" LIMIT {limit}"

    rows = conn.execute(query).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_remark_written(db_path: str, sn: str, remark: str):
    """标记订单的卖家备注已写入"""
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE orders SET seller_remark=?, seller_remark_written_at=? WHERE sn=?",
        (remark, now, sn),
    )
    conn.commit()
    conn.close()


def get_stats(db_path: str) -> dict:
    """获取数据库统计"""
    conn = sqlite3.connect(db_path)
    total = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    written = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE seller_remark IS NOT NULL AND seller_remark != ''"
    ).fetchone()[0]
    unwritten = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE seller_remark IS NULL OR seller_remark = ''"
    ).fetchone()[0]
    senders = conn.execute(
        "SELECT sender_name, COUNT(*) as cnt FROM orders GROUP BY sender_name ORDER BY cnt DESC"
    ).fetchall()
    state_dist = conn.execute(
        "SELECT state, COUNT(*) as cnt FROM orders GROUP BY state ORDER BY cnt DESC"
    ).fetchall()
    conn.close()

    return {
        "total": total,
        "written": written,
        "unwritten": unwritten,
        "senders": senders,
        "states": state_dist,
    }


# ═══════════════════════════════════════════════════════════════════════════
# 聚水潭 API（内联，避免跨项目依赖）
# ═══════════════════════════════════════════════════════════════════════════

JST_CONFIG = {
    "app_key": "d561deb348274f1ba3505ec4578870fd",
    "app_secret": "84ad2c023b9b49378b1161ea569e383c",
    "api_url_prod": "https://open.erp321.com/api/open/query.aspx",
}
JST_TOKEN = "cfda23ff97664494bc6fc5ab46f8ea48"


def _get_timestamp() -> str:
    return str(int(time.time()))


def _generate_sign(method: str, params: dict) -> str:
    """生成聚水潭 API 签名"""
    partnerid = JST_CONFIG["app_key"]
    partnerkey = JST_CONFIG["app_secret"]
    param_str = "".join(str(k) + str(v) for k, v in sorted(params.items()))
    raw = method + partnerid + param_str + partnerkey
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def update_order_remark(o_id: str, remark: str) -> dict:
    """
    写入聚水潭卖家备注

    API: jushuitan.order.remark.upload
    """
    import requests

    ts = _get_timestamp()
    method = "jushuitan.order.remark.upload"
    sys_params = {"token": JST_TOKEN, "ts": ts}
    sign = _generate_sign(method, sys_params)

    url = (f"{JST_CONFIG['api_url_prod']}?method={method}"
           f"&partnerid={JST_CONFIG['app_key']}&token={JST_TOKEN}&ts={ts}&sign={sign}")

    data = {"o_id": o_id, "remark": remark}
    headers = {"Content-Type": "application/json; charset=utf-8"}

    try:
        resp = requests.post(
            url, data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
            headers=headers, timeout=30
        )
        return resp.json()
    except Exception as e:
        return {"code": -1, "msg": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# 卖家备注写入逻辑
# ═══════════════════════════════════════════════════════════════════════════

def extract_o_id(merchant_order_sn: str) -> str:
    """从 merchantOrderSN 提取聚水潭 o_id

    格式: {shop_id}-{o_id}-{suffix}
    例: 18442196-446413-WF → 446413
    """
    if not merchant_order_sn or "-" not in merchant_order_sn:
        return ""
    parts = merchant_order_sn.split("-")
    return parts[1] if len(parts) >= 2 else ""


def build_remark(order: dict) -> str:
    """构建单条卖家备注（普通格式）"""
    biz_no = order.get("temp_line_sn") or order.get("tempLineSN", "")
    tracking = order.get("global_waybill") or order.get("globalWayBillSN", "")
    name = order.get("consignee_name") or order.get("consigneeName", "")
    courier = get_courier(biz_no)

    return (f"{biz_no}-国内快递同为掌上海关分单运号（{courier}） + "
            f"{tracking}-德国货易达 + {name}")


def build_merged_remark(orders: List[dict], common_name: str) -> str:
    """构建合并卖家备注（拆单用）

    格式:
    {biz1}+{biz2}国内快递同为掌上海关分单运号（{快递公司}）
    {track1}+{track2}-德国货易达
    总收件人：{name}
    """
    biz_list = []
    track_list = []
    for o in orders:
        biz = o.get("temp_line_sn") or o.get("tempLineSN", "")
        track = o.get("global_waybill") or o.get("globalWayBillSN", "")
        if biz:
            biz_list.append(biz)
        if track:
            track_list.append(track)

    courier = get_courier(biz_list[0] if biz_list else "")

    return (f"{'+'.join(biz_list)}国内快递同为掌上海关分单运号（{courier}）\n"
            f"{'+'.join(track_list)}-德国货易达\n"
            f"总收件人：{common_name}")


def write_remarks(db_path: str, force: bool = False, limit: Optional[int] = None):
    """
    核心：从数据库读订单 → 分组 → 构建备注 → 写入聚水潭
    """
    orders = get_unwritten_orders(db_path, limit=limit, force=force)
    total = len(orders)
    if total == 0:
        logger.info("✅ 没有待写入的订单")
        return

    logger.info(f"📦 从数据库读取 {total} 条待写入订单")

    # ── 按 platform_sn 分组 ──
    groups: Dict[str, List[dict]] = {}
    for o in orders:
        plat = o.get("platform_sn") or f"_no_plat_{o.get('sn', '')}"
        groups.setdefault(plat, []).append(o)

    logger.info(f"🔀 共 {len(groups)} 个平台单号组")

    # ── 构建写入队列 ──
    write_queue: List[Tuple[str, str, str]] = []  # (sn, o_id, remark)

    for plat, group in groups.items():
        names = set(
            o.get("consignee_name") or o.get("consigneeName", "")
            for o in group if o.get("consignee_name") or o.get("consigneeName")
        )
        is_split = len(group) > 1 and len(names) == 1

        if is_split:
            # 拆单 → 合并格式
            common_name = list(names)[0]
            merged = build_merged_remark(group, common_name)
            for o in group:
                o_id = extract_o_id(o.get("merchant_order_sn", ""))
                if o_id:
                    write_queue.append((o["sn"], o_id, merged))
        else:
            # 普通单 → 独立格式
            for o in group:
                o_id = extract_o_id(o.get("merchant_order_sn", ""))
                if o_id:
                    remark = build_remark(o)
                    write_queue.append((o["sn"], o_id, remark))

    logger.info(f"✏️  共 {len(write_queue)} 条待写入聚水潭（{total} 订单合并后）")

    if len(write_queue) == 0:
        logger.warning("⚠️  没有可提取 o_id 的订单（merchant_order_sn 格式异常）")
        return

    # ── 执行写入 ──
    success = 0
    failed = 0
    batch_start = time.time()

    for i, (sn, o_id, remark) in enumerate(write_queue):
        result = update_order_remark(o_id, remark)
        code = result.get("code", -1) if isinstance(result, dict) else -1

        if code == 0:
            success += 1
            mark_remark_written(db_path, sn, remark)
        else:
            failed += 1
            msg = result.get("msg", str(result)) if isinstance(result, dict) else str(result)
            if failed <= 5:  # 只打印前5个失败
                logger.warning(f"  ✗ #{i+1} o_id={o_id}: {msg[:100]}")

        # 进度
        if (i + 1) % 50 == 0 or (i + 1) == len(write_queue):
            elapsed = time.time() - batch_start
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            logger.info(f"  进度: {i+1}/{len(write_queue)} | "
                        f"成功 {success} 失败 {failed} | "
                        f"{rate:.1f}条/秒")

        # 限速 ~7次/秒
        time.sleep(0.15)

    logger.info(f"\n{'='*50}")
    logger.info(f"✅ 写入完成:")
    logger.info(f"  总提交: {len(write_queue)}")
    logger.info(f"  成功:   {success}")
    logger.info(f"  失败:   {failed}")
    logger.info(f"  耗时:   {time.time()-batch_start:.1f}秒")


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(
        description="货易达订单同步 + 卖家备注自动回写"
    )
    parser.add_argument(
        "--profile", default="default", choices=list(PROFILES.keys()),
        help="账号 profile (default=USTAR, mscj=MSCJ)"
    )

    sub = parser.add_subparsers(dest="command", help="子命令")

    # sync
    sync_p = sub.add_parser("sync", help="从货易达同步订单到本地 SQLite")
    sync_p.add_argument("--full", action="store_true", help="全量同步所有历史")
    sync_p.add_argument("--start", help="开始日期 YYYY-MM-DD")
    sync_p.add_argument("--end", help="结束日期 YYYY-MM-DD")
    sync_p.add_argument("--days", type=int, default=3, help="同步最近N天 (默认3)")
    sync_p.add_argument("--dashboard", action="store_true",
                        help="同步到看板数据库 (heute.db) 并正确标记 month")

    # write-remarks
    wr_p = sub.add_parser("write-remarks", help="写卖家备注到聚水潭")
    wr_p.add_argument("--force", action="store_true", help="重写所有（含已写的）")
    wr_p.add_argument("--limit", type=int, help="只写前 N 条（测试用）")

    # stats
    sub.add_parser("stats", help="查看数据库统计")

    return parser.parse_args()


def cmd_sync(args):
    """执行同步"""
    profile = args.profile
    cred = PROFILES[profile]
    db_path = get_db_path(profile)
    init_db(db_path)

    from heute_sdk import HeuteClient

    # 计算日期范围
    if args.full:
        start, end = "2024-01-01", datetime.now().strftime("%Y-%m-%d")
        logger.info(f"🔄 全量同步: {start} ~ {end}")
    elif args.start and args.end:
        start, end = args.start, args.end
        logger.info(f"🔄 自定义范围: {start} ~ {end}")
    elif args.start:
        start = args.start
        end = datetime.now().strftime("%Y-%m-%d")
        logger.info(f"🔄 从 {start} 到今天")
    else:
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
        logger.info(f"🔄 最近{args.days}天: {start} ~ {end}")

    # 登录
    logger.info(f"🔑 登录货易达 ({cred['username']})...")
    client = HeuteClient.login(cred["username"], cred["password"])

    # 拉取
    def progress(fetched, total, page):
        logger.info(f"📦 第{page}页: {fetched}条")

    logger.info("⏳ 拉取订单中（可能较慢，请稍候）...")
    all_orders = client.fetch_all_orders(start, end, progress_cb=progress)
    logger.info(f"✅ API 返回 {len(all_orders)} 条订单")

    # 过滤寄件人（如果配置了 sender）
    sender_filter = cred.get("sender")
    if sender_filter:
        before = len(all_orders)
        all_orders = [o for o in all_orders if o.get("senderName") == sender_filter]
        logger.info(f"🔍 过滤寄件人={sender_filter}: {before} → {len(all_orders)} 条")

    # 存入数据库
    upsert_orders(db_path, all_orders)
    logger.info(f"💾 已存入 SQLite: {db_path}")

    # 同步到看板数据库（如果指定 --dashboard）
    if args.dashboard:
        dashboard_path = os.path.join(SCRIPT_DIR, "data", "heute.db")
        dashboard_upsert_orders(dashboard_path, all_orders)

    # 统计
    stats = get_stats(db_path)
    logger.info(f"📊 数据库统计: 共 {stats['total']} 条")
    logger.info(f"  已写备注: {stats['written']} | 待写: {stats['unwritten']}")

    # 保存一份 JSON 备份
    suffix = f"{start}_{end}"
    json_path = os.path.join(SCRIPT_DIR, "data", f"heute_orders_{profile}_{suffix}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "profile": profile,
            "start": start,
            "end": end,
            "fetched_at": datetime.now().isoformat(),
            "count": len(all_orders),
            "items": all_orders,
        }, f, ensure_ascii=False)
    logger.info(f"📁 JSON 备份: {json_path}")

    # 返回统计用于 cron 摘要
    return stats


def cmd_write_remarks(args):
    profile = args.profile
    db_path = get_db_path(profile)

    if not os.path.exists(db_path):
        logger.error(f"❌ 数据库不存在: {db_path}，请先运行 sync")
        return

    logger.info(f"✏️  开始写入卖家备注 (profile={profile})")
    logger.info(f"  --force={args.force}  --limit={args.limit}")
    write_remarks(db_path, force=args.force, limit=args.limit)


def cmd_stats(args):
    profile = args.profile
    db_path = get_db_path(profile)

    if not os.path.exists(db_path):
        logger.error(f"❌ 数据库不存在: {db_path}")
        return

    stats = get_stats(db_path)
    logger.info(f"📊 数据库统计 (profile={profile})")
    logger.info(f"  总订单: {stats['total']}")
    logger.info(f"  已写备注: {stats['written']}")
    logger.info(f"  待写备注: {stats['unwritten']}")
    logger.info(f"")
    logger.info(f"  寄件人分布:")
    for s, cnt in stats["senders"][:10]:
        logger.info(f"    {s}: {cnt}")
    logger.info(f"")
    logger.info(f"  状态分布:")
    state_names = {0: "已作废", 1: "待支付", 2: "待入库", 3: "国际运输",
                   4: "国内配送", 5: "签收"}
    for s, cnt in stats["states"]:
        name = state_names.get(s, f"状态{s}")
        logger.info(f"    {name}: {cnt}")


def main():
    args = parse_args()
    if args.command == "sync":
        cmd_sync(args)
    elif args.command == "write-remarks":
        cmd_write_remarks(args)
    elif args.command == "stats":
        cmd_stats(args)
    else:
        print(__doc__)
        return

    logger.info("✅ 完成")


if __name__ == "__main__":
    main()
