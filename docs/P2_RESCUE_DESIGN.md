# pass2 四分类救援实验设计 v3（r12 对抗性审计后，2026-11-09）

状态：**r12 裁决 CONDITIONAL APPROVE——阶段 1A 修复后可执行；1B 与
生产维持 HOLD**。v3 落地三项阻断修复、五个逻辑修订与三臂对照。
历史版本：v1（308e19c，r11 REQUEST CHANGES）、v2（96c3600）。

## 1. 病因（不变）

wrapper `be8f9b8a…` 第 585 行 pass2 固定提示词命令 agent 从零写
poc_gen.py，无视 pass1 存货；B 臂 7/7 会话 0 check / 0 提交。
7 空转工作区存货：有候选 2、仅生成器 2、无产物 3（类别由运行时
triage 决定，不做预标注）。

## 2. 组件（v3 状态）

### 2.1 `scripts/pass2_triage.py`（已实现 + r12 修订）

- **files-only（阶段 1A 唯一模式）**：只读盘点，命名分层排序，
  生成器仅 `possible_generator`，可信工具集来自 ws_manifest.json。
- **A1 不可达（r12 #1）**：项目源码栈帧 ≠ 目标题漏洞。未接入 Y6
  目标机制归因前，任何动态稳定 FATAL 一律标
  `crash_label=CRASH_TARGET_UNKNOWN` → A2（短核对，至多 1 次提交），
  **绝不包装成已确认 A1**；A1 分支保留但阶段一不可达。
- **冻结绑定（r12 #4）**：scan 时读源路径一次记 `scan_sha256` →
  复制到冻结目录 → 冻结副本读一次记权威 `sha256` → 两者必须相等；
  此后原始路径不再打开，执行只挂冻结副本；候选 hash 与该次 oracle
  输出在 exec_results 内绑定。
- **D 序言独立（r12 #2）**：D 类不要求"首三动作基于已有证据"（它
  没有证据），期望首动作=直接进入有界构造；A/B/C 才有证据复用
  要求。验收按类别定义首动作。
- 错误分类学与 advisory/权威分权同 v2（TRIAGE_ERROR/ORACLE_ERROR/
  ARTIFACT_ERROR；LOGDIR 0600 权威 vs ws 0644 advisory）。
- **`--exec`（BLOCKER 3 维持 HOLD）**：加固参数保留为原型代码，
  阶段 1A **禁用**；待独立容器安全测试（非 root 用户执行、seccomp/
  no-new-privileges/userns、只读子路径挂载审计、CPU/磁盘/输出限额、
  超时清理、哨兵=目标启动而非入口脚本启动的证明）通过后另批。

### 2.2 类别提示词（已实现）

| 类 | 语义 | 关键约束 |
|---|---|---|
| A1 | 稳定崩溃 + 目标机制一致（**阶段一不可达**） | 一次提交即止（实验变量，非默认最优） |
| A2 | 稳定崩溃但 CRASH_TARGET_UNKNOWN | ≤2 turns 核对签名 vs description，恰好 1 次提交 |
| B | 有候选无可信崩溃证据 | 假设先行，≤2 次提交、每次异假设，禁盲筛 |
| C | 仅 possible_generator | agent 自身份运行（原 uid/cgroup，会话 cgroup 限资源）；不信任声明输出路径；产物须落在工作区内且 1B–2MB 才可用；root 绝不执行生成器；共 ≤2 次提交 |
| D | 无产物 | 有界构造 ≤8 turns；成则 1 次提交；无实质进展→UNSOLVED 停；禁垃圾字节 |

公共（A/B/C）：不许从零开始、首三动作建立在 pass1 证据上；全部：
每次 check.py 都是计分 final 提交。**指标拆分（r12）**：
`first_check_intent`（核心行为测量）与 `post_feedback_behavior`
（替身罐头反馈后的行为，仅标记为模拟，不推断真实成功率）。

### 2.3 `scripts/check_double.py`（非评分替身，已实现）

同 CLI/输出契约，零官方连接；意图记录（ts/argv/sha256/size/
checks_after/**simulated:true**）写 `.checkdouble.log`。**执行层
隔离（BLOCKER 1，阶段 1A 启动前逐项验证）**：

1. 实验环境不向 agent/替身暴露真实 grader 地址与凭据：agent 的
   run.env 中 CG_SERVER 指向死地址（127.0.0.1:9），CYBERGYM_API_KEY
   不注入；
2. 可信启动侧封网：wrapper 在 pass2 会话前对 agent uid 加
   `iptables -m owner --uid-owner <uid> -d <grader_ip> --dport <port>
   -j REJECT`，会话后移除（不依赖提示词）；
3. 真实 check.py 无可达副本：种子 allowlist 不含 check.py，生成
   ws 的 check.py 由 S4_CHECK_DOUBLE=1 原地替换并记录 hash；
4. 独立输出与账本：p2 出目录自带 protected_ledger/rows（与正式
   跑批物理分目录）；替身不写 submissions.jsonl，promote 为空操作；
5. **开跑前负面测试**：以 agent uid 直连 grader /submit-vul、经
   plugins runner 提交、手工伪造 submissions.jsonl——三者必须全部
   失败且 grader poc.db 无新行，方允许放行 1A。

### 2.4 wrapper 实验补丁（仅 `/data2/arms/p2/p2-agent`，env 门控）

`S4_SEED_FROM`（**默认不复制任何 .claude-config 内容**——r12
BLOCKER 2；存货 allowlist：poc 候选/生成器/fz/artifacts/SOLUTION.md/
.oracle.log。历史转录如需研究，另设 `transcript_replay=true` 独立
条件，不与主实验混因）、`S4_PASS2_ONLY=1`、`S4_PASS2_TRIAGE=1`
（阶段 1A 强制 files-only）、`S4_CHECK_DOUBLE=1`（含 §2.3 隔离
束）、`S4_P2_ARM=A|B|C`（提示词来源三臂选择）。锚点精确替换，
sha 不符即 REFUSE；/opt 零改动。

## 3. 两阶段 + 三臂（r12 改造）

### 阶段 1A：files-only 三臂非评分诊断（修复后可执行）

7 题 × 3 臂（同存货注入、同替身、同隔离）：

| 臂 | pass2 提示词 | 目的 |
|---|---|---|
| A | 原固定提示词（现状） | 基线 |
| B | 原提示词 + 一句"优先检查工作区已有 PoC/生成器，勿从零开始" | 检验一句话是否已够 |
| C | 四类提示词 + files-only triage | 检验分类机制增量 |

r12 之问："四分类的工程复杂度是否值得"——1A 直接回答。零官方
提交。**验收门（工程筛选，非统计证明）**：

| 指标 | 要求 |
|---|---|
| 官方提交记录 | 严格 0（负面测试 + poc.db 差集双验） |
| 分类器可识别文件 | 与人工核对一致 |
| 首动作正确率 | 有产物 4 题中 ≥3 题首动作符合类别期望 |
| 首次 check 意图 | 分臂报告，不预设固定成功率 |
| D 类有界构造 | 报告是否产生有效候选（额外有效候选率） |
| 进程/权限违规 | 严格 0 |
| 假提交账本污染 | 严格 0 |

样本外推声明：7 题中仅 4 题有产物，结论仅用于机制筛选。

### 阶段 1B：动态 triage 安全与准确性（HOLD）

独立测试程序验证 root 容器：正常退出、sanitizer 崩溃、权限错误、
超时与清理路径、非 root 执行、哨兵语义。通过后仅对 7 题冻结候选
做 vul 侧本地验证，仍不连官方评分。

### 阶段二：全新冻结题完整 A/B（单独审批）

不变（v2 §3）：自然 pass1→pass2，主分析集=全部进入任务；A1
"一次即止"与"保留有限后续探索"作为预注册对比变量（r12 #5）。

## 4. 验收阶梯

六门（安全/分类/行为/质量/效果/成本）不变，行为门以**类别期望
首动作**与 `first_check_intent` 为主指标；生产启用维持
REJECT-until-另行评审。

## 5. 红线（r12 复核后状态）

unscored 不正式重评分（1A 全非评分+负面测试）✅；final-only/
25-check 正式链路未动 ✅；不读 fix 侧 ✅；跨题答案不注入（历史
知识不入正式 agent 策略）✅；UID/cgroup（C 类生成器在原身份跑，
root 容器 1B 前不跑）✅；protected ledger 物理分目录 ✅；不绕过
评分反复抽签（A/B 策略变量预注册）✅。

## 6. 阶段 1A 结果（2026-11-09 已执行，详见 `results/p2_phase1a/summary.md`）

- 隔离/纪律门全过：官方提交 0（poc.db 37→37）、假账本污染 0、
  违规 0（iptables 零残留）、21 行 rows 全部打模拟标记；
- 分类门过：files-only triage 7/7 与人工存货一致（0 artifact_issues）；
- 首动作门过：C 臂 4/4（首动作即 triage/存货）、B 臂 4/4（动作 2
  命中）、A 臂 0/4（复现"从零研究"根因）；
- **转化未达标（核心负结果）**：首次 check 意图 A 1/7 = B 1/7 >
  C 0/7——四分类"假设先行+≤2 次"纪律在 15-turn 预算内抑制转化；
  C 类生成器被读未执行；B 臂 65765 执行了生成器并走到 check；A/B
  臂 post-feedback 试图走真实提交路径被隔离层挡下（`submit_error`
  即其痕迹，无外部记录）——替身反馈只作模拟解读；
- D 类有界构造 0/9 产出新候选；
- 成本 $11.49/21 会话；实验范式（死地址+iptables+替身+打标）可复用；
- 阶段二预注册建议（待批）：C 类命令式首步（先执行生成器禁先读
  源码）、B 类放宽首次提交时点、D 类两策略对比（有界构造 vs 立即
  UNSOLVED 省成本）、A1 动态分层待 1B exec 解封；1B/二/生产维持 HOLD。

## 7. 实现状态

- `pass2_triage.py` v3 ✅（A1 不可达+CRASH_TARGET_UNKNOWN、冻结
  绑定、D 独立序言、`--no-advisory`；全套件 OK）
- `check_double.py` ✅（simulated 标记）
- p2-agent 装配 ✅（stock `be8f9b8a` 双锚点补丁：P1A 准备块折入
  pass1 前 + PASS2_PROMPT 改读 root 提示词文件；全 env 门控、
  逐项 fail-closed；`P1A_MANIFEST.json` 在树根）
- r13 安全门 5/5 ✅（N1 死地址/N2 UDS/N3 runner URLError/N4
  iptables REJECT/N5 poc.db 基线）；装配锚点补丁（stock `be8f9b8a`
  校验 + `# ---- pass 1` 前折入 P1A 准备块 + PASS2_PROMPT 替换），
  `P1A_MANIFEST.json` 存档
- **阶段 1A 已执行完毕（2026-11-09T02:16→03:05Z，21 会话，$11.49）**：
  验收表 7 项全过（官方提交 0/poc.db 一致、分类 7/7、C 臂首动作
  4/4、违规 0、污染 0）；行为读数——**B 臂一句话拿到大部分锚定
  收益（动作 2 触存货），C 臂锚定最好（首动作即 triage）但 0/7
  check 意图**（假设先行纪律在 15-turn 内抑制转化；C 类生成器
  读了没跑；D 类 0/9 产出候选）。A/B 臂 65765 实证"重生成→check"
  路径可行且不服罐头 NO_TRIGGER 转寻真实提交被隔离挡下。详见
  `results/p2_phase1a/summary.md`；阶段二预注册变量（C 类命令式
  首步、B 类首次提交时限、D 类双策略、1B exec 解封后的 A1 动态
  分层）已列入待批清单。
