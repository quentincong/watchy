---
name: watchy-model-selection-eval
description: Watchy 选模型的评测方法 + 2026-08 AA 指数/价格实测；关键发现 V4-Pro(44) < V4-Flash(50)
metadata: 
  node_type: memory
  type: project
  originSessionId: 0c826df4-315c-43ad-9514-7423b3ce1f24
  modified: 2026-08-14T13:29:52.130Z
---

# Watchy LLM 选型 & 评测方法（讨论中，2026-08-02）

## 2026-09-10：Urgency / reachability 提示词上线复盘 → 确认过度矫正

9/2 的提示词改动把 `Urgency` 定义为行动时限，并直接告诉模型：用户会读 MEDIUM/HIGH、把 LOW
丢掉不看；同时允许 `HOLD` 为 HIGH、要求按 `Target`/止盈位距离和价格方向判断。9/10 从 VPS
`advice_log` 检查 9/3–9/10 共 **77** 条生产结果：只有 **10 LOW**，66 条合法 MEDIUM/HIGH，另有
1 条格式污染；`HOLD` 共 48 条，其中 **38 条（79%）被升到 MEDIUM/HIGH**。对比改动前 A/B 的
104 个 incumbent HOLD **100% LOW**，以及全部 328 次仅出现 1 个 HIGH，这不是温和校准而是反向过冲。

已确认的三个提示词缺陷：

1. `DISCARDS LOW ones unread` / `never park a call there that you would want seen` 把本应客观的时间分类
   变成“争取曝光”的通知路由；模型有动机把几乎所有值得一提的内容升档。
2. `A HOLD can be HIGH` 与可自由解释的 `receding fast enough` 让无动作卡也能紧急。生产已有 7 个
   `HOLD+HIGH`；VRT 甚至出现 `HOLD+HIGH+Target:N/A`。同日另一条 VRT 上游是 SELL/Underweight、
   明确要求减到不超过半仓，advisor 却给 `HOLD+HIGH`，把“要立即关注”和“不要动作”拼在一起。
3. Urgency 要求依赖 `Target`，但 `Target` 的既有契约只允许**入场/加仓价**。TRIM/SELL/HOLD 没有通用
   的行动价字段，模型会随机拿报告里的 downside objective / support / future re-entry level 来计时；
   例如 VRT 的 PM downside `Price Target: 242` 被 advisor 写成 `Target: 242` 后又给 HOLD+HIGH。

另有一个确定的格式 bug：VST 一次输出把字段写成
`MEDIUM — HOW SOON THE USER MUST ACT, AND NOTHING ELSE.`，因为说明文字被塞在
`Urgency: <HIGH / MEDIUM / LOW — ...>` 同一占位符内；parser 不校验枚举，整串原样入库，Telegram
也匹配不到图标。

结论：异常来自提示词设计，不是 VPS、provider 或模型漂移；生产 advisor 仍是
`gemini-3.5-flash` + Tier1/Tier2 `low`。已按用户批准的方案修正：删除用户读卡行为提示，规定
`HOLD` 永远 LOW，HIGH=今天要改订单、MEDIUM=五个交易日内要决策、LOW=本周无需改订单；BUY/ADD
只用 entry `Target` 计时，TRIM/SELL 只用 detail 的 exit level 或 armed Take-Profit。输出模板恢复纯枚举，
parser 提取开头合法枚举、异常值回退 LOW 并 warning，且代码层强制 HOLD/LOW。odd-lot 也从“sensible”
改成精确股数算术。按用户要求不做部署前 live A/B / replay。

这一些开发内容是codex在powershell里做的。

## 2026-09-10：DeepSeek V4.1 Flash → pipeline 被迫合流，advisor 不换

- 新 canonical id=`deepseek-flash`；旧 Flash 已退役。官方又宣布 9/14 04:00 UTC 起 Pro alias 也转到
  V4.1 Flash，直到 V4.1 Pro，所以“RM/PM=Pro、其它=Flash”的二模型架构客观上不能继续。
- Watchy 两个 pipeline role 直接统一为 canonical `deepseek-flash`，避免服务端已经是 Flash、日志却写
  Pro 的假象。旧 Pro-vs-Flash 降本 A/B 待办到此失效；以后重开要等真正的 V4.1 Pro。
- 不把 advisor 切到 V4.1：发布页只有通用/agent benchmark 和官方综合结论，没有 AA-Omniscience
  幻觉率、AA-LCR、IFBench 这类能迁移到“严格按报告生成价位卡片”的证据。即便综合能力超旧 Pro，
  也没有推翻 advisor 留 Gemini 3.5 Flash 的核心校准判据。
- hosted API 兼容，无需手改 prompt encoding；thinking 仍默认 high。必须改的是 canonical model id 与
  TOKENCOST 的 9/10 新价、9/14 Pro alias 路由切点。

这一些开发内容是codex在powershell里做的。

## 2026-08-14：GPT-5.6 评估 → **不换，比 DeepSeek 那次还干脆**

**AA-Omniscience Index 两个家族完全不重叠**：Gemini 最差档（3.5F-minimal, **1**）> GPT-5.6 最好档
（Terra-max, **0**），其余 GPT 全是负数（−3 ~ −25）。幻觉率：**GPT-5.6 全系推理档 88–93%**
（Terra-max 88 / Terra 其余 89-90 / Luna 90-93 / Terra 不推理 95），= V4-Pro 95% 的同一档，
比 3.5F 的 62% 差 **26–31pp**。advisor 正是吐 `Take-Profit:` 限价 + `Target:` 的组件 → 一票否决。

| 模型 | AA-LCR | 幻觉↓ | $/1M in/out |
|---|---|---|---|
| **3.5F（在跑）** | **81.0** | **62%** | $1.50 / $9.00 |
| GPT-5.6 Terra (max) | 79.7 | 88% | **$2.00 / $12.00** |
| GPT-5.6 Luna (max) | 78.3 | 93% | $0.20 / $1.20 |
| GPT-5.6 Luna (medium) | 72.0 | 91% | $0.20 / $1.20 |

- **Terra 被完全支配**：LCR 更低、幻觉差 26pp、**每 token 还比现在贵**（$12 > $9），
  且 21k 输出 token/task（啰嗦，同 V4-Pro 的死因）。没有取舍可谈。
- **Luna 是唯一真候选，且只赢在价**：输出便宜 7.5× → advisor $15/月 → **~$2/月，年省 ~$155**
  （**目前所有候选里省得最多**），代价是拿 93% 幻觉率的模型写卖出限价。不成立。
- GPT-5.6 定价（7/30 降价后）：Terra $2/$12、Luna $0.20/$1.20；缓存输入 10%，Batch API 再半价。
- GPT-5.6 同样**没有 IFBench 数据**。

**⚠️ 架构论据在这里不适用（要说清楚）**：当初否掉 DeepSeek 的两条非智力理由（SPOF：Tier1 卖出路径
不该依赖 pipeline 的 provider；独立校验：同族模型容易顺着 pipeline 走）**OpenAI 两条都满足**——
它是真正独立的第三家。**GPT-5.6 只输在校准这一条。** 说清楚是因为：哪天 OpenAI 出个 Flash 式校准档，
架构理由不会再帮你挡第二次。

**🔑 两天三个家族 → 这已是规律不是三个孤立结果**：DeepSeek V4-Pro 88–96%、GPT-5.6 88–95%、
Gemini Flash 56–68%——**Gemini 在弃权/校准上孤零零领先 ~25pp**，而另外两家都是「综合分打平或反超、
这一项惨败」。→ **advisor 的模型实质锁死在 Gemini Flash**，直到别家出现幻觉率 <70% 的型号为止；
**不要再因为综合分变动重开这个议题**。

**真迁 OpenAI 的话代码没准备好**（`_call_openai_compatible` @ `advisor.py:545` 是给非推理模型写的）：
① **没有 reasoning-effort 参数** —— GPT-5.6 上这是 40 分的断崖（Luna 不推理 LCR 38.7 vs max 78.3），
默认档静默跑 = 灾难且不可见；② 发的是 `max_tokens` 而非 `max_completion_tokens`，
**OpenAI 推理模型的 reasoning token 计在这个预算里** = 7 月 `thinkingBudget:-1` 截断事故的同款形状、换个厂；
③ **完全没有成本仪表**（`GEMINICOST` 只在 Gemini 分支），迁过去等于退回「advisor 全靠后台看账」。

## 2026-08-14：Gemini 3.7 Flash 评估 → **不换，advisor 留 3.5F**（用户 OCR AA 子项）

3.7 Flash 于 **2026-08-13 发布**，主打编码/agent，**introductory 半价 $0.75/$3.75 到 2026-12-31**，
2027-01-01 恢复 **$1.50/$7.50**（对比 3.5F 的 $1.50/$9.00）。model id `gemini-3.7-flash`，
同样走 `thinkingConfig.thinkingLevel`，档位文档只列 **low/medium/high（无 minimal）**。

| 模型 | AA-LCR | Omni **Index** | Omni 准确率 | **幻觉率**↓ | AA-LCR $/task |
|---|---|---|---|---|---|
| **3.5 Flash（在跑）** | **81.0** | 21 | 51% | **62%** | $0.19 |
| 3.5F (medium) | 79.7 | 21 | 51% | 62% | $0.18 |
| 3.6 Flash | 79.0 | 22 | 50% | **56%** | $0.18 |
| 3.7F (low) | 78.3 | 22 | 54% | 68% | $0.08 |
| 3.7F (medium) | **81.0** | 24 | 54% | 66% | $0.09 |
| 3.7F (high) | 80.0 | **26** | 55% | 65% | $0.09 |
| 3.5F (minimal) | 58.3 | 1 | 43% | 74% | $0.16 |

**🔑 新增判据：Omni Index 涨 ≠ 对 advisor 更好——必须拆成准确率/幻觉率两半看。**
Index = 准确率 与 幻觉率 的净额。3.7F 的 Index 上涨全来自**准确率**（闭卷参数化知识），
而 advisor 是 **grounded on digest、不考知识**，那一半对 watchy 无意义；
能迁移的是**幻觉率 = 不会的时候是编还是弃权**，正对应编造 `Take-Profit:`/`Target:` 价位——
**3.7F 在这一项比 3.5F 差 4–6pp、比 3.6F 差 9–12pp**。

**结论：换 3.7F 智力上买不到东西**（AA-LCR 81.0 = 81.0 平手；3.7F-low 78.3 反而退步），
**纯粹是省钱盘**，而钱很小：advisor ≈$12–15/月 → 现在省 ~$6/月；**2027-01-01 恢复原价后只省 11–15%**，
且 medium 档 reasoning token 若翻倍（AA 实测 3.7F-medium 16k reasoning vs 现在 3.5F-low ~1900 think）
**明年反而更贵**。按既定原则「按架构和风险决策，不按省钱」→ 不动。

**两条加重风险**：① **3.7F 没有 IFBench 数据**（该项已掉出综合分），而它是"编码/agent 调优 + 降价"
的刷新——正是本 memory 警告的"抬综合分、回退尾部格式遵循"画像，失败形态 = `Take-Profit:` 正则静默不匹配；
② 昨天才发，零现场数据。**但它是独立 model id 不是浮动别名 → 没人逼你迁，可以等 IFBench。**

**真要测的话**：用 **medium 不用 low**（low 比现状掉 2.7 LCR；medium 打平且约半价）。先修三个雷：
- `advisor.py:449-450` `_GEMINI_PRICE_IN/OUT` 写死 1.50/9.00 —— **这已是第三次会变陈旧**；
  且 intro 价需要 **2027-01-01 按调用时刻切价**，照抄 `token_tracker._prices_at()` 的做法。
- `advisor.py:576` `_gemini_thinking_config` 把 `off`→`"minimal"`，**3.7 文档无 minimal → 400**。
  现在两层都是 `low` 不会触发，但是埋着的雷。
- `scripts/compare_gemini_thinking.py:136,191` 默认值还写着 `gemini-3.6-flash`（**已陈旧**，会静默测错模型）。
- live model 在 VPS `~/watchy_config/secrets.yaml`，切换是 VPS 侧改配置、不是 push。
- 判据用 `compare_gemini_models.py` + 格式解析指标（Take-Profit 行在否 / 限价 vs ATR / 整股 / 尾表解析率），
  **并记录 think token**——明年的经济性全押在这个数上。

## 现状（2026-08-13 更新）
- **advisor = Gemini 3.5-flash，Tier1 与 Tier2 都是 thinking `low`**
  （3.6 用过一段、用户体感不佳已回退 3.5；Tier1 原 `off`，2026-08-13 上调到 `low`，理由见下）
- TradingAgents: deep_think = `deepseek-v4-pro`(RM/PM 两个节点), quick_think = `deepseek-v4-flash`，
  **两者都在 thinking `high`（DeepSeek 默认，watchy 不传参）；只有 high/max 两档，没有中间档**

## 🚨 2026-08-13 全面翻案：V4-Pro-0813 静默发布 + AA 子项数据推翻综合分结论

**V4-Pro-0813 于 2026-08-13 上午发布——无公告、无 changelog，只是价格表里冒出带日期快照。**
watchy 用浮动别名不锁版本（`pipeline_runner.py:29-30`）→ **VPS 的 RM/PM 当天就吃到新权重**。
下面「刷新前冻结 fixture」那条待办的窗口就此关闭。**但基线没丢**：`notify.py:237-241` 每次跑完都把
完整 markdown 报告当**文档附件发到 Telegram**，所以 pro-old 的 RM/PM 输出全在 Telegram 里（VPS
`~/watchy/reports/*.md` 是另一份）。

### 综合分说 Pro 赢，子项说 Gemini 赢 39 分 —— 以子项为准
| 模型 | AA Index | 8/02 时 |
|---|---|---|
| DeepSeek V4-Pro-0813 (max) | **53** | 44 |
| Gemini 3.6 Flash (high) | 52 | 50 |
| DeepSeek V4-Flash-0731 | 50 | 50 |
| Gemini 3.5 Flash (medium) | 47（估） | — |

**但 AA 指数构成变了**：现在 9 项含 GDPval-AA v2 / τ³-Banking / Terminal-Bench v2.1 / SciCode
——agentic+编码权重比 v4.1 更高，**对 watchy 的代表性反而更差**，53 vs 52 的 1 分可能全来自 Terminal-Bench。
**IFBench 已掉出综合分**（仍单独跑）→ **模型可以一边抬综合分、一边回退恰好会弄坏正则解析的那一项**。

### 三个关键子项实测（用户 OCR 自 AA，2026-08-13）
**AA-Omniscience 幻觉率（越低越好）**：Gemini 3.6F **56%** < 3.5F(medium/满) 62% < 3.5F(minimal) 74%
< V4-Pro 88% < V4-Pro(high) 89% ≈ V4-Flash(high) 89% < V4-Flash-0731(max) 92% < V4-Pro(max) 94%
< **V4-Pro-0813(max) 95%** ≈ V4-Flash 95% < V4-Flash(max) 96%。
**AA-LCR**：3.5F **81.0%** > 3.5F(med) 79.7% > **3.6F 79.0%** > V4-Pro-0813 75.3% > V4-Flash-0731 74.3%
> V4-Pro(max) 70.0% = V4-Flash(max) 70.0% > V4-Flash(high) 69.0% > V4-Pro(high) 67.0%
> 3.5F(minimal) 58.3% > V4-Pro(不思考) 49.7% > V4-Flash(不思考) 37.3%。
**IFBench**：V4-Flash(max) 79.2% > V4-Pro(max) 76.5% > 3.5F 76.3% > 3.5F(med) 74.6% > V4-Flash(high) 73.5%
> V4-Pro(high) 71.3% > 3.5F(minimal) 47.3% ≈ V4-Flash(不思考) 47.2% > V4-Pro(不思考) 45.8%。

### 由此定下的三个结论
1. **advisor 留 Gemini，不换 DeepSeek——已结案。** 幻觉 56/62% vs 95%，差 ~39 分，而 advisor 正是那个
   吐可编造数字的组件（`Take-Profit:` 限价、`Target:`）。**且涨价后省钱理由也没了**：Pro 非高峰 $0.66/$1.98
   vs Gemini 3.5F $1.50/$9.00，叠加 **Pro 很啰嗦（AA 指数跑了 130M 输出 token vs 3.6F 59M = 2.2×）**，
   实际只省 ~$38/年。19× 的老说法作废。**按架构和风险决策，不按省钱**（旧结论仍成立，但现在有两个理由）。
   ⚠️ 口径提醒：AA-Omniscience 是**闭卷参数化知识**测试；watchy 的分析师有工具、advisor 有 digest，
   都是有接地的，所以**绝对值夸大了风险（DeepSeek 88-96% 照样跑得好好的），能迁移的是排序**。
2. **3.6→3.5 回退是对的，且有数据支撑**：3.5 的 AA-LCR 81.0% > 3.6 的 79.0%，而 AA-LCR 正是 advisor
   的本职（综合 4 份分析师尾巴+risk+decision）。代价 = 3.5 输出 $9 vs 3.6 $7.50 且多 17% 输出 token
   ≈ 贵 40%（≈+¥240/年），为信得过的建议值这个钱。**这也解释了当初 n=5 A/B 为什么"混合"**：3.6 换来的
   是幻觉少 6 分、成本低，丢的是长文综合——不是全面变好。
3. **Tier1 advisor thinking `off`→`low`（2026-08-13 已改）**：`advisor.py:555` 把 `off` 映射成
   `thinkingLevel: minimal`，而 3.5F(minimal) 是**三项全场最差**（IFBench 47.3 / AA-LCR 58.3 / 幻觉 74%），
   偏偏 Tier1 跑的是**盘中止盈 zone-entry**（要吐可正则解析的 `Take-Profit:` + 限价 + 整股）。
   上调到 low 只花 ~$8/年。**关键洞察：档位比选模型更能改善幻觉**——3.5 从 minimal 调上去省 12 分，
   换 3.6 只省 6 分。

## ⚠️ thinking 关掉是断崖，不是斜坡（这条决定 #27 怎么做）

关掉 thinking 后三个模型一致暴跌：**IFBench 掉 29–32 分、AA-LCR 掉 23–37 分**（数字见上表）。
且 **DeepSeek 只有 high/max 两档，watchy 已在 high**（[[watchy-api-cost-baseline]]）→ **没有中间档可退**。

**#27（关 4 个 analyst 的 thinking）因此风险很高**：TradingAgents 四个 analyst 的 prompt 结尾都是同一句
"Make sure to append a Markdown table at the end of the report"（`analysts/fundamentals_analyst.py:28`、
`news_analyst.py:27`、`market_analyst.py:51`、`sentiment_analyst.py:160`），而 watchy 的
`advisor._analyst_summary_tail()` **正是锚定那张表**。这是教科书级 IFBench 失败点：
**末尾格式指令是指令遵循下降时最先被丢的东西**。丢了以后 `advisor.py:389` 静默退回**报告前 400 字**
（= 开头 prose，不是结论）→ 4 个分析师同时降级、日志无任何报错。
- **钱**：analyst thinking ≈ 账单 11% ≈ **$25–30/年**。**砍 2–3 只值守票收益相同、风险为零** → #27 不是钱在的地方。
- **要做就先测**：重放 ~20 份 analyst prompt（thinking disabled），数还有几份能被解析出结尾表格。20/20 才做。
- **无论如何先做的**：`ADVISOR_TAIL_FALLBACK` 警告已加（2026-08-13 commit），把静默失效变成可 grep，
  顺便给 #27 的重放测试提供**当前配置下的对照基线**（现在没人知道 fallback 率是不是 0）。
- **辩手（Bull/Bear/3 风险辩手）保留 thinking**（用户定）：对抗性判断节点，thinking 最可能真值回票价。

## 2026-08-02 实测数据（AA Intelligence Index v4.1）
- Gemini 3.6 Flash (high) = **50**（与它取代的 3.5 Flash 同分 → 解释了当初 3.6vs3.5 A/B 为何"混合"：本来就没差）
- DeepSeek V4-Flash-0731 (reasoning, max effort) = **50**
- DeepSeek V4-Pro (reasoning, max effort) = **44**
- AA 混合价：V4-Flash $0.06/1M vs Gemini 3.6 Flash $1.16/1M ≈ **19 倍**
- 官方 per-1M：V4-Flash $0.14/$0.28（cache hit $0.0028）; V4-Pro $0.435/$0.87; Gemini 3.6F $1.50/$7.50（缓存 $0.15）
- V4-Pro 强项是 SWE-bench 80.6%（编码），与 watchy 无关；AA v4.1 权重（HLE/GPQA-D/CritPt/AA-Omniscience/AA-LCR）更贴近本项目

🚨 **44 < 50 不能读成"pro 架构不如 flash"（此前的错误框架，已撤回）**：Pro 和 Flash 都是 2026-04-24 发布，
但 **Flash 已在 7/31 刷新成 0731，Pro 至今未刷新（AA 行名也是 flash 带日期后缀、pro 不带）**。
今天的对比 = 14 周旧模型 vs 2 天新模型，是版本时差不是架构结论。Pro 刷新后大概率反超 flash。

⚠️ **重要口径**：榜单分数都是 max effort。watchy 生产是 thinking off/low，**榜单的平局不能直接搬**，只能说明同档，胜负仍要自己的 harness 测。

## ~~最大待办（有 deadline）~~ → **deadline 已过（2026-08-13 刷新落地），但基线在 Telegram 里没丢**
下面这段保留备查。实际结局：**没来得及冻结，但 `notify.py:237-241` 每跑一次就把完整报告作为文档发 Telegram**，
所以 pro-old 的 RM/PM 输出全在 Telegram 历史里 → pro-old vs pro-new 仍然做得成，只是要从 Telegram 捞。

**在 V4-Pro 早八月刷新前冻结 fixture + 把当前 pro 的 output 存盘**（只存 input 没用，别名一翻旧权重就取不回来）。
目的 = **测量这次刷新到底带来了什么**：翻版之后只能看到"输出不一样了"，没有 baseline 就分不清是变好还是只是变了。

- ~~现在跑 pro vs flash~~ **已放弃**：pro 几周内就被替换，A/B 一个将死的版本没意义，那段区间也就值 ~$5。
- 真正要建的比较是 **pro-old vs pro-new（同一批 fixture）**——冻结工作完全一样，只是问题换了。
- ~~顺带推论：**若刷新后 pro 明显超过 flash，最优解不是 RM/PM 降 flash，而是 pro 接管 advisor**~~
  **→ 已证伪（2026-08-13）**：刷新后 pro 只到 53 vs 3.6F 52（1 分 = 噪声，且档位不可比：pro 是 max
  effort、Gemini 是 high，而生产跑 low），够不上"明显超过"；更要命的是**幻觉 95% vs 56%**。
  省钱理由也被涨价+啰嗦度吃掉（只剩 ~$38/年）。**advisor 留 Gemini。**
  ⚠️ 但**刷新买到的确实是 AA-LCR（70.0→75.3）而不是诚实度（94→95）**——这恰好是 RM/PM 需要的、
  advisor 不需要的 → **现有分工（pro 管 RM/PM、Gemini 管 advisor）被这次刷新验证了，不是被威胁。**

⚠️ **"更聪明" ≠ "对 watchy 更好"**：本地先例就是 Gemini 3.5→3.6，指数没动（都是 50）但行为变了，A/B 才会"混合"。
版本刷新即使不动综合分也会动行为，而**能弄坏你的子项不是上头条的那些**：为 agentic coding 调优的刷新可能抬综合分、
却回退 IFBench 式格式遵循 —— 那的失败形态不是建议变差，而是 `Take-Profit:` 正则静默匹配不到。

## 评测方法论（结论）
- 公榜只用来筛候选，不用来定胜负。AA 子项里只有 3 个相关：**AA-Omniscience**(幻觉，最重要——编造价位是最坏失败)、
  **IFBench**(指令遵循——`Take-Profit:`/`Target:`/整股都是正则抽取，跑格式=静默失效)、**AA-LCR**(长文综合→对应 RM/PM)。
  SciCode/Terminal-Bench/τ²/SWE-bench ≈ 无关。
- **arena.ai / LMArena 最没用**：测开放聊天人类偏好，奖励啰嗦对冲，与要的果断卖出结论相反。
- 金融 benchmark 只作过滤：BizFinBench.v2（最规范）、FinTrace（工具调用轨迹，对应分析师 tool loop）、
  XFinBench（校准用：最强纯文本模型 67.3% vs 人类专家 ~80% = 该品类天花板）。全都是 filings QA，没有"该不该卖"。
- 自家 harness 才是决胜器：`compare_rm_pm_models.py`(agree% + faithfulness + cost) / `compare_gemini_models.py`。
  三个要改：**n=5 是噪声(±20pp)，要 25-30**；**加止盈专属指标**（gain-gate 触发时有没有 `Take-Profit:` 行、
  限价 vs ATR 是否合理、整股整数、格式解析成功率）；**存 output 不只存 input**。
- 真正的 metric 没有榜单会给：**capture ratio = 已实现收益 ÷ 持仓期间峰值浮盈**，直接量化"卖太晚"（见 [[watchy-take-profit-gap]]、issue #17 close-the-loop）。

## advisor 换 DeepSeek 的取舍
智力理由已不成立（50 vs 50、19×）。**剩下的两条都不是智力**：
1. **相关性故障**：现在 Tier1（含盘中 zone-entry 卖出路径）不依赖 DeepSeek；合并后单一 API 成全系统 SPOF。可用 fallback 配置解决，不必付 19×。
2. **独立校验**：advisor 是给 pipeline 输出打分的，同族模型更容易顺着 pipeline 的框架走。真实但量级未测。
- 金额现实：advisor ≈ $0.5/天 ≈ $15/月，换过去 ≈ $1/月，省 ~$14/月。占比 65% 但绝对值小 → **按架构和风险决策，不按省钱**。

## 迁移坑（真换的话）
DeepSeek thinking **默认开且 high effort**，走 `extra_body` 里的 `thinking` 参数；thinking 模式下
**不支持 temperature/top_p/presence_penalty/frequency_penalty**；CoT 走 `reasoning_content` 字段而非 `content`。
Tier1 advisor 是刻意 thinking off 的 —— 不显式关掉会每 30 分钟静默付高 effort 推理钱，还丢采样参数。

## 其他核实（2026-08-02）
- **V4-Pro 官方未公告刷新**（用户另有信源，已按事实采纳）；V4-Flash 0731 已刷新；**R2 八月发布传闻被官方否认**；V5 无 model card/无路线图。
- watchy 用浮动别名 → 刷新零运维，但**风险是静默行为漂移**，需要 canary（见 [[watchy-api-cost-baseline]]）。
- CLAUDE.md 里"10:30 UTC 避开 DeepSeek 峰值计价"的理由**已过期**：V4 是平价，无分时档。排程本身仍合理。
- 便宜高智力已不止 DeepSeek：Qwen3.7 Flash $0.03/$0.13、MiniMax M3 ~$0.30/$1.20（信源打架，另有 $0.60/$2.40）、GLM-5.2 $1.40/$4.40、Kimi K2.6。

## DeepSeek V4 Pro vs V4 Flash — artificialanalysis.ai 实测对比（2026-08-21 查）

为决策"RM/PM 从 pro 降到 flash"（pro 占账单 37%、每票仅 2 调用）先查第三方基准，再跑
`scripts/compare_rm_pm_models.py`。

### 关键数字
| | V4 Pro 0813 | V4 Flash 0731 |
|---|---|---|
| **AA Intelligence Index**（v4.1.1, Max Effort） | **53** | **52** |
| 参数 | 1600B / 49B active | 284B / 13B active |
| **评测总输出 token（啰嗦度）** | **130M** | **210M**（AA 标注"very verbose"，中位数 100M） |
| 输出速度 | 80.3 tok/s | 136.2 tok/s |
| AA 报价 /1M | $0.69 | $0.23 |

Flash 0731 分项（Pro 的分项 AA 页面未公开，**最关键的对比做不完整**）：
GPQA Diamond 91% / AA-LCR 66% / HLE 37% / Terminal-Bench 2.1 79% / SciCode 50% / τ³-Banking 31% / CritPt 17% / GDPval-AA 1559 Elo。

### 结论与陷阱
- **智能差距只有 1 分**（53 vs 52）。7/31 重训把差距从 5 分（52 vs 47）压到 1 分——
  **同一次重训既推高了成本，又几乎抹平了"付 3 倍钱买 pro"的理由。**
- ⚠️ **但 flash 啰嗦 1.62×**（210M vs 130M）。watchy 是输出主导（reasoning 独占 43% 账单），
  所以 pro→flash **不是省 3 倍**：按 AA 啰嗦度折算，pro 节点 ¥3.04→¥1.43，**只省 20% 账单不是 25%**。
- ⚠️ **AA 测的是 `Max Effort`，watchy 跑的是 `high`（默认）。** high 档的差距**无人测过**，不能直接套用。
- ⚠️ **AA 的啰嗦度方向与 watchy 自测矛盾**：AA 说 0731 重训后输出 token **降 12%**（206M vs 234M），
  watchy 的账单却是重训后 flash 成本 **+35%**。工作负载/effort 都不同 → **1.62× 只能当指示性数字，
  不能当 watchy 的预测值**。要钉死只能跑 A/B。
- ⚠️ 发布文章说 Flash 0731"比 V4 Pro 高 6 分"、标题写"scores 50"，与当前对比页 Pro 53 / Flash 52 冲突
  （AA 跨 index 版本会重打分）。**没核实清楚，别引用这条。**
- → **A/B 脚本仍要跑**，而且要同时量两件事：**决策一致性 + flash 在真实 prompt 上的实际输出 token**
  （后者正是基准测不出、又直接决定省多少钱的量）。

### ❌ 定案：RM/PM `pro→flash` 否决（2026-08-21，用户补 AA-LCR + AA-Omniscience 数据后）

**理由不是质量风险，是「根本不省钱、甚至更贵」。** flash 便宜 3×/token，但啰嗦到把价格优势吃光还倒贴。

**三条独立证据同向**（都是 Max Effort）：
| 来源 | flash / pro |
|---|---|
| Intelligence Index 总输出 token（210M / 130M） | **1.6×** |
| **AA-Omniscience token（92M / 20M）** | **4.6×** → 按 $0.23 vs $0.69 折算 **flash 反而贵 1.53×** |
| **AA-LCR 每任务成本（$0.14 / $0.05）** | **flash 贵 2.8×** |

**质量侧**（gap 比 Intelligence Index 的 1 分大得多）：
| | pro | flash | 差 |
|---|---|---|---|
| **AA-LCR**（长上下文推理 = RM/PM 的本职：综合 4 份分析师报告） | **75.3%** | 74.3% | 1.0pt（基本平手）|
| **AA-Omniscience 准确率**（事实召回） | **49%** | 40% | **9pt** |
| AA-Omniscience 总分 | **+1** | −14 | 15pt |
| 幻觉率 | 95% | 92% | flash 略低（但它准确率也低 9pt）|

→ **换过去 = 省不到钱 + 丢 9 个点的事实准确率。** RM/PM 是终审 + 要引用公司事实的节点，这个 trade 没有任何一侧划算。
→ **`scripts/compare_rm_pm_models.py` 降级为低优先级**（不用跑了；真要跑也只是复核 flash 在真实 prompt 上的输出 token）。
→ 这也**追认了当初 PM「保 pro」的建议**是对的，只是当时理由（质量风险）不如现在的理由（更贵）硬。

⚠️ **数据里一处对不上，别当精确值用**：AA-LCR 页报的 token usage 是 3k(pro) vs 4k(flash)，
只差 1.33× 却出现 2.8× 的每任务成本差 —— 用 3k/4k 反推 flash 应该**更便宜**(0.44×)。
AA 的 cost/task 里显然还含别的（多轮/重试/输入侧）。**方向可信（三条证据同向），倍数不可信。**
⚠️ AA 全部测 `Max Effort`；watchy 跑 `high`。啰嗦度的爆炸有可能是 max 档特有的。
