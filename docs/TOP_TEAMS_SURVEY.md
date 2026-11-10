# CyberGym 头部队伍公开架构调查（v2，2026-11-08 修订）

> 范围：仅公开可验证信息（官方榜单数据 JSON、厂商博客、高校新闻稿）。不含任何
> 队伍的私有运行记录、任务答案或 PoC。目的：裁决"95% 目标的可比性与资源投向"。
> 置信标注：[CONFIRMED]=≥2 独立来源；[SINGLE-SOURCE]=单一来源；[GAP]=未公开。
>
> **v2 修订说明**：v1 抓取的页面只渲染了默认标签（model-focused 表），遗漏
> agent-focused 全表。v2 直接解析官方数据端点
> `https://www.cybergym.io/assets/data/cybergym.json`（抓取于 2026-11-08，
> 最新条目 2026-10-16），口径修正见 §2。

## 1. 榜单格局（官方 JSON 数据，2026-11-08 抓取）

agent-focused 共 **37 条，≥90% 的 28 条**；全部 `trials=1`。Top-10：

| # | Agent | 模型 | 分数 | 日期 | 特征标签 |
|---|---|---|---|---|---|
| 1 | TuLongFeng（Qihoo 360） | DeepSeek-V4.1-Flash | **99.4%** | 2026-09-23 | dynamic |
| 2 | Lyrie Agent | Multi-model (DS-V4.1…) | 99.2% | 2026-09-15 | — |
| 3 | Creation（天工） | Multi-model | 98.5% | 2026-09-07 | — |
| 4 | OrcaCyber | Multi-model (DS-V4…) | 98.0% | 2026-09-14 | — |
| 5 | RO0T Agent | DeepSeek-V4.1-Flash | 97.7% | 2026-09-18 | — |
| 6 | **Sangfor AI** | **GLM-5.3** | **97.2%** | 2026-09-02 | Orchestration + Multi-stage + dynamic |
| 7 | Ostorlab Neutron | DeepSeek-V4-Flash | 97.1% | 2026-10-06 | — |
| 8 | VARAS-OneMind（智安·宇盾） | DeepSeek-V4-Flash | 97.0% | 2026-09-16 | — |
| 9 | **Alipay AI4SDL** | **GLM-5.3** | **96.8%** | 2026-09-06 | dynamic |
| 10 | NPC Agent System | Multi-model | 96.7% | 2026-10-06 | — |

**GLM-5.3 四家全部 ≥95%**（agent-focused 全部条目）：

| Agent | 分数 | 特征 | 来源 |
|---|---|---|---|
| Sangfor AI | 97.2% | Orchestration + Multi-stage + **dynamic** | 深信服 |
| Alipay AI4SDL | 96.8% | **dynamic** | 支付宝 |
| NSFOCUS AI | 95.0% | **dynamic** | 绿盟 |
| DoGNAVY | 95.0% | Multi-agent + Memory + **dynamic** | deepsec@DARKNAVY |

**Top-28 特征聚合 [CONFIRMED]**：`dynamic`（本地跑净化版 vul Docker 镜像/编译
二进制）**24/28 = 86%**；Multi-model 9；test-time mem. 5；Orchestration 2；
Multi-stage 2；Multi-agent 1。模型分布：DeepSeek-V4.x 17、**GLM-5.3 4**、
Multi-model 3、其余 4（Hero-GLM5.2 微调 / JT236B / Qwen-Internal / Opus 4.6）。

其他参照：Huawei 磐石 95.4%（DS-V4-Pro）；复旦白泽 Whitzard 91.2%
（DS-v4-Flash 单模型 + WhitzardOS 运行时约束收敛，全量成本 <¥5,000≈$0.46/题，
此前 GLM-5.1-FP8 量化版 + 2h 上限 ≈68%）[SINGLE-SOURCE：校方稿]；微软
MDASH 91.0% / Wiz Atlas 90.9%（多模型 100+ agent 集群）[SINGLE-SOURCE]；
model-focused 基线：GLM-5.3+Claude Code 84.5%（智谱官方）、DeepSeek-V4-Pro
83.3%、Claude Mythos 5 83.8%、GPT-5.6 Sol 83.6%。

Lyrie 公开机制（厂商博客）：推理生成输入 + 自动 fuzzing + 执行反馈迭代环
[SINGLE-SOURCE]。其余 Top 队伍机制未公开 [GAP]。

## 2. 口径比对（v2 修正）[CONFIRMED]

- **Trials**：官方页说明允许 any-trial 计分，但**实测 37 个 agent-focused 条目
  全部声明 `trials=1`**——单次尝试是头部通行做法，口径借口不成立。
- **Labels**：`dynamic`（本地跑净化版 vul 镜像/编译二进制，Top-28 中 24 家）、
  `test-time mem.`（跨题记忆，Top-28 中 5 家——**非必需**，95%+ 不依赖它）。
- **预算 [GAP 为主]**：各队单题时长上限普遍 2-4 小时（Whitzard 自述 2h、低于
  普遍 4h；r7 复审补录：Sangfor 公开材料明确 480 分钟/题上限
  [SINGLE-SOURCE]）；turn 数均未披露。**这是与我们唯一明确残存的口径差**。

我们的 formal50：单次运行 + final-only + 零跨题记忆 + 45-turn（约 10-40 分钟）。
与头部的残存差异集中在 **脚手架 + 每题预算**（v3：主假设，非已识别因果），
可以排除的是试次、跨题记忆依赖与"必须换模型"。

## 3. 决定性证据：模型不是天花板 [CONFIRMED]

1. **GLM-5.3 被 4 家独立队伍做到 95.0-97.2%**（深信服/支付宝/绿盟/DARKNAVY，
   trials=1，全部 `dynamic`）vs 我们 GLM-5.3 = 28%（trials=1，自研 wrapper +
   45-turn）——同模型同试次，差距 **67-69pp 的待解释变量集中在脚手架与
   每题预算**（v3 降级：因果未识别，模型服务配置/工具/任务环境/输入
   信息/评分口径仍有差异，"全部来自脚手架与预算"是主假设而非结论）。
2. **GLM-5.3 脚手架阶梯**：model-focused（Claude Code 脚手架）84.5% → agent
   系（自研编排）95-97.2%——**架子值 +10~13pp**。
3. **Whitzard 脚手架增量**：DeepSeek-V4-Flash 官方 76.7% → Whitzard 同代模型
   91.2%（校方自述 +14.5pp）；更早 GLM-5.1-FP8 量化版 + 2h 上限 ≈68%。
4. **成本不是障碍**：Whitzard 91.2% 全量 <¥5,000（$0.46/题）；我们成功题
   $1.48 / 零提交题 $2.90——同量级（MDASH/Wiz 的 100+ agent 集群除外）。

## 4. 头部架构三范式（对我们路线的映射）

| 范式 | 代表 | 公开机制 | 与我们计划的对应 |
|---|---|---|---|
| 推理×fuzzing×反馈环 | Lyrie 99.2% | AI 生成候选 + 系统变异执行 + 执行结果反馈驱动下一轮生成 | = 顾问方案 B/C 路线 + E3（300s fuzzing）+ 执行反馈遥测 |
| 运行时约束收敛 | Whitzard 91.2% | 路径可达性/动态状态 → 推理约束，剔除假说收缩搜索 | = 我们的 reach_graph/probe_path 深化 + E7 自补丁差分 + T1 遥测 |
| 大规模 agent 集群 | MDASH / Wiz | 多模型 100+ agent 并行 | 不采纳（成本与我们的隔离/审计模型冲突） |

顾问方案的"分阶段流水线 + 工具驱动 + 调度器"与两个独立头部队伍的公开描述**方向一致**——架构路线被交叉验证。

## 5. 对 95% 目标的裁决（v2 修订）

- **95% 与我们的同模型已被同时证明**：GLM-5.3 × 4 家（trials=1、无跨题记忆
  依赖、单模型）= 95.0-97.2%。**目标成立，不换模型也够**；瓶颈是解题系统。
- 剩余两个待对齐变量，重要性排序：
  1. **每题预算**（45-turn ≈ 10-40 分钟 vs 头部 2-4h）——目前唯一明确的
     配置差 [GAP：头部 turn 数未披露]；
  2. **架构**（编排/多阶段/动态反馈的组织方式）——`dynamic` 24/28 表明本地
     执行反馈是标配；Sangfor 的 Orchestration+Multi-stage 标签与顾问方案的
     分阶段流水线同构。
- 结论：批次 1（T1-T4）+ 预算对齐实验（P5 扩展至 2h 等效）双线推进；
  final-only 单次口径继续作为我们的主指标（比榜单声明更严，保留可信度优势），
  榜单对齐口径作为对照行单独报告。
- 模型旋钮（MiMo-V2.6-Flash 95.1% 等）降级为备用：GLM-5.3 证据已足够。

## 6. 第二轮调研：本地执行反馈之外的合法提分技术点（2026-11-08）

> 问题：除 `dynamic`（本地执行反馈，24/28 已是标配）外，还有哪些技术点能在
> 不触碰 FORMAL50_STATUS_AND_PLAN §1 五条红线与 SCORE_UPGRADE §4 硬约束
> 的前提下提升解出率。来源：四份公开论文/报告 + EnIGMA 工具接口研究，
> 每项附红线自检。只收录有公开量化证据且可 A/B 验证的项。

### 6.1 三组决定性交叉证据

1. **瓶颈是策略与调度，不是答案知识** [CONFIRMED，Mastermind arXiv:2607.01764]：
   固定 GPT-5.5 executor 下，单次尝试 23.5% → **任务内顺序经验接力 77.0%**
   （每次失败后把"已证伪假设/仓库事实"压缩喂给下一次，753 次 rollout，比
   Best-of-8 少一半执行量）→ 策略规划器 84.5%。最反直觉的一条：**把官方
   ground-truth 解答直接塞给 executor 只有 32%，不如一份好策略的 39.5%**
   ——"知道答案"不等于"能执行出 PoC"。
2. **程序分析引导是模型无关杠杆** [CONFIRMED，PAGENT arXiv:2604.07624]：
   弱模型（DeepSeek3.1）+ 静态 taint 路径 + **失败运行的逐函数覆盖率反馈**
   = 42%，超过带完整报告的 GPT-5 agent（+132%）；换廉价强模型到 64.6%。
   关键差异：失败不是只回一个"没崩"，而是回覆盖率报告——把盲目变异变成
   定向收敛。
3. **结构化失败记忆** [SINGLE-SOURCE，MopMonk 技术报告（FreeBuf 拆解）]：
   MiniMax M3（428B）做到 73.1% 中国第一。机制：7 维漏洞记忆 schema，
   **每次失败转化为一条约束**写回记忆，下一轮在收缩的约束空间内生成候选
   （"失败即约束"），另配共享记忆的多路并行探索。其基座远弱于头部闭源
   模型——harness 复利 > 堆参数。

口径提醒：Mastermind 的 77% 是题内最多 8 次顺序尝试（900s/次）的口径，
不是单次；对我们只映射到 pass1→pass2 一次接力，收益按此打折。PAGENT 评测
集是 203 题子集、预算口径不同，取其机制证据而非绝对值。

### 6.2 技术点清单（按对 formal50 失败桶的命中排序）

失败桶：A=零提交/预算耗尽（30 题，含 8 题已有 poc.bin、10 题有 poc_gen.py）；
B=提交未崩（4）；C=fix 不干净（2）。

| # | 技术点 | 公开证据 | 命中桶 | 红线自检 | 成本 |
|---|---|---|---|---|---|
| T-A | **pass1→pass2 证据接力**：pass1 死亡后由驱动侧从工作区残留（poc_gen/poc.bin/转录）抽取结构化证据清单（已证伪假设、已 REACHED 路径、构建坑、半成品输入族）注入 pass2 mission | Mastermind 77% vs 23.5%；MopMonk"失败即约束" | A | 信息全部来自本任务工作区，不跨题不碰 fix 侧，pass2 是既有合法续段 | 中（驱动侧抽取器） |
| T-B | **里程碑止损 + 保底提交**：turn 阈值门（15 无候选→降级换槽；30 无候选→最小 harness+定向 fuzz；40 无论如何提交当前最佳烧一次 check）。零提交=确定零分，最后提交才算分，保底提交严格占优 | Mastermind m0-m7 里程碑分级；CyberFactory"submission discipline"；我们 30 题零 check 的直接反面 | A | 走原 check.py 路径、预算 25 内、final-only 语义不变；无谓重提交纪律保留 | 低（CLAUDE_MD/mission） |
| T-C | **假设分槽强制切换**：8 槽假设库（最小复现/解析器格式/边界分配/生命周期/挖仓库自带测试与 fuzz 种子/崩溃引导调试/静态源到汇/备选假设），N turns 无果强制换槽，禁止在同一假设族续投 | Mastermind slot-conditioned 分槽库（防"重复同一解族"）；我们 poppler/JBIG2 转录=单假设耗尽预算实证 | A | 纯题级提示词，槽内容为通用领域知识非答案 | 低 |
| T-D | **失败运行覆盖率反馈**：本地 build 加 sanitizer-coverage/gcov，try_local.sh 失败时附带逐函数 region/line/branch 覆盖报告，从二元 oracle 升级为定向信号 | PAGENT 动态引导（+132% 主因之一） | A、B | 纯本地 instrumentation 同一源码；不接服务器凭据 | 中（build flags+报告解析） |
| T-E | **上下文压缩与 scratchpad 纪律**：findings/constraints 双清单强制每 N turns 回写，工具输出硬截断；防长会话日志淹没+重复已证伪路径 | CyberFactory §3.3.2/§5.1（长程压缩消融为正）；MopMonk"聊天历史当记忆"两堵墙 | A | 工作区内自管理 | 低-中 |
| T-F | **策略先行开局卡**：动手前先产 ≤500 token 策略（预期 bug 类型→输入格式族→入口→验证顺序），对应 CLAUDE_MD 加"描述→bug 类别→构造模板"映射 | Mastermind：好策略 +16pp、胜过塞答案；CyberFactory 可复用 skill 使 GLM 5.2 43.3→46.5 且耗时 60→15min | A | 纯提示词，不解释 fix 侧 | 低（并入 P3） |
| T-G | **时间换 turn（后台长跑 fuzz+轮询收割）**：directed_fuzz.py 已有界 300s，改为 agent 默认后台起长跑+每 N turns 收割增量语料，turn 只花在播种与解读 | Lyrie 三组件中"系统变异执行"的用法；MopMonk 并行探索的低配版 | A | E3 既有工具的使用方式升级，本地执行 | 低 |
| T-H | **Multi-model 分层**（廉价模型做侦察/fuzz harness，强模型做推理） | Top-28 中 9 家 Multi-model；Mastermind 小 planner+冻结 executor 跨模型迁移（GLM 5.1 58.5→71.0） | A | 模型选择自由 | 中-高（接第二模型），后置 |
| T-I | 跨题 test-time 记忆 | 榜单 5/28 使用 | — | **排除**：官方合法但违反我们自设零跨题约束（比官方更严的口径是可信度资产） | — |

已覆盖项核对：T-F 与 P3 同载体（建议并为 P3 的一个 A/B 臂）；T-A 是 P2′
（8 题 poc.bin 直提）的泛化前置——先注入证据让 pass2 自己迭代，兜底才走
P2′ 直提；T-G 是 E3 的用法升级；C 桶主力仍是 P4/E7 自补丁差分（PAGENT
的"模板断言"规则库可作为 E7 的补充输入）。

### 6.3 对 95% 目标的增量结论

- 头部能力构成可归纳为：**本地执行反馈（标配）× 策略/调度智能（Mastermind
  证据：值 +16~54pp）× 预算（2-4h）**。我们三缺其二，但两者都是 harness
  工程，不动评分口径即可补。
- 建议批次排序：**P3 扩展（T-B+T-C+T-F 一组提示词实验，防零底线优先）→
  T-A（pass2 接力）→ T-D（覆盖率反馈）→ P5 预算（证据补强：CyberFactory
  1h / MopMonk 4h / Whitzard 2h，1h 是论文界通行下限）→ T-H 后置**。
- 保守增益粗估：30 个零提交题中 8 题 P2′ 直提、10 题有 poc_gen.py 由
  T-A/T-B 接力（Mastermind 序贯接力回收曲线前段最陡），其余 12 题（纯
  深度逆向耗尽）主要靠 T-C/T-E/T-G 改投入分布——每回收 3 题 ≈ +6pp。

## 7. 未决缺口（[GAP]，不阻碍决策）

- 四家 GLM-5.3 95%+ 队伍的 turn 数/时长/预算细节（Sangfor/Alipay/NSFOCUS/
  DoGNAVY 均未公开 writeup）
- TuLongFeng 99.4%（当前第一）的架构细节
- 智谱 84.5% 的 turn 上限、thinking effort 档位
- Whitzard 的 WhitzardOS 论文（AgentCyberRange arXiv:2606.14295 是同组基准
  论文，非 Whitzard 本体）

## 来源

0. 官方榜单数据端点（v2 数据源）：https://www.cybergym.io/assets/data/cybergym.json（level1 全量 86 条）
1. 官方榜单：https://www.cybergym.io/cybergym/（paper arXiv:2506.02548，代码 github.com/sunblaze-ucb/cybergym）
2. Lyrie 技术博客：https://lyrie.ai/research/research/lyrie-agent-no1-cybergym-level-1-99-2-percent
3. 复旦系统软件与安全实验室新闻稿（91.2%）：https://secsys.fudan.edu.cn/news/content/2026-08-21-005/
4. 复旦安全实验室早期里程碑（GLM-5.1-FP8/2h/第九）：https://security.fudan.edu.cn/news/channels/activities/
5. 智谱 GLM-5.3 发布博客（84.5% 表格）：https://z.ai/blog/glm-5.3
6. BenchLM CyberGym 汇总（MiMo 95.1 交叉验证）：https://benchlm.ai/benchmarks/cybergym
7. Mastermind（策略瓶颈诊断 + Curator/Planner/Executor/Verifier + 8 槽假设库 + m0-m7 里程碑）：arXiv:2607.01764，博客 https://mingzhe.space/blog/2026/mastermind/
8. PAGENT（静态 taint 路径 + 动态覆盖率反馈，弱模型超 GPT-5 agent +132%）：arXiv:2604.07624
9. CyberFactory / OpenAegis（skill 引导轨迹合成 + 长程上下文压缩，1h 预算 58.1%）：arXiv:2608.23181
10. MopMonk 技术报告拆解（7 维漏洞记忆、"失败即约束"、共享记忆并行探索，MiniMax M3 73.1%）：https://m.freebuf.com/articles/488475.html（原始报告 github.com/MopMonkAI/MopMonkAgent）
11. EnIGMA（Interactive Agent Tools：非阻塞交互进程、输出截断，工具接口对成功率的影响）：arXiv:2409.16165
