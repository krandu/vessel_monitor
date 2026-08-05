# 粤珠渔养20003 靠港监控

自动监控渔船 **粤珠渔养20003**（MMSI: 412536814）进出洪湾渔港，通过 Telegram 推送通知。

## 数据源

按优先级自动降级：

| 优先级 | 数据源 | 延迟 | 说明 |
|---|---|---|---|
| 1 | 船讯网 API | ~2 分钟 | 中国近海覆盖最佳，免费 50次/月 |
| 2 | shipinfo.net API | ~14 小时 | 延迟较高，接受 ≤20 小时数据 |
| 3 | shipinfo.net 网页 | ~15 小时 | 兜底 |

## 运行频率

**仅工作日运行**，周末静默，每天两次：

| 北京时间 | UTC | 说明 |
|---|---|---|
| 08:00 | 00:00 | 上午上班前 |
| 14:00 | 06:00 | 下午上班前 |

> GitHub Actions 定时任务存在排队延迟，实际执行时间可能晚 15 分钟至 1 小时。

## 部署步骤

### 1. 推送代码到 GitHub

```bash
git init && git add . && git commit -m "init"
git remote add origin https://github.com/你的用户名/vessel-monitor.git
git push -u origin main
```

### 2. 配置 Telegram Bot

1. 找 [@BotFather](https://t.me/BotFather) 新建 Bot，获取 `BOT_TOKEN`
2. 找 [@userinfobot](https://t.me/userinfobot) 获取你的 `CHAT_ID`

### 3. 配置 GitHub Secrets

仓库 → **Settings → Secrets and variables → Actions** → 新建：

| Secret | 说明 | 必填 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather 给的 token | ✅ |
| `TELEGRAM_CHAT_ID` | 你的 chat ID | ✅ |
| `SHIPXY_API_KEY` | 船讯网 API key（[api.shipxy.com](https://api.shipxy.com) 注册，免费 50次/月） | 推荐 |

### 4. 手动触发首次运行

仓库 → **Actions → Vessel Monitor → Run workflow**

首次运行只记录初始状态，不推送通知。

## 监控逻辑

- **靠港判定**：距洪湾渔港中心（22.178°N, 113.437°E）≤1.5km 且船速 ≤1节
- **防重推**：按数据源分别记录 AIS 时间戳，shipinfo 延迟数据不会重复触发
- **连续失败告警**：所有数据源连续 6 次失败时推送告警
- **状态持久化**：通过 GitHub Actions Cache 保存，不提交到仓库

## 通知示例

```
⚓ 粤珠渔养20003 已返回洪湾渔港

📍 位置: 22.1826°N, 113.4318°E
📏 距港心: 0.74 km
⚡ 速度: 0.0 节
🕐 AIS数据: 8 分钟前
📡 数据源: shipxy
🔗 查看位置
```

```
🌊 粤珠渔养20003 已出海

📍 位置: 22.1650°N, 113.5148°E
📏 距洪湾渔港: 15.23 km
⚡ 速度: 6.2 节
🕐 AIS数据: 5 分钟前
📡 数据源: shipxy
```

## 注意事项

- 船讯网免费额度 **50次/月**，按每天 2 次、每月 21 个工作日计算约用 42 次，在额度内
- 船讯网到期后自动降级到 shipinfo，无需手动处理，通知会正常发送但延迟增加
- 手动触发默认启用船讯网，可在触发界面选择关闭以节省额度
