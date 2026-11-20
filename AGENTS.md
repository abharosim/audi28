# 写代码时的规则（for coding agents）

audi28 是本机的 agent 会话层和统一审批中心。用户通过 Audi（Grok Bot）下达任务；本进程在 Mac 上与 DeepSeek Harness 对话，并把审批/提问转回去。设计以 [`docs/DESIGN.md`](docs/DESIGN.md) 为准。还没写到的行为，先改设计再写代码。

## 项目习惯

- Python 3.10+，用 uv。依赖写在 `pyproject.toml`。实现之后：`uv sync --extra dev`，测试用 `uv run pytest`。
- 需要 DeepSeek 运行时的 extra 建议叫 `deepseek`。CI 里默认不要强行下载完整 runtime；用 stub 测协议。本地跑真实 Harness 再 `uv sync --extra dev --extra deepseek`。
- 配置是 `config.toml`。新加 section 必须同步 `config.example.toml` 和加载器。未知 section 要报错，不要静默忽略。
- 密钥只来自环境变量或项目根 `.env`（gitignore）：`AUDI28_GROKBOT_WEBHOOK_URL`、`AUDI28_GROKBOT_WEBHOOK_KEY`、`DEEPSEEK_API_KEY`。不要写进 `config.toml`、事件、日志或 webhook 重试信息。
- 模块之间只通过 `data/events/YYYY-MM-DD.jsonl` 和明确的函数接口通信。事件固定字段：`id`、`ts`（带时区的 ISO 8601）、`type`、`source`、`ref`、`data`。不认识的 `type` 也要原样保存，读的时候不要拒绝。
- 几个进程可以同时追加事件。追加要排他锁并 fsync；读者跳过写了一半的最后一行。
- 仪表盘和以后的 hook HTTP 只绑 `127.0.0.1`，默认端口 8728，不要占用 audi27 的 8727。
- 这是独立服务。不要 import audi27 的包，不要写 audi27 的 `data/`。需要对接时走 inbox 文件或 CLI。

## DeepSeek

- 用官方 Python SDK `deepseek-harness-sdk`，不要用 TypeScript 的 `client.onRequest` 签名。Python 侧是 `approval_requests=True`，并且必须用独立线程/循环调用 `client.next_request()`，与 `run()` 并发，再用 `client.respond(...)` 回答。用户提问走 `on_user_question`。
- 一次允许只映射为 `allowed-once`，拒绝为 `rejected`，取消为 `cancelled`。不要实现「永久允许」。
- 显式设置 `dsh_home`（`data/dsh-home`）。SDK 不会去发现 `~/.dsh`。
- `resume_session` 失败时不要静默另建 session。它也不会恢复 shell / browser 等外部进程。
- 协议没有 session-close / 取消 turn 的 RPC。不要假装已经中止了正在跑的工具调用。

## 审批与文案

- 三个工具（DeepSeek、Claude Code、Codex）共用规范化的 `ApprovalRequest` / `ApprovalDecision`。新 adapter 只做映射，不另做一套状态机。
- Phase 1 不对写文件、shell、装包、git、工作区以外的路径做 auto-approve。pending 的审批和提问一直等到用户回答，不因为超时自动拒绝；重启后仍在，可以再次提醒，仪表盘上看得到。
- 发给用户的 `spoken_text` 必须带来源，例如「DeepSeek 在问你：…」。口述不要包含路径、URL 或 token。

## 传输

- Audi 不能访问本机 HTTP。任务从 `data/inbox/audi/` 进，结果从 webhook 出。不要设计「让云端去打 127.0.0.1」。
- inbox 不跟随 symlink；超过 1MB 或无法解析的文件在有限次重试后进入 `quarantine/`。同一 `message_id` / `idempotency_key` 只处理一次。领取用原子 rename。
- 出站 webhook 先写入 `data/outbox/grokbot/`，再 POST。flush 持有排他锁。网络错误或超时在同一次发送里按短退避重试一次；失败留在 outbox，重启后续发。别的活进程认领中的 `inflight/` 文件不能抢走。事件和日志里不能出现 webhook URL 或 key。
- 路由先看消息里的 `session_id`。否则只有明确的新任务说法才新开；`不是新任务` 和 `接着上一个` 继续上一个。没有仍开放的 session 才新开。决定要写 `reason`。
- 云端助手往 Mac 写文件，除了用户已批准的 inbox Allow rule 之外，每一次都要单独批准。不要在实现阶段扩大写入范围。

## 测试与 PR

- 先写验收测试再写实现，尤其是路由规则、session 恢复、审批幂等、pending 跨重启仍在且不自动拒绝、inbox 隔离。
- 每个 PR 保持可独立合并、可测试。骨架、传输、session、DeepSeek adapter、审批中心不要塞进同一个 PR。
- 不把真实 API key、真实 webhook URL、真实用户对话提交进仓库。
- 改动行为时同步更新 `README.md`、`AGENTS.md` 和 `config.example.toml`。
