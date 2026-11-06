# 50 题 pilot 实验卡 v2（r5 复审后修订，2026-11-06）

> v2 依据提升方案复审裁决重写：撤销全部定量增益承诺（改情景/上界）、
> E2② 移出发车清单、E4 改置信分层、E6 保留现状（pass2 是零提交救援，
> 非死代码）、E5 升级为 root 侧可信归因、新增 B1-B6 补卡与发车门槛
> G1-G6。数据口径见 taxonomy v2 顶部勘误。

## 总评

主要瓶颈假设（均待 G2 归因与 50 题实测定量，不预承诺）：

1. 单轮重试的真实回收率 r1 未知（多轮累计 76% 仅是上界情景）；
2. 失败题的 row-check 中位 0 是观测值，"提交前死亡"假设待 root 侧四格
   联表裁决；
3. 98 题跨源判定不一致未解释，不构成 stage4 免费增益；
4. fix 不干净类在 50 题等比例期望 ~1.5 题，是 E4/B5 的类别上界；
5. 5 题 A/B 只能验证可执行性，终判提升需数十题配对评估。

## E1 严格一次有效重试（G3，发车必做）

- **有效失败**：`score_status == "ok" ∧ official_success == False`——
  只有评分完成的 False 才可重试。`None/query_error/integrity_error/
  no_records(未提交)` 一律走 scoring recovery 或人工诊断，**绝不重跑**
  （防止把评分链路问题变成 agent 抽签）。
- **恰好一次**：以 `attempt` 归档（stage4.py 已有机制）证明同一题在本
  正式批次内至多一次有效重试；重试用 fresh workspace，且前置校验上一
  attempt 的进程树清理完成（无 process residual）。
- **双向度量**：按题保存首轮/retry/终判全轨迹；统计新增成功与**降级数**
  （首轮成功题不受影响是设计约束，仍需在报告列明）。
- 增益：情景表（taxonomy §4），r1 由本 pilot 实测。
- 成本明细：失败题期望 ~8.5 × $0.6-1.5 ≈ **$5-13**（v1 的 $25-35 系误
  把多轮成本当单轮，已修正）。

## E2① 止损检查点（后置实验，非停止线）

- CLAUDE_MD 增加"进度检查点"：turn 25 无本地 crash 且无 REACHED 证据 →
  触发一次换道决策（见 E3）；turn 35 仍无 → 收敛写 SOLUTION.md（明确
  标记 `STATUS: UNSOLVED`，禁止伪装成功）。
- 优先级声明：既有"15 turns 首提交"**不覆盖** stab 稳定性门；检查点
  **不停止**已持有目标可达/崩溃证据的题。
- 阈值 25/35 为初始值，仅在 G2 归因证实"提交前死亡"类存在后启用；
  A/B 度量用过程指标（有效进展率、首个提交延迟、空转 turns、wall-time），
  不用 5 题终判。

## E2② infra 快败（撤回，未实施；重设计后再议）

- v1 的"wrapper 连续两错杀会话"**移出发车清单**，且 v1 文档中
  "只读错误计数"与"杀会话"的自相矛盾在此声明：**当前没有任何已实施的
  杀会话代码**。
- 重设计方向（未实施）：root 侧可观察错误分类（refused/timeout/429/5xx/
  鉴权失败/已受理但响应丢失，最后一类绝不能当"未提交"）+ 经既有进程
  清理、账本推广与完整性检查后记 `infra_abort`；不直接杀 PID。
- 上线前置：四类故障注入回归（连接拒绝/连接超时/429/已受理断线），
  逐项验证分类、账本、评分、进程残留不变量；任何重复计分/误归因/清理
  不全即否决。

## E3 换道阶梯（可选 A/B，条件分支非替换）

- CLAUDE_MD 第 5 步**之下**加条件分支（不替换原"每次换方法、批量实验"
  纪律）：reach_graph 前两名候选各 2 次构造机会后仍无 REACHED →
  format-first（找输入解析器→造结构合法最小容器）→ 以容器为 seed 跑
  **一次** 300s directed_fuzz。
- 追加 fuzzing 轮须同时满足：有新覆盖/新可达证据、预算余量充足、当前
  路线优于替代（pass1 上限 2400s、pass2 另有 900s，fuzzing 占用需显式
  记账）。
- A/B 过程指标：REACHED 比例、有效输入比例、目标 sanitizer 命中率、
  首次提交时间。类别上界提醒：零服务器记录类 50 题等比例期望 ~1.8 题。

## E4 崩溃位置置信分层（可选 A/B，无绝对拒交门）

- 取代 v1 "报告不点名函数/文件即禁止提交"，改四档：

| 证据级别 | 行为 |
|---|---|
| 明确匹配目标函数/文件/可解释调用链 | 优先提交 |
| 位置不能直接识别，但崩溃稳定、可复现、语义合理 | 允许提交，记录低置信度 |
| 明确在无关初始化/解析/环境路径崩溃 | 先修正输入，不作为优先提交 |
| 符号缺失/栈不完整 | 记录 unknown，不自动判死 |

- 理由：sanitizer 常先命中分配/释放/拷贝等通用函数；fix=1 也不必然是
  崩错地方（输入格式/独立缺陷/环境差异均可能）。
- 验证：从历史已判样本离线比对新旧门槛的**误拒率**（原本成功被新门槛
  拒绝的比例）为主指标，不只看匹配率。

## E5 root 侧可信失败归因（G2，发车必做，最高优先工程项）

- schema（root 侧生成，agent 可写数据仅作提示性输入）：

```
task_id / run_id / attempt_id / agent_id / pass_no /
session_exit_reason / checks_counter_observed /
server_accepted_submissions / final_poc_id / score_status /
primary_failure_class / secondary_failure_classes /
evidence_confidence / classification_version
```

- `primary_failure_class` 至少含 `unknown` 与 `unscored`；优先级：root
  可信源（服务器记录/protected ledger/会话退出原因）> 工作区观测
  （`.checks`/workspace ledger，仅诊断）。
- 类别互斥规则：`no_check_attempt`（观测）与 `session_dead` 可并存→
  primary 取会话死因、secondary 记 check 观测；`fix_not_clean` 仅在
  fix 侧可信评分完成后判定；最后 verdict TIMEOUT 不外推全程。
- `local_tools_used`（agent 写的 JSONL）降级为提示性遥测，不进决策链。
- 验证：确定性故障注入（缺失 .checks、账本空、网络中断、提交成功但
  客户端断线、评分未完成、pass2 触发、process residual）+ 历史案例回放
  （pilot5b / pool-smoke3 infra 事故），按优先级规则要求输出稳定。

## E6 会话结构：保留现状（pass2 为零提交救援）

- 【代码证实】`run_agent_task.sh`：`checks=0 ∧ ledger_rows=0` 触发
  forced-submit 第二段；stage3 全 False 不构成删除依据。
- 已识别缺口（E2/E5 观测重点）：pass1 有 check 尝试但服务器零接受时
  **不触发** pass2——正是 infra/网络类可疑区间。
- 三臂实验（后置，用 50 题数据）：A 现状 45+条件15；B 60 单段；
  C 45+条件15 且注入仅限当前题的失败假设摘要。指标：同题通过率、
  零提交恢复率、wall-time、成本、最终提交完整性。

## 补卡（r5 评审产出，引用其全文定义）

- **B1** 描述质量桥接（明确函数型/调用链型/模糊症状型三分，模糊型先
  查 harness 输入入口）；增益未知，先 20 题盲评描述质量。
- **B2** 当题 repo 测试资产只读清单与来源标记；**红线**：含现成目标
  触发 PoC 的样本排除，人工审计先行。
- **B3** 并发×key 池负载诊断（workers 1/3/5，指标 429/千次请求、有效
  完成率、$/成功题）；不改 key 池信任链。
- **B4** 50 题代表性分层与冻结（G1，发车必做，见下）。
- **B5** CRASH 后最终提交质量优化（条件性终止；事前固定质量判据；严禁
  多提交抽签/利用评分波动；最小化非单调指标，须同签名+位置证据支撑）。
- **B6** 上下文压力与调用有效性遥测（AGCLI_CTX 指纹、token usage、
  工具失败率、INDEX 输出规模；先验证参数真实生效）。

## 发车门槛（G1-G6，r5 评审版本）

- [x] **G4** 分类学与实验卡表述修正（本文件 v2 + taxonomy v2）
- [x] **G1** 冻结固定 commit、50 题 queue manifest 与评估口径——已生成
      `formal50.queue.json`（ARVO 45 + OSS-Fuzz 5，排除已验证 5 题，
      seed 20261106，queue sha256 `0ca260f4…`，manifest 全量留档）；
      casualties 不排除（样本代表性）
- [x] **G2** E5 root 侧可信归因落地——`scripts/failure_taxonomy.py`
      （classification_version=1.0，session_dead 优先于 unscored，
      unscored 永不进重试）+ 9 项注入单测 + 历史回放（pilot5b→5×success/
      high，pool-smoke3→no_check_attempt/medium，与手工归因一致）
- [x] **G3** E1 runbook 落地——部署手册 §4.0 三阶段跑法；taxonomy
      `--emit-retry-queue` 只导出 scored-False 候选（fix_not_clean/
      vul_no_crash/no_server_record），unscored/* 一律恢复/人工
- [ ] **G5** 故障注入回归：归因分类部分已完成（单测 + 历史回放）；
      连接级四类注入（refused/timeout/429/已受理断线）属 E2② 重设计的
      前置——E2② 不在发车清单，G5 余项随 E2② 一并后置
- [x] **G6** 评分器与清理路径零改动声明：本轮全部改动 = 新增只读归因
      脚本 + 队列冻结脚本 + 文档；评分/清理/信任链代码未动（failure_
      taxonomy 只读 rows 与 protected_ledger，不写任何评分输入）

正式报告指标：**首轮成功率 / 一次有效 retry 后成功率 / 按题源分层成功
率**，三个分开报告，不预承诺 96-98%。
