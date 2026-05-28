#!/usr/bin/env python3
"""从Hermes本地DB提取4月数据，SSH pipe到容器插入"""
import sqlite3
import json
import subprocess
import sys

HERMES_DB = '/opt/data/workspace/heute_express/data/heute.db'

def extract_apr_data():
    conn = sqlite3.connect(HERMES_DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    orders = c.execute("SELECT * FROM orders WHERE month='apr'").fetchall()
    tracking = c.execute("SELECT * FROM tracking WHERE month='apr'").fetchall()
    conn.close()
    print(f"提取: {len(orders)} 订单, {len(tracking)} 轨迹", file=sys.stderr)
    return [dict(o) for o in orders], [dict(t) for t in tracking]

def make_insert_sql(orders, trackings):
    """生成INSERT OR IGNORE语句，写入到标准输出"""
    # Orders insert
    for o in orders:
        cols = ', '.join(o.keys())
        vals = ', '.join('?' for _ in o)
        print(f"INSERT OR IGNORE INTO orders ({cols}) VALUES ({vals});")
    
    # Tracking insert  
    for t in trackings:
        cols = ', '.join(t.keys())
        vals = ', '.join('?' for _ in t)
        print(f"INSERT OR IGNORE INTO tracking ({cols}) VALUES ({vals});")

if __name__ == '__main__':
    orders, trackings = extract_apr_data()
    make_insert_sql(orders, trackings)
