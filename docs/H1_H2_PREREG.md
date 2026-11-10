# H1/H2 预注册（r21 P1-D/P1-E，开跑前提交外审）

## 共同框架

- 队列：全新冻结（确定性等距选取，排除 formal50 + pair + p2 + y1 +
  ab 第一轮全部用题）；队列文件+sha256 提交外审后才开跑。
- 两臂交错并行（各 1 worker，同一 ab-driver-v2：全局 worker 等待 →
  顺序 backfill/score/verify——r21 P0-C 已实现）。
- 主分析集 = 全部预注册任务（含未触发 pass2 者）；同时按类别报告
  条件性结果并说明两臂触发集差异。
- 三层提交指标（r22 口径修正）：**check_intent** = transcript 命令
  （任意路径形式，含绝对路径——r21 教训）；**check_executed** = 可信
  执行记录（.checks 计数变化与 mtime、protected ledger 行互核），
  transcript 中的 VERDICT 文本**仅作辅助线索**，不单独构成执行证明；
  **check_accepted** = .checks + ledger + verify 三方一致。另记录候选
  sha256、来源（poc*/crash-*/fz/生成器名）、提交前假设文本、官方
  结果、成本、剩余预算。
- 扩样规则（r22 明确化）：**触发数按臂分别计**——任一臂 D 类自然
  触发 <4 即启动追加轮；round-2 队列**已与 round-1 同时冻结**
  （`q16frozen_round2.json`，同一确定性等距规则的第 17–32 个，sha
  记录于 QFREEZE.txt），不存在看结果挑题空间；同时报告两臂配对
  任务的 D 触发一致性；主成功率分析覆盖全部预注册任务。
- 预算（r23 实装）：第一轮实测 **A 臂单臂 12 任务 $27.48**（双臂合计
  $55）→ H1 首轮 32 任务预计 **~$73**（区间 $65–85），预算申请 **$90**
  （含两轮上限则 $150，第二轮开跑前另行确认）。**驱动器内嵌预算门**：
  轮询两臂已完成 rows 的 `cost_usd` 累计，**≥$85 即触发停止**（终止本
  实验 worker、写 FAILED_AT、不最终化）；在途暴露 ≤2 任务（每臂各 1，
  实测单任务最高 $3.49）→ 绝对最坏 ≈$93，仍在 $90 量级内且不会静默
  超支（失败显式上报）。
- 停止条件：预算触发 $85/硬顶 $90（首轮）；integrity_fail>0 或 verify
  violations>0 立即停并上报（fail-closed 驱动器自动执行）。
- 驱动器与验收（r23 加固）：`h1_driver.sh` fail-closed（R1–R6：deadline
  **在 launch 之前设定**并覆盖全程（zombie 感知轮询，无裸 wait）；预算
  门在轮询循环内实装；本实验 run-id token 残留检查；外来 stage4 阻断
  最终化不越权清理；cgroup 残留；**R2.5 运行时提示词审计**（每个实际
  交付的 .p2prompt 必须命中本臂 manifest sha；triage 失败回退 legacy
  提示词的任务记为 fallback、**不计入实验臂成功应用**，入统计报告）；
  finalize 链每条命令置于 `timeout` 监督下（卡死命令被真实终止，
  rc=124/137 → fail）而非事后时间戳检查；首错即停、FINALIZED_AT 仅在
  双臂 verify 全过后原子写入、输出目录防覆盖；**任何失败只回收本驱动
  启动的 worker 进程组（TERM→≤90s→KILL→核验，cleanup=verified /
  inspection_incomplete 落盘），绝不触碰外来进程**。装配器在任何失败
  下绝不删除既有目录（tmp 装配 + 原子改名）。已通过：故障注入 v2
  **13/13**（真实驱动器全文运行：装配拒覆盖+哨兵、wall 超时回收、
  worker 非零阻断 finalize、backfill/score/verify rc、violations>0、
  卡死监督终止、预算停止+在途回收、全成功路径、运行时提示词审计
  正/毒样本、dry-run 门控）、无评分 dry-run（pipefail 逐步 rc 门控）。
- triage 回退口径（r23）：pass2 会话若因 triage 失败回退 wrapper 内联
  legacy 提示词（H1_WARN 记录于 *.triage.stdout），该任务在两臂统计中
  均标注 fallback，不作为实验臂策略的成功应用参与机制分析；主成功率
  仍按官方评分计入（实验完整性记录在 H1_RUNTIME_PROMPT_AUDIT.json）。
- 红线不变：final-only、25-check、fix 侧隔离、protected ledger、
  无跨题知识、unscored 不重跑。

## H1：D 类构造策略（APPROVED 设计）

- 唯一自变量：D 类（triage 无产物）提示词。臂 D-Legacy = legacy
  构造文案（write poc_gen.py, produce poc.bin, run check.py，保留
  r19 基本有效性约束：非空、题目格式、禁填充）；臂 D-V4 = v4.2
  D-construct 原文。B/C/GENERIC 类两臂同用 v4.2（不在本实验变动）。
- 预期 D 类触发：按第一轮 2/12 推断 16 题 ≈ 2–4 题/臂；<4 则按扩样
  规则追加。
- 指标：D 类有效候选产出率（concrete non-empty task-format）、
  check_intent/accepted、官方成功率、零提交率、成本。
- 不以 1268 单题或 0/11 历史作为先验调参依据。

## H2：B 类立即提交 vs 受限本地质量验证（CONDITIONAL——依赖 1B）

- 前置：本地 oracle 在 agent 身份下端到端自检（r21 P1-E）。现状：
  agent UID 对 docker 无权限 → agent 侧只可能 ORACLE_ERROR（Y1 v2 已
  证诚实上报）。因此"受限本地质量验证"只能在 **root 侧 triage
  --exec**（阶段 1B 隔离审计）落地。**1B 审计未过前 H2 不开跑。**
- 质量门状态机（预注册，有界决策非硬性拒交）：
  | root 侧 exec 结果 | 行为 |
  |---|---|
  | 稳定 FATAL + 目标机制一致（Y6 归因） | A1：优先提交 |
  | 稳定 FATAL、目标未知 | A2：≤2 turns 机制核对后可提交 |
  | 真实运行、无崩溃 | 提示调整候选，不消耗 check |
  | ORACLE_ERROR | 不当 CLEAN；按降级（B 类文案）执行 |
  | 超时/证据不足 | 标 unknown，回退 v4.2 立即提交路径 |
  本地上限：每候选 1 次 exec、每任务 ≤3 候选、单次 ≤20s、总 ≤90s。
- 臂：B-Now = v4.2 submit-early 原文；B-Gate = 质量门分流后的提示词。
- 指标：有效提交转化率（accepted CRASH / accepted）、首 check turn、
  官方成功率、oracle 有效执行率、成本。

## 资源矩阵（分实验，r23 统一口径）

每任务 pass1 45 turns + pass2 15 turns；16 题 × 2 臂 = 32 任务/实验。

| 实验 | 预计成本 | 触发/硬顶 | 状态 |
|---|---|---|---|
| H1 首轮 | ~$73（$65–85；单臂单任务实测 $2.29 均值/$3.49 峰值） | $85 触发 / $90 硬顶（驱动器内嵌） | 待外审放行 |
| H1 二轮（仅当扩样触发） | ~$73 | 另行申请（两轮合计上限 $150） | 未开跑 |
| H2 | ~$73（同规模估算，待 1B 后复核） | 开跑前单独申请 | HOLD（依赖 1B） |
