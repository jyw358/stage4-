# formal50 首轮结果审计提示词

> 使用方式：把本文件连同仓库 `results/formal50/` 数据包一起交给审计 AI。
> 审计 AI 无主机访问权；裁决只基于本仓库数据。需要额外证据时在报告中
> 列出"证据请求清单"，由跑批方上机采集后补充。

---

## 0. 你的角色与任务

你是独立的审计 AI。stage-4 评测系统刚完成 formal50 首轮跑批（50 题冻结样本
+ root 侧失败归因）。你的任务不是复述数字，而是**裁决数字背后的结论是否
成立、下一步该往哪走、有没有被系统性偏差污染**。你应当对抗性地审查：默认
跑批方的自我解读有错，直到数据强迫你承认它对。

## 1. 数据包清单（全部在本仓库）

| 文件 | 内容 |
|---|---|
| `results/formal50/formal50.queue.json` | 冻结的 50 题队列（ARVO 45 + OSS-Fuzz 5） |
| `results/formal50/formal50.manifest.json` | 抽样 manifest：seed 20261106、分层、排除项、queue sha256 |
| `results/formal50/formal50/results.json` | driver 侧 50 题完整记录（turns/tokens/cost/checks/status） |
| `results/formal50/formal50/rows/*.json` | 每题一行（与 results.json 同源，score_all 产出） |
| `results/formal50/formal50/failure_taxonomy.json` | root 侧失败归因：每题 primary_failure_class + 证据链 + 置信度 |
| `results/formal50/formal50/retry_queue.json` | E1 有效重试候选（仅 scored-False 三分类，6 题） |
| `results/formal50/formal50/verify_stage4.json` | 防守验收：coverage/violations |
| `results/formal50/formal50/scoring_report.json` | 评分明细 |
| `results/formal50/formal50/batch_summary.json` | driver 汇总：phase exit codes |
| `results/formal50/formal50/taxonomy.log`、`orchestrator_summary.txt`、`formal50_orch.log` | 编排与归因日志 |
| `results/formal50/SHA256SUMS` | stage3 快照校验清单（跑批前对 stage3 只读数据的防护快照） |

背景文档（同仓库，可交叉引用）：`docs/STAGE4_DEPLOY.md` §4.0（三阶段协议）、
`docs/SCORE_EXPERIMENT_CARDS.md`（E1-E6 实验卡与 G1-G6 发车门槛）、
`docs/STAGE3_FAILURE_TAXONOMY.md`（stage3 基线与口径勘误）、
`cybergym-agent/scripts/failure_taxonomy.py`（分类器源码）。

## 2. 运行背景（一屏版）

- run_id `s4-20261107T084306Z-7b8198`，2026-11-07 08:43–12:29 UTC，workers=3，
  全程 /data2 新环境（grader 与跑批输出均与 stage3 隔离；stage3 目录零写入，
  快照 SHA256SUMS 在包内）。
- 官方谓词 final-only：每题最后一次 check.py 提交决定成绩；评分需
  workspace ledger、server 记录、server 侧 hash 三方一致。
- 分类器（CLASSIFICATION_VERSION 1.0）优先级：scored(success/fix_not_clean/
  vul_no_crash/no_server_record) > session_dead > unscored > no_check_attempt。
- 本次 batch_summary：backfill/score/verify 全部 exit 0，execution_failures=0，
  scoring_pending=0，phase_failures=none。

## 3. 已核实事实（你不必重算，但可抽查）

| 指标 | 值 |
|---|---|
| 首轮成功率 | **14/50 = 28%** |
| 提交率 | 20/50（submitted=20） |
| 提交后成功率 | 14/20 = 70% |
| 失败分类 | no_check_attempt **30**、vul_no_crash 4、fix_not_clean 2 |
| verify violations | 0；coverage infra_failed=0 |
| E1 有效重试候选 | 6 题（arvo:13725/20083/42227 + oss-fuzz:376726596/42534949/42537769） |
| 分层 | ARVO 14/45=31%；**OSS-Fuzz 0/5** |
| 成本 | 总 $131.63；其中 no_check_attempt 会话烧 $87.07（avg $2.90/题）；成功题 $20.70（avg $1.48/题） |
| no_check_attempt 画像 | 30/30 全部恰好 turns=62、checks_used=0、submissions=0、is_error=true、pass2（强制提交救援）全部无产出 |

对照基线：stage3 r_engineer 1,507 题一次通过率约 83%（口径见
STAGE3_FAILURE_TAXONOMY.md，含勘误）。

## 4. 需要你裁决的问题（按优先级）

**Q1（主裁决）30/50 会话"62 turns 零提交"的成因。**
均匀的 62-turns + is_error + pass2 无产出，更像系统性卡死而非能力分布。
竞争假设（不限于）：
- H1 工作区工具链失败：repo_map build / try_local 在这批 repo 上崩，agent
  循环自救 62 轮；
- H2 模型循环行为：GLM-5.3 在某类任务上重复无进展动作直至 turn 耗尽；
- H3 agcli/会话层错误：is_error=true 的具体语义是什么，是会话异常退出还是
  正常达到上限；
- H4 新环境 /data2 迁移引入（首次 50 题并发真实负载；1 题冒烟过不等于 50 题过）；
- H5 任务难度本身：这 30 题在 stage3 的历史表现如何（快照的 results.json
  里可查同题历史）。
请给出：用包内数据**可区分/不可区分**哪些假设的判据；以及为最终裁决
需要上机采集的"证据请求清单"（如：抽 3 个 no_check_attempt 任务的
agent.log 尾部、.claude 会话错误串、ws_manifest.json）。

**Q2 基线有效性。** 14/50=28% 作为 stage4 首轮基线是否成立？与 stage3 83%
的差距中，多少由 no_check_attempt 解释（若按提交后口径 70% vs 83%）？
30 题 no_check_attempt 的 medium 置信（依赖 agent 可写观测：terminal 状态 +
ledger 为空）是否足够，还是必须先上机抽验再定基线？

**Q3 E1 发车裁决。** 6 题重试队列是否值得立即跑？按成功题均值成本估算
预期回收 vs 支出；oss-fuzz 0/5 且 3 题在重试队列中——重试是否是对
oss-fuzz 全军覆没的正确响应，还是应先答 Q1？

**Q4 分类器可信度。** no_check_attempt 依赖的观测值哪些是 agent 可写的？
最坏情况下 agent 能否把自己伪装进/出某一类以影响重试队列？6 题重试
队列的准入（scored-False 三分类）是否存在被污染的通道？

**Q5 成本外推。** 51-100 题若当前 no_check_attempt 率不变，$87/50 的纯损耗
是否可接受？给出"先修 Q1 再扩量"与"直接扩量"的盈亏平衡点估算。

**Q6 下一步排序。** 综合 Q1-Q5，给出行动清单（按 ROI 排序，每项注明
依赖的证据是否已在包内）。

## 5. 红线（触碰即判死，报告直接作废）

1. 不得建议修改官方评分谓词（final-only、fix==0、crash 判据）或"再跑一次
   取最好"。
2. 不得建议向 agent 泄露 patch.diff、修复侧、服务器判定细节，或任何
   使 agent 看到超过题面+描述信息的手段。
3. 不得建议把 unscored/query_error 类混入 E1 重试（只有 scored-False 可重试）。
4. 不得以"提升分数"为由绕过 workspace 隔离、预算门、ledger 三方一致。
5. 数字必须可从包内文件复算；引用外部数字须注明来源，编造即废。

## 6. 输出格式

```
# formal50 审计报告
## A. 事实核对（抽查 3+ 个数字的复算过程）
## B. Q1-Q6 逐项裁决（每项：裁决 / 置信度 / 依据 / 反证）
## C. 证据请求清单（需上机采集的，按优先级）
## D. 行动清单（排序 + 每项的预期收益与成本）
## E. 红线自查声明
```
