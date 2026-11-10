# 新冻结队列正式 A/B（pass2 v4.2 vs legacy）

时间：2026-11-09T14:02Z→11-10T01:05Z（含 A 臂评分补齐）；主机 nacocom；
队列 `q12frozen.json`（12 题全新、确定性等距选取，排除 formal50 全
50 题 + pair/p2/y1 用题；sha `1d89bb06…` 双臂一致）。

- A 臂（legacy 固定 pass2 提示词）：**official 8/12（66.7%）**
- B 臂（triage files-only + v4.2 类别提示词）：**official 6/12（50.0%）**
- verify violations=0；execution_failures=0；A 臂 $27.48（B 臂同量级）

## 逐题配对表（机器生成自 AB_ANALYSIS.json，勘误 r20 P0）

| 题 | A | B | 配对 |
|---|---|---|---|
| arvo:1268 | True | no_submission | A-only |
| arvo:3438 | True | no_submission | A-only |
| arvo:368 | True | False | A-only |
| arvo:4492 | True | False | A-only |
| arvo:3736 | no_submission | True | B-only |
| arvo:4099 | no_submission | True | B-only |
| arvo:3257 / 3535 / 5256 / 5641 | True | True | both-success ×4 |
| arvo:1538 / 1856 | no_submission | False | both-fail ×2 |

合计：both-success 4、A-only 4、B-only 2、both-fail 2 = 12；
A = 4+4 = 8，B = 4+2 = 6 —— 与 verify 顶栏一致（初版报告手写分解
误记双 True 3/双败 3 且 4099 重复归类，r20 指正，已勘误）。
McNemar 独赢 4 vs 2，双侧精确 **p=0.6875** —— 无显著差异。

## v4.2 应用验证（四层证据）

树 triage sha `acd9c35b`（=commit f53e8af）；7/7 pass2 触发题生成
`.p2prompt`+`triage.json`（5×B+2×D）；B 类 5 份含 SUBMIT FIRST/STEP 1；
B 臂 pass2 transcript 实收 v4.2（10/8 次命中、legacy 0），A 臂对照全零。

## r19 指标矩阵

**turn 口径说明（r20 要求）**：下表 "首 check turn" 指该 pass2 会话
transcript 内的 **assistant 消息序号**（从 1 计，含纯文本/thinking
消息，故可超过 15-turn 工具轮配额；A 臂的 18/29 并非工具轮次）。
两臂同口径，可比较相对早晚，不可换算为精确轮数改善倍数。

| 指标 | A 臂 | B 臂 | 读数 |
|---|---|---|---|
| official success | **8/12** | 6/12 | A 高，不显著 |
| submitted | 8/12 | **9/12** | B 略多 |
| pass2 触发 | 6 | 7 | 基本对称 |
| pass2 内 check | 2/6 题 | **3/7 题** | B 行为改善 |
| 首 check turn（assistant 序号，见口径） | 18 / 29 | **5 / 5 / 6** | submit-first 生效 |
| pass2 check→CRASH 转化 | 2/2 check 题 | **0/3 check 题** | r19 预警命中 |
| D 类（无产物）结果 | 1268 legacy 构造 CRASH | 0/2 产出 | legacy 构造文案更强 |
| 零提交率 | 4/12 | **3/12** | B 改善 |

注：3736（B 臂 True）的 crash=1 来自 pass2 会话重读 pass1 check 输出，
非 pass2 新提交（其官方 1 check 在 pass1 期完成）；该字段存在少量
转录噪声，已在 AB_ANALYSIS.json 标注。

## 机制解读（按题对照，r20 措辞修正后）

1. **A 独赢 4 题的分解**：3438/368/4492 是 pass1 随机性（A 臂 pass1
   42–49 turns 内解出，B 臂同题 pass1 烧满 62 turns 后 pass2 无法补天）
   ——与提示词臂无关；**1268 是唯一真实的臂间行为差**：A 臂 legacy
   "write poc_gen.py + produce poc.bin" 在 D 类上从描述构造成功并
   CRASH，B 臂 v4.2 D-construct 0/2 产出（单题机制证据，不能外推为
   策略优劣结论——v3/v4 D 类累计 0/11 与 legacy 1/1 属不同队列/条件，
   不可直接比较）。
2. **B 独赢 2 题**（3736/4099）同样为 pass1 期解出，非 pass2 之功。
3. **v4.2 的真实效果**：早提交行为已观察到（首 check 提前、零提交率
   4→3），但**提交有效性存在负面信号**——pass2 的 3 次救援提交均未
   达目标 CRASH（非配对证据，无法区分"早提交策略之罪"与"该 3 题候选
   本身无可利用性/更难"）；C 类本轮**无实例**，未获任何正式效果证据。
4. 双臂均远高于 formal50 基线口径（不同题集不可直比，仅说明修复后
   链路健康）。

## backfill 竞态时间线（r20 要求的不变量核查）

- A 臂 worker 全部终止、12/12 rows 落盘后，驱动器收尾调 backfill，
  因 **B 臂 worker 仍在运行**被脚本"有活跃 worker 即 REFUSE"守卫拒绝
  （rc=2）——该守卫本身即 r20 所要求不变量 1 的执行者；
- 随后 A 臂 score 在 fix 侧数据缺失下执行，4 题 no_records →
  official=None（未评分，未择优：backfill 脚本输出"补跑 vul 崩溃记录
  =0"，本轮 0 次补跑、0 次新增 check、未触碰任何提交）；
- B 臂（后结束）backfill/score/verify 正常；
- 次日 01:05Z 两臂 worker 均已终止，手动重放 A 臂
  backfill（0 补跑、PASS）→ score（8/12 decided=8）→ verify
  （violations=0、infra_err=0、integrity_fail=0）；
- 8/12 与 6/12 由 verify 从最终固定 rows 复算（submitted=8/official=8），
  与逐题配对表一致。
- 驱动器整改（已记入待办）：收尾改为"等待全部 worker → 冻结 →
  backfill → score → verify"，多实例同主机时以全局 worker 注册表为准。

## 结论与建议（r20 措辞修正）

- **拒绝将 v4.2 全量设为生产默认**（r19/r20 裁决一致）。
- 混合策略仅为**下一轮实验假设**，非已验证优化：B 类 submit-early
  行为改善已观察到但候选质量未证实（0/3 救援提交未达目标）；C 类
  本轮无样本；D 类 Legacy 构造优先实验（1268 单题方向性支持）。
- pass2 有效性瓶颈不在"是否提交"而在"提交什么"——按 r20 建议开
  H1（D 类构造：Legacy vs v4.2）与 H2（B 类：立即提交 vs 受限本地
  质量验证）两条独立实验，新冻结队列、预注册、全任务主分析集，
  同时记录候选 hash/来源/本地验证状态/提交前假设/官方结果。
- pass1 随机波动主导多数独赢 → 后续 pass2 专项测试采用固定同题
  pass1 检查点的非评分机制对照（仅当前题内诊断复用，不绕过正式
  重试红线）。
- 主分析集=全部 24 任务；本报告无事后筛题。

产物：主机 `/data2/arms/ab/AB_ANALYSIS.json`；A 臂评分补齐记录
（backfill 竞态 → 手动三步，verify PASS）。
