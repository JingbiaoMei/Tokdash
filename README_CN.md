<p align="center">
  <a href="README.md">English</a> &nbsp;|&nbsp; <a href="README_CN.md">中文</a>
</p>

<p align="center">
  <a href="https://tokdash.github.io/"><img src="https://raw.githubusercontent.com/JingbiaoMei/tokdash/main/docs/assets/tokdash_logo_full.png" alt="Tokdash" width="420" /></a>
</p>

<p align="center">
  <b>适用于 AI 编程工具的本地 Token 与费用仪表盘</b>
</p>

<p align="center">
  <a href="https://opencode.ai/" title="OpenCode"><img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/agents/pills/opencode.png" alt="OpenCode" height="34"></a>
  <a href="https://openai.com/codex/" title="Codex"><img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/agents/pills/codex.png" alt="Codex" height="34"></a>
  <a href="https://www.claude.com/product/claude-code" title="Claude Code"><img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/agents/pills/claude.png" alt="Claude Code" height="34"></a>
  <a href="https://github.com/google-gemini/gemini-cli" title="Gemini CLI"><img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/agents/pills/gemini.png" alt="Gemini CLI" height="34"></a>
  <a href="https://openclaw.ai/" title="OpenClaw"><img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/agents/pills/openclaw.png" alt="OpenClaw" height="34"></a>
  <a href="https://github.com/MoonshotAI/kimi-cli" title="Kimi CLI"><img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/agents/pills/kimi.png" alt="Kimi CLI" height="34"></a>
  <a href="https://pi.dev/" title="Pi"><img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/agents/pills/pi.png" alt="Pi" height="34"></a>
  <a href="https://github.com/features/copilot" title="GitHub Copilot CLI"><img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/agents/pills/copilot.png" alt="GitHub Copilot CLI" height="34"></a>
  <a href="https://hermes-agent.nousresearch.com/" title="Hermes"><img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/agents/pills/hermes.png" alt="Hermes" height="34"></a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/FastAPI-009688?style=flat&logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=flat&logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/License-MIT-green?style=flat" alt="License" />
  <a href="https://tokdash.github.io/"><img src="https://img.shields.io/badge/%E5%AE%98%E7%BD%91-tokdash.github.io-1E40AF?style=flat&logo=githubpages&logoColor=white" alt="官网" /></a>
  <a href="https://tokdash.github.io/demo/"><img src="https://img.shields.io/badge/%E5%9C%A8%E7%BA%BF%E4%BD%93%E9%AA%8C-tokdash.github.io%2Fdemo-F59E0B?style=flat&logo=githubpages&logoColor=white" alt="在线体验" /></a>
</p>

<p align="center">
  <b>无需安装即可体验 → <a href="https://tokdash.github.io/demo/">tokdash.github.io/demo</a></b>
</p>

<p align="center">
  <b>性能：冷启动使用量扫描比 0.6.0 之前快约 30×，在同一台机器的本地基准中比 ccusage 快 15×。</b>
</p>

> [!IMPORTANT]
> **保留你的历史：** Claude Code 与 Gemini CLI 默认会删除超过约 30 天的本地会话，因此 Tokdash 早期月份的统计可能会悄悄变少——每个客户端改一行配置即可避免（[历史数据保留](#历史数据保留)）。

## 目录

- [功能特性](#功能特性)
- [已支持客户端](docs/SUPPORTED_CLIENTS.md)
- [快速开始](#快速开始)
  - [平台支持](#平台支持)
- [配置](#配置)
- [隐私与安全](#隐私与安全)
- [API（本地）](#api本地)
- [费用精度说明](#费用精度说明)
- [历史数据保留](#历史数据保留)
- [路线图](#路线图)
- [贡献 / 安全](#贡献--安全)
- [项目结构](#项目结构)
- [License](#license)

## 功能特性

- **精确 Token 统计**：输入 / 输出 / 缓存 Token 明细
- **状态栏集成** *[新]*：把实时 Token 使用量挂到 Claude Code（或任何能访问本地 HTTP 端点的 Agent）的状态栏中 — 见[状态栏集成](#状态栏集成statusline-integration)
- **贡献日历**：2D 热力图 + 3D 等距视图，支持 Tokens / Cost / Messages 切换
- **会话浏览器**：逐会话下钻
- **主题与应用体验**：10 款样式主题、明暗模式与 PWA 安装支持

<p align="center">
  <b>总览</b><br />
  <a href="https://tokdash.github.io/demo/">
    <img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/demo-overview-cn.png" alt="Tokdash 总览仪表盘 — 点击体验在线 Demo" width="860" />
  </a>
</p>
<p align="center">
  <b>会话列表</b><br />
  <a href="https://tokdash.github.io/demo/">
    <img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/demo-session-cn.png" alt="Tokdash 会话列表 — 点击体验在线 Demo" width="860" />
  </a>
</p>
<p align="center">
  <b>月度使用热力图</b><br />
  <a href="https://tokdash.github.io/demo/">
    <img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/demo-heatmap-cn.png" alt="Tokdash 月度使用热力图 — 点击体验在线 Demo" width="860" />
  </a>
</p>
<p align="center">
  <b>年度使用热力图</b><br />
  <a href="https://tokdash.github.io/demo/">
    <img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/demo-heatmap-year-cn.png" alt="Tokdash 年度使用热力图 — 点击体验在线 Demo" width="860" />
  </a>
</p>
<p align="center">
  <b>额度追踪</b><br />
  <a href="https://tokdash.github.io/demo/">
    <img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/demo-quota-cn.png" alt="Tokdash 额度追踪 — 点击体验在线 Demo" width="860" />
  </a>
</p>
<p align="center">
  <b>Codex 额度与重置额度</b><br />
  <a href="https://tokdash.github.io/demo/">
    <img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/demo-quota-codex-cn.png" alt="Tokdash Codex 额度与重置额度 — 点击体验在线 Demo" width="440" />
  </a>
</p>

## 快速开始

### 平台支持

- **Linux（含 WSL2）**：支持
- **macOS**：支持
- **Windows（原生）**：实验性支持

### 前置要求

- Python **3.10+**
- 已安装一个或多个[支持的客户端](docs/SUPPORTED_CLIENTS.md)

### 安装

推荐使用隔离安装：

```bash
pipx install tokdash
```

如果你不使用 pipx：

```bash
python3 -m pip install --user tokdash
```

### 首次运行

运行 onboarding 向导：

```bash
tokdash setup
```

在平台支持时，向导会配置一个可逆的用户级后台服务，并打印仪表盘地址（默认
`http://127.0.0.1:55423`）。如果没有可用的服务管理器，它会记录 setup 状态并打印前台运行指引。
它默认只监听 localhost，本地服务不需要 `sudo`，并且除非你后续使用 `--purge` 卸载，否则会保留使用历史。

如果你通过 Agent、脚本或上层 bundle 做非交互安装：

```bash
tokdash setup --auto --json
```

如需先预览 setup 会做什么：

```bash
tokdash setup --dry-run
```

### 验证

```bash
tokdash doctor
```

`doctor` 会检查运行时、后台服务、配置端口、数据路径以及更新检查状态。自动化场景可使用
`tokdash doctor --json`。

### 更新或移除

```bash
tokdash update       # 升级受管运行时，并在可能时重启服务
tokdash uninstall    # 精确撤销 setup 创建的内容；默认保留使用历史
```

`update` 只会驱动 Tokdash 能安全管理的安装方式。如果当前运行时来自 Tokdash 不拥有的包管理器，
它会打印明确的手动升级建议，而不是修改该环境。对于受管运行时，`update` 会显示升级前后的
Tokdash 版本；如果版本没有变化，会明确说明 Tokdash 已经在该版本，而不是让人误以为安装了新包。

<details>
<summary>既有安装：从 v1.0 前迁移</summary>

如果你是在 onboarding 流程加入前安装的 Tokdash，请先升级：

```bash
pipx upgrade tokdash
# 或：python3 -m pip install --user -U tokdash
```

然后运行 `tokdash doctor`；当你希望 Tokdash 接管后台服务时，再运行 `tokdash setup`。如果你已经有
手写的 systemd 或 launchd 服务，setup **不会** 静默替换它：默认会拒绝覆盖未带 Tokdash setup 标记的
`tokdash.service` / plist。你可以继续自行维护该服务、先移除它再运行 setup，或在确认
`tokdash setup --dry-run` 输出后使用 `tokdash setup --force`。`--force` 也会处理已经占用
`55423`、但还没有新版 `/health` 指纹的 1.0 之前服务：它会重写并重启现有 `tokdash.service`。
如果要跳过服务创建，使用 `tokdash setup --no-service`。

如果当前 setup 使用的是 conda / 系统 Python / user-pip 解释器，而你希望后续由
`tokdash update` 自动管理升级，可以把服务迁移到 Tokdash 自己创建并拥有的 venv：

```bash
# 先升级你接下来要运行的 tokdash 命令，例如：
python3 -m pip install --user -U tokdash
# 如果是 conda base 安装：
conda run -n base python -m pip install -U tokdash
tokdash setup --runtime venv --force
tokdash doctor
```

这会保留 `~/.tokdash` 下的使用历史，重写用户级服务，让它改为运行
`~/.tokdash/runtime/python-venv/bin/python -m tokdash`；之后 `tokdash update` 就可以升级这个
受管 venv 并重启服务。如果你使用的是 pipx 安装，也可以继续使用 pipx 运行时，并通过
`tokdash update` 或 `pipx upgrade tokdash` 升级。

</details>

### 远程访问

Tokdash 默认保持回环地址绑定。如需远程访问，推荐：

- 交互式 `tokdash setup`，在检测到 Tailscale 时可显式确认配置 Tailscale Serve；
- SSH 转发：`ssh -L 55423:127.0.0.1:55423 <user>@<host>`。

有些 Tailscale 安装要求先授予 operator 权限，非 root 用户才能配置 Serve。如果 Tailscale 拒绝
Serve 配置，交互式向导会提示是否运行一次性的 `sudo tailscale set --operator=$USER`，然后重试
`tailscale serve`。Tokdash 会使用 tailnet 主机上的 `/tokdash` 路径，因此不会占用域名根路径，
方便同一台机器继续 Serve 其他工具。Serve 成功后，setup 会打印可从 tailnet 访问的准确
`https://...ts.net/tokdash` 地址。

Tailscale Serve 下的写接口会因 Tokdash 的回环写入保护而保持只读；如果你需要可信的远程写入，
请使用 SSH 转发作为认证层。

**Windows 上的 Tailscale：** Windows 版 Tailscale 客户端会同时安装图形界面和 `tailscale`
命令行工具，客户端运行后，在 PowerShell/cmd 下使用 `tailscale serve` 的方式与其他平台相同。
Tokdash 原生 Windows 支持已有初步 CI 与 smoke test 覆盖，但 Tailscale Serve + 原生 Windows
这个组合尚未针对 Tokdash 做过端到端验证；请将这条具体路径视为实验性功能，等待确认后再依赖它。

可以直接绑定到 `0.0.0.0`，但不推荐：Tokdash 的本地 API 不是面向公网的认证服务。

### 前台运行备用方式

如果你只需要一次性的前台进程：

```bash
tokdash serve
```

打开 `http://127.0.0.1:55423`。如果默认端口被占用，使用 `tokdash serve --port <port>`。

完整 onboarding 说明，包括运行时选择、WSL/systemd 行为、macOS launchd、Tailscale、bundle
集成、更新检查和安全卸载语义，见 **[`docs/ONBOARDING.md`](docs/ONBOARDING.md)**。


### OpenClaw 摘要（定时报表）

Tokdash 可以通过定时查询本地 API，生成 OpenClaw 的日报 / 周报 / 月报。

#### 面向人类用户

把下面这段提示词复制给你的 LLM 代理（Claude Code、AmpCode、Cursor 等）：

```text
Install and configure scheduled Tokdash usage reports for OpenClaw by following the instructions here:
https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/agents/openclaw_reporting/AGENTS.md

Or read the guide yourself, but seriously, let an agent do it.
```

#### 面向 LLM 代理

拉取安装指南并按照其说明执行：

```bash
curl -s https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/agents/openclaw_reporting/AGENTS.md
```

### 状态栏集成（Statusline integration）

本地 API 可为编程 Agent（如 Claude Code）提供实时 token/费用状态栏。

**开箱即用的模板**位于 [`docs/examples/statusline/`](docs/examples/statusline/)：把其中一个脚本复制到 `~/.claude/scripts/`，再把 `statusLine` 配置块加入 `~/.claude/settings.json` 即可。

- [`statusline-minimal.sh`](docs/examples/statusline/statusline-minimal.sh) → 单行：`[Claude Sonnet 4.6] 📁 myproject | 📊 12.3M ($4.56) today`
- [`statusline-full.sh`](docs/examples/statusline/statusline-full.sh) → 四行面板，含今日 + 本周合计，以及按工具的 Top-3 明细
- [`statusline.ps1`](docs/examples/statusline/statusline.ps1) → 输出与 minimal 模板相同的单行，供在原生 Windows 上运行 Claude Code 的用户使用（PowerShell 原生实现，无需 `curl`/`jq`）

三者均为只读、仅本地访问，Tokdash 未运行时会静默隐藏 📊 段。安装与配置见[该目录的 README](docs/examples/statusline/README.md)，端点细节见 [`docs/API.md`](docs/API.md)。

想自己定制？把下面这段提示词发给你的 Agent，并把 [`docs/API.md`](docs/API.md) 一起给它：

> *"I would like to add a statusline item from the tokdash endpoint's API; it should show the total tokens used today."*

<p align="center">
  <img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/demo-statusline.png" alt="Tokdash 状态栏集成示例" width="900" />
</p>

## 配置

Tokdash 默认**只监听 localhost**。

- `TOKDASH_HOST`（默认：`127.0.0.1`）
- `TOKDASH_PORT`（默认：`55423`）
- `TOKDASH_CACHE_TTL`（默认：`600` 秒）
- `TOKDASH_COMPUTE_CONCURRENCY`（默认：`2`）——同时进行的重型历史重解析数量上限；超出的冷请求会立即返回 `503`，而不是在高负载下耗尽服务线程
- `TOKDASH_LIMIT_CONCURRENCY`（默认：`64`）——uvicorn 接受的最大并发连接数（背压）
- `TOKDASH_KEEPALIVE`（默认：`5` 秒）——uvicorn keep-alive 超时
- `TOKDASH_ALLOW_ORIGINS`（逗号分隔，默认：空）
- `TOKDASH_ALLOW_ORIGIN_REGEX`（默认仅允许 localhost/127.0.0.1）
- `TOKDASH_NO_RETENTION_NOTICE`（设为 `1` 可静默 `tokdash serve` 启动时打印的历史保留提醒）

持久化使用量数据库（默认开启）：

Tokdash 默认会在 `~/.tokdash/usage.sqlite3` 维护一个本地 SQLite 索引。它保存解析后的 token 行以及 Codex/Claude 会话摘要，让仪表盘和 API 的重复读取可以走索引 SQL，而不是每次重新解析所有源日志。源日志仍然是事实来源；这个 DB 是本地性能索引，禁用或不可用时 Tokdash 会回退到实时解析。

- `TOKDASH_USAGE_DB`（默认：`1`）——设为 `0`、`false`、`no` 或 `off` 可禁用持久化使用量 DB
- `TOKDASH_DATA_DIR`（默认：`~/.tokdash`）——Tokdash 本地状态目录
- `TOKDASH_USAGE_DB_PATH`（默认：`$TOKDASH_DATA_DIR/usage.sqlite3`）——显式指定 SQLite 文件路径
- `TOKDASH_USAGE_DB_DURABLE`（默认：`1`）——当源文件临时消失或解析器返回空结果时保留已索引行；设为 `0` 则严格按源文件替换
- `TOKDASH_USAGE_DB_WATCH`（默认：`0`）——设为 `1` 后，`tokdash serve` 内部会启动后台同步循环
- `TOKDASH_USAGE_DB_WATCH_INTERVAL`（默认：`30` 秒）——`tokdash db watch` 和 serve-time watch 循环的同步间隔

DB 维护命令：

```bash
tokdash db status --pretty
tokdash db sync --pretty
tokdash db verify --verify-period today --pretty
tokdash db repair --dry-run --pretty
tokdash db resync --pretty
tokdash db watch --pretty
```

通过 Tailscale Serve 远程访问：

```bash
tokdash setup
# 当向导询问是否配置 Tailscale Serve 时，确认即可。
# Serve 成功后，setup 会打印准确的 https://...ts.net/tokdash 地址。
```

如果你已经通过 setup 启动 Tokdash，并希望自己手动管理 Tailscale：

```bash
tailscale serve --bg --https=443 --set-path=/tokdash http://127.0.0.1:55423
```

打开 `https://<machine>.<tailnet>.ts.net/tokdash`。用
`tailscale serve --https=443 --set-path=/tokdash off` 停止这条手动 Serve 规则。
`tokdash uninstall` 只会撤销 setup 向导创建并记录下来的 Tailscale Serve 规则。
Tailscale Serve 下的写接口仍然保持只读；如果你需要可信的远程写入，请使用 SSH 转发。

默认情况下，`tokdash serve` 会在启动时自动在浏览器中打开仪表盘一次。使用 `--no-open` 可禁用此行为（在无界面/SSH 环境以及后台服务模板中也会自动跳过）。

## 隐私与安全

- **无遥测**：Tokdash 不会主动把你的数据发送到任何地方。
- **本地解析**：使用量由本机会话文件计算得出（见[支持的客户端](docs/SUPPORTED_CLIENTS.md)）。
- **可选额度轮询**：「额度」标签页默认仅使用本地数据。可在标签页内或用 `tokdash quota consent` 按服务商开启 API 轮询；它只用你本机的 CLI 凭据去调用对应服务商自己的额度接口，并把响应存入本地 SQLite 数据库。
- **服务暴露**：Tokdash 默认绑定 `127.0.0.1`。如需远程访问，优先使用 Tailscale Serve 或 SSH 隧道；除非你明确知道风险并配置好了防火墙/认证，否则不要使用 `--bind 0.0.0.0`。Tailscale Serve 下的写接口会因 Tokdash 的回环写入保护而保持只读；如果你需要远程写入，请使用 SSH 转发作为认证层。

### 额度跟踪（可选）

「额度」标签页展示订阅用量窗口与重置倒计时。未授权时，Tokdash 只读取本地文件，例如 Codex 会话中的 `rate_limits`，以及 Claude 本地的套餐/等级信息。实时额度轮询默认关闭，可按服务商单独开启：

```bash
tokdash quota consent --codex-api on --claude-api on --antigravity-api on
tokdash quota consent --poll-interval 30      # 后台轮询周期：15、30、60 或 120 分钟
tokdash quota consent --enabled off           # 总开关：关闭全部额度跟踪
tokdash quota poll
tokdash quota show
```

**总开关。** `quota.enabled`（默认开启）控制*所有*额度工作——会话扫描、网络轮询与快照写入。可在「额度」标签页或用 `tokdash quota consent --enabled on|off` 切换。关闭后（或设置了 `TOKDASH_QUOTA_POLL=0` 终止开关时），后台轮询会完全停摆，`POST /api/quota/refresh` 会返回「额度跟踪已禁用」错误，标签页也会显示「启用额度跟踪」卡片而非数据。按服务商的授权键仍保留其更窄的、仅网络的含义。

**轮询间隔。** 后台轮询默认每 **30 分钟** 记录一次快照。可在「额度」标签页、`tokdash setup` 过程中或用 `tokdash quota consent --poll-interval N` 选择 15/30/60/120 分钟，它会保存为 `config.json` 中的 `quota.poll_interval_minutes`。环境变量 `TOKDASH_QUOTA_POLL_INTERVAL`（单位秒，下限 300）会覆盖保存的值，标签页会显示当前生效的来源。间隔调整会在下一个轮询周期生效，无需重启服务。Codex 会话摄取采用增量方式——首次一次性回填历史后，每个周期只对增长过的会话文件做尾部读取，因此稳态轮询只需个位数毫秒。

开启后，Tokdash 从 `$CODEX_HOME/auth.json`、Claude 的 `CLAUDE_CODE_OAUTH_TOKEN` 覆盖值或 `$CLAUDE_CONFIG_DIR/.credentials.json`，以及 `~/.gemini/antigravity-cli/antigravity-oauth-token` 读取凭据，并只调用对应服务商的额度接口。在 macOS 上，Claude Code 的凭据保存在钥匙串（Keychain）而不是 `.credentials.json` 中；如果没有设置环境变量且不存在 `.credentials.json`，Tokdash 会直接只读读取钥匙串条目（`Claude Code-credentials`），首次读取可能弹出一次性的钥匙串授权提示。如果钥匙串不可用（已锁定、被拒绝或无界面会话），可设置 `CLAUDE_CODE_OAUTH_TOKEN`（可用 `claude setup-token` 生成）作为替代。Tokdash 从不刷新或写入服务商凭据。`TOKDASH_QUOTA_POLL=0` 是关闭全部额度跟踪的硬终止开关。`tokdash export` 默认排除额度数据；只有当你确实想把它写入 JSON 时才使用 `--include-quota`。

`tokdash setup` 会提供一个可选的额度步骤（按服务商的网络授权，默认为否，以及轮询间隔），`tokdash doctor` 会报告额度状态：总开关、按服务商授权、终止开关、生效间隔及其来源、上次轮询时间，以及已保存的快照数量。

额度快照及其历史保存在本地使用量数据库（`usage.sqlite3`，默认开启），**默认永久保留**——将 `TOKDASH_QUOTA_RETENTION_DAYS` 设为正整数天数可开启对更早快照的清理。如果你用 `TOKDASH_USAGE_DB=0` 关闭本地持久化，「额度」标签页将失去主要数据来源：不再保留快照历史，后台轮询也不运行，标签页只会在当前服务进程存活期间展示手动**刷新**（已授权的网络服务商）得到的内存中结果。日常额度跟踪请保持使用量数据库开启（默认）。

## API（本地）

Tokdash 是一个本地 HTTP 服务。常用接口：

- `GET /api/usage?period=today|week|month|N`
- `GET /api/usage?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD`
- `GET /api/tools?period=...`（仅编程工具）
- `GET /api/openclaw?period=...`（仅 OpenClaw）
- `GET /api/sessions?tool=codex|claude|opencode|pi_agent|mimo&period=...`（追加 `&include_review_sessions=true` 可包含默认隐藏的 Codex 审核/权限会话）
- `GET /api/quota` 与 `GET /api/quota/history`（订阅额度快照；网络刷新受写入保护且需显式授权）
- `GET /api/stats`（贡献日历与统计数据）

示例：

```bash
curl 'http://127.0.0.1:55423/api/usage?period=today'
```

完整 API 参考：[`docs/API.md`](docs/API.md) — 包含每个端点的请求参数与响应结构。

## 费用精度说明

Token 统计依赖各客户端本地记录的内容。费用默认由内置定价数据库（`src/tokdash/pricing_db.json`）计算；如果存在你在「定价」标签页保存的覆盖文件 `<data_dir>/pricing_db.json`，则改用该覆盖文件（它会完全替换内置费率）。两种情况都可能滞后于真实服务商价格，请将其作为估算值，如金额敏感请以你的账单来源为准。

## 历史数据保留

Tokdash 通过读取各客户端的**本地**会话日志来统计用量，同时也维护一个本地 SQLite 性能索引。这个索引可以保留 Tokdash 已经见过的行，但无法恢复在索引前就被删除的日志，也不能替代原始客户端历史。如果客户端在 Tokdash 同步前删除了旧日志，过去某个月的统计仍然**可能比你最初记录时更低**。只有两个受支持的客户端会默认这样做，且都只需改一行配置：

- **Claude Code** 会在启动时删除超过 `cleanupPeriodDays`（**默认 30 天**）的会话。请把这个键添加到你现有的 `~/.claude/settings.json`（以及任何其他 `CLAUDE_CONFIG_DIR`）：
  ```json
  { "cleanupPeriodDays": 3650 }
  ```
- **Gemini CLI** 会删除超过 30 天的会话。在 `~/.gemini/settings.json` 中关闭它；如果某个项目有 `.gemini/settings.json`，也要同步修改，因为工作区设置会覆盖用户设置：
  ```json
  { "general": { "sessionRetention": { "enabled": false } } }
  ```

其他所有受支持的客户端默认都会无限期保留历史。完整的逐客户端清单、配置细节，以及本地 SQLite 索引能保留什么、不能保留什么，详见 **[docs/HISTORY_RETENTION.md](docs/HISTORY_RETENTION.md)**。

## 路线图

参见 `docs/ROADMAP.md`。

## 贡献 / 安全

- 贡献指南：`docs/CONTRIBUTING.md`
- 安全策略：`docs/SECURITY.md`

## 项目结构

```text
tokdash/
├── main.py                 # 源码入口（python3 main.py）
├── tokdash                 # CLI 包装器（./tokdash serve）
├── src/
│   └── tokdash/
│       ├── cli.py
│       ├── api.py                # FastAPI 路由 / 应用
│       ├── compute.py            # 聚合 / 合并逻辑
│       ├── dateutil.py           # 共享的日期范围解析
│       ├── sessions.py           # 会话浏览器逻辑
│       ├── pricing.py            # PricingDatabase 封装
│       ├── assets.py             # 静态资源管理
│       ├── model_normalization.py
│       ├── pricing_db.json
│       ├── sources/
│       │   ├── openclaw.py       # OpenClaw 会话日志解析器
│       │   └── coding_tools.py   # 本地编程工具解析器
│       └── static/
│           ├── index.html        # 单页仪表盘
│           ├── theme-config.js   # 主题调色板 & 热力图颜色
│           └── themes.css        # 各主题 CSS 覆写
└── docs/                   # Onboarding 指南、API 文档、发布说明与 agent 提示词
```

## License

MIT License，详见 `LICENSE`。
