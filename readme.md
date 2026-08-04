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