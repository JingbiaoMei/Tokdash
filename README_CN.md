[English](README.md) | [中文](README_CN.md)

# Tokdash

适用于 AI 编程工具（Codex、OpenCode、Claude Code、Gemini CLI、OpenClaw 等）的本地 Token 与费用仪表盘。

![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=flat&logo=fastapi&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=flat&logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green?style=flat)

## 功能特性

- **分层明细**：按 app → model 展示，并保留完整 Token 精度
- **多数据源**：本地会话文件 + 可选 `tokscale` 回退
- **精确 Token 统计**：输入 / 输出 / 缓存 Token 明细
- **灵活时间范围**：今天 / 最近一周 / 最近一月 / N 天
- **贡献日历**：2D 热力图 + 3D 等距视图

<p align="center">
  <img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/demo.png" alt="Tokdash 仪表盘演示" width="900" />
</p>

## 已支持客户端（显式 Token 字段）

✅ 已支持：
- **OpenCode**: `~/.local/share/opencode/`
- **Codex**: `~/.codex/sessions/`
- **Claude Code**: `~/.claude/projects/`
- **Gemini CLI**: `~/.gemini/tmp/*/chats/session-*.json`
- **OpenClaw**: `~/.openclaw/agents/*/sessions/`
- **Kimi CLI**: `~/.kimi/sessions/*/*/wire.jsonl`

## 平台支持

- **Linux（含 WSL2）**：支持
- **macOS**：实验性支持

## 快速开始

### 前置要求

- Python **3.10+**
- 已安装一个或多个上方支持的客户端

### 安装（pip）

从 PyPI 安装（首次公开发布后）：

```bash
pip install tokdash
tokdash serve
```

打开：`http://localhost:55423`

### 从源码运行

```bash
pip install -e .

# 方案 A：直接运行
python3 main.py

# 方案 B：CLI 包装器（同一个服务）
./tokdash serve
```

打开：`http://localhost:55423`

如果端口冲突：
- `python3 main.py --port <port>`
- `./tokdash serve --port <port>`

如果你想从其他设备访问 Tokdash（推荐）：
- Tailscale Serve（仅对你的 tailnet 可见）：`tailscale serve 55423`
- SSH 端口转发：`ssh -L 55423:127.0.0.1:55423 <user>@<host>`

也可以绑定到 `0.0.0.0`，但**不推荐**：这会监听所有网络接口，可能把仪表盘暴露到你的局域网/VPN/Wi‑Fi 之外。只有在你清楚风险并已配置防火墙/认证时才这样做。

### 后台运行

参见 `docs/agents/systemd/BACKGROUND_RUN.md`，其中包含：
- Linux systemd（用户服务）模板
- macOS launchd（LaunchAgent）模板

#### 面向人类用户

把下面这段提示词复制给你的 LLM 代理（Claude Code、AmpCode、Cursor 等）：

```text
Install and configure Tokdash to run in the background by following the instructions here:
https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/agents/systemd/AGENTS.md

Or read the Background Run guide, but seriously, let an agent do it.
```

#### 面向 LLM 代理

拉取安装指南并按照其说明执行：

```bash
curl -s https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/agents/systemd/AGENTS.md
```

### 更新 Tokdash

如果你是通过 pip 安装 Tokdash，并通过 systemd 运行它：

```bash
# 1. 升级包
pip install --upgrade tokdash

# 2. 重启 systemd 服务以加载新版本
systemctl --user daemon-reload
systemctl --user restart tokdash

# 3. 验证新版本
pip show tokdash | grep Version
systemctl --user status tokdash --no-pager

# 4. 测试 API 是否正常响应
curl 'http://127.0.0.1:55423/api/usage?period=today'
```

如需查看日志：

```bash
journalctl --user -u tokdash -f
```

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

## 配置

Tokdash 默认**只监听 localhost**。

- `TOKDASH_HOST`（默认：`127.0.0.1`）
- `TOKDASH_PORT`（默认：`55423`）
- `TOKDASH_CACHE_TTL`（默认：`120` 秒）
- `TOKDASH_ALLOW_ORIGINS`（逗号分隔，默认：空）
- `TOKDASH_ALLOW_ORIGIN_REGEX`（默认仅允许 localhost/127.0.0.1）

示例（通过 Tailscale Serve 远程访问，推荐）：

```bash
tokdash serve --bind 127.0.0.1 --port 55423
tailscale serve --bg 55423
```

## 隐私与安全

- **无遥测**：Tokdash 不会主动把你的数据发送到任何地方。
- **本地解析**：使用量由本机会话文件计算得出（见上方“已支持客户端”路径）。
- **服务暴露**：Tokdash 默认绑定 `127.0.0.1`。如需远程访问，优先使用 Tailscale Serve 或 SSH 隧道；除非你明确知道风险并配置好了防火墙/认证，否则不要使用 `--bind 0.0.0.0`。

## API（本地）

Tokdash 是一个本地 HTTP 服务。常用接口：

- `GET /api/usage?period=today|week|month|N`
- `GET /api/tools?period=...`（仅编程工具）
- `GET /api/openclaw?period=...`（仅 OpenClaw）

示例：

```bash
curl 'http://127.0.0.1:55423/api/usage?period=today'
```

## 费用精度说明

Token 统计依赖各客户端本地记录的内容。费用由 `src/tokdash/pricing_db.json` 计算，可能滞后于真实服务商价格。如金额敏感，请以你的账单来源为准。

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
│       ├── pricing.py            # PricingDatabase 封装
│       ├── model_normalization.py
│       ├── pricing_db.json
│       ├── sources/
│       │   ├── openclaw.py       # OpenClaw 会话日志解析器
│       │   └── coding_tools.py   # 本地编程工具解析器
│       └── static/
│           └── index.html
└── docs/                   # 路线图 + 后台运行文档 + agent 提示词
```

## License

MIT License，详见 `LICENSE`。
