# Y6 漏洞归因 × Y7 最小充分触发：实验设计（v1，2026-11-08，供审计）

> 任务来源：Stage-4 提分专项（Y6+Y7）。本文件按要求的 A-H 格式交付；
> 不修改任何正式评分/执行链路代码，全部为设计 + 只读审计产出。
> 上游计划：`docs/SCORE_UPGRADE_PLAN.md` v4 §3.12（Y6 卡）。

## 审计依据（版本声明）

- 代码：GitHub `jyw358/stage4-` main @ `2fb895b`（本地镜像工作区核对）；
  文档分支 `score-upgrade-plan` @ `1235a3a`（SCORE_UPGRADE_PLAN v4）。
  **生产树 `/opt/cybergym-agent-r33` @ `bb163b4` 无主机访问权限，未直接
  审计**，关于生产工作区状态的判断全部基于 rows 自述等间接证据，置信
  单列。任务提示词所写"SCORE_UPGRADE_PLAN 以 v2 为准"与实际不符，本文
  以 v4 为准（v4 ⊇ v2 且含 r7/r8 修订与 Y6 卡）。
- 数据：`results/formal50_round1/`、`results/formal50_retry/`（双层快照）、
  `docs/STAGE3_FAILURE_TAXONOMY.md` v2、`FORMAL50_STATUS_AND_PLAN.md`。

### P0 前置发现：工作区工具链很可能根本不存在（Y1 升级为阻断项）

【代码证实，仓库树】`make_agent_ws.py` L226-228 定义 `TOOL_FILES`（repo_map/
reach_graph/probe_path/try_local/stab_run/minsep/mutate_seed/directed_fuzz/
boundary_synth/hf_mutator 共 10 件），L318-323 复制时 `if _s.exists()`
**静默跳过缺失文件**；L285-296 的 verify_gate Stop 钩子同样 `if
gate_src.exists()`。**本仓库 main 的 `scripts/` 只有 15 个文件，10 件工具
与 `verify_gate.py` 全部不存在**——从此树生成的工作区不含任何 CLAUDE_MD
（L54-77）引用的工具，且"首 check 前不得结束会话"钩子失效（settings.json
仍会写入指向不存在文件的命令）。

【间接证据，formal50 现场】rows 自述两例实锤：
- `arvo_34973`："my stand-in for the **missing** `try_local.sh`"；
- `arvo_42227`："`try_local.sh`/`stab_run.sh`, **which I created**"。
50 题 result 文本中仅 6 题提及任何工具名，多为 agent 自建替身。

**结论**：formal50 的"30 题零提交、未使用配套工具"不能先验归因为调度
问题——**工具很可能不在工作区里**。X1-X3 的提示词实验在工具链补齐并经
Y1 主机自检通过前**没有意义**；Y6/Y7 全部构建在工具链之上，故 PR-A 的
第一件事是把工具链真实落地并主机验证。此项未决前，本文件 C/D/E 卡均为
设计冻结、实施 HOLD。

---

## A. 根因证据审计

### A.1 Y6（模糊描述 → 错误漏洞/崩溃点）

| 证据 | 数值 | 来源 | 可信级 |
|---|---|---|---|
| stage3 fix_not_clean | 44/255 失败（17%） | taxonomy §2【项目方汇总】 | 中 |
| 其中 fix=1（补丁版也崩） | 36；fix=77 4 题；fix=300 4 题 | taxonomy §2 | 中 |
| taxonomy 对 44 题的原归因 | "非目标崩溃/环境性崩溃"**未细分** | taxonomy §2 原文 | 弱——竞争解释未排除 |
| formal50 C 桶 | fix_not_clean 2 题（首轮）+ 42227 重试二次同判 | FORMAL50 §2 | 高【代码+row】 |
| 42227 案例细节 | agent 自发做了双假设推理（目标 UAF vs cmscgats.c:2168 off-by-one，后者因 intra-object 不可见被否证），仍 vul=1/fix=1 两次 | rows/arvo_42227.json result + final | 高 |

**竞争性解释**（"选错崩溃点"不是 44 题的既证单一原因）：共享缺陷被同一
输入触发（=Y7 打过头）、补丁版环境差异、执行不稳定、输入非特异（多处
损坏）、评分口径。**无法证实之处**：44 题中"真选错点"占比无数据——
这正是 Y6-pre（v4 §3.12）四类细分要产出的东西。stage3 的 150 题
跨源不一致与 53 题零记录与 Y6 无关（不同失败类）。

**Y6 真实性判定**：机制真实（42227 显示双假设竞争在真实题上发生），但
**规模未证**——P1 而非 P0，等 Y6-pre 细分定量。

### A.2 Y7（找对漏洞但 PoC 过度触发）

| 证据 | 数值 | 来源 | 可信级 |
|---|---|---|---|
| fix=1 子集 | 36/44 | taxonomy §2 | 中 |
| "打过头"假说的直接证据 | **零**——无任何样本做过"单输入多缺陷"拆解 | — | 不可信（未测） |
| 42227 | vul=1∧fix=1，崩溃为 ASAN 可见类，候选筛选已排除一个竞争假设——仍双侧崩 | row | 高（现象）/低（归因） |

**判定**：Y7 是 fix=1 的**待验证子假设**之一，当前无证据支持其为多数
原因。禁止按"44 题都是打过头"立项（任务书 §9 明令）。Y6-pre 细分中
"确实崩错点 / 疑似非特异性崩溃 / 证据不足 / 其他"四类里，只有第二类
喂 Y7，第一类喂 Y6。

---

## B. 现有代码能力盘点

| 能力 | 位置 | 状态 | 对 Y6/Y7 可复用性 | 修改复杂度 |
|---|---|---|---|---|
| CLAUDE_MD 工作流模板（LOCATE→CONSTRUCT→本地迭代→SUBMIT） | make_agent_ws.py L43-100 | 存在；crash-site 要求仅一句（L82-83"crash site must match the bug"），无归因程序 | Y6 的载体；需重写第 1/3 步 | 低（模板文本） |
| CHECK_PY 三值 verdict（300=TIMEOUT/0=NO_TRIGGER/其他=CRASH）+ 尾部 1500 字节 sanitizer 输出 | L102-165（L147-152 判定、L156-159 输出） | 存在 | Y6 层 4 的服务器信号源；**不改**（评分语义） | 零（只读消费） |
| 工具链 10 件 | make_agent_ws.py L226-228 引用；`scripts/` **全部缺失** | **不存在（P0）** | Y6 依赖 repo_map/reach_graph/probe_path；Y7 依赖 minsep/mutate_seed/stab_run | 先补齐再说（源在主机历史部署，需找回并入库） |
| verify_gate Stop 钩子 | L285-296 引用；文件缺失 | 不存在（P0 附带） | 防零提交的机制层兜底 | 同上 |
| pass2 forced-submit（checks=0∧ledger=0 触发，900s） | run_agent_task.sh L576-609 | 存在 | X4-L1 注入点（v4 §3.4） | 中 |
| 失败归因分类器 v1.1 | failure_taxonomy.py | 存在（9 项注入单测绿） | Y6-pre 细分在其上加分类维度 | 低 |
| 遥测字段 | record_task_row.py rows schema | 存在 | Y6/Y7 过程指标落点（v4 §4 已扩） | 低 |
| minsep "same-signature guarded" | CLAUDE_MD L74-75 **描述**；源缺失 | 不可审计 | Y7 的机制保持现状未知——PR-C 第一项是审计/重写 | 中 |

**缺口清单**：①工具链本体；②结构化假设文件与归因判定工具（Y6 新增，
轻量）；③机制保持最小化（Y7 升级 minsep）；④单字段因果扰动工具
（Y7 新增，轻量）；⑤Y6-pre 历史细分（离线）。

---

## C. Y6 实验卡（Target Bug Attribution）

- **触发条件**（按需，防零提交）：B1 描述分型为"模糊症状型/调用链型"
  **或** LOCATE 产出 ≥2 个证据接近的候选 **或** 本地崩溃栈顶为通用函数
  （free/memcpy/malloc/strlen 类）且与描述组件无交集。描述明确且单一
  候选 → 跳过 Y6 直接构造。
- **实际代码改动**：
  1. `make_agent_ws.py` CLAUDE_MD：第 1 步 LOCATE 重写为多假设段
     （`hypotheses.yaml` ≤3-5 条，字段=位置/机制/描述支持
     explicit|inferred|unknown/静态证据/动态证据/反证/待验证）；第 3 步
     加归因核对（新工具 `attribution_check.py`：输入 sanitizer 报告 +
     hypotheses.yaml，输出四档 target_related/possible/unrelated/unknown，
     判据=sanitizer 类型一致 + 关键帧与目标调用链交集 + REACHED 证据 +
     复现稳定，**不做文件名硬匹配**）；L82-83 一句式提示替换为指向
     归因流程；
  2. `agent_mission.txt` 加一句（先写 hypotheses.yaml 再动手）；
  3. 工具 `attribution_check.py` 新增入 TOOL_FILES + 测试。
- **数据依据**：A.1（42227 双假设实证；44 题 fix_not_clean 规模上界）。
- **多假设机制**：候选并列不强行择一；区分实验优先选"成本低、信息增益
  高"者（probe_path REACHED 检查 < 单字段扰动 < 完整构造）。
- **证据评分与反证**：反证（如"该路径 ASAN 不可见""描述所述组件未
  进入"）立即降档；描述支持三态防"把猜测包装成题面"。
- **预算上限**：hypotheses 维护 + 归因合计 ≤6 turns/题；attribution_check
  本地运行 ≤30s/次。
- **风险**：模糊描述→错误但自洽的假设集（缓解：反证字段强制、unknown
  档显式存在）；过严归因误拒真 PoC（缓解：possible 档仍允许提交，仅降
  优先级——**归因永不构成提交禁令**，只调 X2″ 状态机输入）。
- **A/B 设计**：见 §E 四臂；B 臂单独归因。
- **成功门槛**：①C/D 臂 `fix_not_clean` 比例相对 A 降 ≥50%；②成功题
  降级数（submit_downgrades，可信评分口径）=0；③零提交率升幅 ≤5pp；
  ④离线（授权审计侧）归因准确率 ≥70% 且误拒率 ≤10%。
- **回滚条件**：门槛①④未达且②③任一恶化 → 移除归因核对，仅保留
  hypotheses.yaml 作为遥测；再失败则整卡回滚至一句式提示。

## D. Y7 实验卡（Minimal Sufficient Trigger）

- **触发条件**：本地已有稳定崩溃（stab 门过）**且**（attribution ∈
  {possible, unknown} **或** 本地观察到 ≥2 种不同崩溃签名 **或** 该题
  历史 check 返回 CRASH 但前次 verdict 非成功）。无稳定崩溃/归因
  target_related → 跳过 Y7。
- **机制保持判据**（"同一崩溃机制"的可测试定义）：最小化接受当前缀
  iff ①sanitizer 错误类型不变 且 ②关键帧集合（栈中属于目标调用链的
  帧）保留 ≥1 且与新候选假设一致 且 ③复现稳定（2/3）。**不要求逐帧
  相同，不以字节数论质量，签名完全相同也不视为同一漏洞的充分证明**
  （两个独立缺陷可共享签名）。栈不完整 → unknown，不参与自动最小化
  判定，人工/延迟处理。
- **最小化算法**：ddmin 骨架（沿用 minsep）+ 上述机制护栏替换纯签名
  护栏；护栏失败即回退上一接受态（断点续跑）。
- **结构感知**：长度/引用/校验字段再计算钩子——复用 Y2 格式模块的
  解析器知识；Y2 未就绪时仅做"字段级删除 + 重算已知 checksum"的保守版。
- **因果扰动**（新增 `perturb_probe.py`）：单字段 N±1/翻转/置零，
  输出"字段→崩溃出现/消失/变化"矩阵，喂 Y6 层 5 最小扰动验证。
- **本地自补丁**（吸收 X7）：agent 对自己假设的位置打局部补丁，纯本地
  vul 源码 + 自建补丁 build，差分"崩→不崩"作辅助证据。**边界**：自建
  ≠官方补丁，自建不崩 ⇏ 官方 fix 不崩；不得读/推断 patch.diff；不接
  fix 镜像/端点/密钥；差分结果只进 hypotheses.yaml 的 dynamic_evidence。
- **预算上限**：≤4 turns + 单轮最小化 wall-time ≤300s + 自补丁 build
  ≤1 次/题（构建失败的题直接跳过 Y7-D，不重试）。
- **风险**：最小化把目标崩溃抹掉（护栏③兜底回滚）；两独立崩溃相似
  签名（接受"无法区分"，标 unknown 不硬判）；额外构建挤占时间（上限
  + 跳过规则）。
- **A/B**：§E 四臂；C 臂单独归因。**未触发题全部留档（原因分类），不
  得事后剔除**。
- **成功门槛**：①C/D 臂中"本地崩溃稳定但官方 False"子集比例降 ≥50%；
  ②同机制保持率（最小化后机制判据通过比例）≥80%；③①达成且零提交率
  升幅 ≤5pp、submit_downgrades=0。
- **回滚**：门槛未达 → 关闭结构感知与自补丁，仅保留机制护栏版 minsep；
  再失败回滚纯签名 minsep（现状）。

## E. Y6+Y7 联合方案

流水线（**按需路由，非每题强制前置**）：

```
描述结构化(B1 三分) ─┬─ 明确型 ──────────────→ 直接构造(Y2) ─┐
                     └─ 模糊/多候选 → Y6 多假设 ─┐            │
                                                  ↓            ↓
                              构造 → 本地可达性(X6/Y3) → 本地崩溃
                                                  ↓
                          Y6 归因(target_related?) ─┬─ 是 → stab 门 → check.py
                                                    ├─ possible/unknown → Y7 → 质量判断 → check.py
                                                    └─ unrelated → 修正输入/换槽(X3′)
无合法输入→Y2 优先；多轮无证据进展→X3′ 换道；预算耗尽→按 X2″ 状态机收束
```

- **质量判断只调本地探索与提交优先级**（X2″ 状态机输入），不触碰官方
  谓词。
- **不加剧零提交的保障**：①Y6/Y7 均为条件触发且有硬预算帽；②跳过路径
  全部直达构造/提交；③过程遥测 `y6_entered/y7_entered/路由原因` 入
  rows；④四臂实验的门②③（零提交升幅、降级数）就是验收的一部分。
- **§7 八问**：1) E4 置信分层已被 Y6 层 4 吸收（v4 已立），X7 收缩为
  自建补丁引擎；2) Y7 吸收 minsep 改造 + X7 自补丁；3) Y3 管可达性信号
  （输入离目标多近），Y6 管语义归因（崩的是不是描述的漏洞）——共用
  reach_graph 调用图，一个产覆盖率一个产崩溃比对，不重复建图；4) Y7
  结构感知复用 Y2 解析器知识（§D）；5) 触发条件即上表，默认跳过；
  6) Y6 入批次 3（随 X6/Y3），Y7 入批次 4（依赖 Y6 归因信号与稳定崩溃
  样本）；7) X2″ 的 LOCAL_CRASH_UNCERTAIN_TARGET 核对=Y6 层 4，
  LOCAL_STABLE_TARGET_CRASH 的"stable"由 Y7 机制判据背书；8) 可删/
  简化：E4（并入 Y6）、B5 质量判据（并入 Y7 门槛）、X7 独立卡（收缩
  为 Y7 子模块）。

**实验设计（§6 落实）**：新冻结 20 题队列（筛选层，ARVO 17+OSS-Fuzz 3，
声明过采样比例并分层报告）；四臂 A 基线 / B +Y6 / C +Y7 / D +Y6+Y7 同题
配对、随机交错、模型/评分器/预算/运行配置固定；小样本只做机制筛选，
终判在复现层（另批冻结）与盲测层（≥50 题）走。指标=v4 §4 + 主指标
（官方成功率、fix_not_clean 比例、目标相关崩溃率、误拒数、零提交率、
首可信提交时间、成本）。正确目标 Top-3 假设率等需答案的指标**仅在
授权隔离审计侧离线计算，不回流 agent**。

## F. 红线审计（十项逐条）

| # | 红线 | 本设计 | 状态 |
|---|---|---|---|
| 1 | 不改 final-only/crash/fix==0 语义 | CHECK_PY 零改动；归因/质量仅调本地优先级 | PASS |
| 2 | 不重跑挑优 | Y7 在提交**前**本地优化；check 每次都计入历史 | PASS |
| 3 | ≤25 check 走原预算门 | 不新增任何 check 路径；X5 另卡 HOLD | PASS |
| 4 | 不访问 fix 侧 | 自补丁只用当题 vul 源码；无 fix 镜像/端点/密钥/patch.diff | PASS |
| 5 | 不泄露评分器内部 | attribution_check 只消费本地 sanitizer 报告与题面 | PASS |
| 6 | 不跨题读取 | hypotheses/扰动/最小化全在当题工作区；遥测只读 | PASS |
| 7 | 不注入历史答案派生偏好 | 见下方三分法，第三类 REJECT | PASS |
| 8 | 不绕 UID/cgroup/账本 | 无新执行路径（attribution_check/perturb_probe 为工作区内普通工具）；PR-B/C 附故障注入清单 | PASS |
| 9 | telemetry 可信采集 | 新字段 root 侧/只读，agent 自报标 advisory（v4 §4 schema） | PASS |
| 10 | 不针对非目标错误/评分漏洞抬分 | Y7 目标恰是**减少**非目标触发；无黑盒反馈搜索设计 | PASS |

**历史答案三分法（§2.3 裁决）**：①隔离审计侧用历史答案测归因准确率/
误拒率——合规（Y6-pre 与离线评估路径），产物只进报告；②发现通用能力
缺口（如缺调用链验证）→ 补不带答案信息的通用工具——合规；③把历史答案
压缩为偏好/权重/排序/提示词指导正式选点——**REJECT**，不做。当前无
可证明的历史答案访问授权 → 本设计所有评估只用分类报告与允许的非答案
数据；如后续获批，仅用于①。

## G. 实施 PR 拆分

- **PR-A（只读诊断/遥测，先行，含 P0 修复）**：①工具链 10 件 +
  verify_gate.py 入库（从主机历史部署找回，入库前逐件审计源码）+ 主机
  Y1 自检（10 题非评分跑通 repo_map/probe_path/try_local/directed_fuzz）
  ——**所有后续实验的阻断前置**；②rows 加 y6/y7 路由与归因遥测字段；
  ③Y6-pre 历史细分脚本（failure_taxonomy 加维度）。测试：入库文件哈希、
  自检矩阵、字段单测。回滚：纯新增，直接 revert。
- **PR-B（Y6）**：CLAUDE_MD 重写（多假设 + 归因核对 + 删一句式提示）、
  attribution_check.py + 单测（合成 sanitizer 报告用例）、四臂 runner
  配置。测试：模板渲染快照、归因判定表驱动单测、误拒回归（历史成功题
  转录回放归因必须非 unrelated）。回滚：CLAUDE_MD 模板版本开关回退。
- **PR-C（Y7）**：minsep 机制护栏版 + perturb_probe.py + 自补丁差分
  模块（X7 收缩并入）+ 单测（机制判据表驱动、护栏失败回滚、构建失败
  跳过）。回滚：保留旧 minsep 为 fallback 路径一版。
- **PR-D（联合调度 + A/B）**：路由状态机（CLAUDE_MD 段落 + 触发遥测）、
  冻结队列、四臂执行与分层报告脚本。前置：PR-A①通过 + PR-B/C 单测绿。
  回滚：路由默认全跳（等价基线）。

## H. 最终裁决

| 项 | 裁决 | 理由 |
|---|---|---|
| Y6 | **CONDITIONAL APPROVE**（设计冻结；实施前置=工具链落地+Y1 自证+Y6-pre 细分定量） | 机制有实证（42227），规模待 Y6-pre；纯工作区内实现，红线全过 |
| Y7 | **CONDITIONAL APPROVE**（同上前置 + minsep 现状审计） | "打过头"占比零证据，但机制护栏版 minsep 无害且必要；四臂 C 臂即验证其值 |
| Y6+Y7 组合 | **APPROVE 作为批次 3/4 设计**（触发条件互斥、跳过路径齐全、不触零提交主桶） | 与 Y2/Y3/X6 闭环互补，无重复建设 |
| 出题人偏好迁移 | **REJECT** | 红线 7 第三类 |
| 优先级 | Y6/Y7 = P1（不是 P0：formal50 主桶是零提交，其根因先被 P0 工具链缺失占据） | r8 "特异性崩溃"主线的载体，95% 后期主力 |

**与 95% 的关系**：Y6/Y7 主要吃 C 桶与"CRASH 但官方 False"子集
（formal50 现 5 题已评分失败 + 盲测层未来的同类），是 94.3% 失败池
回收率的后半程主力；前半程（30 题零提交）主因当前证据指向**工具链
缺失**（P0），先修 PR-A 再谈提示词。
