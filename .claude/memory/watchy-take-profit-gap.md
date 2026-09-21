---
name: watchy-take-profit-gap
description: 用户痛点=卖太晚赢家 round-trip；#28 已实现 LLM+预挂限价单方案(gain-gate floor 浮盈%+ATR runway,advisory-only 整股,Tier2 主+Tier1 盘中触发),opt-in 待上线验证；3.6 vs 3.5 A/B=混合、别为治止盈切模型
metadata: 
  node_type: memory
  type: feedback
  originSessionId: f643c967-dcdd-4f5f-8361-42190c001e1e
  modified: 2026-08-13T16:59:21.911Z
---

# 用户交易痛点 & advisor 止盈缺口

用户自述（2026-07-22）：**「之前卖的都比较晚。往往都是有超额收益不卖、然后就跌回来了。」**
= 赢家冲高时没落袋，浮盈 round-trip 回吐。这是他最想让系统治的毛病。

## ✅ 已落地：advisor 止盈条款（commit f284518）
`ADVISOR_PROMPT` 新增 `TAKE-PROFIT / DON'T ROUND-TRIP A WINNER` 段（advisor.py:55-69）。
触发 = **浮盈可观（软锚 ~15%+）＋ 走势已 extended/动能衰竭（进阻力/目标区、MACD 走弱、
RSI 超买回落、低量反弹、剩余上行 vs 止损不划算）两条件叠加** → 倾向 TRIM。明确不是"涨 X% 就卖"；
**趋势还强/浮盈小则放行 HOLD**。改 Decision(→TRIM) 不改 `Target`(仍 entry-only)。

## ⚠️ 关键结论（2026-07-22 受控 A/B，n=5 + ANET 历史回放）——修正之前判断

**止盈条款目前补不了这个病，而且不是模型能治的——是系统性缺口。**

**ANET 铁证**（用户真实 round-trip 案例：7/13 冲 189、收 181、后回吐；均价 490/3≈163.33、3股、
账户~5900；用 `--position-file` 注入历史持仓 + 7/14 报告回放）：
- @181(+10.8%,锚下)：3.6=ADD / 3.5=ADD ——都想加仓。
- @189(+15.7%,锚上)：**3.6=ADD（还在高点追加！）/ 3.5=HOLD**（"avoid chasing at extended $189"、
  用 $169 止损"protect your 15.7% gain"）。
- **两个模型在任何价位都没 TRIM。** 原因：7/14 报告 Market Analyst 把 ANET 读成"强上升趋势、
  MACD 加速、站上所有均线"——**止盈条款按设计正确地没触发**（它明写强势 intact 趋势应 HOLD）。
  **分析在顶部还喊强，advisor 就永远慢半拍；round-trip 是之后才发生的。**

**3.6 vs 3.5 = 混合，各有失效模式**（同 prompt/持仓快照/low 档、only 模型变，5 票）：
| 票 | 情形 | 3.6 | 3.5 |
|---|---|---|---|
| GOOG | 17.7%超配+FCF崩+今日财报 | TRIM | TRIM（清晰局面都减）|
| AVGO | 13.2%超配、盘整 | TRIM | HOLD |
| NVDA | 10.6%超配、无催化 | HOLD | HOLD |
| SKHY | 2.8%低配、财报前 | ADD | HOLD |
| ANET@189 | 9.6%、+15.7%、extended顶 | **ADD(追顶)** | **HOLD(不追)** |

- **规律**：信号清晰时两个一致；只在**灰区**分歧，且 **3.6 无脑偏行动（双向）**——超配就减(AVGO)、
  见机会就加(SKHY)、**连 extended 顶都想加(ANET)**。3.5 更 inertial/谨慎、**不追顶**。
- **对用户 #1 痛点(round-trip)**：**3.6 追顶(ANET ADD@189)是减分项**——正是制造 round-trip 的行为。
  之前"3.6 更愿卖、可采用"的旧判断（n=2 旁证 AVGO/COHR）**据此撤回**：AVGO 的 TRIM 是集中度驱动，
  遇到真正的 extended 赢家(ANET)3.6 反而追顶。
- **成本**：3.6 在 AVGO/GOOG/NVDA 便宜 ~24%，但 ANET 两跑略贵——**因案而异、大致打平**，非稳定优势。

**live 现状（2026-07-22 定案）**：用户把 live `~/watchy_config/secrets.yaml` 的 model 改成 **gemini-3.5-flash**
（需 `systemctl restart watchy` 生效）→ 现在 **repo 默认 + live + thinking = 3.5-flash / low 三处一致**。
选 3.5 的理由：ANET 显示 **3.6 会追顶(extended 顶还 ADD)=对 round-trip 痛点减分**，3.5 不追顶更贴需求；
成本大致打平。**结论=别为"治止盈"去切模型**（ANET 证伪；止盈靠 #28 机械规则，与模型无关）。
之前"3.6 更愿卖、可采用"旧判断作废。

## 治本方向 → issue #28（2026-07-22 开）→ **已实现（2026-07-23，LLM+限价单方案，非机械止损）**

**⚠️ 最终方案与 issue 正文/下面这段旧设想都不同——用户拍板走 LLM，别再想机械 trailing-stop。**

**落地设计（用户逐项拍板）**：机械部分缩成一个 **gain-gate 触发器**，LLM 仍主导，执行靠**预挂 sell-limit 单**
（限价单自己抓日内高点，所以不需要实时 trailing 代码，日更 cadence 就够）：
- **floor = 浮盈%**（默认 10%，`take_profit_floor_gain_pct` 可按票覆盖）触发进入止盈区；喂给 LLM 的是
  **机械事实**（浮盈越过 floor）当 ground truth，不让 LLM 判顶（绕开"分析喊强/extended 判断不一致"）。
- **runway = ATR**（离分析师上方位还剩几个 ATR → 贴顶就落袋/有空间就让它跑）；上方位从 digest 正则尽力提取，
  提不到就退化 `现价 + k×ATR` 限价。
- advisor 新增 **`Take-Profit:` 输出行** = 挂单价 + **整股**数（用户不玩碎股）。
- **主触发=日常 Tier 2**（持仓票在区内就注入指引）；**盘中触发=Tier 1 zone-entry**（浮盈盘中首次越 floor→
  只跑 advisor、复用 digest 不跑全 pipeline，cooldown 保护，靠新列 `state.prev_take_profit_zone` 做 on-entry 检测）。
- **advisory-only**：系统给价+股数，用户手动挂限价单。

**新增文件/改动**：`watchy/take_profit.py`（纯逻辑）、`watchy/digest_store.py`（复用最近 digest）、
`take_profit` config 段（opt-in `enabled:false` 出厂）、`notify.take_profit_alert`、advisor 注入+解析。
顺手修了 `config.py` 读 YAML 未指定编码的隐性 bug（Windows gbk 读非 ASCII 注释崩→改 `encoding="utf-8"`）。
6 个 commit（a815f33→20fb12d）已 push，357 tests green。

**上线状态（2026-07-27 核实）**：用户已在 **VPS `~/watchy/config.yaml` 直接改 `enabled: true`**，
自 **2026-07-23 起 live 跑了 4 天**，journal 里 take-profit 日志量很大（确在触发）。当时那个改动是
**VPS 本地 dirty（`git status` 显示 ` M config.yaml`）、没进 repo** → 下次 `git pull` 会在这行冲突。
**已收敛**：本地 commit `799c7f8` 把 repo 默认也改成 `enabled: true`（连带 README 配置表 + 本行说明 +
IMPLEMENTATION_PLAN 的 "opt-in" 措辞一起改），357 tests green。
**VPS 收敛步骤**（必须等本地 push 之后再跑，否则会把 live 的 flip 丢掉、止盈静默）：
`cd ~/watchy && git checkout -- config.yaml && git pull`。
⚠️ push/重启避开 10:30–12:00 UTC Tier-2 窗口（auto-update timer 会重启 daemon）。
**2026-07-27 验证结论：4 天零触发 = 正确行为，不是 bug**。持仓最高浮盈仅 EMR +7.0% / SKHY +6.2% /
NVDA +4.6%，全都够不到 floor 10%（Schwab live 正常、cost basis 正常）。**注意：journal 里根本没有
`Take-Profit:` 字样**——那是 LLM 输出行、只进 Telegram；真正的 journal 标记是
`take-profit zone active for X: gain=..` (advisor.py:151, INFO) 和 `Take-profit zone entered` (tier1.py:269)。

**受控 dry-run 验证通过（EMR，floor 临时降 5% 且只改内存、不动 live config）**：gate 触发 → 正则从
7/24 digest 提到 `upside=185.0` → runway 9.8 ATR(>far 2.5) → 走 stretch 3×ATR → 输出
`take_profit: sell 1 share at 157.44`(整股、在市价上方)、decision=HOLD。一次 advisor 调用 $0.0112。
**核心链路全对。** dry-run 脚本模板见本条历史（load_config→改 c.take_profit.floor_gain_pct→
compute_indicators→load_digest→get_advice）；注意 IndicatorBundle 字段是 `current_price` 不是 `price`。

**🐛 限价锚定价源不一致 → ✅ 已修（commit `461e476`）**。原 `advisor.py` 有 bundle 时 `price =
indicator_bundle.current_price`（yfinance，EMR=145.33），但 gain 来自 Schwab（live=148.72）——
差 $3.40≈0.84 ATR，限价偏低=卖便宜，方向性错误。现改走 `tpmod.anchor_price(pos, bundle)`：
**优先持仓 live mark（gain 的同一价源），bundle 只当 fallback**；ATR 仍只能取自 bundle。

## 🏁 2026-08-07 实盘验证：Tier 2 + Tier 1 两条路都已确认 live 触发

VPS 状态：`461e476`（= origin/main 最新）、`git status` 干净（config.yaml 已收敛、不再 dirty）、
`take_profit.enabled: true` / `floor_gain_pct: 10.0`。

**Tier 2 主触发已证实（APH，2026-08-06，+11.8% > floor 10）**：Telegram 卡片 `Trigger: Scheduled
Daily Run`（=`_signal_label("scheduled_daily")`，Tier 2 而非 Tier 1），advisor 正文原话
*"the **mechanical take-profit trigger** is technically active with an **11.8%** unrealized gain"*
——这句复述的正是 `build_guidance()` 注入的 `TAKE-PROFIT ZONE ACTIVE (mechanical trigger — ground
truth...)` 抬头 + 机械 gain 数字，**LLM 不可能凭空产出**，即 `tier2.py:211 → get_advice(
indicator_bundle=) → advisor.py:190` 全链路跑通。

**Tier 1 zone-entry 也首次实跑（AVGO，2026-08-07 21:35，+10.5%）**：`💰 Take-Profit Zone — $AVGO`
即 `notify.take_profit_alert()` 抬头原文，带 `Sell-limit: sell 1 share at 454.16`。
**顺带证明锚定修复生效**：Schwab live 430.57 + 1.5×ATR 15.73 = 454.165 → 输出 454.16，
锚在 broker mark（与 gain 同源）而非 yfinance bundle 价。

**⚠️ 结构性限制：小仓位上止盈天然失效 → ✅ 已修（commit `046b1cf`，2026-08-07）**

原因：`build_guidance()` 里 runway-large 分支和 odd-lot 护栏**双重导向 HOLD**，模型写 `N/A`，被
`_has_take_profit()` 过滤 → Telegram 一个字没有。8/7 实例：EMR +12.9%/1 股、APH +10.8%/1 股 都是零输出，
**只有 2 股的 AVGO 出了单**（`sell 1 share at 454.16`）。更糟：EMR 那张卡还建议**加到第二股**。
另外原文写着 "the user does not trade fractional shares" —— **这个前提是假的**（ASML 实持 0.2 股），
且与同段的 "or the whole position" 自相矛盾。

**修法 = 股数三档**（新增 `take_profit._sizing_directive`，`build_guidance(..., shares=)`）：

| 股数 | 可提议的动作 |
|---|---|
| **≥2** | 现状：限价减 N 整股 |
| **==1** | 部分减仓在算术上不存在 → 只有全清或持有。**仅当 ATR runway < `runway_near_atr`（贴顶）才允许提议全清**，有空间就 HOLD（= 用户拍板的 "A+runway 护栏"） |
| **<1 碎股** | 限价单需要整股 → **不给限价**，只给市价"减仓 or 全清"文字指令。用户确认：0.2 股**不能挂限价单**，但**可以部分卖出 0.1 股** |

碎股档的代价要记住：**丢掉了"预挂限价单自动抓日内高点"这个 #28 的核心机制**，退化成需要手动执行的提醒。

**待查（低优先）**：用户贴的 APH 卡片在同一时间戳 20:34 出现两次。可能只是粘贴重复；若 Telegram
真收到两条相同 Tier 2 卡片则是 duplicate-notify bug。核验：
`journalctl -u watchy --since '2026-08-06' | grep -c 'Advisor for APH'`（应为 1）。

**#28 剩余**：核心 + 两条触发路径均已实盘验证；可考虑结单。

## 🐛 2026-08-13 用户发现：HIFO 成本基使浮盈% 单调棘轮 → issue #30

**用户按 HIFO 卖出（先卖成本最高那股）**。每次减持剥掉最贵的 lot，剩余股均价下降，
**价格完全不动，报告浮盈% 反而上升**。lots 130/110/100/85/75、价钉死 $115：
5股 +15.0% → 4股 +24.3% → 3股 +32.7% → 2股 +43.8% → 1股 +53.3%。
数字对剩余股是**算术正确的**，但它响应的是**我们自己的卖出**、不是行情，且只增不减。

**三处后果**：
1. **Tier 1 盘中触发在首次减持后死掉**。`_check_take_profit_zone` 靠 `prev_take_profit_zone`
   做 0→1 边沿；latch 在 1 后只有浮盈跌回 floor 以下才复位，而虚高的基使这个门槛越来越深
   （从 $115 起：未减持需 −4.3%，减1次 −11.5%，减2次 −17.1%）→ **复位时赢家已经 round-trip 完了**。
2. **喂给 LLM 的 gain 幅度虚高，共三个注入点**（只改一个无效）：
   `take_profit.build_guidance`（`"+X% WINNER"` + 无 runway 时的 `"given the gain"` 兜底）；
   **`advisor.py` 常驻系统提示**里 `see "Unrealized P&L" ... think roughly 15%+`
   ——**每次调用都在、与门控无关**，还硬编了数值锚；仓位块 `render_position()` 本身
   （这个**必须保留**，同一份 `format_position_context()` 也进 Telegram 给人看）。
3. **成本**：`tier2.py` 对区内票永久豁免 `tier2_days` 节流 → 天天跑全量 pipeline（~$9.6/年/票）。

**用户定调（关键设计原则）**：gain% **当触发器保留**（">10%=值得持续评估"仍成立），
**当决策依据剔除**——挂单价和股数只能来自 price/ATR/runway（这部分本来就干净）。

**✅ 已修（commit 4fd9436）**：Tier 1 改为**持仓股数下降即重新武装**。
选股数而非价格水位线的理由：**这套设计的保护力来自"挂着的限价单"、不是提醒**——
单子在市场上挂着时盘中冲高它自己成交，不需要新建议；**你只在成交后那一刻无保护**
（单子没了 + flag 还 latch 着），暴露约一个交易时段。Watchy 从不看订单簿，只能从持仓推断成交。
手动卖出也会重新武装，这是对的。新列 `ticker_state.prev_quantity`（**需 ALTER TABLE 迁移**，
已对预存 schema 验证）。护栏：股数**上升**永不算成交（cache 快照会给出减持前的大数）、
无基线不武装、`cooldown_h` 仍生效、低于 floor 也记录股数（避免区外卖出被当成新成交重放）。

**✅ 已修其二（commit e43b3dc）：从提示词剥离 gain 幅度**。三个注入点里**前两个改掉、第三个必须留**
（`render_position()` 的百分比是真实券商数字，且同一份文本要进 Telegram 给人看）——所以前两处
改成**显式中和**而不是静默省略：只删数字的话 LLM 照样从仓位块里读。
- `build_guidance` **签名去掉 `gain_pct`**（让"幅度不许进 prompt"变成结构约束而非约定）；
  是否越过 floor 留在 caller 当武装条件；`advisor._take_profit_guidance` 仍 log 那个数字供对账。
- **紧迫感刻意保留**（用户痛点是卖太晚不是卖太早）：指令仍然要求"必须给结论——挂单落袋 or
  用具体剩余上行论证持有，不许沉默"，常驻段落仍 `lean toward TRIM`；只是幅度没了，
  定量本来就由 ATR runway + 股数驱动。
- **提示词改动没有测试能证明有效**（单测只能验字符串在不在）——真实效果看上线后 advisor 输出。
410 tests green，`4fd9436`+`e43b3dc` 已 push（2026-08-13 09:53 UTC，Tier2 起跑前 7 分钟，
用户知情并要求推；用户手动 pull&restart）。

**⏳ #30 剩余（用户明确说先不动）**：Tier 2 止盈区永久豁免 `tier2_days` 节流。
钱不是重点（latch 一票 +$4~6/年），**真正代价是批次时长**——`tier2_days` 当初就是为了让批次
赶在 13:30 UTC 开盘前跑完（7/31 重训后时长翻倍），而 latch 恰好对最可能 latch 的那批
（持仓赢家）静默解除节流。repo config 12 票里 11 票配了轻量轮换，持仓的 6 票是
EMR/APH/NVT(mon,wed,fri) + ASML/VST/LUMN(tue,thu)，latch 一次就跳回 5 天/周。
**建议先测再改**：`journalctl -u watchy --since '7 days ago' | grep -c 'not scheduled today'`
（cadence 还在不在起作用）+ `grep 'take-profit zone active'`（几票在区内）。
不紧急：方向是"多跑"不是"少跑"，不会漏止盈信号。

**未做（deferred）**：机械 trailing-stop（撤销）；一等公民阻力位提取（现正则尽力）；触发时的 full-pipeline hybrid；
Schwab 自动下单。= **#17 候选 A 卖出侧实例**（#26 买入侧无关）。

---
**（作废的旧设想，留档对比）** 曾想：机械止盈/移动止损跑 Tier 1、峰值回落 X%/ATR 倍触发、摸 #16 derived_target
+浮盈阈值→TRIM。**已被 LLM+限价方案取代**——关键洞察=限价单预挂就能抓日内高点，不必实时 trailing；且
#16 `derived_target` 是**入场价**（对持仓赢家在现价下方，方向错），当止盈天花板用不了。

## 工具/方法学备注（本次 session 修的坑）
`scripts/compare_gemini_models.py` / `compare_gemini_thinking.py` 之前有 **digest 还原 bug**：
把报告 `### Portfolio Manager` 段(=risk-debate judge_decision，生产放 `risk_assessment`)错塞进
`_decision_raw`、且丢 risk_assessment → 同模型同档 decision 都会翻。**已修**（aafe74b/1ace8b6：
正确映射 risk_assessment；`_decision_raw` 留空,因 .md 不存图的 final_trade_decision）。
**含义**：① 回退 3.5 当初那次 A/B 用的是脏 digest，依据存疑；② 离线工具**复刻不了 live 批次的逐决策**
（缺 final_trade_decision 块 + position 现拉），只适合**同一次运行内的受控 A/B**（模型vs模型、档vs档）。
新增 `--model`(thinking脚本)、`--position-file`(models脚本,注入历史持仓回放已卖出的票)。
详见 [[watchy-api-cost-baseline]]。

## 🐛 2026-08-13：SKHY 止盈两条腿全断（issue #31，实现待议）

SKHY 是新上市 ADR、只有 23–24 根日线，而 `indicators.py:76` 有 `len(close) < 200 → return None` 的硬门槛：

1. **Tier 1**：`tier1.py:52-55` 在 `scan_ticker` 开头就 `return []`，**第 95 行的 `_check_take_profit_zone`
   永远走不到** → 盘中 zone-entry 对它根本不存在。
2. **Tier 2**：容忍 `bundle is None` 照跑照付钱，但 `avg_atr=None` → **限价锚 `price + mult×ATR` 算不出来**。

**净结果 = 一只持仓票、挂在最贵的每日档（涨价后 ~$24/年）、止盈完全无保护**，自然痊愈要等约 176 个交易日（8–9 个月）。

⚠️ **不能只把门槛数字调低**：`rolling(200)` 在短序列上返回 **NaN 而不是报错**，而 **NaN 在 Python 里是 truthy**，
会穿透 `detect_signals:145` 的 `if sma50 and sma150 and sma200:` 和 `_classify_sepa_stage:370` 的
`if not all([...]): return None` → **假死叉** + **编造的 SEPA stage 被当事实喂给 advisor**（不报错、不为 None、
就是个看着正常的错答案）。**那道 200 门槛现在是承重墙**，挡的就是 NaN 外泄。

修法（#31）：取消全局门槛改逐指标判断（不够写 None 绝不写 NaN）+ `avg_atr_20d` 不足 34 行给 None
（现在会静默把单日 ATR 塞进这个字段，而止盈限价就骑在它上面）+ 启动校验 Telegram 告警 + 短历史 fixture 测试。
下游零改动（消费方已全部 None-safe，逐一核过）。

⚠️ 连带：MU 已于 2026-08-13 删除（理由正是"与持仓的 SKHY 重复"），所以现在 **HBM 这条线没有任何可用监控**。

## 2026-09-21 用户改判：1 股持仓也给止盈（推翻 8/7 的 "A+runway 护栏"）

触发：SKHY(+25%)/COHR(+15%) 都是 1 股，零止盈输出。用户说 "Allow take profit at one share."
新规则（`_sizing_directive` ==1 档）：**永远要求整股全清的限价单**——贴顶(runway<`runway_near_atr`)
用 reachable 价(price+1.5×ATR)；有空间或 runway 未知 → **stretch 价(price+3×ATR)**，预挂、只在冲高时成交，
不在当前价卖掉赢家。N/A 只在分析给出"连 stretch 都太低"的具体理由时才允许。advisor 奇股护栏补一句：
护栏只管 Decision，HOLD + 预挂整股 Take-Profit 不矛盾。
