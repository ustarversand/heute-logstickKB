#!/usr/bin/env python3
"""货易达物流看板 — SQLite 数据库模块

单库数据源: 数据文件目录下的 heute.db

3张表:
  - orders: 订单基础信息（来自货易达同步）
  - tracking: 轨迹结果（来自 batch_track_hybrid.py）
  - anomalies: 异常对比结果（来自 scan_anomalies.py）

所有 CRUD 通过上下文管理器自动提交，无需手动 commit。
"""
import json, os, sqlite3
from datetime import datetime
from collections import defaultdict
from typing import Optional

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
DB_PATH = os.path.join(DATA_DIR, 'heute.db')

# ─── Schema ────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
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
);

CREATE TABLE IF NOT EXISTS tracking (
    tracking_no     TEXT NOT NULL,
    month           TEXT NOT NULL DEFAULT 'may',
    current_status  TEXT DEFAULT '',
    latest_desc     TEXT DEFAULT '',
    latest_time     TEXT DEFAULT '',
    ext_track_no_cn TEXT DEFAULT '',
    tracking_json   TEXT DEFAULT '[]',   -- trackingDetails
    order_json      TEXT DEFAULT '{}',   -- 附加订单信息
    source          TEXT DEFAULT '',
    queried_at      TEXT DEFAULT '',
    PRIMARY KEY (tracking_no, month)
);

CREATE TABLE IF NOT EXISTS anomalies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    month       TEXT NOT NULL,
    order_sn    TEXT DEFAULT '',
    intl_tracking TEXT NOT NULL,
    dom_tracking  TEXT DEFAULT '',
    match       TEXT DEFAULT 'warning',  -- ok / warning / severe
    intl_json   TEXT DEFAULT '{}',
    dom_json    TEXT DEFAULT '{}',
    scanned_at  TEXT DEFAULT ''
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_tracking_month ON tracking(month);
CREATE INDEX IF NOT EXISTS idx_tracking_status ON tracking(current_status);
CREATE INDEX IF NOT EXISTS idx_tracking_ext ON tracking(ext_track_no_cn);
CREATE INDEX IF NOT EXISTS idx_orders_month ON orders(month);
CREATE INDEX IF NOT EXISTS idx_orders_sender ON orders(sender_name);
CREATE INDEX IF NOT EXISTS idx_orders_gw ON orders(global_waybill_sn);
CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(temp_line_sn);
CREATE INDEX IF NOT EXISTS idx_anomalies_month ON anomalies(month);
CREATE INDEX IF NOT EXISTS idx_anomalies_match ON anomalies(match);

-- Apollo 推送订单（鲸芽/淘分销）
CREATE TABLE IF NOT EXISTS apollo_orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tc_order_id TEXT NOT NULL,
    supplier_username TEXT DEFAULT '',
    product_code TEXT DEFAULT 'taobaoFX',
    order_type  TEXT DEFAULT '',
    receiver_name TEXT DEFAULT '',
    receiver_phone TEXT DEFAULT '',
    receiver_address TEXT DEFAULT '',
    receiver_idcard TEXT DEFAULT '',
    pay_price   REAL DEFAULT 0,
    order_price REAL DEFAULT 0,
    tax_fee     REAL DEFAULT 0,
    post_fee    REAL DEFAULT 0,
    discount_fee REAL DEFAULT 0,
    currency    TEXT DEFAULT 'CNY',
    sku_json    TEXT DEFAULT '[]',
    raw_json    TEXT DEFAULT '{}',
    received_at TEXT DEFAULT '',
    month       TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_apollo_order_id ON apollo_orders(tc_order_id);

CREATE TABLE IF NOT EXISTS after_sales (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tracking_no TEXT NOT NULL DEFAULT '',   -- 用户填的单号（国内或国际）
    order_sn    TEXT DEFAULT '',
    domestic_tracking_no TEXT DEFAULT '',   -- 国内单号（如顺丰）
    intl_tracking_no    TEXT DEFAULT '',    -- 国际单号（自动匹配）
    sender_name TEXT DEFAULT '',
    issue_type  TEXT DEFAULT '',
    description TEXT DEFAULT '',
    status      TEXT DEFAULT '待处理',
    contact_info TEXT DEFAULT '',
    amount      REAL DEFAULT 0,
    operator    TEXT DEFAULT '',
    month       TEXT NOT NULL DEFAULT 'may',
    created_at  TEXT DEFAULT '',
    updated_at  TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_after_sales_tracking ON after_sales(tracking_no);
CREATE INDEX IF NOT EXISTS idx_after_sales_month ON after_sales(month);
CREATE INDEX IF NOT EXISTS idx_after_sales_status ON after_sales(status);
CREATE INDEX IF NOT EXISTS idx_after_sales_sender ON after_sales(sender_name);

-- 手动状态覆盖（用户手动标记订单状态）
CREATE TABLE IF NOT EXISTS order_status_overrides (
    sn          TEXT NOT NULL,
    month       TEXT NOT NULL DEFAULT 'may',
    manual_status TEXT NOT NULL DEFAULT '',
    updated_at  TEXT DEFAULT '',
    updated_by  TEXT DEFAULT '',
    PRIMARY KEY (sn, month)
);
CREATE INDEX IF NOT EXISTS idx_override_month ON order_status_overrides(month);

-- 财务流水缓存（称重补款等）
CREATE TABLE IF NOT EXISTS finance_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_sn    TEXT DEFAULT '',
    money_changed INTEGER DEFAULT 0,
    current_balance INTEGER DEFAULT 0,
    account_type INTEGER DEFAULT 0,
    pay_type    INTEGER DEFAULT 0,
    type_val    INTEGER DEFAULT 0,
    description TEXT DEFAULT '',
    creation_time TEXT DEFAULT '',
    month       TEXT NOT NULL DEFAULT 'may',
    UNIQUE(order_sn, creation_time, money_changed)
);
CREATE INDEX IF NOT EXISTS idx_finance_month ON finance_logs(month);
CREATE INDEX IF NOT EXISTS idx_finance_sn ON finance_logs(order_sn);
CREATE INDEX IF NOT EXISTS idx_finance_time ON finance_logs(creation_time);
"""

# ─── 数据库连接上下文 ───────────────────────────────────────────────────────

class DB:
    """数据库操作上下文管理器，自动提交"""
    def __init__(self, path: str = DB_PATH):
        self.path = path
        self.conn: Optional[sqlite3.Connection] = None

    def __enter__(self):
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        return self.conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            if exc_type is None:
                self.conn.commit()
            self.conn.close()

def init_db(path: str = DB_PATH):
    """初始化/升级数据库表结构"""
    with DB(path) as db:
        db.executescript(SCHEMA_SQL)

# ─── Orders CRUD ──────────────────────────────────────────────────────────

def upsert_order(db: sqlite3.Connection, o: dict, month: str = 'may'):
    """插入或更新一条订单记录"""
    db.execute("""
        INSERT OR REPLACE INTO orders
        (sn, month, global_waybill_sn, temp_line_sn, consignee_name, consignee_tel,
         sender_name, state, weight, creation_time, line_name, line_id,
         merchant_order_sn, platform_sn, id_card_info_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        o.get('sn', ''),
        month,
        o.get('globalWayBillSN', ''),
        o.get('tempLineSN', ''),
        o.get('consigneeName', ''),
        o.get('consigneeTel', ''),
        o.get('senderName', ''),
        o.get('state', 0),
        o.get('weight', 0),
        (o.get('creationTime') or '')[:19].replace('T', ' '),
        o.get('lineName', ''),
        o.get('lineId', 0),
        o.get('merchantOrderSN', ''),
        o.get('platformSN', ''),
        o.get('idCardInfoStatus', 0),
    ))

def bulk_upsert_orders(orders: list, month: str = 'may', path: str = DB_PATH):
    """批量写入订单（事务级）"""
    with DB(path) as db:
        for o in orders:
            upsert_order(db, o, month)

def get_orders(month: str = 'may', sender: str = None, path: str = DB_PATH) -> list[dict]:
    """查询订单列表"""
    with DB(path) as db:
        if sender:
            rows = db.execute(
                "SELECT * FROM orders WHERE month=? AND sender_name=?",
                (month, sender)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM orders WHERE month=? ORDER BY creation_time DESC",
                (month,)
            ).fetchall()
        return [dict(r) for r in rows]

def get_order_count(month: str = 'may', path: str = DB_PATH) -> int:
    """快速获取订单数"""
    with DB(path) as db:
        return db.execute("SELECT COUNT(*) FROM orders WHERE month=?",
                          (month,)).fetchone()[0]


def lookup_order_by_tracking(tracking_no: str, path: str = DB_PATH) -> Optional[dict]:
    """通过运单号（国际或国内）快速查找订单，直接SQL查询（带索引）"""
    with DB(path) as db:
        # 先查国际单号（global_waybill_sn 有索引）
        row = db.execute(
            "SELECT * FROM orders WHERE global_waybill_sn=? LIMIT 1",
            (tracking_no,)
        ).fetchone()
        if row:
            result = dict(row)
            return {
                'gw': result.get('global_waybill_sn', ''),
                'ts': result.get('temp_line_sn', ''),
                'sender': result.get('sender_name', ''),
                'sn': result.get('sn', ''),
            }
        # 再查国内单号（temp_line_sn 已有索引）
        row = db.execute(
            "SELECT * FROM orders WHERE temp_line_sn=? LIMIT 1",
            (tracking_no,)
        ).fetchone()
        if row:
            result = dict(row)
            return {
                'gw': result.get('global_waybill_sn', ''),
                'ts': result.get('temp_line_sn', ''),
                'sender': result.get('sender_name', ''),
                'sn': result.get('sn', ''),
            }
    return None


def get_senders(path: str = DB_PATH) -> list[str]:
    """获取所有寄件人列表"""
    with DB(path) as db:
        rows = db.execute(
            "SELECT DISTINCT sender_name FROM orders WHERE sender_name != '' ORDER BY sender_name"
        ).fetchall()
        return [r[0] for r in rows]

# ─── Tracking CRUD ────────────────────────────────────────────────────────

def upsert_tracking(db: sqlite3.Connection, tracking_no: str, month: str,
                    tracking: dict, order_info: dict = None):
    """插入或更新一条轨迹记录
    
    保护已有完整轨迹明细不被不完整数据覆盖（方案二：增量更新）
    如果新数据 trackingDetails 为空/短，但DB已有完整明细 → 保留旧明细
    """
    details = tracking.get('trackingDetails', [])
    if not isinstance(details, list):
        details = []

    # ── 检查DB是否已有更完整的轨迹明细 ──
    old = db.execute(
        "SELECT current_status, tracking_json FROM tracking WHERE tracking_no=? AND month=?",
        (tracking_no, month)
    ).fetchone()
    if old:
        old_status = (old[0] or '').strip()
        old_json = old[1]
        old_details = []
        if old_json:
            try:
                old_details = json.loads(old_json) if isinstance(old_json, str) else []
            except (json.JSONDecodeError, TypeError):
                pass

        # 保护条件：新数据没明细或只有1条，但旧数据有≥3条明细
        new_details_count = len(details) if isinstance(details, list) else 0
        old_details_count = len(old_details) if isinstance(old_details, list) else 0
        if new_details_count < 3 and old_details_count >= 3:
            # 保留旧明细
            details = old_details
            # 如果新API返回"其他"但旧有具体状态 → 保留旧状态
            new_status = (tracking.get('currentStatus', '') or '').strip()
            if new_status in ('', '其他', '其它') and old_status not in ('', '其他', '其它'):
                tracking['currentStatus'] = old_status

    db.execute("""
        INSERT OR REPLACE INTO tracking
        (tracking_no, month, current_status, latest_desc, latest_time,
         ext_track_no_cn, tracking_json, order_json, source, queried_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        tracking_no,
        month,
        tracking.get('currentStatus', ''),
        tracking.get('latestDesc', ''),
        tracking.get('latestTime', ''),
        tracking.get('extTrackNoCn', ''),
        json.dumps(details, ensure_ascii=False),
        json.dumps(order_info or {}, ensure_ascii=False),
        tracking.get('source', ''),
        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    ))

    # 写入轨迹后同步订单 state
    _sync_order_state_from_tracking(db, tracking_no, month)


# ─── 轨迹状态 → 订单 state 同步 ───

_TRACKING_TO_ORDER_STATE = {
    '已签收': 5,
    '已撤销': -6,
    '清关中': 3,
    '已清关': 4,
    '已揽收': 3,
    '快件航班批次已创建': 3,
    '运单已经创建': 2,
}

_TERMINAL_ORDER_STATES = {-6, 0, 5}  # 不覆盖这些终端状态

# 中国主要城市/地区关键词（用于判断是否已到国内段）
_CHINA_CITY_KEYWORDS = [
    '北京', '上海', '广州', '深圳', '杭州', '南京', '成都', '武汉',
    '天津', '重庆', '苏州', '西安', '长沙', '郑州', '东莞', '青岛',
    '沈阳', '宁波', '昆明', '大连', '厦门', '合肥', '佛山', '福州',
    '哈尔滨', '济南', '温州', '长春', '石家庄', '常州', '泉州',
    '南宁', '贵阳', '南昌', '太原', '烟台', '嘉兴', '南通', '金华',
    '珠海', '惠州', '徐州', '海口', '乌鲁木齐', '绍兴', '中山',
    '台州', '兰州', '中国',
]


def _is_china_domestic(ext_track_no: str, latest_desc: str, tracking_details: list = None) -> bool:
    """判断包裹是否已进入国内配送阶段"""
    # 信号1: 有国内单号（SF/JDV 开头）
    if ext_track_no:
        return True
    # 信号2: 最新轨迹描述含中国城市
    if latest_desc:
        for kw in _CHINA_CITY_KEYWORDS:
            if kw in latest_desc:
                return True
        # 明确是德国城市 → 还在国际段
        if '杜塞尔' in latest_desc or '法兰克福' in latest_desc:
            return False
    # 信号3: trackingDetails 中的站点名含中国城市
    if tracking_details:
        for d in tracking_details:
            if not isinstance(d, dict):
                continue
            for field in ('currentSiteName', 'nextSiteName', 'address', 'trackingDesc', 'desc'):
                val = d.get(field, '') or ''
                for kw in _CHINA_CITY_KEYWORDS:
                    if kw in val:
                        return True
    return False


def _judge_state(tracking_status: str, ext_track_no: str = '',
                 latest_desc: str = '', tracking_details: list = None) -> int | None:
    """综合判断：轨迹状态 + 位置信息 → orders.state"""
    # 直接确定的
    if tracking_status == '已签收': return 5
    if tracking_status == '已撤销': return -6
    if tracking_status == '运单已经创建': return 2
    if tracking_status == '清关中': return 3
    if tracking_status == '已清关': return 4
    if tracking_status in ('已揽收', '快件航班批次已创建'): return 3

    # 模糊状态：靠位置信息判断国际还是国内
    if tracking_status in ('在途', '离开', '到达', '转寄'):
        return 4 if _is_china_domestic(ext_track_no, latest_desc, tracking_details) else 3

    return None


def _sync_order_state_from_tracking(db: sqlite3.Connection, tracking_no: str, month: str):
    """根据刚写入的轨迹状态，同步更新 orders.state"""
    row = db.execute(
        "SELECT current_status, ext_track_no_cn, latest_desc, tracking_json "
        "FROM tracking WHERE tracking_no=? AND month=?",
        (tracking_no, month)
    ).fetchone()
    if not row:
        return

    tracking_status = (row[0] or '').strip()
    ext_track_no = (row[1] or '').strip()
    latest_desc = (row[2] or '').strip()
    tracking_details = []
    if row[3]:
        try:
            tracking_details = json.loads(row[3]) if isinstance(row[3], str) else []
        except:
            pass

    new_state = _judge_state(tracking_status, ext_track_no, latest_desc, tracking_details)
    if new_state is None:
        return

    # 找到关联的订单
    order_row = db.execute(
        "SELECT sn, state FROM orders WHERE global_waybill_sn=?",
        (tracking_no,)
    ).fetchone()
    if not order_row:
        return

    order_sn, current_state = order_row

    # 终端状态不降级
    if current_state in _TERMINAL_ORDER_STATES:
        return
    if current_state == new_state:
        return

    # 正常递进：2→3→4→5
    if current_state in (2, 3, 4) and new_state in (3, 4, 5):
        if new_state > current_state:
            db.execute("UPDATE orders SET state=? WHERE sn=?", (new_state, order_sn))
    # 已撤销：任何非终端状态都可覆盖
    elif new_state == -6:
        db.execute("UPDATE orders SET state=? WHERE sn=?", (new_state, order_sn))

def bulk_upsert_tracking(results: dict, month: str, path: str = DB_PATH):
    """批量写入轨迹结果"""
    with DB(path) as db:
        for tracking_no, entry in results.items():
            if not isinstance(entry, dict):
                continue
            t = entry.get('tracking', {})
            if not isinstance(t, dict):
                continue
            upsert_tracking(db, tracking_no, month, t, entry.get('order', {}))

def get_tracking(month: str = 'may', status_filter: str = None,
                 page: int = 1, per_page: int = 50, path: str = DB_PATH) -> tuple[list, int]:
    """分页查询轨迹，返回 (items, total_count)"""
    with DB(path) as db:
        clauses = ["month=?"]
        params = [month]
        if status_filter:
            if status_filter == '待查询':
                clauses.append("(current_status IS NULL OR current_status='')")
            elif status_filter in ('all', '', '全部'):
                pass  # 不过滤
            else:
                clauses.append("current_status=?")
                params.append(status_filter)

        where = " AND ".join(clauses)
        total = db.execute(f"SELECT COUNT(*) FROM tracking WHERE {where}", params).fetchone()[0]
        offset = (page - 1) * per_page
        rows = db.execute(
            f"SELECT * FROM tracking WHERE {where} ORDER BY latest_time DESC LIMIT ? OFFSET ?",
            params + [per_page, offset]
        ).fetchall()

        items = []
        for r in rows:
            d = dict(r)
            d['trackingDetails'] = json.loads(d.pop('tracking_json', '[]'))
            d['order'] = json.loads(d.pop('order_json', '{}'))
            items.append({'tracking': d})

        return items, total

def get_tracking_status_dist(month: str = 'may', sender: str = None, path: str = DB_PATH) -> dict:
    """获取轨迹状态分布统计"""
    with DB(path) as db:
        if sender:
            # 从orders表过滤寄件人的global_waybill_sn
            rows = db.execute("""
                SELECT t.current_status, COUNT(*) as cnt
                FROM tracking t
                INNER JOIN orders o ON t.tracking_no = o.global_waybill_sn AND t.month = o.month
                WHERE t.month=? AND o.sender_name=?
                GROUP BY t.current_status
            """, (month, sender)).fetchall()
        else:
            rows = db.execute(
                "SELECT current_status, COUNT(*) as cnt FROM tracking WHERE month=? GROUP BY current_status",
                (month,)
            ).fetchall()
        return {r[0]: r[1] for r in rows}

def get_tracking_by_no(tracking_no: str, month: str = 'may', path: str = DB_PATH) -> dict | None:
    """按运单号查单条轨迹"""
    with DB(path) as db:
        r = db.execute(
            "SELECT * FROM tracking WHERE tracking_no=? AND month=?",
            (tracking_no, month)
        ).fetchone()
        if r:
            d = dict(r)
            d['trackingDetails'] = json.loads(d.pop('tracking_json', '[]'))
            d['order'] = json.loads(d.pop('order_json', '{}'))
            return {'tracking': d}
        return None

def get_tracking_count_by_status(month: str = 'may', path: str = DB_PATH) -> dict[str, int]:
    """按月份获取各状态数量（快速统计用）"""
    with DB(path) as db:
        rows = db.execute(
            "SELECT current_status, COUNT(*) FROM tracking WHERE month=? GROUP BY current_status",
            (month,)
        ).fetchall()
        return {r[0]: r[1] for r in rows}

def search_tracking(q: str, month: str = 'may', path: str = DB_PATH) -> list[dict]:
    """搜索运单号/订单号/收件人"""
    with DB(path) as db:
        like = f"%{q}%"
        rows = db.execute("""
            SELECT t.*, o.sender_name, o.consignee_name, o.sn as order_sn
            FROM tracking t
            LEFT JOIN orders o ON t.tracking_no = o.global_waybill_sn AND t.month = o.month
            WHERE t.month=?
              AND (t.tracking_no LIKE ? OR t.ext_track_no_cn LIKE ? OR o.consignee_name LIKE ? OR o.sn LIKE ?)
            LIMIT 20
        """, (month, like, like, like, like)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d['trackingDetails'] = json.loads(d.pop('tracking_json', '[]'))
            result.append({'tracking': d})
        return result

# ─── Anomaly CRUD ─────────────────────────────────────────────────────────

def bulk_upsert_anomalies(anomalies: list, month: str, path: str = DB_PATH):
    """写入异常对比结果（先清旧数据再写新）"""
    with DB(path) as db:
        db.execute("DELETE FROM anomalies WHERE month=?", (month,))
        for a in anomalies:
            db.execute("""
                INSERT INTO anomalies (month, order_sn, intl_tracking, dom_tracking,
                                       match, intl_json, dom_json, scanned_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                month,
                a.get('order_sn', ''),
                a.get('intl_tracking', ''),
                a.get('dom_tracking', ''),
                a.get('match', 'warning'),
                json.dumps(a.get('intl', {}), ensure_ascii=False),
                json.dumps(a.get('dom', {}), ensure_ascii=False),
                a.get('scanned_at', ''),
            ))

def get_anomalies(month: str = 'may', match_filter: str = None,
                  sender: str = None,
                  path: str = DB_PATH) -> list[dict]:
    """查询异常对比结果，可选按寄件人过滤"""
    with DB(path) as db:
        if sender:
            if match_filter:
                rows = db.execute("""
                    SELECT a.* FROM anomalies a
                    JOIN orders o ON a.order_sn = o.sn AND a.month = o.month
                    WHERE a.month=? AND o.sender_name=? AND a.match=?
                    ORDER BY a.id
                """, (month, sender, match_filter)).fetchall()
            else:
                rows = db.execute("""
                    SELECT a.* FROM anomalies a
                    JOIN orders o ON a.order_sn = o.sn AND a.month = o.month
                    WHERE a.month=? AND o.sender_name=?
                    ORDER BY a.id
                """, (month, sender)).fetchall()
        elif match_filter:
            rows = db.execute(
                "SELECT * FROM anomalies WHERE month=? AND match=? ORDER BY id",
                (month, match_filter)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM anomalies WHERE month=? ORDER BY id", (month,)
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d['intl'] = json.loads(d.pop('intl_json', '{}'))
            d['dom'] = json.loads(d.pop('dom_json', '{}'))
            result.append(d)
        return result

def get_anomaly_counts(month: str = 'may', sender: str = None, path: str = DB_PATH) -> dict[str, int]:
    """获取异常分类计数，可选按寄件人过滤"""
    with DB(path) as db:
        if sender:
            rows = db.execute("""
                SELECT a.match, COUNT(*)
                FROM anomalies a
                JOIN orders o ON a.order_sn = o.sn AND a.month = o.month
                WHERE a.month=? AND o.sender_name=?
                GROUP BY a.match
            """, (month, sender)).fetchall()
        else:
            rows = db.execute(
                "SELECT match, COUNT(*) FROM anomalies WHERE month=? GROUP BY match",
                (month,)
            ).fetchall()
        return {r[0]: r[1] for r in rows}

def count_anomalies_by_sender(sender: str, month: str = 'may', path: str = DB_PATH) -> int:
    """统计某寄件人的严重异常数"""
    with DB(path) as db:
        r = db.execute("""
            SELECT COUNT(*) FROM anomalies a
            INNER JOIN orders o ON a.order_sn = o.sn AND a.month = o.month
            WHERE a.month=? AND o.sender_name=? AND a.match='severe'
        """, (month, sender)).fetchone()
        return r[0] if r else 0

# ─── Apollo 推送订单 CRUD ─────────────────────────────────────────────────

def store_apollo_order(data: dict, path: str = DB_PATH):
    """存储来自 Apollo 的订单推送数据"""
    sku_list = data.get('skuList', [])
    if isinstance(sku_list, str):
        try:
            sku_list = json.loads(sku_list)
        except:
            sku_list = []
    
    now = datetime.now()
    month = now.strftime('%B').lower()[:3]
    
    with DB(path) as db:
        db.execute("""
            INSERT INTO apollo_orders
            (tc_order_id, supplier_username, product_code, order_type,
             receiver_name, receiver_phone, receiver_address, receiver_idcard,
             pay_price, order_price, tax_fee, post_fee, discount_fee, currency,
             sku_json, raw_json, received_at, month)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data.get('tcOrderId', ''),
            data.get('supplierUsername', ''),
            data.get('productCode', 'taobaoFX'),
            data.get('orderType', ''),
            data.get('receiveMan', ''),
            data.get('receiveManPhone', ''),
            f"{data.get('receiveProvince','')} {data.get('receiveCity','')} {data.get('receiveCounty','')} {data.get('receiveManAddress','')}",
            data.get('receiveManId', ''),
            float(data.get('payPrice', 0)),
            float(data.get('orderPrice', 0)),
            float(data.get('taxFee', 0)),
            float(data.get('postFee', 0)),
            float(data.get('disCountFee', 0)),
            data.get('currencyCode', 'CNY'),
            json.dumps(sku_list, ensure_ascii=False),
            json.dumps(data, ensure_ascii=False),
            now.isoformat(),
            month
        ))
        return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_apollo_orders(limit: int = 50, offset: int = 0, month: str = None,
                      path: str = DB_PATH) -> list[dict]:
    """查询 Apollo 推送订单列表"""
    with DB(path) as db:
        if month:
            rows = db.execute(
                "SELECT * FROM apollo_orders WHERE month=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (month, limit, offset)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM apollo_orders ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset)
            ).fetchall()
        return [dict(r) for r in rows]


def get_apollo_order_count(month: str = None, path: str = DB_PATH) -> int:
    """Apollo 推送订单总数"""
    with DB(path) as db:
        if month:
            return db.execute(
                "SELECT COUNT(*) FROM apollo_orders WHERE month=?", (month,)
            ).fetchone()[0]
        return db.execute("SELECT COUNT(*) FROM apollo_orders").fetchone()[0]


def delete_apollo_order(order_id: int, path: str = DB_PATH) -> bool:
    """删除指定 Apollo 订单"""
    with DB(path) as db:
        c = db.execute("DELETE FROM apollo_orders WHERE id=?", (order_id,))
        return c.rowcount > 0


# ─── After-Sales CRUD ────────────────────────────────────────────────────

def create_after_sales(data: dict, path: str = DB_PATH) -> int:
    # 创建一条售后记录，返回 id
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with DB(path) as db:
        db.execute("""
            INSERT INTO after_sales
            (tracking_no, order_sn, domestic_tracking_no, intl_tracking_no,
             sender_name, issue_type, description, status,
             contact_info, amount, operator, month, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data.get('tracking_no', ''),
            data.get('order_sn', ''),
            data.get('domestic_tracking_no', ''),
            data.get('intl_tracking_no', ''),
            data.get('sender_name', ''),
            data.get('issue_type', ''),
            data.get('description', ''),
            data.get('status', '待处理'),
            data.get('contact_info', ''),
            float(data.get('amount', 0)),
            data.get('operator', ''),
            data.get('month', 'may'),
            now, now,
        ))
        return db.execute("SELECT last_insert_rowid()").fetchone()[0]

def get_after_sales(month: str = 'may', status: str = None, sender: str = None,
                    page: int = 1, per_page: int = 50,
                    path: str = DB_PATH) -> tuple[list, int]:
    # 分页查询售后记录，返回 (items, total)，带物流状态
    with DB(path) as db:
        clauses = ["a.month=?"]
        params = [month]
        if status and status != 'all':
            clauses.append("a.status=?")
            params.append(status)
        if sender and sender != 'all':
            clauses.append("a.sender_name=?")
            params.append(sender)
        if month == 'all':
            clauses = []
            params = []
        where = " AND ".join(clauses) if clauses else "1=1"
        total = db.execute(f"SELECT COUNT(*) FROM after_sales a WHERE {where}", params).fetchone()[0]
        offset = (page - 1) * per_page
        rows = db.execute(f"""
            SELECT a.*, t.current_status as current_tracking_status
            FROM after_sales a
            LEFT JOIN tracking t ON a.intl_tracking_no = t.tracking_no AND a.month = t.month
            WHERE {where}
            ORDER BY a.created_at DESC LIMIT ? OFFSET ?
        """, params + [per_page, offset]).fetchall()
        return [dict(r) for r in rows], total

def search_after_sales(q: str, page: int = 1, per_page: int = 50,
                        sender: str = None, path: str = DB_PATH) -> tuple[list, int]:
    """按单号/订单号/描述搜索售后记录（跨月），支持分页+寄件人过滤"""
    with DB(path) as db:
        like = f"%{q}%"
        where = "(tracking_no LIKE ? OR order_sn LIKE ? OR domestic_tracking_no LIKE ? OR intl_tracking_no LIKE ? OR description LIKE ?)"
        params = [like, like, like, like, like]
        if sender and sender != 'all':
            where += " AND a.sender_name=?"
            params.append(sender)
        total = db.execute(f"SELECT COUNT(*) FROM after_sales a WHERE {where}", params).fetchone()[0]
        offset = (page - 1) * per_page
        rows = db.execute(
            f"""SELECT a.*, t.current_status as current_tracking_status
                FROM after_sales a
                LEFT JOIN tracking t ON a.intl_tracking_no = t.tracking_no AND a.month = t.month
                WHERE {where} ORDER BY a.created_at DESC LIMIT ? OFFSET ?""",
            params + [per_page, offset]
        ).fetchall()
        return [dict(r) for r in rows], total

def update_after_sales(after_id: int, data: dict, path: str = DB_PATH) -> bool:
    # 更新售后记录（只更新非空字段）
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    fields = []
    params = []
    for key in ('status', 'description', 'contact_info', 'issue_type', 'operator'):
        if key in data:
            fields.append(f"{key}=?")
            params.append(data[key])
    if 'amount' in data:
        fields.append("amount=?")
        params.append(float(data['amount']))
    if not fields:
        return False
    fields.append("updated_at=?")
    params.append(now)
    params.append(after_id)
    with DB(path) as db:
        c = db.execute(
            f"UPDATE after_sales SET {', '.join(fields)} WHERE id=?",
            params
        )
        return c.rowcount > 0

def delete_after_sales(after_id: int, path: str = DB_PATH) -> bool:
    # 删除售后记录
    with DB(path) as db:
        c = db.execute("DELETE FROM after_sales WHERE id=?", (after_id,))
        return c.rowcount > 0

def get_after_sales_stats(month: str = 'may', path: str = DB_PATH) -> dict:
    # 售后统计
    with DB(path) as db:
        if month == 'all':
            total = db.execute("SELECT COUNT(*) FROM after_sales").fetchone()[0]
            by_status = {}
            rows = db.execute("SELECT status, COUNT(*) as cnt FROM after_sales GROUP BY status").fetchall()
            for r in rows:
                by_status[r[0]] = r[1]
            by_type = {}
            rows = db.execute("SELECT issue_type, COUNT(*) as cnt FROM after_sales GROUP BY issue_type").fetchall()
            for r in rows:
                by_type[r[0]] = r[1]
        else:
            total = db.execute("SELECT COUNT(*) FROM after_sales WHERE month=?", (month,)).fetchone()[0]
            by_status = {}
            rows = db.execute(
                "SELECT status, COUNT(*) as cnt FROM after_sales WHERE month=? GROUP BY status",
                (month,)
            ).fetchall()
            for r in rows:
                by_status[r[0]] = r[1]
            by_type = {}
            rows = db.execute(
                "SELECT issue_type, COUNT(*) as cnt FROM after_sales WHERE month=? GROUP BY issue_type",
                (month,)
            ).fetchall()
            for r in rows:
                by_type[r[0]] = r[1]
        return {
            'total': total,
            'by_status': by_status,
            'by_type': by_type,
        }

# ─── 数据字典（cron监控用）─────────────────────────────────────────────────

def get_month_overview(month: str = 'may', path: str = DB_PATH) -> dict:
    """月度概览统计"""
    with DB(path) as db:
        order_count = db.execute(
            "SELECT COUNT(*) FROM orders WHERE month=?", (month,)
        ).fetchone()[0]
        track_count = db.execute(
            "SELECT COUNT(*) FROM tracking WHERE month=?", (month,)
        ).fetchone()[0]
        signed = db.execute(
            "SELECT COUNT(*) FROM tracking WHERE month=? AND current_status LIKE '%签收%'",
            (month,)
        ).fetchone()[0]
        severe = db.execute(
            "SELECT COUNT(*) FROM anomalies WHERE month=? AND match='severe'",
            (month,)
        ).fetchone()[0]
        return {
            'month': month,
            'orders': order_count,
            'tracked': track_count,
            'signed': signed,
            'severe_anomalies': severe,
        }

# ─── 手动状态覆盖 CRUD ────────────────────────────────────────────────────

def set_manual_status(sn: str, status: str, month: str = 'may',
                       updated_by: str = 'dashboard', path: str = DB_PATH):
    """设置手动状态覆盖（存入 order_status_overrides 表）"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    with DB(path) as db:
        db.execute("""
            INSERT OR REPLACE INTO order_status_overrides
            (sn, month, manual_status, updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?)
        """, (sn, month, status, now, updated_by))

def clear_manual_status(sn: str, month: str = 'may', path: str = DB_PATH):
    """清除手动状态覆盖"""
    with DB(path) as db:
        db.execute(
            "DELETE FROM order_status_overrides WHERE sn=? AND month=?",
            (sn, month)
        )

def get_manual_statuses(month: str = 'may', path: str = DB_PATH) -> dict:
    """获取指定月份所有手动状态覆盖，返回 {sn: {status, updated_at}}"""
    with DB(path) as db:
        rows = db.execute(
            "SELECT sn, manual_status, updated_at FROM order_status_overrides WHERE month=?",
            (month,)
        ).fetchall()
        return {r[0]: {'status': r[1], 'updated_at': r[2]} for r in rows}

# ─── 财务数据 ────────────────────────────────────────────────────────────────

def get_finance(month: str = 'may', path: str = DB_PATH) -> dict:
    """从本地库读取财务统计数据"""
    with DB(path) as db:
        rows = db.execute(
            "SELECT order_sn, money_changed, description, creation_time FROM finance_logs WHERE month=? ORDER BY creation_time DESC",
            (month,)
        ).fetchall()
        total_count = len(rows)
        total_amount = sum(r[1] for r in rows) / 100  # 分→元
        # 按天统计
        daily = {}
        for r in rows:
            day = r[3][:10] if r[3] else 'unknown'
            if day not in daily:
                daily[day] = {'count': 0, 'amount': 0}
            daily[day]['count'] += 1
            daily[day]['amount'] += r[1] / 100
        daily_list = sorted(
            [{'date': d, 'count': v['count'], 'amount': round(v['amount'], 2)}
             for d, v in daily.items()],
            key=lambda x: x['date'], reverse=True
        )
        # 按订单统计
        by_order = {}
        for r in rows:
            sn = r[0] or 'N/A'
            if sn not in by_order:
                by_order[sn] = {'order_sn': sn, 'count': 0, 'amount': 0}
            by_order[sn]['count'] += 1
            by_order[sn]['amount'] += r[1] / 100
        order_list = sorted(by_order.values(), key=lambda x: x['amount'], reverse=True)

        return {
            'total_count': total_count,
            'total_amount': round(total_amount, 2),
            'daily': daily_list,
            'by_order': order_list,
        }

def sync_finance(month: str = 'may', path: str = DB_PATH) -> dict:
    """从货易达API拉取财务流水并存入本地库"""
    from heute_api import HeuteAPI, ORDER_BASE, BROWSER_HEADERS
    import urllib.request, ssl, json, time

    api = HeuteAPI()
    api.login_all()
    token = api.order._token

    # 确定日期范围
    month_map = {'apr': '2026-04', 'april': '2026-04', 'may': '2026-05', 'jun': '2026-06'}
    prefix = month_map.get(month, '2026-05')

    url = f"{ORDER_BASE}/Prod/api/app/member-center/get-member-money-logs"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": BROWSER_HEADERS["User-Agent"],
        "Referer": f"{ORDER_BASE}/members/member-money-log",
        "Origin": ORDER_BASE,
    }
    ctx = ssl.create_default_context()

    all_logs = []
    page = 1
    while True:
        payload = {"pageIndex": page, "pageSize": 200,
                   "startTime": f"{prefix}-01", "endTime": f"{prefix}-30",
                   "type": -2, "orderSn": None}
        body = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        items = data.get("items", [])
        if not items:
            break
        # 本地过滤：只保留当前月的
        month_items = [i for i in items if i.get("creationTime", "").startswith(prefix)]
        all_logs.extend(month_items)
        # 如果本页已经全是其他月的，停止
        if len(month_items) == 0:
            break
        total = data.get("totalCount", 0)
        if len(all_logs) >= total:
            break
        page += 1
        time.sleep(0.3)

    # 存入DB
    inserted = 0
    with DB(path) as db:
        for l in all_logs:
            db.execute(
                """INSERT OR REPLACE INTO finance_logs
                   (order_sn, money_changed, current_balance, account_type, pay_type,
                    type_val, description, creation_time, month)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (l.get('orderSN', ''),
                 l.get('moneyChanged', 0),
                 l.get('currentBalance', 0),
                 l.get('accountType', 0),
                 l.get('payType', 0),
                 l.get('type', -2),
                 l.get('description', ''),
                 l.get('creationTime', ''),
                 month)
            )
            inserted += 1
    return {'inserted': inserted, 'month': month}

# ─── 初始化 ────────────────────────────────────────────────────────────────

if __name__ == '__main__':

    init_db()
    print(f"✅ {DB_PATH}")
# BIND_MOUNT_VERIFIED: 11:29:31
