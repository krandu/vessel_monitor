# ⚓ 渔船靠港监控 (Vessel Monitor)

基于 GitHub Actions 与 Python 的自动化渔船位置监控系统。定时抓取渔船 AIS/位置数据，并在状态发生变化或靠港时通过 Telegram Bot 发送实时通知。

---

## 🌟 功能特性

* **⏰ 定时自动巡检**：利用 GitHub Actions 每小时自动运行一次，无需自备服务器。
* **📱 Telegram 实时推送**：集成 Telegram Bot，关键变更即时通知到手机。
* **💾 状态持久化**：自动将最新的状态更新并提交至 `vessel_state.json`，实现极轻量的数据持久化与历史比对。
* **⚡ 零成本部署**：完全基于 GitHub 免费额度运行。

---

## 📂 项目结构

```text
├── .github/workflows/
│   └── vessel_monitor.yml   # GitHub Actions 自动化工作流配置文件
├── vessel_monitor.py        # 核心监控逻辑脚本
├── vessel_state.json        # 渔船状态记录文件（由脚本自动更新）
└── README.md

⚙️ 配置说明
在使用此项目前，需要配置 Telegram Bot 的凭据以接收推送。

1. 获取 Telegram 配置
在 Telegram 中联系 @BotFather 创建一个新的 Bot，并获取 TELEGRAM_BOT_TOKEN。

联系 @userinfobot 获取你的 TELEGRAM_CHAT_ID（支持个人 Chat ID 或群组 ID）。

2. 配置 GitHub Secrets
进入本 GitHub 仓库的 Settings -> Secrets and variables -> Actions。

点击 New repository secret，依次添加以下两个变量：

TELEGRAM_BOT_TOKEN: 你的 Telegram Bot Token

TELEGRAM_CHAT_ID: 你的 Telegram Chat ID

3. 配置仓库写入权限（重要 ⚠️）
为了让 GitHub Actions 能够自动保存更新后的 vessel_state.json：

进入仓库 Settings -> Actions -> General。

拉至底部 Workflow permissions，选择 Read and write permissions。

点击 Save 保存。

🚀 本地运行与测试
如果你想在本地开发或测试 Python 脚本：

克隆仓库

Bash
git clone [https://github.com/krandu/vessel_monitor.git](https://github.com/krandu/vessel_monitor.git)
cd vessel_monitor
安装依赖

Bash
pip install requests
设置环境变量并运行

Linux / macOS:

Bash
export TELEGRAM_BOT_TOKEN="你的Token"
export TELEGRAM_CHAT_ID="你的ChatID"
python vessel_monitor.py
Windows (PowerShell):

PowerShell
$env:TELEGRAM_BOT_TOKEN="你的Token"
$env:TELEGRAM_CHAT_ID="你的ChatID"
python vessel_monitor.py
🤝 许可证
本项目采用 MIT License 许可证。
