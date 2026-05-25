#!/usr/bin/env python3
"""
从 heute_express_ustar.db → 同步到 heute.db（看板用）
解决两库数据不一致问题，自动跑在 heuete-sync cron 末尾
"""
import sys, os, sqlite3
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

DATA_DIR = os.path.join(SCRIPT_DIR, 'data')
SRC_DB = os.path.join(DATA_DIR, 'heute_express_ustar.db')
DST_DB = os.path.join(DATA_DIR, 'heute.db')

# 字段映射: sync库SnakeCase → upsert_order接受的API驼峰名
FIELD_MAP = {
    'sn':                'sn',
    'global_waybill':    'globalWayBillSN',
    'temp_line_sn':      'tempLineSN',
    'consignee_name':    'consigneeName',
    'consignee_tel':     'consigneeTel',
    'sender_name':       'senderName',
    'state':             'state',
    'weight':            'weight',
    'creation_time':     'creationTime',
    'line_name':         'lineName',
    'line_id':           'lineId',
    'merchant_order_sn': 'merchantOrderSN',
    'platform_sn':       'platformSN',
    'idcard_info_status':'idCardInfoStatus',
}

def main():
    if not os.path.exists(SRC_DB):
        print(f"❌ 源库不存在: {SRC_DB}")
        return 1
    if not os.path.exists(DST_DB):
        print(f"⚠️ 目标库不存在，稍后会自动建表: {DST_DB}")

    # 从 sync 库读所有订单
    src = sqlite3.connect(SRC_DB)
    src.row_factory = sqlite3.Row
    rows = src.execute("SELECT * FROM orders").fetchall()
    src.close()
    print(f"📖 源库: {len(rows)} 条订单")

    # 转换成 upsert_order 需要的格式
    from heute_db import init_db, DB, upsert_order
    from datetime import datetime

    month = 'may'
    if datetime.now().month == 6:
        month = 'june'
    # 按需加更多月份

    init_db(path=DST_DB)

    count = 0
    with DB(DST_DB) as db:
        for row in rows:
            d = dict(row)
            mapped = {}
            for src_key, dst_key in FIELD_MAP.items():
                mapped[dst_key] = d.get(src_key, '')
            upsert_order(db, mapped, month=month)
            count += 1

    print(f"✅ 同步完成: 写入 {count} 条到 {DST_DB} (month={month})")
    return 0

if __name__ == '__main__':
    sys.exit(main())
