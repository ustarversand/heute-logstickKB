#!/usr/bin/env python3
"""删除heute.db中MSCJ账号的数据"""
import sqlite3, shutil, os

DATA = "/opt/data/workspace/heute_express/data"
src = DATA + "/heute.db"
bak = src + ".bak_before_cleanup"
if not os.path.exists(bak):
    shutil.copy2(src, bak)
    print(f"备份: {bak} ({os.path.getsize(bak)} bytes)")
else:
    print(f"备份已存在: {bak}")

db_m = sqlite3.connect(DATA + "/heute_express_mscj.db")
mscj_sns = set(r[0] for r in db_m.execute("SELECT sn FROM orders").fetchall())
db_m.close()
print(f"MSCJ单号: {len(mscj_sns)}个")

db_h = sqlite3.connect(src)
before = db_h.execute("SELECT COUNT(*) FROM orders WHERE month='may'").fetchone()[0]
print(f"删除前5月订单: {before}")

removed = 0
track_removed = 0
chunks = list(mscj_sns)
for i in range(0, len(chunks), 200):
    batch = chunks[i:i+200]
    ph = ",".join(["?" for _ in batch])
    
    # 先删tracking（查这些单号的global_waybill_sn）
    gws_rows = db_h.execute(
        f"SELECT global_waybill_sn FROM orders WHERE month='may' AND sn IN ({ph})",
        tuple(batch)
    ).fetchall()
    gws_list = [r[0] for r in gws_rows if r[0]]
    if gws_list:
        ph2 = ",".join(["?" for _ in gws_list])
        db_h.execute(
            f"DELETE FROM tracking WHERE month='may' AND tracking_no IN ({ph2})",
            tuple(gws_list)
        )
        track_removed += len(gws_list)
    
    # 删orders
    r = db_h.execute(
        f"DELETE FROM orders WHERE month='may' AND sn IN ({ph})",
        tuple(batch)
    ).rowcount
    removed += r

after = db_h.execute("SELECT COUNT(*) FROM orders WHERE month='may'").fetchone()[0]
print(f"删除后5月订单: {after}")
print(f"净减少: {before - after}")
print(f"同时删除tracking: {track_removed}条")
db_h.commit()
db_h.close()
print("完成!")
