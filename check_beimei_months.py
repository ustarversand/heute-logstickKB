"""查 BEIMEI-DE 各月订单分布"""
import sqlite3

db = sqlite3.connect('/opt/data/workspace/heute_express/data/heute.db')
c = db.cursor()

# 按月份统计
rows = c.execute("""
    SELECT month, COUNT(*) as cnt
    FROM orders
    WHERE sender_name='BEIMEI-DE'
    GROUP BY month
    ORDER BY month
""").fetchall()

print("BEIMEI-DE 各月订单:")
for r in rows:
    print(f"  {r[0]}: {r[1]}单")

print()

# 看下 BEIMEI-DE 最早和最晚的订单
rows2 = c.execute("""
    SELECT MIN(creation_time), MAX(creation_time), COUNT(*)
    FROM orders
    WHERE sender_name='BEIMEI-DE'
""").fetchone()
print(f"时间范围: {rows2[0]} ~ {rows2[1]}")
print(f"总单数: {rows2[2]}")
print()

# 看看 month 字段有哪些值
months = c.execute("SELECT DISTINCT month FROM orders ORDER BY month").fetchall()
print(f"数据库内 month 值: {[m[0] for m in months]}")

db.close()
