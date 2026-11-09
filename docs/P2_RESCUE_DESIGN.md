# pass2 四分类救援实验设计 v2（r11 对抗性审计后，2026-11-09）

状态：**v1 被 r11 裁决 REQUEST CHANGES；本 v2 按逐项裁决修订**。
已放行：分类器（files-only）与四类提示词开发、历史 7 题**非评分**诊断
回放。维持 HOLD：历史题正式重跑（E1 红线）、root `--exec` 进生产路径、
X5 自动提交、生产默认启用。v1 原文要点保留在 git 历史（commit 308e19c）。

## 1. 问题与病因（不变）

pass2 触发条件（checks==0 ∧ ledger 空）下 7/7 会话 0 check / 0 提交；
wrapper `be8f9b8a…` 第 585 行固定提示词命令 agent "from description.txt
… write poc_gen.py"——**从零开始、无视 pass1 存货**。7 个空转工作区
存货实测：有候选 2 题、仅生成器 2 题、无产物 3 题。

> r11 勘误采纳：65765 等任务**不得预标 A/B**——类别只能由运行时的
> 可信盘点（triage）结果决定；审计者的历史知识不得注入正式 agent
> 策略。下文表格中"before 臂曾证明可崩"仅作诊断参考。

## 2. 组件（按 r11 修订）

### 2.1 `scripts/pass2_triage.py`（已实现，root 侧）

- **files-only（默认）**：只读盘点——候选（`poc*`/`crash-*`/`*.poc`/
  深度≤2 `*.bin`，1B–2MB，排除 ws_manifest.json 可信工具集），命名
  分层排序（poc* > crash-* > fz/artifacts > 其他，层内 mtime 降序，
  非 mtime-only）；生成器仅标 `possible_generator`（内容含写文件
  模式，永不当"已证实"）。
- **`--exec`（仅阶段一诊断，HOLD 进生产）**：候选**冻结副本**
  （sha256 双读校验，防 TOCTOU）经加固容器执行——`--network none
  --cap-drop ALL --memory 1536m --pids-limit 64 --read-only --tmpfs
  /tmp:noexec`，vul out/libs 只读挂载，执行哨兵（RUNNER_V2）证明
  目标真实运行，`classify_exec` 判定；每候选 2 跑定稳定性，
  12s/跑，总预算 120s 硬顶。root 绝不执行工作区 Python；agent
  输入不拼 shell。
- **A1/A2 细分**：稳定 FATAL 且栈帧落在 target/项目源内 → A1；
  稳定 FATAL 但无目标帧 → A2（短核对后至多 1 次提交）；oracle
  未执行成功 → `ORACLE_ERROR`，候选保持未验证（B），绝不冒充
  A/记 CLEAN。
- **错误分类学（fail-loud）**：`TRIAGE_ERROR`（分类器异常→GENERIC
  回落提示词，明示"分类未完成"）/ `ORACLE_ERROR`（执行后端失败）/
  `ARTIFACT_ERROR`（符号链接、外部硬链接、超限、hash 竞态——逐文件
  记录并剔除）。
- **输出分权**：权威记录 `$LOGDIR/<task>.triage.json`（root 0600）；
  工作区副本 `pass2_triage.json`（0644，显式标 `advisory_copy`，
  agent 可改，不作 root 证据）；提示词写 root 0600 文件供 wrapper。
- 单测 11 项（含红线测试：任何类别提示词不得出现宿主裸跑指令）。

### 2.2 类别提示词（已实现，替换固定 PASS2_PROMPT）

公共：不许从零开始；**最初三个动作必须建立在 pass1 证据上**（r11
新诊断指标的锚）；每次 check.py 都是计分 final 提交、无事后择优。

| 类 | 行为约束 |
|---|---|
| A1 | 立即 `check.py <file>` 一次；CRASH→SOLUTION.md 停；非 CRASH 即停 |
| A2 | ≤2 turns 核对栈 vs description，可信则恰好 1 次提交 |
| B | **先形成具体假设**（输入结构+漏洞路径），再对最优支持候选提交；共 ≤2 次，每次须有不同假设；禁止盲筛 |
| C | agent 自身份跑 possible_generator，验证输出（存在、1B–2MB、合理输入）后 1 次提交；共 ≤2 |
| D | **有界最后构造救援**：≤8 turns 依 description+harness 构造一个具体输入，成则 1 次提交；无实质进展即写 UNSOLVED 停止；禁止填垃圾字节（r11 REJECT 了立即放弃） |

### 2.3 `scripts/check_double.py`（阶段一专用非评分替身）

与真实 check.py 同 CLI/同输出契约（`VERDICT: NO_TRIGGER`/`vul_exit=0`
/尾部输出/预算耗尽路径），**绝不连官方 grader**；每次调用把
(ts, argv, sha256, size, checks_after) 追加进 `.checkdouble.log`——
提交**意图**序列即阶段一测量载荷。罐头判答的偏置在报告中声明；
凡含此替身的工作区永远不得接官方评分。仅 `S4_CHECK_DOUBLE=1` 时由
实验 wrapper 安装。

### 2.4 wrapper 实验补丁（仅 `/data2/arms/p2/p2-agent` 副本，env 门控）

`S4_SEED_FROM`（存货注入；**不复制** .checks/submissions.jsonl/
run.env/repo；`.claude-config/` 仅复制 `projects/**/*.jsonl` 会话
转录，其余配置一律不带——r11 BLOCKER 2）、`S4_PASS2_ONLY=1`（跳
pass1，沿用原 pass2 分支含 launch 记录/promote/记行）、
`S4_PASS2_TRIAGE=1`（调 triage，按 root 提示词文件发会话）、
`S4_CHECK_DOUBLE=1`（换非评分替身）。补丁用锚点精确替换实现，
锚点 sha 不匹配即 REFUSE；`/opt` 生产树零改动。

## 3. 两阶段实验（r11 改造）

### 阶段一：7 题**非评分**诊断回放（已批准，待用户点头开跑）

同 7 题、同 B 臂存货注入、pass2-only、四类提示词、`--exec` 诊断
模式 + check 替身。**零官方提交**。测量：

- 分类正确性（triage 类别 vs 人工核对 7 ws 存货；--exec 是否真跑
  目标：哨兵+容器 rc 证据）；
- 行为改变：首 3 个有效动作中利用 pass1 证据的比例（r11 新指标，
  转录分析）、首次 check 意图的 turn、check 意图次数、D 类是否
  产出新候选（额外有效候选率）而非提前躺平；
- 生成器在原 uid/cgroup 下能否执行（C 类诊断）。

达标（分类门+行为门）：分类与可信观测一致、错误不冒充 A/B；有
候选任务的首个有效行动显著前移。**不得**引用任何官方分。

### 阶段二：全新冻结题完整 A/B（安全回归后另行评审批准）

新冻结题集（未参与开发），**自然 pass1→pass2**，不注入历史工作区。
A 臂=原固定提示词、B 臂=四类提示词；两臂同 oracle 配置、同模型/
turns/wall-time/隔离/预算/评分器，预注册交错执行顺序。主分析集 =
**进入实验的全部任务**（防 pass2 触发子集的选择偏差）；pass2 触发
任务的条件结果另报，并说明两臂触发集合是否一致。C 臂（四分类+本地
验证）待 --exec 隔离审计通过后加。

## 4. 验收阶梯（r11 六门，替代 v1 的 ≥4/7 过程门槛）

| 门 | 要求 |
|---|---|
| 安全门 | 0 越权、0 预算绕过、0 账本不一致、0 进程清理违规 |
| 分类门 | 阶段一回放分类与可信观测一致；错误/未知不得冒充 A/B |
| 行为门 | 有候选任务首个有效行动与提交意图明显改善；首 3 动作证据利用率为主指标 |
| 质量门 | 提交率不得靠无效 check 堆出来（每次提交须绑定假设/候选） |
| 效果门 | 阶段二新冻结配对官方成功率不退化且有可解释收益 |
| 成本门 | 提交数、turns、triage 时延、每题成本全量报告 |

v1 的"≥4/7 即生产"已废除；达标生产化需另行评审（默认 REJECT）。

## 5. 红线与边界（重申）

- E1：unscored 任务不重试——历史 7 题只做非评分诊断（BLOCKER 1）；
- 无宿主裸跑提示；`--exec` 隔离待审（BLOCKER/HOLD）；agent 输入
  不进 shell；root 不执行工作区脚本；
- X5 wrapper 自动提交维持 HOLD——A1"root 确认即自动提交"不在
  本实验范围；
- `/opt` 与 formal50 历史零接触；全部产物在 `/data2/arms/p2`。

## 6. 实现状态

- `scripts/pass2_triage.py` ✅（files-only + exec 加固后端 + A1/A2 +
  错误分类学 + advisory/权威分权）
- `scripts/check_double.py` ✅（意图记录 + 契约仿真 + 预算路径）
- `tests/test_pass2_triage.py` ✅ 11/11（全套件 65 tests OK / 7 skipped）
- wrapper 补丁 + p2-agent 装配脚本：待阶段一开跑前实现（锚点替换，
  REFUSE on mismatch）
- 阶段二协议：待阶段一过分类门+行为门后起草冻结队列
