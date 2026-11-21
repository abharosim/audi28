# audi28

本机的 agent 会话层和统一审批中心。你跟 Audi（Grok Bot）说话，Audi 做 routing，把该交给 coding agent 的任务转到这台 Mac 上的 DeepSeek Harness，并维持 session。DeepSeek 要提问或请求批准时，Audi 会告诉你，并且明确说是 DeepSeek 在问。

第一阶段只接通 DeepSeek Harness。Claude Code（HTTP `PermissionRequest` hook）和 Codex（command hook + curl）会接到同一个审批中心，但留到后面实现。

完整设计见 [`docs/DESIGN.md`](docs/DESIGN.md)。调研原文（2026-11-18 通话，DaDa 整理）在 [`docs/sources/approval-hub-harness-interfaces.md`](docs/sources/approval-hub-harness-interfaces.md)。

## 和 audi27 的关系

audi28 是独立项目，放在 `~/Code/audi28`，和 `~/Code/audi27` 并列。它复用 audi27 里已经跑通的做法（Python + uv、`config.toml`、JSONL 事件、supervisor、只监听 `127.0.0.1` 的仪表盘），但进程、端口、数据目录都不共用。仪表盘默认是 `http://127.0.0.1:8728`。以后 audi27 可以用文件 inbox 或本地 CLI 把语音任务交给 audi28，第一阶段不搭这座桥。

## 现状

仓库里已经有骨架、传输、规则路由、session 存储、DeepSeek adapter、审批中心、本机仪表盘，以及 `audi28 doctor`。CLI 还有 `events`、`supervise`、`web`、`grokbot test`、`sessions list`、`sessions close`、`approvals list`、`approvals decide`。`start.sh` 在 `[deepseek] enabled = true` 时会加上 `--extra deepseek`。没有 `DEEPSEEK_API_KEY` 时不会连接 DeepSeek，也不会崩溃。没有说要新任务时，继续最近一个仍开放的 session。`不是新任务` 和 `接着上一个` 不会另开 session。仪表盘除 `/health` 外需要 `AUDI28_DASHBOARD_TOKEN`。

## 第一次跑

```bash
uv sync --extra dev --extra deepseek
cp config.example.toml config.toml
```

在项目根目录建 `.env`（这个文件已在 `.gitignore` 里，不要提交）。四个变量是：

```bash
DEEPSEEK_API_KEY=sk-...
AUDI28_GROKBOT_WEBHOOK_URL=https://...
AUDI28_GROKBOT_WEBHOOK_KEY=...
AUDI28_DASHBOARD_TOKEN=...
```

```bash
uv run audi28 doctor
./start.sh
```

改完代码后，在项目里执行 `touch data/restart.request`，supervisor 会自己停掉再拉起来，不用在原来的终端按 Ctrl+C。`pyproject.toml` 有变化时，它会先跑 `uv sync`；`[deepseek] enabled = true` 时加上 `--extra deepseek`。找不到 `uv` 或 sync 失败会记一条 error 事件，服务仍会重新拉起。

`doctor` 每一项打 `PASS`、`INFO`、`WARN` 或 `FAIL`。`INFO` 只是说明，没有修法，也不让退出码变成非 0。有 `FAIL` 时退出码不是 0，并给一行可以直接复制的修法。它只说密钥有没有设置、长度是多少，不打印值。`audi28 doctor --json` 打同样的结果。沙箱默认关闭时，这一项是 `INFO`。

浏览器打开 `http://127.0.0.1:8728/?token=你的token`。登录成功后会跳回不带 token 的地址，之后靠 cookie。

给 DeepSeek 发一条测试任务，把 JSON 写进 `data/inbox/audi/`（文件名随意，比如 `msg-01.json`）：

```json
{
  "kind": "task",
  "message_id": "msg-01",
  "session_id": null,
  "target": null,
  "text": "在仓库里跑测试。"
}
```

没有真实密钥时，可以用进程内的假 DeepSeek 把 inbox、session 和 outbox 跑通。它不访问网络：

```bash
uv run python scripts/mock_deepseek.py
```

传输：Audi 不能访问本机 HTTP。任务和审批决定写成 `data/inbox/audi/*.json`。Ruoran 已批准 Audi 把 JSON 写入 `~/Code/audi28/data/inbox/audi/`（目录不存在可以建），其它 Mac 写入仍要单独批准。同一 `message_id` 只处理一次。坏 JSON、symlink 和超过 1MB 的文件会进 `quarantine/`。audi28 把要发给 Audi 的 JSON 先写入 `data/outbox/grokbot/`，再 POST 到 `AUDI28_GROKBOT_WEBHOOK_URL`。失败会留下，重启后续发。`uv run audi28 grokbot test` 发一条测试消息。CI 只用本机假 HTTP server。

DeepSeek 的默认工作区是 `~/Code/deepseek-workspace`（可在 `config.toml` 里改），权限预设是 `workspace-write`。新 session 的文件放在 `<workspace>/<YYYY-MM-DD>/<会话目录>/`。日期是这条 session 创建时刻在 America/New_York 的本地日期。目录名来自任务正文的短 ASCII slug；没有可用 ASCII 时用 `task-HHMM`；同一天重名就加 `-2`、`-3`。相对路径记在 session 的 `workdir` 里。继续同一个 session 时，过了几天也还用原来的目录。没有 `workdir` 的旧 session 留在工作区根目录，不自动搬文件，下一次任务会记一条警告。运行时 initialize 的 cwd 仍是工作区根，这样 `workspace-write` 覆盖所有日期目录。SDK 有 `set_working_directory` 时，每个会话会指到自己的绝对路径；新会话的第一条提示，以及恢复会话的那一条，会说明只在这个目录里创建和修改文件。工具调用仍要审批。用户没有明确说要新任务时，继续最近一个仍开放的 session。审批和提问没有超时自动拒绝，一直等到回答。提示在会话自己的线程里跑，传输循环继续收 inbox、发 webhook。backend session id 在 `start_session()` 返回后、第一轮提示开始前就写入 session 文件。进程收到 SIGINT 或 SIGTERM，或者传输循环退出时，会关掉 DeepSeek 运行时，避免上次的 `dsh` 占着 session。

DeepSeek 的 macOS 沙箱默认关闭，所有平台都一样。在 `config.toml` 里把 `[deepseek.sandbox] enabled` 设为 `true` 之后，`dsh` 才会套进 `sandbox-exec`。SDK 0.2.2rc2 只提供 `dsh_bin` 来替换运行时可执行文件，不能在它前面加命令。启用后 audi28 把 `dsh_bin` 指到 `data/sandbox/dsh-sandboxed`，这个脚本再 `exec /usr/bin/sandbox-exec -f data/sandbox/deepseek.sb` 去跑真正的 `dsh`。沙箱默认允许，然后拒绝读取和写入用户家目录，只重新放开配置的工作区、`data/dsh-home`，以及读 `.venv` 和运行时自己的文件。项目目录里的 `.env` 一律读不到，密钥只从环境变量进运行时。没有 `sandbox-exec` 的系统会记一条 WARN，然后不套沙箱继续跑。`[deepseek.sandbox] extra_read_paths` 可以再加只读目录。`audi28 doctor` 在沙箱关闭时打 `INFO`。

沙箱没关上的口子：网络仍然开着，DeepSeek 还能访问 API；`/tmp`、`/private/tmp`、`/usr`、`/System`、`/Library`、`/opt/homebrew` 这些系统路径仍可读。家目录里的 `~/.ssh` 和 `~/.aws` 读不到。

## 许可证与密钥

不要把 API key 或 webhook 密钥写进 `config.toml` 或仓库。用项目根目录的 `.env`（gitignore），或导出到环境变量。`DEEPSEEK_API_KEY` 由 Ruoran 稍后提供。没有这把密钥时，进程不崩溃；`audi28 doctor` 会说明把密钥写进 `~/Code/audi28/.env` 或环境变量。
