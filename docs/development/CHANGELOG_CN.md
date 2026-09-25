# 更新日志（简体中文）

本页面由 `scripts/changelog_cn.py` 生成，内容来自应用内“更新日志”所用的
`src/tokdash/static/release-notes.zh.json`，请勿手工编辑。

- 中文条目面向使用者；每一条改动背后的完整实现推理仍记录在英文
  [CHANGELOG.md](https://github.com/JingbiaoMei/Tokdash/blob/main/docs/development/CHANGELOG.md)，本页面在每节末尾附上对应的 PR。
- 未收录的版本表示尚未翻译，应用内同样回落英文。

## 2.6.4 - 2026-09-25

### 新增

- 更新日志支持本地化：中文语言的仪表盘下，What's New 会在英文条目上叠加简体中文，页脚链接指向生成的中文更新日志页面；未翻译的语言与版本回落英文。
- tokdash tui 打开交互式终端仪表盘，包含 Overview、Report 与 Quota 标签页，与网页仪表盘共用同一套计算与缓存，无需启动服务器。
- tokdash report 一次性打印单个时间窗口的活动报告；--json、--pretty 与 --output 遵循 tokdash export 的约定。

### 调整

- 不带子命令直接运行 tokdash 现在只打印命令帮助并退出，不再静默启动 tokdash serve 并打开浏览器。

### 修复

- Claude Code 配额不再把用量百分比 1 误读为 100% 已用：线上 API 的 0-100 整数值保持该量程。
- 当 agy CLI 把 OAuth token 存到 macOS 登录钥匙串而非文件时，Antigravity 配额现在能找到登录状态；失败快照会对无法识别的 token 元数据脱敏。

相关 PR：#111、#115、#113、#114

## 2.6.3 - 2026-09-23

### 新增

- Claude Code 的额度重置现在显示在 Quota 页，与 Codex 卡片所用的 Reset Credits 区块同处一处。每次重置列在持有它的安装下并标注到期时间，而 Tokdash 只读取不消耗：使用额度仍然要在 Claude Code 里完成。

相关 PR：#109

## 2.6.2 - 2026-09-23

### 调整

- 定价数据库跟随 9 月的模型更新：Claude Opus 5.5 Fast 按标准费率的 2 倍计价，Claude Mythos 5.1 与 OpenAI 的 GPT-5.6 Cyber 完成定价，GPT-5.6 Sol 与 Sol Pro 在 OpenAI 促销价下降至 $4.00 / $20.00，Claude Sonnet 5 的 $2.00 / $10.00 确认为标准价格。

相关 PR：#106

## 2.6.1 - 2026-09-22

### 新增

- Devin CLI 用量追踪：从 Devin 本地 SQLite 存储读取每次调用的 token 用量，Overview、时间范围筛选与 Stats 都会统计。

### 调整

- 定价数据库按 2026-09-22 的模型扫描刷新：Claude Opus 5.5 定价为每 MTok 输入 $4.00 / 输出 $20.00，Claude Fable 5.1 与其他 Anthropic 条目并列。

### 修复

- 已登出的 Claude Code profile 不再显示永远无法刷新的配额窗口。

相关 PR：#105、#107、#104

## 2.6.0 - 2026-09-21

### 新增

- MiniMax Code 用量追踪：mcode CLI 每次调用的 token 用量进入 Overview、时间范围筛选与 Stats，与已经覆盖账户侧的 MiniMax 配额卡片并列。
- Command Code 订阅配额追踪：显示 API 报告的 5 小时与每周窗口，并按解析出的套餐推算月度窗口；需显式授权：tokdash quota consent --credential-scan on --commandcode-api on。
- 支持读取 OpenCode v2 的 session_message / session_v2 结构，OpenCode 迁移后写入的用量重新计入 Overview 与 Sessions；Kilo Code 数据库同样按此方式识别。

### 修复

- 服务方恢复应答后，配额卡片立即停止“无法刷新”的告警，不再在恢复之后继续报错数日。
- 同一秒内更新两次的配额窗口现在保留较新的读数，本该撤下的进度条会真正消失。

相关 PR：#100、#98、#99、#96

## 2.5.7 - 2026-09-15

### 修复

- Antigravity 配额追踪现在能定位当前的 OAuth 令牌，同时保留对旧令牌路径的兼容。
- Quota 页的服务方筛选按主机分别保存，改动一台主机不再影响其他主机。

相关 PR：#91、#94、#95

## 2.5.6 - 2026-09-14

### 新增

- 启用 Muse Code 用量追踪：读取主会话与子代理会话日志，并做镜像流过滤与跨分支去重。
- DeepSeek V4.1 Flash 按官方费率定价。

### 调整

- 分享卡片现在列出前 5 名的 harness、模型与项目，与 Report 页保持一致。

### 修复

- 刷新按钮现在会等待已在进行的重新计算并展示其新结果，而不再短暂地报告缓存数据。

相关 PR：#86、#89、#87、#90

## 2.5.5 - 2026-09-14

### 新增

- Qwen Code、OpenClaw 与 Qoder CLI 新增 Session Explorer 面板，其 token 合计与 Overview 共用同一套解析与去重规则。
- 可选开启的 OpenCode Go 配额追踪：通过密钥鉴权的用量接口读取滚动、每周与每月订阅窗口。

### 调整

- 安全远程访问指南新增 Cloudflare Tunnel（配合 Access）以及带鉴权的 Caddy/nginx 部署，并说明客户端与子路径的限制。

### 修复

- Claude Code 的单轮对话保留最完整的累计用量快照，找回此前因保留较早的流式区块而丢失的输出 token。
- 基于符号链接的包安装方式下静态资源可以正常加载，不再返回 404。
- 文件用量同步在日志于发现与解析之间消失时保留已有记录；无论是否启用持久数据库，跨文件副本都会选择同一条作为归属。

相关 PR：#85、#80、#81、#82、#83、#84、#77、#79

## 2.5.4 - 2026-09-08

### 调整

- 重新打开 Report 页明显更快：会话按 session 缓存并在周、月、年窗口之间共用，不再重建三次；TOKDASH_SESSION_CACHE_TURNS 限制缓存保留的内容。
- Report 页把快捷范围预设换成周期按钮（本周与上周、具名月份、年份），每次点击把窗口整体前移一个周期。
- 分享卡片跟随应用主题，导出尺寸为 1800x3200，表头同时给出 token 与费用（默认显示费用，开关用于关闭）。月度卡片使用更小的热力方块并为尚未到来的日期留空格，周卡片去掉热力行，项目卡片列出前 3 个项目。
- 时长用单位词表述，最多两级：25 小时写作 1 天 1 小时，34 天写作 1 个月 4 天。

### 修复

- claude-3.5-sonnet 按公布的 $3 / $15 每 MTok 计价而非翻倍，此前 2024 年 10 月至 2025 年 2 月初 Claude Code 的默认模型显示为实际成本的两倍；已存储的记录在读取时重新计价。

相关 PR：#76、#74、#75

## 2.5.3 - 2026-09-05

### 新增

- 全新 Report 页：按本周、本月或本年至今生成周期报告，包含日期地图、harness / 模型 / 项目前三名、小时与星期的节律图，以及每个 harness 一行明细。
- Report 页底部提供两张可分享卡片，导出为 1080x1920 PNG。每次导出都会同时生成浅色与深色两张，费用默认不显示，需要你主动开启。
- 新增 7 套取自 cosyncing 的风格主题：Teal Obsidian、Graphite、Nordic Warmth、Cyber Amber、Royal Navy、Soft Minimalist 与 Flat White，各自带有独立的明暗色板、热力图与图表配色。
- 统计通过 ACP 宿主运行的 Antigravity 会话。除 antigravity-cli 外还会扫描 antigravity-acp 与 antigravity-ide 的产品目录，三者统一归入同一个 Antigravity 条目；ANTIGRAVITY_HOME 可在不覆盖默认值的前提下追加更多目录。
- 第二个 Claude Code 订阅也能进入 Quota 页。在已同意凭据扫描的前提下，每个自带登录态的 ~/.claude* 安装都会被轮询，Claude 卡片按安装分组，组名使用其配置时的目录名。
- gpt-6-astra 按 OpenAI 公布的标准费率定价，不再显示为 $0。
- tokdash serve --dev-fixture dense 现在会提供 /api/insights，Report 页可以直接基于该数据集开发。

### 修复

- 修复整套风格主题的多处缺陷：浅按钮主题下刷新图标与下拉箭头不再消失；等宽字体主题下 Overview 的 KPI 数值不再折行；Flat White 的面板恢复了与页面的分隔；9 组主题与模式组合中低于 WCAG AA 对比度的标签文字已修正。
- Report 页的面板跟随当前风格主题，不再在所有主题上绘制固定的白色或石板蓝。
- 覆盖两个账户的配额卡片不再把一方的故障算到另一方头上：MiniMax 中国套餐出错时显示为该套餐的问题，而不是状态正常的全球套餐；某一区域恢复后也不会让另一区域的过期错误留在界面上。
- 当一个 Claude 安装正常、另一个仍损坏时，低配额提醒不再对正常的那个静默。配额行的最近已知状态标记与其告警资格现在都以该行所属账户的状态为准。
- 配额卡片现在会显示不属于任何已列账户的错误，而不是让任意账户条目把它静音。
- 多服务器视图不再把正常工作的订阅算作待处理。只要某个服务方至少有一个账户健康、且卡片自身的错误属于其中某个账户，该服务方即视为正常。
- tokdash serve --dev-fixture dense 在较短的显式日期范围下不再对 /api/insights 返回 500。

相关 PR：#73、#68、#69、#70、#71

## 2.5.2 - 2026-09-03

### 新增

- Gemini 3.8 Flash、腾讯 HY4 Preview 与 Muse Spark 1.3 完成定价，Muse Spark 贡献者档位也已定价，不再显示为 $0。
- 此前匹配不到任何条目的模型名称现在可以解析：Qwen3.8-Flash-Next 的各种写法按 Qwen3.8 Flash 计价，gemini-3.7-flash-control 按 Gemini 3.7 Flash，doubao-seed-code、doubao-seed-2.0-code 与 ark-code 按 Seed 2.0 Code。

相关 PR：#67

## 2.5.1 - 2026-09-02

### 新增

- Claude Fable 5.1 完成定价，并支持 fable-5.1 形式的别名，不再显示为 $0。
- tokdash serve --dev-fixture dense 以稠密的合成数据集启动仪表盘用于界面开发，完全不读取你的真实历史。

### 修复

- 大型 Codex 会话正在写入时刷新不再耗时 30-60 秒或让内存膨胀：Codex 与 Claude 解析器改为流式读取日志，每个数据源同一时间只运行一次同步，对比区间复用当前区间的同步结果。

### 调整

- 升级后的首个请求会一次性重新解析 Codex 会话文件；历史较多时会出现几十秒的一次性停顿。

相关 PR：#64、#63

## 2.5.0 - 2026-09-01

### 新增

- 新增 Zed、Qwen Code 与 Charm Crush 三个用量来源，均从各工具自身的本地存储读取。
- WorkBuddy 与 Qoder IDE 拥有独立的 Session Explorer 面板，展示每个会话的轮次与活跃时长。
- 新增 /api/insights 接口，一次请求返回小时分布、星期分布、热力图、按项目统计与连续天数。
- 用量响应在按 token 排序的 top_models 之外新增 top_models_by_cost，stats 新增 messages、most_used_model 与 highest_cost_model。

### 调整

- 模型列表按 token 排序，与 API 参考文档的描述一致；按费用的排名单独提供，而不再取代它。
- 最常使用的模型按 token 而非费用评选，少量使用但价格昂贵的模型不会再胜出。
- 项目名同样取自 Windows 路径的最后一段（此前只处理 POSIX 路径），从 Windows 与 WSL 看到的同一个项目会归为一组。

### 修复

- 日历热力图的日子带上真实强度而非恒为零，日历的深浅色恢复。
- 当前连续天数与最长连续天数会实际计算，不再恒返回零。
- 刷新在返回缓存数据时不再报告成功。

相关 PR：#61、#59、#56、#57

## 2.4.3 - 2026-09-01

### 新增

- 默认扫描 Hermes 的具名 profile，存放在 ~/.hermes/profiles/<name> 下的用量不再从 Overview 与 Session Explorer 中缺失。
- 午夜过后即预热昨天的数据，一天中第一次点击 Yesterday 时结果已在缓存里。

### 调整

- 重型计算的并发上限改为随本进程实际可用 CPU 数伸缩（最高 8），不再固定为 2。

### 修复

- 切换到尚未计算过的日期范围时，不再有大半 Sessions 面板报错；拿不到计算配额的请求会短暂排队，而不是直接被拒绝。

相关 PR：#54、#55

## 2.4.2 - 2026-08-30

### 修复

- 昨天等已结束的日期范围会在范围结束后重新计算，不再显示范围尚在进行中时缓存的不完整数字；此前只有点击刷新按钮才会纠正。

相关 PR：#52

## 2.4.1 - 2026-08-28

### 修复

- Quota 的服务方可见性菜单现在列出各自主机报告的 harness，共享偏好变更时所有服务器区块保持同步。

相关 PR：#51

## 2.4.0 - 2026-08-28

### 新增

- 仪表盘新增日语、韩语、西班牙语与葡萄牙语，支持按浏览器语言自动检测，日期与数字按区域设置显示。
- 新增可选的 Z.ai Coding Plan 配额追踪，覆盖 5 小时、每周额度与旧版 MCP 窗口。
- 定价数据更新至 2.0.20，新增 14 个已上架模型。

### 调整

- 对当前显示范围重复刷新时同样给出刷新报告，并尊重用户显式的关闭操作。

相关 PR：#49、#48、#50、#47
