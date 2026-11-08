# formal50 提分统一计划 v1（2026-11-08）

> 三来源归并：r5 实验卡（`docs/SCORE_EXPERIMENT_CARDS.md` E1-E6/B1-B6）、
> r6 裁决 P 系列（`FORMAL50_STATUS_AND_PLAN.md` §3）、第二轮头部调研 T 系列
> （`docs/TOP_TEAMS_SURVEY.md` §6，公开论文交叉验证）。供审核 AI 复审：
> 本计划不预承诺定量增益（r5 教训），全部以过程指标先行、情景上界表述。

## 0. 基线、目标与红线

- 基线：首轮 14/50=28%，E1 后 15/50=30%（分层 ARVO 14/45、OSS-Fuzz 1/5）。
- 失败桶：**A 零提交/预算耗尽 30 题**（其中 8 题工作区已有 poc.bin、10 题
  有 poc_gen.py，全部 `error_max_turns`）；**B vul_no_crash 4 题**；
  **C fix_not_clean 2 题**（重试中 42227 二次同判）。
- 红线（全文见 `FORMAL50_STATUS_AND_PLAN.md` §1 五条 + `SCORE_UPGRADE_PROMPT.md`
  §4 硬约束）：不改评分谓词、不泄露 fix 侧、unscored 不入重试、不绕隔离/
  25-check 预算门/账本三方一致、无跨题信息、不利用评分漏洞。
- **实验队列纪律**：所有 A/B 用新冻结队列（`freeze_queue.py`，排除 formal50
  50 题与已验证 5 题，seed+manifest 留档）；实验跑批与正式跑批**分开报告**，
  不混入 formal50 主指标；每臂记录 commit SHA 与 CLAUDE_MD/mission 文件哈希。

## 1. 归并总表（旧编号 → 统一编号 X1-X10）

| 统一 | 内容 | 吸收来源 | 状态 | 批次 |
|---|---|---|---|---|
| X1 | 策略先行开局卡 + 描述三分桥接 | P3（r6 放行 6-10 题）+ T-F + B1 | 待跑 | 1 |
| X2 | 里程碑止损 + 保底提交 | E2①（turn 25/35 门）+ T-B | 待跑 | 1 |
| X3 | 8 槽假设库强制换道 + 后台 fuzz 时间换 turn | E3 换道阶梯 + T-C + T-G + B2 | 待跑 | 1 |
| X4 | pass1→pass2 证据接力（E6 C 臂落地） | E6 三臂 C + T-A | 待跑（依赖 X2 遥测） | 2 |
| X5 | 受控 pass2 候选提交 | P2′（r6 裁决版，兼容性测试先行） | 设计中 | 2 |
| X6 | 失败运行覆盖率反馈 | T-D（新，PAGENT 证据） | 待设计 | 3 |
| X7 | 自补丁差分 + 崩溃置信分层 | P4/E7 + E4 + B5 | 后置（r6 原裁决） | 3 |
| X8 | 上下文遥测 → 压缩/scratchpad 干预 | B6 + T-E | 遥测先行 | 3 |
| X9 | 预算敏感性 45→90 | P5（r6 只批小样本） | 待 90 数据 | 4 |
| X10 | Multi-model 分层 | T-H（9/28 榜单证据） | 储备 | 4 |
| — | 已完成归档 | E1（+1 题）、E5/G1-G4/G6 | 完成 | 0 |
| — | 维持撤回/排除 | E2②（撤回）、T-I 跨题记忆（违反自设口径）、B3（独立 infra 项，随下一轮跑批做） | — | — |

## 2. 批次与依赖

```
批次0(完成) ─→ 批次1 提示词组 X1/X2/X3（同载体一次改，分臂归因）
                    │  过程遥测落地（first_check_turn 等）
                    ▼
              批次2 pass2 组 X4 → X5（X4 先行：自己迭代优于直提兜底）
                    ▼
              批次3 工具组 X6 / X7 / X8（build 变更，独立 A/B）
                    ▼
              批次4 X9 预算 / X10 模型（需前面显示进展）
```

排序依据：r6 已放行 P3 小样本在前；X4 的"证据抽取点"定义依赖 X2 落地的
turn 里程碑遥测；X5 依赖 X4 排空（8 题 poc.bin 直提是最后手段）；X6/X7
动 build 与验证器语义放后；X9 需 90-turn 先显示进展再议 150。

## 3. 实验卡

### X1 策略先行开局卡（批次 1）

- **改动点**：`make_agent_ws.py` 的 CLAUDE_MD 模板加"第 0 步"：动手前先写
  `strategy.md`（≤500 token：预期 bug 类别 → 输入格式族 → 入口函数 →
  验证顺序 → 首选换道槽），此后每换槽更新一段；`agent_mission.txt` 加一句
  "Begin by writing strategy.md per CLAUDE.md step 0"。
  B1 描述三分桥接并入同一模板：明确函数型 / 调用链型 / 模糊症状型；
  模糊型指引先定位 harness 输入入口再谈漏洞函数。
- **实验步骤**：
  1. 新冻结 12 题队列（ARVO 10 + OSS-Fuzz 2，分层比例同 formal50，
     `freeze_queue.py --seed` 记录）；
  2. 臂 A 对照 = 现状模板；臂 B = X1 模板；同队列两臂各跑一遍
     （配对，顺序随机交错避免时段漂移）；
  3. 逐题归档 strategy.md 与 rows 新增字段。
- **度量**：过程指标——首次有效输入 turn、首次 check turn、零提交率、
  REACHED 证据获取率；终判成功数仅作参考（12 题不判终效）。
- **判定门**：零提交率相对对照下降 ≥1/3 且无成功题降级 → X1 并入后续
  全部批次基线；无差异 → 回滚模板，仅保留 B1 三分指引。
- **红线自检**：纯题级提示词；槽库/三分法为通用领域知识，不含答案；
  不解释 fix 侧逻辑。
- **公开证据**：Mastermind 好策略 +16pp 且胜过直接塞答案；CyberFactory
  skill 使 GLM 5.2 43.3→46.5 同时耗时 60→15min。

### X2 里程碑止损 + 保底提交（批次 1）

- **改动点**：CLAUDE_MD 纪律段改为显式里程碑门：
  - turn 15：无本地崩溃候选 → 触发换槽决策（接 X3）；
  - turn 30：仍无 → 收缩到最小 harness + 一次 300s directed_fuzz；
  - turn 40：仍无 → **提交当前最佳 poc.bin 烧 1 次 check**（保底提交）；
    若连候选都没有，写 `SOLUTION.md` 标 `STATUS: UNSOLVED` 收敛退出
    （E2① 原 turn 35 门对齐到 40 保底前置）。
  - 冲突裁决（沿用 r5）：stab 稳定性门**优先于**一切提交纪律；已持有
    目标可达/崩溃证据的题不受止损门干扰。
- **关键论据**：final-only 下最后提交计分，零提交=确定零分；turn 40 单次
  保底是严格占优动作（要么真触发，要么买到服务器真实反馈）。formal50
  30/50 零 check 是直接反面证据。
- **实验步骤**：并入 X1 的同一 12 题队列作为臂 C（X1+X2 组合），
  使三臂：A 现状 / B X1 / C X1+X2；组合臂先验证互不打架。
- **度量**：turn 40 保底触发率；保底提交的 verdict 分布（真触发率）；
  无谓 check 消耗（同想法重提交次数，防纪律滥用）。
- **红线自检**：走原 check.py 路径、25 预算内、服务器返回即提交语义
  不变；不构成"再跑一次取最好"（每次 check 都是官方允许的提交动作，
  final-only 自动取最后）。
- **风险**：不稳输入浪费 1 check——已由"保底仅在 turn 40 仍无任何候选
  时触发 + 单次"限制。

### X3 8 槽假设库强制换道 + 后台 fuzz（批次 1）

- **改动点**：CLAUDE_MD 换道段（原"reach_graph 前两名候选各 2 次构造
  机会"扩展）：同一假设槽连续 8 turns 无新证据（无 REACHED/无新覆盖/
  无构造进展）→ 强制换槽，禁止在同一假设族续投。8 槽 =
  minimal-reproducer / parser-format / bounds-allocation / lifetime-state /
  **existing-tests-fuzz（挖当题 repo 自带测试、fuzz target、种子语料）** /
  crash-guided-debug / static-source-sink / alternate-hypothesis。
  T-G 用法：directed_fuzz.py 支持有界后台长跑（≤600s，nohup + 轮询），
  turn 只花在播种与收割，fuzzing 占用显式记账不超 pass 预算。
- **B2 红线继承**：existing-tests-fuzz 槽只读；**含现成目标触发 PoC 的
  样本排除**，启用前人工审计该题 repo 测试资产清单（B2 原文）。
- **实验步骤**：作为臂 D（X1+X2+X3 全组合）跑同队列；若三臂对比已耗尽
  预算，D 臂用第二个 12 题队列。
- **度量**：槽切换次数与每槽进展率；REACHED 比例；fuzz 收割有效输入数；
  有效输入比例（目标 sanitizer 命中率）。
- **红线自检**：纯本地执行；槽内容为通用方法论；不跨题。
- **公开证据**：Mastermind slot-conditioned 分槽库（防"重复同一解族"）；
  我们 poppler/JBIG2 转录=单假设耗尽预算的实证；Lyrie 三组件。

### X4 pass1→pass2 证据接力（批次 2，E6 C 臂）

- **现状**（`run_agent_task.sh` L576-609）：pass2 仅在 `checks=0 ∧
  ledger_rows=0` 触发，prompt 只让看 description + 至多 5 个 repo 文件，
  **拿不到任何 pass1 证据**——pass1 留下的 poc_gen.py/poc.bin 不被点名，
  转录中的失败假设不可见。
- **改动点**：
  1. 新脚本 `scripts/extract_pass1_evidence.py`（驱动侧、只读）：
     从本任务工作区（poc_gen.py 存在性+头部摘要、poc.bin 大小+magic、
     已构建产物）与本任务 pass1 会话日志抽取证据清单，分三层：
     L1 文件清单 / L2 内容摘要 / L3 失败假设与已证伪路径列表（有限条数）；
  2. `run_agent_task.sh` pass2 段：注入 L1（默认）或 L1+L2/L3（臂变量）
     进 PASS2_PROMPT。
- **实验步骤**：E6 三臂原设计，新冻结 20 题：A 现状 45+条件15 / B 60 单段 /
  C 45+条件15+证据注入；C 内再分 L1-only 与 L1+L3 两个子臂（若预算紧，
  L3 子臂缩到 10 题）。
- **度量**：同题通过率、零提交恢复率、wall-time、成本、最终提交完整性
  （E6 原指标）。
- **判定门**：C 臂零提交恢复率 > A 臂且成本/题增幅 ≤50% → X4 并入基线。
- **红线自检**：信息仅本任务来源（无跨题）；抽取器只读、root 侧；
  pass2 身份/隔离/账本不变量零改动。
- **公开证据**：Mastermind 顺序经验接力 77% vs 单次 23.5%（题内最多 8 次
  口径，我们只有一次接力，收益打折）；MopMonk"失败即约束"。
- **风险**：转录抽取质量差 → 分层注入逐层验证；L3 抽取失败自动降 L1。

### X5 受控 pass2 候选提交（批次 2，P2′ 原案）

- 按 r6 裁决版推进：先做**不提交的兼容性测试**（root 只读校验 poc.bin：
  常规文件/大小界内/路径界内）与故障注入，再在既有隔离身份与 cgroup 内、
  由原 check.py 路径执行提交、消耗正式预算、走原账本——**不新建
  root-to-server 旁路**（r6 否决原文）。
- 范围：仅 pass 结束仍零 check 且工作区有 poc.bin 的题（formal50 历史
  8 题属此类；未来队列同规则；X4 落地后此集合应收缩）。
- 前置依赖：X4 先行——让 pass2 自己迭代优于直提兜底，X5 只救
  "X4 也失败"的残余。

### X6 失败运行覆盖率反馈（批次 3）

- **改动点**：`make_agent_ws.py` 生成的本地验证工具升级——独立目录
  coverage build（sanitizer-coverage 或 -fprofile-instr-generate，**不替换
  原崩溃 oracle**）；`try_local.sh` 失败时附带逐函数 region/line/branch
  覆盖 top-N 报告（结构化 JSON），把"没崩"从终点变成定向信号。
- **实验步骤**：
  1. 5 题离线验证：coverage build 不改变崩溃行为（同输入同签名），
     构建耗时增量 ≤300s（不挤爆 2400s 硬超时——超时则改懒构建：
     仅在首次本地失败后按需构建）；
  2. 通过后并入批次 3 A/B（12 题队列，对照 = 无覆盖率反馈）。
- **度量**：失败后下一轮输入的覆盖率提升幅度；本地崩溃命中率；
  B 桶（提交未崩）复发率。
- **红线自检**：纯本地 instrumentation 同一源码；不接服务器凭据。
- **公开证据**：PAGENT 弱模型+覆盖率反馈超 GPT-5 agent +132%。

### X7 自补丁差分 + 崩溃置信分层（批次 3，P4/E7 + E4 + B5）

- 触发条件（r6 原文）：仅对"有稳定本地崩溃但特异性不明"的题（C 桶
  主力，formal50 历史仅 2+1 题）。
- E4 四档置信分层作为提交前自评框架并入 CLAUDE_MD；B5 质量判据**事前
  固定**（同签名+位置证据），条件性终止，严禁多提交抽签。
- 红线：仅本题源码 + agent 自建补丁 + 纯本地，不接官方 fix 资源/凭据。

### X8 上下文遥测 → 压缩干预（批次 3，B6 + T-E）

- 第一步只做遥测（B6 原案）：AGCLI_CTX 指纹、token usage 曲线、工具
  失败率、INDEX 输出规模，先验证参数真实生效。
- 干预门（数据驱动）：仅当遥测证实"上下文压力与零提交失败相关"
  （如后 20 turns 工具失败率/重复读文件率显著升）才上 T-E 干预：
  CLAUDE_MD scratchpad 纪律（findings/constraints 双清单每 N turns 回写）
  + 工具输出截断上限。
- 红线：工作区内自管理。

### X9 预算敏感性（批次 4，P5 原案）

- r6 裁决：只批小样本 45/90 对比；150 需 90 显示进展后再议。
- 固定外部时间/模型/预算，记录有效工具动作（过程指标复用 X1-X3 遥测）。
- 公开证据补强：CyberFactory 1h、Whitzard 2h、MopMonk 4h——1h 是论文界
  通行下限，我们的 45-turn（10-40min）是唯一明确配置差。

### X10 Multi-model 分层（批次 4，储备）

- 9/28 榜单证据 + Mastermind 跨模型迁移（小 planner 冻结 executor）。
- 前置：第二模型接入的成本评估与 key 池信任链影响（key 池信任链不许动，
  需独立评审）；本计划内不实施。

## 4. 度量与日志补丁（批次 1 前落地）

`record_task_row.py` / rows schema 新增过程字段（root 侧观测，agent 写
数据仅提示性——E5 原则）：

```
first_candidate_turn / first_check_turn / strategy_written(bool)
slot_switches / floor_submit_triggered(bool) / fuzz_harvest_count
pass2_evidence_level(L0-L3) / context_pressure(遥测指纹)
```

实验报告模板：每臂一节，过程指标分布 + 终判参考 + 成本/题 + 违规检查
（verify_stage4 照跑）。

## 5. 预期（情景推演，非承诺）与成本

- 桶 A 30 题：X5 直提候选 8 题、X4 接力候选 10 题（有 poc_gen.py）、
  X1/X2/X3 改善其余 12 题的投入分布——每回收 3 题 ≈ +6pp（30%→36%
  量级情景）。桶 B 4 题主要靠 X6+X3 槽 5；桶 C 2 题靠 X7。
- 成本量级：批次 1 三/四臂 × 12 题 × $1.5-2.5 ≈ $55-120；批次 2 三臂 ×
  20 题 ≈ $90-150；批次 3 ≤$100。总盘子 ≤$400，与首轮 $144 同量级可控。

## 6. 请审核 AI 裁决的问题

1. **X2 保底提交**与"禁止无谓重提交"纪律的边界：turn 40 单次保底 +
   stab 门优先，是否成立？是否存在被解读为"利用 final-only 取最后"的
   合规风险（我们认为每次 check 都是官方提交动作，final-only 语义即取
   最后一次，无旁路）？
2. **X4 转录抽取**由驱动侧（root）做 vs 要求 pass1 agent 自写 handoff
   文件：前者不依赖 agent 配合但引入 root 解析 agent 产物的新代码面，
   哪个信任面更小？
3. 批次 1 队列 12 题（四臂）vs r6 放行的 P3 6-10 题：扩容是否需要新裁决？
4. X6 coverage build 的懒构建方案（首次失败后构建）是否可接受
   （避免挤占 2400s）？
5. 批次排序（X4 先于 X5）与 r6"主线转向尽早形成/验证候选"方向的一致性。
