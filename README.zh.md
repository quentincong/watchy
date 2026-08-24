# Watchy（看门狗）

[![tests](https://github.com/quentincong/watchy/actions/workflows/ci.yml/badge.svg)](https://github.com/quentincong/watchy/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

> 🌐 English version: [README.md](README.md)


基于 [TradingAgents](https://github.com/TauricResearch/TradingAgents) 多智能体 LLM 交易框架的股票监控守护进程（daemon）。Watchy 帮你盯着自选股（watchlist）——每小时跑一次零成本的技术指标扫描（indicator scan），每天跑一次全深度分析（full-depth analysis），并通过 Telegram 推送持仓感知的交易建议（position-aware advice）。

## 架构（Architecture）

```
┌─────────────────────────────────────────────────┐
│                  Watchy 守护进程                   │
│                                                   │
│  第一层 Tier 1（每小时）      第二层 Tier 2（每天）   │
│  ──────────────────────      ──────────────────  │
│  OHLCV + 技术指标             完整四分析师流水线      │
│  不调用 LLM                   (pipeline)           │
│       │                       + 辩论 (debate)      │
│       │                       + 风险管理 (risk)    │
│       ▼                            │              │
│  触发信号？                        │              │
│  (signal breach?)                 │              │
│       │                            │              │
│    ┌──┴──┐                         │              │
│    │ 是  │───→ 分级分析 ────────────┘              │
│    │     │    (graduated subset)                  │
│    │ 否  │───→ 更新状态,                           │
│    └─────┘    退出（零成本）                        │
│                                                   │
│  每次分析完成后：                                   │
│    持仓数据源 → LLM 顾问 → Telegram 推送           │
└─────────────────────────────────────────────────┘
```

**Tier 1（第一层）**按可配间隔（默认每小时）逐票扫描，**仅在美股常规交易时段运行**（休市、周末、节假日自动跳过——靠 `exchange_calendars` 判断，含夏令时/DST 修正）。通过 yfinance 获取 OHLCV 数据并计算技术指标（technical indicators），不调用任何 LLM。检测 10 种信号类型，包括金叉/死叉（golden/death cross，含完整均线阶梯确认 full MA staircase）、RSI 极值、MACD 交叉、布林带突破（Bollinger breach）、成交量异动（volume anomaly）和 ATR 飙升。信号触发时，根据信号重要程度启动分级（graduated）的 TradingAgents 分析师子集。

**Tier 2（第二层）**在配置的 UTC 时间运行（**仅美股交易日**；周末与 NYSE 节假日（如 7/3）跳过——当日休市、只会重复分析前一日收盘、且无法交易，属冗余成本）。对自选股中的每一只票启动完整的四分析师流水线（市场 Market + 情绪 Sentiment + 新闻 News + 基本面 Fundamentals）+ 多空辩论（Bull/Bear debate），风险管理深度按日：**普通交易日为简化（simplified），每周第一个交易日升级为完整三维风险辩论（3-way risk debate）**（通常是周一；周一逢节假日则顺延到周二，保证每票每周仍有一次完整风控，且不必为一份"分析周五陈旧收盘"的周末批次额外付费）。

**Tier 2 价格邻近门控（price-proximity gate，#15）**：用顶层 `min_price_proximity_pct` 设一个**全局默认**百分比（自动套到所有 watch-only 票；也可在长表里按票用同名键 `min_price_proximity_pct` 覆盖），**普通交易日**若现价离 **入场目标价（entry target）** 超过该百分比，就跳过这次昂贵的 LLM 流水线（省 DeepSeek 成本）。门控只针对 **watch-only（非持仓）** 的票：**只要当前持有该票（position source 查到非零持仓），Tier 2 永远运行**——有资金敞口就值得每天分析，与价格无关（持仓查询出错时也按"持有"处理，宁可多跑）。**每周完整风控日（每周第一个交易日）永远运行**每一只票（每周一次完整更新，含新闻）。入场目标价优先用手动 `target_price`，否则用 **自动推导值（#16）**：每次 Tier 2 运行时从顾问输出的结构化 `Target:` 字段提取（语义明确为"建仓/加仓的入场价"，不是止损也不是止盈）并存入 `state.db`（手动值始终优先）。注意 **Tier 1 永不门控**——它是每 30 分钟的常开雷达，远离目标的票之间靠 Tier 1 信号兜底。

**ATR 自适应带宽（#15 后续，可选）**：不用固定百分比，设 `atr_proximity_mult`（全局或按票），门控带宽变成 `mult × ATR%`（`ATR% = avg_atr_20d / price × 100`），即"现价离目标超过 `mult` 个交易日的常规波动才跳过"——波动大的票带宽更宽、安静的票更窄。带宽钳到 `[proximity_pct_floor, proximity_pct_ceiling]`（默认 4–20%），ATR 数据缺失时回退固定百分比。启用前先用 `scripts/calibrate_atr_proximity.py` 校准 mult。

**Tier 2 批次顺序（#21）**：每日批次**先跑持仓票**（有资金敞口），再跑 watch-only 中**离目标最近**的，无目标价的票排最后——这样长批次被打断（auto-update 重启、崩溃、token 过期）时，最重要的票已经分析过。指标每票预取一次（throttled）并被流水线复用，不重复抓取。

**每次分析完成后**，Watchy 获取该票的当前持仓（position），调用轻量 LLM（默认 Gemini）将**精简后的分析摘要（digest：决策链 + 各分析师的总结尾巴，非全文）**与持仓合成可执行的交易建议，推送自然语言摘要到 Telegram。顾问自身的 token 用量打成 `GEMINICOST` 行；其 thinking 档位按层设置（`secrets.yaml` 里 `llm.gemini_thinking_tier1`、`llm.gemini_thinking_tier2`，均为 low）。

「总结尾巴」锚定在各分析师 prompt 都要求的报告末尾 Markdown 表格上。**表格若缺失，digest 会静默退回报告的开头几行（而非结论）**——所以该分支会打一条可 grep 的 `ADVISOR_TAIL_FALLBACK` 警告（含 ticker 与分析师名）。换模型后要盯这条：DeepSeek 用浮动别名且有过不公告就上新快照的先例，而末尾格式指令正是指令遵循能力下降时最先被丢掉的东西。

**持仓数据源（position source，#4）是分层的，保证 Schwab 无法刷新时仍可用**：

1. **Schwab API（实时）** —— 主数据源。每次成功获取后，快照（snapshot）会缓存到 `~/watchy_config/positions_cache.json`。
2. **缓存快照（cached snapshot）** —— 当实时获取失败（token 过期需 7 天重新授权、API 故障、网络中断）时，回退到上次成功的快照，并在推送中标注数据时效（如 `Schwab cache, ... (3d 4h old)`），绝不把陈旧数据当成实时。
3. **手动文件（manual file）** —— 最终兜底：`~/watchy_config/positions.yaml`（schema 见 `positions.example.yaml`）。用于 Schwab 首次授权前的引导，或彻底无可用数据时。手动文件的持仓会用 yfinance 实时价格补全市值与浮动盈亏（unrealized P&L），**同样标注时效**——优先读文件里可选的 `as_of:` 字段（你声明的持仓截至日期），否则退回文件修改时间（mtime）。

> Schwab 实时层通过 **`schwabdev`** 包实现（只读：持仓 + 余额）。首次需在运行守护进程的机器上做一次浏览器 OAuth（schwabdev 打印授权 URL，授权后把回调 URL 粘回终端），token 存到 `tokens_path`（schwabdev 3.x 的 SQLite 库，默认 `~/watchy_config/schwab_tokens.db`）；refresh token 有效期 7 天，到期需重新授权——任何实时获取失败都会自动回退到缓存快照、再到手动文件，守护进程不中断。配置见 `secrets.example.yaml` 的 `schwab:` 段。
>
> **持仓在每个 Tier 2 批次开头抓取一次，并在该批所有 ticker 间共享**（整批一致的持仓视图 + 一次 API 调用，而非每个 ticker 各抓一次）。Tier 1 在信号触发时抓取，且在跑分析 pipeline 之前。
>
> **Token 过期提醒（不再静默用陈旧数据）：** 每个 Tier 2 批次(以及每次 Tier 1 触发扫描)会检查它刚解析出的持仓快照——若 refresh token **已失效**（扫描回退到缓存/手动数据，需重新授权）或**即将过期**就推送 Telegram 提醒。过期提醒**分三档逐级升级**：剩余 **≤3 天 / ≤2 天 / ≤1 天**各发一次、越来越紧急（同一档不重复，按授权周期去重；重新授权后重置）——**≤3 天档**留出足够提前量，覆盖周末连着好几天碰不到 VPS 的情况。此外每逢**周五**会推送一次**主动重新授权提醒**，让 7 天计时在周末前重新锚定，避免过期日漂进多天空档。这些提醒用**醒目的 emoji 边框 + 大写标题**（🔴/🟠/🟡/🚨），刻意区别于普通的持仓建议，避免被淹没。不额外发请求,直接复用扫描已做的那次抓取；重新授权提示按天去重（每天最多一条），周五提醒每周五一条。7 天计时由 `scripts/schwab_oauth.py` 在授权成功时打点。
>
> **每周重新授权：** 在 VPS 上跑 `python scripts/schwab_oauth.py --force`。`--force` 会把现有 token 库挪到 `.bak` 后强制走完整浏览器 OAuth（签发新的 7 天 refresh token 并重置计时）——**不加 `--force` 的普通重跑只会刷新 access token，不会重置 7 天计时，也不会触发新授权**。授权成功后自动删除 `.bak`；失败则恢复 `.bak`，绝不丢掉仍可用的旧 token。（7 天到期是 Schwab 个人开发者账号的硬限制，无法绕过；浏览器登录步骤无法自动化。）

## 快速开始（Quick Start）

```bash
# 1. 克隆仓库
cd ~
git clone https://github.com/quentincong/watchy.git

# 2. 安装依赖
~/.pyenv/versions/3.11.9/envs/trading/bin/pip install -e ~/watchy
# -e 表示可编辑安装（editable install），后续 git pull 自动生效

# 3. 创建配置文件
mkdir -p ~/watchy_config
cp ~/watchy/config.yaml ~/watchy_config/config.yaml
cp ~/watchy/secrets.example.yaml ~/watchy_config/secrets.yaml

# 4. 填入敏感信息（API key、Telegram token）
nano ~/watchy_config/secrets.yaml

# 5. 编辑自选股（可通过 GitHub 远程编辑，git pull 同步）
nano ~/watchy_config/config.yaml

# 6. 启动（测试用）
WATCHY_CONFIG=~/watchy_config/config.yaml python -m watchy.daemon
```

### systemd 生产部署（Production）

```bash
sudo cp ~/watchy/watchy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now watchy
journalctl -u watchy -f  # 查看日志
```

**自动更新（Auto-update）** —— `watchy-update.timer` 每 5 分钟从 GitHub 拉取，有新提交时
`git pull --ff-only` 并重启 daemon：

```bash
# 必需：允许 watchy 用户重启服务（auto-update.sh 以 watchy 身份运行，重启系统单元需 root）
echo 'watchy ALL=(root) NOPASSWD: /usr/bin/systemctl restart watchy' \
  | sudo tee /etc/sudoers.d/watchy-autoupdate
sudo chmod 0440 /etc/sudoers.d/watchy-autoupdate && sudo visudo -c

sudo cp ~/watchy/watchy-update.service ~/watchy/watchy-update.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now watchy-update.timer
```

> ⚠️ 上面的 sudoers drop-in 是**必需的**。`auto-update.sh` 以 `User=watchy` 运行，而
> `systemctl restart watchy` 需要 root —— 缺了它，`git pull` 会成功但重启**静默失败**，
> daemon 会一直跑旧代码（2026-06-14 踩过这个坑）。
> 副作用：每次 push 都会触发重启，**勿在 Tier-2 窗口（~10:00–13:00 UTC；周一跑得长，到 ~14:00）push**，否则打断批次。每票耗时会随 DeepSeek flash 模型重训而变，用前先实测。

## 配置（Configuration）

配置分两个文件：

- **`config.yaml`**（可安全提交）—— 自选股、阈值、冷却时间
- **`secrets.yaml`**（git-ignored）—— LLM API key、Telegram token、Schwab 凭证

详见 `config.yaml` 和 `secrets.example.yaml` 中的完整注释。主要配置项：

| 配置项 | 用途 |
|--------|------|
| `watchlist` | 监控的股票列表（自选股），可按票设置 Tier 1 间隔、Tier 2 UTC 时间、`tier2_days`（分层 cadence，见下）、可选的 `target_price`，以及按票覆盖的 `min_price_proximity_pct`（Tier 2 邻近门控，#15，默认取顶层全局值；目标价缺省时用 #16 自动推导值，持仓票与每周完整风控日永不门控）以及按票覆盖的 `max_tier1_pipelines_per_day`（Tier 1 盘中重扫上限，#23）。Tier 1 不做邻近门控，交易时段内始终扫描 |
| `min_price_proximity_pct` | Tier 2 邻近门控（#15）的**全局默认**百分比，套到所有 watch-only（非持仓）票；普通交易日现价离入场目标价超过该值就跳过当日 LLM。持仓票与每周完整风控日（每周第一个交易日）永不门控，Tier 1 不受影响。可按票用同名键覆盖；删除/留空即全局关闭 |
| `tier2_days` | **Tier 2 分层 cadence**。星期缩写列表（`["mon","wed","fri"]`），指定该票在哪几天跑日常流水线；全局默认套用到没单独设置的票。**整个不写 = 每个交易日都跑**（历史行为）。一次 4 分析师流水线的年成本与仓位大小无关，所以小仓位可以走轻档，同时让批次赶在 13:30 UTC 开盘前跑完。**每周完整风控日**和**已进入止盈区的持仓**永不被跳过 |
| `atr_proximity_mult` | 可选的 ATR 自适应带宽（#15 后续），全局或按票。设了且有 ATR 数据时，门控带宽 = `mult × ATR%`（`ATR% = avg_atr_20d / price × 100`），替代固定百分比——波动大的票更宽、安静的更窄。钳到 `[proximity_pct_floor, proximity_pct_ceiling]`（默认 4–20%）；无 ATR 数据时回退 `min_price_proximity_pct`。用 `scripts/calibrate_atr_proximity.py` 校准 |
| `max_tier1_pipelines_per_day` | Tier 1 盘中重扫上限（#23），全局或按票。每次 Tier 1 信号触发都会跑一条付费 `[market+social]` pipeline + 顾问（仅受每信号冷却约束），所以一只票一天触发多种信号会叠加多次付费重扫（实测 KLAC×4、LRCX×3）。该值上限每票每 UTC 日的 Tier 1 LLM pipeline 次数；超限的触发仍记录+推送（`Signal Fired (rescan capped)`）但跳过 pipeline。按票用同名键覆盖；删除/留空即全局关闭。Tier 2 定时跑不受影响。**出厂值为 `1`**——重扫已不再是当初设上限时的「半价」：2026-08-20 实测一次重扫要花掉完整 Tier 2 的 79%（¥0.453 vs ¥0.573），因为砍分析师只砍 flash 侧，而 `pro` 的 Research Manager + Portfolio Manager 每条 pipeline 都跑、一点不缩 |
| `signal_thresholds` | RSI、成交量、ATR 等信号检测阈值（thresholds） |
| `cooldown` | 每种信号的冷却窗口（cooldown window），防止重复推送 |
| `tier2_throttle_s` | Tier 2 每日扫描时票与票之间的间隔秒数（默认 2.0），平滑 yfinance 请求、避免触发限流 |
| `llm` | 顾问 LLM 配置——支持 Gemini、DeepSeek、OpenAI、Anthropic |
| `telegram` | Telegram 机器人令牌（bot token）和聊天 ID |
| `schwab` | Schwab 券商凭证（持仓数据主源；未配置时自动回退到缓存/手动文件） |
| `positions.yaml` | 手动持仓文件（最终兜底，放 `~/watchy_config/`，不提交）；schema 见 `positions.example.yaml`。**建议填 `total_account_value:`**（账户总值 = 股票 + 现金 + 现金等价物，直接从券商读到的那个数，作为权威分母；或退而填 `cash:` 让 Watchy 自己加）——让顾问按 **总账户价值** 而非仅股票市值判断集中度，避免把正常持仓误判为「过度集中」而错误建议 TRIM |

> **数据获取与缓存**：行情通过 `yfinance` 获取，并叠加 `yfinance-cache` 磁盘缓存层
> （智能缓存，仅拉取缺失/过期的 bar），减少对 Yahoo 的重复请求。缓存层为可选依赖——
> 未安装时自动退回纯 `yfinance`；缓存出现非限流错误时也会优雅降级，不影响扫描。

## 信号检测（Signals Detected）

| 信号 | 检测逻辑 | 默认冷却 |
|------|----------|----------|
| 金叉 Golden Cross | 50MA 上穿 200MA + 完整阶梯 (price > 50 > 150 > 200) + 200MA 上行 | 7 天 |
| 死叉 Death Cross | 50MA 下穿 200MA | 7 天 |
| RSI 超卖 Oversold | RSI 跌破 30 | 12 小时 |
| RSI 超买 Overbought | RSI 升破 70 | 12 小时 |
| MACD 金叉 Bullish Cross | MACD 线上穿信号线（signal line） | 24 小时 |
| MACD 死叉 Bearish Cross | MACD 线下穿信号线 | 24 小时 |
| 布林上轨突破 Upper Breach | 价格 ≥ 上轨 (2σ) | 6 小时 |
| 布林下轨突破 Lower Breach | 价格 ≤ 下轨 (2σ) | 6 小时 |
| 成交量异动 Volume Anomaly (≥2x) | 成交量 ≥ 20日均量的 2 倍 | 4 小时 |
| ATR 飙升 ATR Spike | ATR ≥ 20日均 ATR 的 1.5 倍 | 6 小时 |

> **触发语义**：交叉类（金叉/死叉、MACD、RSI）和水平类（布林、成交量、ATR）信号都是
> **进入态触发（fire on entry）**——只在「从未满足到满足」的那一刻触发一次，条件持续
> 存在期间保持静默，待条件解除并再次穿越才会重新触发。冷却时间是触发之上的额外去重窗口。

## 分级分析师响应（Graduated Analyst Response）

并非所有信号都需要完整的四分析师辩论。Watchy 根据信号重要程度分级调用：

| 触发条件 Trigger | 分析师 Analysts | 辩论 Debate | 风险管理 Risk |
|------------------|----------------|-------------|---------------|
| Tier 2 普通交易日 | 市场 + 情绪 + 新闻 + 基本面 | 多空 Bull/Bear | 简化 Simplified |
| Tier 2 每周第一个交易日 | 市场 + 情绪 + 新闻 + 基本面 | 多空 Bull/Bear | 完整三维 Full 3-way |
| Tier 2 周六 / NYSE 节假日 | —（跳过，休市且冗余） | — | — |
| 金叉/死叉 | 市场 + 情绪 + 新闻 | 多空 | 完整三维 |
| RSI、MACD、布林、强放量 (≥2x)、ATR | 市场 + 情绪 | 多空 | 简化 Simplified |

## Telegram 消息示例

**信号触发时：**
```
Signal Fired — $NVDA
Signal: Golden Cross (50MA ↑ 200MA)
Price: $142.37  RSI: 58.3  SEPA Stage: Advancing
Analysts launching: market, sentiment, news
```

**分析完成 + 持仓建议（Schwab 启用后）：**
```
Analysis Complete — $NVDA
Trigger: Golden Cross (50MA ↑ 200MA)
Verdict: 🟢 BUY (4 analysts)
```

> 分析完成的消息是一眼可读的**摘要标题**——只给判定（BUY/SELL/HOLD + 几位分析师参与）。
> 交易员计划（Trader Plan）、组合经理的最终判定（Risk / Final Call）以及各分析师的原始
> 报告**都不再塞进正文**，而是作为完整的 `.md` 报告附件发送。（没有判定的稀疏流水线回退到
> 一行简短 summary。）持仓 + 顾问建议在**另一条**消息里，保持全文：

```
Your Position:
Current position in NVDA:
  Shares: 50  Average cost: $98.40
  Market value: $7,118.50  Unrealized P&L: $2,198.50

Position Advice: 🟢 ADD (low urgency)
You hold 50 shares with 44% gain. The golden cross confirms the
uptrend is intact. Analysts are bullish with targets 15% above current.
Suggested size: 10-15 shares (~2% of portfolio)
Key risk: If price breaks below the 50MA, the signal is invalidated.
```

## 文件结构（File Structure）

```
watchy/
├── config.yaml              # 非敏感配置（可安全提交，通过 GitHub 编辑）
├── secrets.example.yaml     # 敏感配置模板（本地拷贝后填入真实 key）
├── requirements.txt         # Python 依赖
├── watchy.service           # systemd 单元文件
├── project_doc.md           # 完整技术文档（英文）
└── watchy/                  # 包
    ├── __init__.py           # 包标记
    ├── config.py             # YAML 配置 → 类型化数据类 (dataclass)
    ├── state.py              # SQLite 状态存储 (交叉记忆、冷却、历史)
    ├── indicators.py         # 技术指标计算 (yfinance + pandas, 无 LLM)
    ├── market_calendar.py    # XNYS 交易日历助手 (交易日 / 每周首个交易日)
    ├── orchestrator.py       # 按信号类型的分级流水线选择
    ├── pipeline_runner.py    # TradingAgents 桥接: PipelineSpec → TradingAgentsGraph
    ├── token_tracker.py      # DeepSeek 组件级 token/成本追踪 (TOKENCOST 行, 含 thinking token 拆分, 按调用时刻套高峰/非高峰价)
    ├── advisor.py            # LLM 合成: 分析 digest + 持仓 → 交易建议 (打 GEMINICOST 行)
    ├── positions.py          # 分层持仓源: Schwab → 缓存快照 → 手动文件
    ├── schwab.py             # Schwab 券商 API 客户端 (实时层, schwabdev)
    ├── notify.py             # Telegram 机器人通知
    ├── tier1.py              # 每小时信号扫描
    ├── tier2.py              # 每日完整流水线
    └── daemon.py             # APScheduler 入口
```

## 对接 TradingAgents（Wiring）

`orchestrator.py` 中的 `pipeline_runner` 参数是对接点：一个可调用对象 `(ticker, PipelineSpec) -> dict`。生产实现是 `pipeline_runner.py` 的 `create_tradingagents_runner(...)`，它把 `PipelineSpec` 映射成 `TradingAgentsGraph` 调用（按信号类型选择分析师子集、辩论轮数、风险深度），运行流水线、保存 markdown 报告，并通过 `token_tracker.py` 打出组件级 `TOKENCOST` 成本日志行。

## 文档（Documentation）

完整技术文档见 [`project_doc.md`](project_doc.md) —— 涵盖模块内部实现、数据流、部署、测试策略和配置参考。

## 许可证（License）

MIT
