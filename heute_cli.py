#!/usr/bin/env python3
"""
货易达 CLI 工具
===============
用法:
  # 🔐 自动登录（推荐）
  python3 heute_cli.py login
  
  # 查看 Token 信息
  python3 heute_cli.py token info
  
  # 拉取订单列表
  python3 heute_cli.py list --start 2026-01-01 --end 2026-05-15
  
  # 按日统计
  python3 heute_cli.py stats --start 2026-04-01 --end 2026-05-15
  
  # 查单个订单详情
  python3 heute_cli.py detail 2605150906547151
  
  # 查国际运单物流轨迹（自动OCR验证码）
  python3 heute_cli.py track DEUHYD600169132940EU
  
  # 导出 CSV
  python3 heute_cli.py export --start 2026-04-01 --end 2026-05-15 -o orders.csv
"""

import sys
import os
import json
from datetime import datetime, timedelta

# Make sure SDK is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from heute_sdk import HeuteClient, ORDER_STATES, track_package

TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".heute_token")
CRED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".heute_cred")


def cmd_login():
    """自动登录（从凭证文件或交互式输入）"""
    creds = {}
    if os.path.exists(CRED_FILE):
        with open(CRED_FILE) as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    creds[k] = v
    
    username = creds.get("username") or input("用户名: ").strip()
    password = creds.get("password") or input("密码: ").strip()
    
    print("正在登录...")
    client = HeuteClient.login(username, password, token_file=TOKEN_FILE, save=True)
    info = HeuteClient.decode_token(client.token)
    print(f"✅ 登录成功! Token 有效期至 {info.get('exp_date', '?')}")
    
    # 保存凭证（仅用于下次自动登录）
    if not creds:
        with open(CRED_FILE, "w") as f:
            f.write(f"username={username}\n")
            f.write(f"password={password}\n")
        os.chmod(CRED_FILE, 0o600)
        print(f"🔑 凭证已保存 (权限600)")


def cmd_token(args):
    """管理 Token"""
    if not args:
        print("用法: token info")
        return
    
    if args[0] == "info":
        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE) as f:
                token = f.read().strip()
            info = HeuteClient.decode_token(token)
            print(f"Token: {token[:30]}...{token[-10:]}")
            print(f"过期: {info.get('exp_date', 'N/A')}")
            remaining = (datetime.fromisoformat(info['exp_date']) - datetime.now()).total_seconds() if 'exp_date' in info else 0
            if remaining > 0:
                print(f"剩余: {int(remaining//86400)}天{int((remaining%86400)//3600)}小时")
            else:
                print("⚠️ 已过期，请重新登录: python3 heute_cli.py login")
        else:
            print("❌ 未设置 Token")
            print("请运行: python3 heute_cli.py login")


def cmd_list(args):
    """拉取订单列表"""
    start, end = _parse_date_args(args)
    
    token = _load_token()
    client = HeuteClient(token=token)
    
    def progress(fetched, total, page):
        pct = fetched * 100 / total if total else 0
        print(f"\r  进度: {fetched}/{total} ({pct:.1f}%) - 第{page}页", end="", flush=True)
    
    print(f"拉取订单: {start} ~ {end}")
    orders = client.fetch_all_orders(start, end, progress_cb=progress)
    print()
    print(f"\n✅ 共 {len(orders)} 条订单")
    
    # 保存
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"orders_{start}_{end}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"count": len(orders), "items": orders}, f, ensure_ascii=False, indent=2)
    print(f"📁 已保存: {out}")


def cmd_detail(args):
    """查单个订单详情"""
    if not args:
        print("用法: detail <订单号>")
        return
    
    token = _load_token()
    client = HeuteClient(token=token)
    
    for sn in args:
        try:
            d = client.get_order_detail(sn)
            print(f"\n=== {sn} ===")
            print(f"  状态: {ORDER_STATES.get(d.get('state'), d.get('state'))}")
            print(f"  线路: {d.get('lineSettingCreateOrderAlias')}")
            print(f"  收件人: {d.get('consigneeName')} / {d.get('consigneeTel')}")
            print(f"  重量: {(d.get('weight') or 0)/1000:.3f} kg")
            print(f"  商品:")
            for p in d.get("orderDetails") or []:
                print(f"    - {p.get('goodsName')} x{p.get('num')}  "
                      f"¥{p.get('priceRMB',0)/100:.2f}/个")
            print(f"  费用: 预估¥{d.get('moneyEstimate',0)/100:.2f}  "
                  f"实际¥{d.get('moneyFinal',0)/100:.2f}")
            print(f"  物流: {d.get('globalWayBillSN', '')}")
        except Exception as e:
            print(f"  {sn}: ❌ {e}")


def cmd_stats(args):
    """按日统计"""
    start, end = _parse_date_args(args)
    
    token = _load_token()
    client = HeuteClient(token=token)
    
    def progress(fetched, total, page):
        pct = fetched * 100 / total if total else 0
        print(f"\r  拉取: {fetched}/{total} ({pct:.1f}%)", end="", flush=True)
    
    print(f"拉取数据: {start} ~ {end}")
    orders = client.fetch_all_orders(start, end, progress_cb=progress)
    print()
    
    # 按日统计
    from collections import Counter, defaultdict
    daily = Counter()
    line_counter = Counter()
    state_counter = Counter()
    total_weight = 0
    
    for o in orders:
        date = o.get("creationTime", "")[:10]
        daily[date] += 1
        line_counter[o.get("lineName", "未知")] += 1
        state_counter[ORDER_STATES.get(o.get("state"), f"状态{o.get('state')}")] += 1
        total_weight += (o.get("weight") or 0)
    
    print(f"\n=== 统计摘要 ({len(orders)} 条) ===")
    print(f"总重量: {total_weight/1000:.1f} kg")
    print()
    
    print("按线路:")
    for line, cnt in line_counter.most_common():
        print(f"  {line}: {cnt}")
    print()
    
    print("按状态:")
    for st, cnt in state_counter.most_common():
        print(f"  {st}: {cnt}")
    print()
    
    print("每日订单量 (前20):")
    for date in sorted(daily)[:20]:
        print(f"  {date}: {daily[date]}")


def cmd_export(args):
    """导出 CSV"""
    # Parse --start --end -o
    start = "2026-04-01"
    end = "2026-05-15"
    out = "heute_export.csv"
    include_detail = False
    
    i = 0
    while i < len(args):
        if args[i] == "--start" and i+1 < len(args):
            start = args[i+1]; i += 2
        elif args[i] == "--end" and i+1 < len(args):
            end = args[i+1]; i += 2
        elif args[i] == "-o" and i+1 < len(args):
            out = args[i+1]; i += 2
        elif args[i] == "--detail":
            include_detail = True; i += 1
        else:
            i += 1
    
    token = _load_token()
    client = HeuteClient(token=token)
    
    def progress(fetched, total, page):
        pct = fetched * 100 / total if total else 0
        print(f"\r  拉取: {fetched}/{total} ({pct:.1f}%)", end="", flush=True)
    
    print("拉取订单列表...")
    orders = client.fetch_all_orders(start, end, progress_cb=progress)
    print()
    
    if include_detail:
        print("拉取订单详情（这可能需要较长时间）...")
        sns = [o["sn"] for o in orders]
        details = client.fetch_order_details(sns, progress_cb=lambda i,t,s: 
            print(f"\r  详情: {i}/{t}", end="", flush=True))
        print()
        client.order_details_to_csv(list(details.values()), out)
    else:
        client.orders_to_csv(orders, out)
    
    print(f"✅ 已导出 {len(orders)} 条到 {out}")


def cmd_track(args):
    """查物流轨迹"""
    if not args:
        print("用法: track <国际运单号> [--verbose]")
        return
    
    verbose = "--verbose" in args or "-v" in args
    numbers = [a for a in args if not a.startswith("-")]
    
    for tracking_no in numbers:
        print(f"\n🔍 查询: {tracking_no}")
        result = track_package(tracking_no, verbose=verbose)
        
        if "error" in result:
            print(f"❌ {result['error']}")
            continue
        
        print(f"📦 物流公司: {result.get('logisticsCompany', '未知')}")
        print(f"🏷️  国内单号: {result.get('extTrackNoCn', '-')}")
        print(f"📌 当前状态: {result.get('currentStatus', '-')}")
        print()
        
        details = result.get("trackingDetails", [])
        if not details:
            print("  暂无轨迹数据")
            continue
        
        print(f"📋 轨迹 ({len(details)} 条):")
        for log in reversed(details):  # 正序显示（最早的先）
            print(f"  [{log['trackingTime']}] {log['statusName']}")
            print(f"    {log['trackingDesc']}")
            if log.get('address'):
                print(f"    📍 {log['address']}")
            if log.get('signerName'):
                print(f"    👤 签收: {log['signerName']} ({log.get('signerTypeDesc','')})")
            if log.get('contact'):
                print(f"    📞 联系人: {log['contact']} {log.get('contactPhone','')}")
            print()


# ─── 辅助 ──────────────────────────────────────────────────────────────────

def _load_token() -> str:
    auto_login = os.environ.get("HEUTE_AUTO_LOGIN", "").strip()
    
    if auto_login:
        # 从环境变量自动登录（适合 cron 任务）
        parts = auto_login.split(":", 1)
        if len(parts) == 2:
            client = HeuteClient.login(parts[0], parts[1], token_file=TOKEN_FILE, save=True)
            return client.token
    
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            token = f.read().strip()
            if token:
                # 检查是否过期
                info = HeuteClient.decode_token(token)
                exp = info.get("exp_date")
                if exp:
                    exp_dt = datetime.fromisoformat(exp)
                    if exp_dt.tzinfo:
                        if exp_dt > datetime.now(exp_dt.tzinfo):
                            return token
                    else:
                        if exp_dt > datetime.now():
                            return token
                # Token 过期了，尝试自动重新登录
                if os.path.exists(CRED_FILE):
                    creds = {}
                    with open(CRED_FILE) as f:
                        for line in f:
                            if "=" in line:
                                k, v = line.strip().split("=", 1)
                                creds[k] = v
                    if "username" in creds and "password" in creds:
                        client = HeuteClient.login(creds["username"], creds["password"],
                                                    token_file=TOKEN_FILE, save=True)
                        return client.token
    
    print("❌ Token 未设置或已过期")
    print("请运行: python3 heute_cli.py login")
    sys.exit(1)

def _parse_date_args(args) -> tuple:
    start = "2026-04-01"
    end = "2026-05-15"
    for a in args:
        if a.startswith("--start="):
            start = a.split("=", 1)[1]
        elif a.startswith("--end="):
            end = a.split("=", 1)[1]
    return start, end


# ─── 入口 ──────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    
    cmd = sys.argv[1]
    args = sys.argv[2:]
    
    commands = {
        "login": lambda args: cmd_login(),
        "token": cmd_token,
        "list": cmd_list,
        "detail": cmd_detail,
        "stats": cmd_stats,
        "export": cmd_export,
        "track": cmd_track,
    }
    
    if cmd in commands:
        commands[cmd](args)
    else:
        print(f"未知命令: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
