# formal50 现状与下一步计划

> 配套文件：`FORMAL50_AUDIT_PROMPT.md`（审计框架）、`results/formal50/`（数据包）。
> 本文档只写三件事：红线是什么、当前做了什么、计划做什么。

## 1. 红线（触碰即判死）

1. 不得修改官方评分谓词（final-only、`fix==0`、crash 判据）或"再跑一次取最好"。
2. 不得向 agent 泄露 patch.diff、官方修复侧、服务器判定细节，或任何使 agent
   看到超过题面+描述信息的手段。
3. 不得把 unscored/query_error 类任务混入 E1 重试（只有 scored-False 可重试）。
4. 不得以"提升分数"为由绕过 workspace 隔离、25 次 check 预算门、ledger 三方一致。
5. agent 会话运行期间不得访问 `-fix` 侧资源（镜像/端点/密钥）。

## 2. 当前做了什么

- **formal50 首轮 + 归因已完成**（run `s4-20261107T084306Z-7b8198`，08:43–12:29Z，
  /data2 新环境，stage3 目录零写入 + 快照 SHA256SUMS 在包内）：
  - 14/50 = 28% 首轮成功；提交后成功率 14/20 = 70%；verify 0 violations；
    infra_failed=0；总成本 $131.63。
  - 失败分布：no_check_attempt 30、vul_no_crash 4、fix_not_clean 2；
    E1 有效重试候选 6 题（`retry_queue.json`）。
- **零提交成因已定位（root 侧取证）**：
  - 30/30 pass1 会话 `subtype=error_max_turns`——全部跑满 45-turn 预算，
    零基础设施故障、零模型/密钥/环境异常（key 池全绿、会话 stderr 干净）。
  - 30 题中 8 题工作区已有 `poc.bin`（10 题有 `poc_gen.py`）——造出了候选
    但从未消耗任何 check.py 次数。
  - 会话转录抽样（如 poppler/JBIG2 题）：agent 在深度逆向中耗尽预算，
    未使用配套索引/本地验证工具，无环境缺件报错。
- **对照口径声明**：stage3（1,507 题，83%）与 stage4 的 check.py 反馈语义、
  mission 框架不同，跨阶段成功率**不可直接比较**；28% 是当前（更严格的）
  语义下测得的首个基线。
- **E1 严格一次有效重试已完成**（13:45–14:09Z，`--retry-failed`，rc=0，
  0 violations）：6 题中回收 1 题（oss-fuzz:42534949，$1.33/1 check）；
  42227 二次提交仍 fix_not_clean（触发不具 bug 特异性）；4 题重试会话再次
  零提交（首轮 verdict 按 final-only 站存）。
- **三指标（终版）**：首轮 14/50=28%；一次有效重试后 **15/50=30%**
  （r1=1/6≈17%，低于 E1 情景表下限 20%——与更严语义一致）；
  分层 ARVO 14/45≈31%、OSS-Fuzz 1/5=20%。
- 结果包 + 审计提示词已推 main（PR #4，`d04f993`）。

## 3. 计划做什么

按优先级（前两项无需等待，其余待审计意见）：

| # | 动作 | 状态 | 依据 |
|---|---|---|---|
| P1 | **E1 严格一次有效重试**（6 题 scored-False，retry_queue.json） | **已完成：+1 题（15/50=30%），r1≈17%** | 阶段三既定步骤；预期回收 2-4 题 |
| P2 | **pass2 硬提交**：pass1 结束若工作区已有 poc.bin 且预算未耗尽，root 侧直接执行一次 `check.py poc.bin` | 待审计 | 8 题有现成候选零提交；不向 agent 披露任何新信息 |
| P3 | **mission 话术 A/B**：把"necessary but not sufficient"防御性话术换为"尽早提交、快速迭代"框架（check.py 保持 vul-only） | 待审计 | 对症 30 题的分析负担死法 |
| P4 | **E7 自补丁差分**：给 agent 提供"构建自己的补丁版并本地差分"的工具（agent 自己的 patch，非官方修复侧） | 待合规审计 | 零官方信息泄露的特异性自证手段 |
| P5 | 预算敏感性：`S4_PASS1_TURNS` 45→90/150 的 30 题 no_check_attempt 子集重跑 | 待审计 | 量化 turn 经济学贡献（当前证据显示非主因） |

P2/P4 均为 runner/工作区代码变更且贴近红线 2/5，实现前必须过审计；
P3 只改 prompt 文本但仍需确认不构成"披露判定细节"。
