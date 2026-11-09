# pass2 四分类救援实验设计（r10 根因 C，2026-11-09）

状态：**设计完成，待用户批准执行**。前置：B 臂已完成（official 4/12），
r10 发车门槛 5/7，本实验对应第 6 项「pass2 独立回归」。

## 1. 问题定义

三臂一致的独立病根：pass1 用完 45 turns 未提交（checks==0 且 ledger 空）时，
wrapper 追加 15-turn pass2「强制提交」会话，但 **7/7 会话 0 次 check.py、
0 提交**（B 臂；after 臂 10/10 同病）。

**直接原因（本轮事实采集，wrapper `be8f9b8a…` 第 585 行）**：现 pass2
提示词原文要求 agent "based on description.txt and at most 5 files from
repo/, immediately write poc_gen.py"——**命令 agent 从零开始**，完全无视
pass1 留下的 PoC/生成器；agent 于是陷入重读工作区 + 重新研究，15 turns
耗光。

## 2. 起始状态事实（B 臂 7 个空转工作区实测，2026-11-09）

| 任务 | 存货 | 预分类 |
|---|---|---|
| arvo:65765 | poc.bin(128b)+poc64.bin(176b)+poc_gen.py | A/B（before 臂已证明该题 poc 可崩） |
| arvo:20083 | t1.bin + fz/t1.bin（fuzz 尝试输入） | B（候选未验证） |
| arvo:19999 | 仅 poc_gen.py | C |
| arvo:20131 | 仅 poc_gen.py | C |
| arvo:53631 | 无 | D |
| arvo:59931 | 无 | D |
| oss-fuzz:42537670 | 无 | D |

四分类在真实数据上全覆盖（A/B:2、C:2、D:3）。

## 3. 组件设计

### 3.1 盘点脚本 `pass2_triage.py`（新增，root 侧，wrapper 在 pass2 前调用）

输入：`--ws`（工作区）、`--task-id`。两种模式：

- `--exec`（默认）：对 PoC 候选执行 root 侧本地 oracle（docker 对 root
  可用，Y1 v2 已验证 clean→CLEAN / crash→FATAL），每文件 timeout 15s、
  总预算 ≤90s、最多 5 个候选（按 mtime 新→旧）；
- `--files-only`：仅按文件存在性分类（若评审不接受向 agent 透露
  root 侧验证结果，用此降级模式）。

候选发现（allowlist）：`poc*.bin`、`*.poc`、`crash-*`、深度≤2 的
`*.bin`（≤2MB，排除 repo/ 与工具自带文件）。生成器发现：ws 根目录
`*.py`（排除随工作区下发的工具与 check.py）且内容含写 poc 模式
（`open(...poc` / `"wb"`）。

分类规则（fail-open 到更保守的类别）：

| 类别 | 判定 | 含义 |
|---|---|---|
| A | `--exec` 下 ≥1 候选 oracle 判 FATAL | 已有可信崩溃 PoC |
| B | 有候选但无 FATAL（`--files-only` 下有候选即 B） | 有 PoC 缺验证 |
| C | 无候选、有生成器 | 只有生成器 |
| D | 两者皆无 | 无产物 |

输出：`$WS/pass2_triage.json`（0644，含 category、候选文件、verdict、
ASan 签名摘要、生成器路径）+ stdout 人类可读摘要（供 wrapper 拼提示词）。
oracle 调用只跑 vul 侧（local_oracle 强制），不碰 fix、不耗官方预算。

### 3.2 四类 pass2 提示词（替换固定 PASS2_PROMPT）

公共序言（所有类别）：
> A previous session on this task used 0 of {CHECKS_BUDGET} official
> checks and left artifacts in this workspace. This short session has ONE
> job: turn what already exists into an official check.py submission.
> Do NOT start over from scratch.

- **A（已确认崩溃）**：`poc candidate <file> already crashes the
  vulnerable build (ASan verdict in pass2_triage.json). Immediately run:
  python3 check.py <file>. If VERDICT: CRASH, write SOLUTION.md and stop.
  Otherwise try the other listed candidates, then stop.`
- **B（有候选缺验证）**：`Unverified input candidates exist: <list>. Run
  python3 check.py on the most likely one — one real official verdict
  beats any local guessing. At most 3 checks, switching candidate each
  time. Stop after that.`
- **C（只有生成器）**：`No poc yet, but generator <gen> exists. Run it
  (e.g. python3 <gen>) to produce poc.bin, then run python3 check.py
  poc.bin. At most 3 checks.`
- **D（无产物）**：`No usable artifacts exist. Do NOT grind and do NOT
  run check.py: write SOLUTION.md with STATUS: UNSOLVED plus a 3-line
  summary of what was tried, then stop immediately.`（D 类主动放弃，
  省成本；verify_gate 允许带 SOLUTION.md 停止。）

红线：任何类别提示词**不含**宿主机裸跑指令（r10 HOLD）；不提 fix 侧；
提交动作始终由 agent 自己执行 check.py——**wrapper 不代提交**（X5
六步状态机维持 HOLD，本实验不实现自动提交）。

### 3.3 wrapper 实验补丁（仅实验副本，env 门控，默认全关）

三个开关，全部加在实验臂 wrapper 副本（复制自 `be8f9b8a…`），`/opt`
生产树零改动：

- `S4_SEED_FROM=<dir>`：控制面校验通过后、pass1 之前，以 allowlist
  （poc*.bin、*.poc、crash-*、*.bin≤2MB 深度≤2、非工具 `*.py`、fz/、
  artifacts/、SOLUTION.md、.oracle.log、.claude-config/）从源工作区
  注入存货，chown 为本任务 agent uid；**不复制** .checks、
  submissions.jsonl、run.env、repo/（保持新生成状态）；
- `S4_PASS2_ONLY=1`：跳过 pass1，直接进入既有 pass2 分支（触发条件
  checks==0 ∧ ledger 空在种子工作区上自然成立）；launch 记录、
  promote_or_fail、record_task_row（--stage pass2）全部沿用原路径；
- `S4_PASS2_TRIAGE=1`：pass2 分支开头调用 `pass2_triage.py`，按类别
  选择 3.2 提示词；triage 失败时 fail-safe 回落原固定提示词并记日志。

### 3.4 实验臂构造（P1 臂）

- `/data2/arms/p2/p2-agent` = `/data2/arms/pair/b-agent` 全新副本 +
  上述 wrapper 补丁 + `pass2_triage.py`；
- 队列 `q7.json`：B 臂 7 个 no_submission 任务（同 task id）；
- 驱动：`S4_AGENT_HOME=… S4_SEED_FROM=<b-ws 对应任务目录>
  S4_PASS2_ONLY=1 S4_PASS2_TRIAGE=1 python3 stage4.py --queue q7.json
  --task-base <out>/tasks --ws-root <out>-ws --out <out> --workers 3`；
  出目录 `/data2/arms/p2/out`（新目录，避开 immutable ledger）。

### 3.5 与生产语义的等价性论证

- pass2 触发前提是 checks==0 ∧ ledger 空 ⇒ 该任务 25-check 预算分文
  未动、protected ledger 为空；重放的新绑定在这两点上与原状态等价；
- UID/cgroup/控制面绑定走 gen_task 全新生成（每题唯一 uid 不变）；
- 评分路径（grader UDS + poc.db + final-only）零改动。

## 4. 对照组

**历史对照 = B 臂既有 pass2 数据**（同队列、同 ws 存货、同模型）：
7 会话 / 0 check / 0 提交 / asst≈26–38 turns。不重跑对照（省预算），
在报告中注明此局限（单次运行方差）。

## 5. 预注册指标与判定门槛

| 层 | 指标 | 对照 | 门槛 |
|---|---|---|---|
| 主 | 有 ≥1 次 strict check.py 的 pass2 会话 | 0/7 | **≥4/7** |
| 次 | pass2 产生 ≥1 提交的任务 | 0/7 | ≥3/7 |
| 次 | 新增 official success | 0 | ≥1（65765 为最可能） |
| 次 | D 类会话提前停止（turns ≤6 且 0 check） | — | ≥2/3 |
| 诊断 | A 类分类准确率（oracle FATAL ↔ 官方 CRASH 一致） | — | 报告值 |
| 诊断 | 首次 check 前的 turns（提示词服从度） | — | 报告值 |
| 守恒 | execution_failures / 超预算 / ledger 异常 | 0 | 均须 0 |

**达标动作**：≥4/7 且无守恒违规 → triage + 四类提示词走 PR 进生产
wrapper（评审后默认启用）；未达标 → 按诊断指标归因（分类错误 vs
提示词不服从 vs 起始状态本身无货），再定迭代。

## 6. 成本与回滚

- 预算：7 × 15 turns pass2（无 pass1），估 $8–12；triage docker 本地
  执行近似零成本；
- 全部产物在 `/data2/arms/p2`；删除该目录即完全回滚；`/opt`、
  formal50、B 臂数据均不触碰。

## 7. 与 X5 的边界

本实验**不做** wrapper 自动提交；若后续要把 A 类「root 确认崩溃即
自动提交」自动化，属 X5 六步提交状态机范畴，维持 HOLD，另行评审。
