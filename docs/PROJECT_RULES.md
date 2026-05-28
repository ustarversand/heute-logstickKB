# 货易达物流看板 — 项目规则

## 🚫 禁止使用的第三方物流接口

**绝对禁止调用以下第三方物流查询接口：**

| 接口 | 禁止原因 |
|------|---------|
| 快递100 (kuaidi100.com) ❌ | 任何 kuaidi100 API（公共/企业版）均不得使用 |
| 快递鸟 (kdniao.com) ❌ | 仅限货易达官方数据 |
| 其他物流轨迹聚合平台 ❌ | 任何非货易达官方渠道 |

## ✅ 唯一允许的物流数据来源

**只能使用货易达官方物流轨迹**，通过以下方式获取：

1. **货易达 Track API** (`track.heute-express.com`)
   - 通过 `HeuteAPI.track.query()` 查询
   - 返回 `currentStatus`, `latestDesc`, `latestTime`, `extTrackNoCn`
   - 注意：`trackingDetails` 字段在货易达API中始终为空数组 `[]`

2. **货易达订单API** (`www.heute-express.com`)
   - 通过 `HeuteAPI.order.list()` / `.detail()` 获取订单数据
   - 同步订单状态（state值：0=已作废, 1=待支付, 2=待入库, 3=国际运输, 4=国内配送, 5=签收, -6=已撤销）

3. **货易达物流轨迹SDK** (`track.heute-express.com` with CAPTCHA) ✅
   - 通过 `heute_sdk.track_package()` 查询（**推荐用于详情弹窗时间线**）
   - 需要验证码OCR（tesseract-ocr），耗时约3-10秒
   - 返回完整 `trackingDetails`（含多条轨迹事件，时间倒序）
   - 已集成到 `query-live` 端点：先快速Track API，再调SDK补全时间线

## 📌 核心原则

- 所有物流轨迹信息必须源自货易达官方系统
- 不要引入任何第三方物流查询中间件
- 国内段轨迹信息（extTrackNoCn 如顺丰/京东单号）仅供显示，不通过第三方接口查询其详细轨迹
- 如果货易达API不返回 `trackingDetails`，则轨迹时间线显示"暂无轨迹数据"，不通过其他渠道补全

## 🔧 实现指引

### 详情弹窗的轨迹时间线

当前货易达 Track API 不返回 `trackingDetails`，所以详情弹窗的物流轨迹区域显示"尚无轨迹数据"+ 查询轨迹按钮是正常行为。

如需展示完整轨迹，应通过 `heute_sdk.track_package()`（货易达官方SDK，含验证码OCR）获取，而不是任何第三方API。

### 轨迹状态分类

轨迹状态从货易达API的 `currentStatus` 获取，分类规则：
- `已签收/签收(国内确认)/签收` → 已签收
- `清关中/已清关` → 清关中
- `运单已经创建` → 待入库（运单已创建但物流未开始）
- `在途/离开/已揽收/快件航班批次已创建` → 国际运输
- `到达/转寄` → 国内配送
- `问题件` → 问题件
