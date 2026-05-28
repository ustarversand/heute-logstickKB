# 货易达物流看板

货易达(heute-express.com)订单管理与物流轨迹追踪系统 — Docker 一键部署。

## 功能

- 📦 **订单同步** — 自动从货易达拉取订单（每天 02:00 / 14:00）
- 📍 **轨迹查询** — 批量查询国际物流轨迹（8线程并发）
- 📊 **数据看板** — 状态分布、签收统计、周趋势图
- 🔍 **售后管理** — 售后登记、状态追踪
- 👥 **多寄件人** — 每个寄件人独立登录查看自己的订单
- 🚨 **异常检测** — 国际断更、超时未入库自动标记
- 📋 **一键更新** — 批量查询所有未签收订单的物流状态

## 快速开始

### 1. 准备

- Docker 和 Docker Compose
- 货易达账号（USTAR）

### 2. 部署

```bash
# 下载项目
git clone https://github.com/ustarversand/ESYGJST.git
cd ESYGJST

# 配置账号
cp .env.example .env
# 编辑 .env 填入货易达密码

# 构建并启动
docker compose up -d

# 查看日志
docker compose logs -f
```

### 3. 访问

打开浏览器：http://服务器IP:8890

| 账号 | 密码 | 权限 |
|------|------|------|
| `admin` | `123456` | 管理员（查看全部） |
| 各寄件人名称 | `123456` | 仅看自己的订单 |

> ⚠️ 首次启动后请登录 `admin` 账号，系统会自动创建所有寄件人账户。
> 建议首次登录后修改 admin 密码。

## 架构

```
                     ┌──────────────────────┐
                     │   货易达 API          │
                     │  heute-express.com    │
                     └──────────┬───────────┘
                                │
                    ┌───────────▼───────────┐
                    │ 同步引擎 (cron)        │
                    │ 每天 02:00 / 14:00     │
                    └───────────┬───────────┘
                                │
                    ┌───────────▼───────────┐
                    │  SQLite 数据库         │
                    │  /app/data/           │
                    └───────────┬───────────┘
                                │
                    ┌───────────▼───────────┐
                    │  FastAPI 看板         │
                    │  端口 8890            │
                    └──────────────────────┘
```

## 数据持久化

所有数据存储在 `./data/` 目录（Docker 卷），包括：
- `heute_express_ustar.db` — USTAR 订单
- `heute_express_mscj.db` — MSCJ 订单
- `heute.db` — 看板数据库
- `users.json` — 用户账户
- `sync.log` — 同步日志

## 常用命令

```bash
# 手动同步
docker exec ustar-heute-dashboard python3 /app/heute_express_sync.py sync --days 3

# 查看同步日志
docker exec ustar-heute-dashboard tail -f /app/data/sync.log

# 一键查询所有轨迹
# 在网页上点击「一键更新」按钮

# 查看容器日志
docker compose logs -f
```

## 技术栈

- **后端**: Python 3.11 + FastAPI + Uvicorn
- **前端**: Vue 3 (CDN) + HTML/CSS
- **数据库**: SQLite
- **部署**: Docker + Docker Compose
