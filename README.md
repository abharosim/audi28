# audi28

本机的 agent 会话层和统一审批中心。你跟 Audi（Grok Bot）说话，Audi 做 routing，把该交给 coding agent 的任务转到这台 Mac 上的 DeepSeek Harness，并维持 session。DeepSeek 要提问或请求批准时，Audi 会告诉你，并且明确说是 DeepSeek 在问。

第一阶段只接通 DeepSeek Harness。Claude Code（HTTP `PermissionRequest` hook）和 Codex（command hook + curl）会接到同一个审批中心，但留到后面实现。

完整设计见 [`docs/DESIGN.md`](docs/DESIGN.md)。调研原文（2026-11-18 通话，DaDa 整理）在 [`docs/sources/approval-hub-harness-interfaces.md`](docs/sources/approval-hub-harness-interfaces.md)。

## 和 audi27 的关系

audi28 是独立项目，放在 `~/Code/audi28`，和 `~/Code/audi27` 并列。它复用 audi27 里已经跑通的做法（Python + uv、`config.toml`、JSONL 事件、supervisor、只监听 `127.0.0.1` 的仪表盘），但进程、端口、数据目录都不共用。仪表盘默认是 `http://127.0.0.1:8728`。以后 audi27 可以用文件 inbox 或本地 CLI 把语音任务交给 audi28，第一阶段不搭这座桥。

## 现状

目前只有设计文档，还没有实现。实现会按 `docs/DESIGN.md` 里的 PR 步骤进行。

计划中的启动方式（实现之后）：

```bash
cp config.example.toml config.toml
# 在 .env 里放 AUDI28_GROKBOT_WEBHOOK_URL、AUDI28_GROKBOT_WEBHOOK_KEY、DEEPSEEK_API_KEY
./start.sh
# 仪表盘：http://127.0.0.1:8728
# 重启：touch data/restart.request
```

传输：Audi 不能访问本机 HTTP。任务和审批决定写成 `data/inbox/audi/*.json`；audi28 用 webhook 把结果和「DeepSeek 在问你：…」推回 Audi。这条路径需要一条只覆盖该 inbox 目录的 Allow rule，见设计文档。

## 许可证与密钥

不要把 API key 或 webhook 密钥写进 `config.toml` 或仓库。用项目根目录的 `.env`（gitignore）。
