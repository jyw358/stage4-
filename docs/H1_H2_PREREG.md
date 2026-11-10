# H1/H2 预注册（r21 P1-D/P1-E，开跑前提交外审）

## 共同框架

- 队列：全新冻结（确定性等距选取，排除 formal50 + pair + p2 + y1 +
  ab 第一轮全部用题）；队列文件+sha256 提交外审后才开跑。
- 两臂交错并行（各 1 worker，同一 ab-driver-v2：全局 worker 等待 →
  顺序 backfill/score/verify——r21 P0-C 已实现）。
- 主分析集 = 全部预注册任务（含未触发 pass2 者）；同时按类别报告
  条件性结果并说明两臂触发集差异。
- 三层提交指标（r21 P0-A 修复版提取器）：check_intent（transcript
  任意路径形式命令）/ check_executed（VERDICT 输出证据）/
  check_accepted（.checks + protected ledger 行）；另记录候选 sha256、
  来源（poc*/crash-*/fz/生成器名）、提交前假设文本、官方结果、成本、
  剩余预算。
- 扩样规则预注册：首轮 16 题；若某实验的自然类别触发 <4，追加一轮
  16 题（同样确定性选取、同一规则），至多两轮；不得依结果选择性扩样。
- 停止条件：预算上限 $40/实验；出现 integrity_fail>0 或 verify
  violations>0 立即停并上报。
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

## 资源矩阵（两实验）

每任务 pass1 45 turns + pass2 15 turns；16 题 × 2 臂 ≈ 32 任务/
实验；按第一轮 $27.5/12 题计 ≈ $35/实验（上限 $40）。
