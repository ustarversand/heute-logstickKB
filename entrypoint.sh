#!/bin/bash
# ===================================================
# 货易达物流看板 — 容器入口脚本
# ===================================================
# 功能: 启动定时同步(cron) + 看板(FastAPI)
# ===================================================
set -e

echo "╔═══════════════════════════════════════════════╗"
echo "║       货易达物流看板  v2.0                    ║"
echo "╚═══════════════════════════════════════════════╝"

# ── 加载环境变量 ──
if [ -f /app/.env ]; then
    set -a
    source /app/.env
    set +a
    echo "✅ 已加载 .env 配置"
fi

# ── 初始化数据库 ──
mkdir -p /app/data
python3 /app/heute_db.py 2>/dev/null || true

# ── 设置定时同步（cron） ──
cat > /tmp/heute-cron << 'CRON'
# 货易达同步: 每天 2:00 和 14:00
0 2 * * * cd /app && python3 /app/heute_express_sync.py sync --days 3 >> /app/data/sync.log 2>&1 && python3 /app/sync_to_dashboard.py >> /app/data/sync.log 2>&1
0 14 * * * cd /app && python3 /app/heute_express_sync.py sync --days 3 >> /app/data/sync.log 2>&1 && python3 /app/sync_to_dashboard.py >> /app/data/sync.log 2>&1
CRON
crontab /tmp/heute-cron
cron
echo "✅ 定时同步已启动 (每天 02:00 / 14:00)"

# ── 启动看板 ──
PORT="${DASHBOARD_PORT:-8890}"
WORKERS="${DASHBOARD_WORKERS:-4}"
echo "📊 启动看板 → http://0.0.0.0:${PORT}  (workers=${WORKERS})"
echo ""

cd /app
exec python3 -m uvicorn dashboard.app:app --host 0.0.0.0 --port ${PORT} --workers ${WORKERS}
