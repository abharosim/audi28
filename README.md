# audi28

本机的 agent 会话层和统一审批中心。你跟 Audi（Grok Bot）说话，Audi 做 routing，把该交给 coding agent 的任务转到这台 Mac 上的 DeepSeek Harness，并维持 session。DeepSeek 要提问或请求批准时，Audi 会告诉你，并且明确说是 DeepSeek 在问。

第一阶段只接通 DeepSeek Harness。Claude Code（HTTP `PermissionRequest` hook）和 Codex（command hook + curl）会接到同一个审批中心，但留到后面实现。

完整设计见 [`docs/DESIGN.md`](docs/DESIGN.md)。调研原文（2026-11-18 通话，DaDa 整理）在 [`docs/sources/approval-hub-harness-interfaces.md`](docs/sources/approval-hub-harness-interfaces.md)。

## 和 audi27 的关系

audi28 是独立项目，放在 `~/Code/audi28`，和 `~/Code/audi27` 并列。它复用 audi27 里已经跑通的做法（Python + uv、`config.toml`、JSONL 事件、supervisor、只监听 `127.0.0.1` 的仪表盘），但进程、端口、数据目录都不共用。仪表盘默认是 `http://127.0.0.1:8728`。以后 audi27 可以用文件 inbox 或本地 CLI 把语音任务交给 audi28，第一阶段不搭这座桥。

## 现状

仓库里已经有骨架、传输、规则路由、session 存储，以及 DeepSeek adapter。CLI 有 `events`、`supervise`、`web`、`doctor`、`grokbot test`、`sessions list`、`sessions close`。`start.sh` 在 `[deepseek] enabled = true` 时会加上 `--extra deepseek`。没有 `DEEPSEEK_API_KEY` 时不会连接 DeepSeek，也不会崩溃。审批中心的仪表盘还没做。没有说要新任务时，继续最近一个仍开放的 session。`不是新任务` 和 `接着上一个` 不会另开 session。

```bash
uv sync --extra dev
cp config.example.toml config.toml
# .env 里以后放 AUDI28_GROKBOT_WEBHOOK_URL、AUDI28_GROKBOT_WEBHOOK_KEY、DEEPSEEK_API_KEY
# 密钥还没提供也可以。doctor 会说明怎么补，不会崩溃。
uv run pytest
uv run audi28 doctor
./start.sh
# 仪表盘：http://127.0.0.1:8728
```

传输：Audi 不能访问本机 HTTP。任务和审批决定写成 `data/inbox/audi/*.json`。Ruoran 已批准 Audi 把 JSON 写入 `~/Code/audi28/data/inbox/audi/`（目录不存在可以建），其它 Mac 写入仍要单独批准。同一 `message_id` 只处理一次。坏 JSON、symlink 和超过 1MB 的文件会进 `quarantine/`。audi28 把要发给 Audi 的 JSON 先写入 `data/outbox/grokbot/`，再 POST 到 `AUDI28_GROKBOT_WEBHOOK_URL`。失败会留下，重启后续发。`uv run audi28 grokbot test` 发一条测试消息。CI 只用本机假 HTTP server。

DeepSeek 的默认工作区是 `~/Code/deepseek-workspace`（可在 `config.toml` 里改），权限预设是 `workspace-write`。工具调用仍要审批。用户没有明确说要新任务时，继续最近一个仍开放的 session。审批和提问没有超时自动拒绝，一直等到回答。

## 许可证与密钥

不要把 API key 或 webhook 密钥写进 `config.toml` 或仓库。用项目根目录的 `.env`（gitignore），或导出到环境变量。`DEEPSEEK_API_KEY` 由 Ruoran 稍后提供。没有这把密钥时，进程不崩溃；`audi28 doctor` 会说明把密钥写进 `~/Code/audi28/.env` 或环境变量。
