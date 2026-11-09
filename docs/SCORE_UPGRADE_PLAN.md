# formal50 提分统一计划 v6（2026-11-08，r9 开工前终审修订）

> **v6 依据 r9 终审（CONDITIONAL APPROVE，可开工但先修 5 项 P0）修订**：
> P0-1 工具缺失改 fail-closed（必需/可选/安全 Hook 三类依赖验证）；
> P0-2 工具来源与可信部署链（版本/许可证/依赖锁/可审计来源，禁不明
> 来源恢复）；P0-3 双基线重建（环境回归 5-10 题 + 冻结对配 12-20 题，
> formal50 历史 30% 保留不覆盖）；P0-4 Y6 归因不得成为隐形评分器
> （只管核查/优先级/预算分配，预算耗尽允许按预注册策略提交）；
> P0-5 Y8-3 拆 E3a-d 消融。另增：门面接口不变 ≠ 语义等价（版本化
> 结果 schema + 不可比标 unknown）、工具使用转化遥测入 Y1 验收、
> **30% 基线重释为"疑似缺工具部署态成绩"，新基线前冻结一切增益推算**、
> 新 50 题正式提分跑批暂不批准。
> v5 及以前内容保留（v5 = Y8 工具链主线；v4 = Y6；v3 = r8 八项）。

## 0. 基线、目标与红线

- 基线：首轮 14/50=28%，E1 后 **15/50=30%**（零提交 30 + 已评分失败 5）。
- **95% 算术（r8 修正版）**：48/50 需从剩余 35 题回收 ≥33——已评分失败
  ≥3/5 **且** 零提交池 ≥28/30 **且** 两组回收之和 ≥33（3/5+30/30、
  4/5+29/30、5/5+28/30 三条路径均合法；v2"≥4 且 28-30"错误排除了 3/5
  路径）。失败池总回收率须达 **94.3%**——降低零提交率只是必要条件。
- 双能力框架不变：(1) 执行可靠性（X1/X2″/X3′/X4/X5/Y1 防求解进展在
  流程中损耗）；(2) 漏洞求解能力（**Y2/X6/Y3/Y5/Y6/Y8 为高分突破主线**，
  其中 Y6 补上"崩对了没有"的归因环节，与 Y2/Y3 构成
  描述→假设→证据→构造→验证→归因→提交闭环；**Y8 为该闭环提供成熟
  引擎后端**（fuzz/覆盖率/AST 索引/格式生成），改变的是证据生产效率
  而非证据语义；Y4 判定所需资源——r8 最终结论 + v4/v5 顾问建议）。
- 红线不变（`FORMAL50_STATUS_AND_PLAN.md` §1 + `SCORE_UPGRADE_PROMPT.md`
  §4）；check 使用率/覆盖率/时长是中间变量不是解出率。
- **实验样本三层纪律（r8）**：
  1. **筛选层**（12 题）：安全、可执行性、早期机制；超参调整只在此层；
  2. **复现层**（另一批冻结题）：机制改善复现确认，不用筛选层调过的题；
  3. **盲测层**（独立冻结样本，≥50 题同 formal50 分层）：唯一可用于
     最终成功率结论的口径；前两层指标**永不**进入正式 95% 宣称。
- **队列比例更正（r8）**：12 题队列 ARVO 10 + OSS-Fuzz 2 = 83.3%/16.7%，
  与 formal50 的 90%/10% **不同**——声明为**有意过采样 OSS-Fuzz 以做
  诊断**，结果按题源分层报告，总体成功率不得直接解释为正式队列成功率。

## 1. r8 复审逐项响应表

| r8 裁决 | v3 处置 |
|---|---|
| 95% 数学表述错误（MAJOR） | §0 修正：≥3/5 ∧ ≥28/30 ∧ Σ≥33，94.3% 失败池回收率 |
| X2′ 第 5 分支质量门（MAJOR） | §3.2 X2″ 五状态机，删除"非崩溃服务器反馈即可提交"；LOCAL_VALID_NO_CRASH 下官方提交仅限预注册探索性动作（计入预算与 final-only 历史，不跑后挑优） |
| Y4 缺 wall-time/pass2/资源联动（MAJOR） | §3.9 拆两类实验 + pass2 保持原触发 + 完整资源矩阵 |
| X5 缺状态机（MAJOR，上线阻断） | §3.5 六步状态机（终止→冻结→隔离重启→预算核对→提交→推广清理），正式自动提交 HOLD |
| X1 规划成本 | §3.1 加两条硬约束 + 首次动作四分位预注册 |
| X3′ 进展信号/worker 缺口 | §3.3 三档进展分类 + worker 补 5 项测试 |
| X4 字段可信度 | §3.4 字段来源表 + handoff 不可信笔记格式 |
| X6/Y3 假 oracle | §3.6 observed/inferred/unknown 三态 + 证据来源/版本标识 |
| Y2 资产泄漏 | §3.8 逐题白名单审计，不确定即不用 |
| 实验有效性 5 项 | §0 三层样本纪律、§3.9 组合效应限制、§4 schema 溯源字段、§5 停止条件 |
| Y5 难题升级 | §3.11 新增 |

### 1.1 r9 终审响应表（开工前 P0×5）

| r9 项 | v6 处置 |
|---|---|
| P0-1 工具缺失静默跳过 | PR-A 规格升级：`make_agent_ws.py` 复制逻辑改三类依赖验证——**必需工具**（repo_map/reach_graph/probe_path/try_local/stab_run/minsep）缺失即工作区生成失败；**可选工具**（directed_fuzz/boundary_synth/mutate_seed/hf_mutator）允许缺失但 manifest 记 `unavailable` 且 CLAUDE_MD 提示词同步降级不引用；**安全 Hook**（verify_gate）缺失或注册失败 **fail-closed**（现 L293-296 仍写入指向缺失文件的配置——缺陷实证）。生成后校验：存在性/权限/依赖导入/基本执行/版本哈希 |
| P0-2 来源可信链 | PR-A 增加：来源/许可证/版本/依赖锁/构建产物来源登记；agent 会话禁网络下载更新；工具在既有隔离边界内运行无 fix/他题/root 私有访问；**历史工具不得从不明来源复制同名脚本"恢复"**——可审计来源（部署主机 git 历史/备份）+ 入库前逐件源码审计 |
| P0-3 基线重建 | PR-A 后双验证：①环境回归 5-10 道已知诊断题（安装/调用/安全不变量）；②新冻结 12-20 题配对（当前部署态 vs 工具恢复态）。formal50 历史 30% **保留不覆盖**，重释为"疑似缺工具部署态成绩"；新基线前**冻结一切基于 30% 的增益推算**（含本计划 §5 情景数） |
| P0-4 Y6 防隐形评分器 | §3.12 增补：归因四档只决定"是否继续核查/优先验证哪个假设/剩余预算分配"；**可信本地崩溃 + 预算接近耗尽 + 归因仍不确定 → 允许按事前预注册策略经原 check.py 真实提交**（不为归因置信度制造零提交）；验收含目标相关崩溃率/误拒率/零提交率/官方成功率，**不采信模型自报 confidence** |
| P0-5 Y8-3 拆消融 | §3.13 Y8-3 拆 E3a（Clang AST：定位/调用链质量）/ E3b（LLVM coverage：目标可达性）/ E3c（Y6 归因：错点识别）/ E3d（组合：交互效应）；小样本验机制，终判在独立冻结队列 |

## 2. 批次顺序（v2 骨架 + r8 发车分级）

```
批次0 [APPROVE]  Y1 工具链自检、可信遥测（§4 schema）、冻结三层队列、评分链路回归、
                 Y6-pre 历史样本离线研究（§3.12 前置）
批次1 [有条件批准] X1 + X2″ + X3′(a) ‖ Y4（补齐资源矩阵后批准）
批次2 [有条件批准] X4-L1 + X5 原型（正式提交 HOLD）‖ Y2
批次3 [有条件批准] X6 + Y3 双通道反馈闭环 ‖ Y6 归因 A/B（待复审）‖ Y8-3
批次4 [有条件批准] X7 / X8 / X10 / Y5 按剩余失败类别启用 ‖ Y8-1（随
      X3′(b) 放行）/ Y8-2（随 Y2）
批次5 [盲测层]     独立冻结样本最终验收（95% 唯一宣称口径）
HOLD：X3′(b) fuzz worker（故障注入通过前）、X5 正式自动提交、AFL++ 正式
      集成（隔离评估允许）、新 50 题正式提分跑批（r9 暂不批准）
```

**基线校正（2026-11-08，P0-3 结果）**：12 题配对显示"补齐工具"没有提高
官方通过率（2/12 → 1/12），且 9/12 任务两臂都撞 62 turns 上限。转录层
复核（`toolcall_report.py`）进一步定位到**确定性根因 P0-6：生产隔离下
本地 oracle 全部返回假 CLEAN**（agent 每题唯一 uid 无 docker socket 权限，
`local_oracle.classify` 把 docker CLI rc=1 误判为"无触发"；after 臂
`.oracle.log` 42/42 条 CLEAN rc=1，无一次真实容器执行）。据此第一批顺序：
（0）**P0-6 oracle 修复**（ORACLE_ERROR 分类 + 裸跑降级路径，见
`results/pair_baseline/toolcall_analysis.md` §6）；
（1）X2′/X2″ 五态提交机与 X5 六步提交状态机（pass2 强制提交 10/10 失效，
提示词无法对抗共享工作区再锚定，必须做进 wrapper）；
（2）Y2 格式感知输入生成（压缩 time-to-first-input）；
（3）X1 strategy.md 与 X4-L1 pass2 接力（在相同 62-turn 饱和下验证）。


### 2.1 第一批开工清单（r9 授权立即启动，按序执行）

1. [x] 确认工具链缺失范围：两棵部署树（/opt/cybergym-agent-r33 @ bb163b4
   与默认 /opt/cybergym-agent）`scripts/` 均无全部工具；verify_gate.py
   全主机不存在；恢复来源=stage3 1,507 生产工作区多数决 + naco 开发树
   local_oracle.py（溯源见 `docs/TOOLCHAIN_PROVENANCE.md`）；
2. [x] 实现 fail-closed 工作区预检（P0-1 三类依赖验证 + 生成后校验；
   单测 9 项 + 主机注入测试：删 minsep.py → REFUSE 实证）；
3. [x] 补齐工具依赖与安全验证（P0-2：逐件源码审计、SHA256SUMS.tools、
   3 个 /home/naco 硬编码补丁、local_oracle 入 REQUIRED、verify_gate
   新写+5 单测；共 208 测试绿）；
4. [x] 运行 Y1 工具自检：10 题（9 ARVO 含 C 桶 42227 + 1 OSS-Fuzz）×
   11 工具全 started_ok，生产等价 uid 60101（setpriv + 清 env + 私有
   HOME/TMPDIR），try_local/stab_run/minsep/directed_fuzz 真实 docker
   oracle 执行——报告 `results/y1_selfcheck_report.json`，verdict=PASS；
   （工具使用**转化**遥测待真实 agent 会话，随 P0-3 基线跑采集）
   **勘误（2026-11-08 晚）**：Y1 对 docker 依赖工具（try_local/stab_run/
   minsep/directed_fuzz/probe_path）的 PASS 为**假阳性**——固定 uid 60101
   同样无 docker socket 权限，docker CLI 小写 "permission denied" 未被
   Y1 失败模式匹配，`container rc=1` 实为 docker CLI 失败码；静态工具
   （repo_map/reach_graph/mutate_seed/boundary_synth/hf_mutator 编译检查）
   的 PASS 仍有效。`results/y1_envreg_prod.json` 同口径受影响；
5. [x] 建立可复核新基线（P0-3 双验证：5-10 题环境回归 + 12-20 题冻结
   配对，记录工具可用性/成功率/成本）；
   - 生产树滚动已执行（2026-11-08T06:41Z）：两棵 /opt 树 `scripts/`
     安装 14 件（13 工具 + 新 make_agent_ws.py），`sha256sum -c
     SHA256SUMS.tools` 两树全 OK；备份
     `/data2/backup/pre-pr-a-20261108T064133/`（旧 make_agent_ws
     `aa0dd5fa…` 原样保留）；
   - 5-10 题环境回归（生产树 -r33、8 题、uid 60101）：verdict=PASS，
     11/11 工具 8/8 started_ok——`results/y1_envreg_prod.json`
     （受上述勘误影响，docker 依赖项结论作废）；
   - 12 题冻结配对已完成（`/data2/arms/pair`，两臂仅差 scripts，base
     复制自 /opt/cybergym-agent-r33；queue sha256 `572c7163…`）：
     **before 2/12（16.7%，$30.57）vs after 1/12（8.3%，$25.78）**。
     转录层复核（`cybergym-agent/scripts/toolcall_report.py`）定位根因：
     after 臂工具被大量调用但 **42/42 次 oracle 调用返回假 CLEAN**
     （docker 权限拒绝被 `classify()` 误判），agent 被锁死在变异循环，
     strict check.py 4→2；before 臂靠裸跑预构建二进制 + 早提交反而多活；
     pass2 强制提交 10/10 会话 0 次 check.py（两臂共病，X5 对象）——
     详见 `results/pair_baseline/toolcall_analysis.md` 与勘误后的
     `summary.md`。历史 formal50 30% 未覆盖；
   - **P0-6 修复（r10 裁决后已实施，2026-11-08）**：
     (1) [APPROVE→已实施] `local_oracle.classify_exec()` 状态优先分类：
     容器内哨兵 `__ORACLE_RUNNER_V2__` / `__ORACLE_TARGET_EXIT_V2__<rc>`
     证明执行；无哨兵=ORACLE_ERROR（kind 仅诊断），126/127=
     TARGET_LAUNCH_ERROR，124/137/libFuzzer-timeout=TIMEOUT，0+无特征=
     CLEAN，其余=UNKNOWN——**未知绝不落 CLEAN**；docker CLI rc 与目标
     rc 分离；run_side 返回 schema-2 结构化结果并写入 .oracle.log；
     (2) [HOLD→按裁决落地] 工具脚本遇错误 verdict 打印
     VERIFICATION-UNAVAILABLE（"这不是无触发信号，勿再变异"），**不含**
     宿主机裸跑指令；try_local/stab_run/minsep/probe_path/directed_fuzz
     全部接入（exit 7）；
     (3) [CONDITIONAL→未启用] 受限原生执行后端（Backend B）仅记录设计，
     待隔离审计（文件/网络/syscall/资源/进程生命周期）后另行评审；
     (4) [APPROVE→已实施] Y1 v2：端到端真执行场景表——agent uid 下
     socket 拒绝必须 ORACLE_ERROR（不再假 CLEAN）；root 侧
     daemon_unreachable/image_unavailable/target_missing→
     ORACLE_ERROR/ORACLE_ERROR/TARGET_LAUNCH_ERROR，clean_run→CLEAN
     （哨兵证明真实执行+退出 0），real_crash→FATAL（配对基线 arvo_1348
     官方崩溃 poc）；timeout/大小写变体由 17 项分类单测覆盖；
     主机实测 verdict=PASS（3 题 + 6 场景，`results/y1v2_report.json`）；
     (5) [APPROVE→已实施] toolcall_report.py 两层遥测：
     target_executions_confirmed / oracle_errors / oracle_clean_confirmed /
     oracle_crashes_confirmed / valid_local_verification_rate /
     turns_after_first_oracle_error / first_check_turn；schema-1 旧日志的
     CLEAN-rc=1 降级计入 oracle_errors；
   - r10 实验设计记录（待批后执行）：三臂 A/B/C（A=before 原工作流、
     B=工具齐全+oracle 诚实报错、C=工具齐全+oracle 真可执行——需先过
     Backend B/A 安全验收）；pass2 救援实验独立于 oracle 变量；
     C 臂提交数恢复 ≠ 成功率提高，须同查真实崩溃率/官方成功率/成本；
     X5 自动正式提交维持 HOLD；
   - **B 臂已完成（2026-11-08T15:09Z，用户批准后执行）**：与 after 臂
     唯一差异 = 6 个 P0-6 补丁文件（diff -rq 证实），同队列/模型/wrapper。
     **official 4/12（33.3%）vs before 2/12 vs after 1/12**；成功题
     11033/1348/13725/3497 全部走"源码分析+check.py"路径。机制证实：
     9 次 oracle 调用全部诚实 ORACLE_ERROR（0 假 CLEAN），首个错误信号
     后浪费 turns 702→270，strict check 任务 2→5，pass2 仍 7 会话 0
     check（根因 C 独立未解）。成本 $32.42/12 题。运行注记：驱动器收尾
     瞬间调 backfill 被防抢保护 REFUSE（rc=2），手动重跑后 verify
     violations=0——驱动器需在 backfill 前等待 worker 全退（待修）。
     详见 `results/pair_baseline/summary.md` B 臂节；
   - r10 发车门槛进度（7 项）：oracle 状态分类 ✅、真实目标可运行 ✅
     （root 侧）、Y1 不再假阳性 ✅、agent 正确使用反馈 ✅（B 臂）、
     评分防守不变量 ✅（verify 0 违规）；no-Docker 隔离 ⏸（C 臂前置）、
     pass2 独立回归 ⏳——**5/7**。pass2 线（r11/r12/r13 三轮审计）：
     设计 v3 + 阶段 1A 已执行（7 题 × 3 臂非评分诊断，$11.49）——
     验收 7/7（官方提交 0、分类 7/7、C 臂首动作 4/4、污染 0）；
     读数：一句话提示拿到大部分锚定收益，四分类锚定最好但 15-turn
     内 0 check 转化（C 类生成器读了没跑、D 类 0/9 产出）；
     `results/p2_phase1a/summary.md`；阶段 1B（exec 安全测试）与
     阶段二（新冻结题 A/B + 预注册变量）待批；
   - **r14 外审（用户委托独立评审 AI）已处置（2026-11-09）**：A1
     哨兵伪造（stdout 专属解析+docker-rc 信任门+末位全行匹配）、
     A2 退出语义细化（137 无超时证据=UNKNOWN、假 FATAL 需非零退出）、
     B1/B2 生命周期（输入拒绝、bytes、容器命名与外层清理）、
     C1-C4 前端 fail-closed 补漏（NO_INPUT/中途失败/gdb 异常/
     fuzz 启动失败与 -dict 真 bug）、D1 遥测三层口径、D2 追加写、
     E1 单位勘误（702/1296 vs 270/1127，McNemar p=0.25 方向性）、
     F1 realpath 守卫、legacy shim 删除；单测 25+11 全绿、全套件
     OK；两棵 /opt 树滚动部署 15/15 OK（备份 pre-r14-…034620Z）——
     详见 `docs/TOOLCHAIN_PROVENANCE.md` §5b 与 summary.md 勘误；
   - 运行中发现（与本次改动无关的既有部署事实）：默认
     /opt/cybergym-agent 的 run_agent_task.sh 是**无 keypool 的旧版**
     （`b91fa5fb…`），生产跑批必须用 /opt/cybergym-agent-r33
     （`be8f9b8a…`，含 keypool），否则模型凭据缺失 rc=78；
6. [ ] 启动 LLVM coverage / Clang AST 原型（独立分支，不改正式评分与
   提交路径；结果 schema 版本化，Clang AST 调用图/崩溃判定与自研输出
   **不保证语义等价，不可比处标 unknown**）。

> 生产树滚动（把恢复后的工具链复制进 /opt 两棵树）**待 PR 合并后按
> SHA256SUMS.tools 校验执行**；Y1 已在 /data2/runs/y1-diag staging 验证
> 同一批文件可用。

## 3. 实验卡 v3

### 3.1 X1 策略先行开局卡（APPROVE + 两条硬约束）

- 改动点同 v2（CLAUDE_MD 第 0 步 strategy.md ≤500 token + B1 三分桥接）。
- **r8 硬约束**：① strategy.md **不是**进入 INDEX/工具执行的前置门槛——
  可先写简短规划随即动手，事后完善；② 规划产物必须含**具体可执行的
  下一步**（运行哪个当题工具、观察哪个入口条件），只写抽象漏洞类别
  不合格。
- **首次动作四分位（预注册定义）**：`first_tool_call` / 
  `first_successful_tool_call` / `first_input_constructed` / 
  `first_local_run` 分开记录，防止"写了 strategy.md 就算行动完成"的
  指标污染。

### 3.2 X2″ 候选驱动提交状态机（REQUEST CHANGES 已改）

v2 §7 曾提议第 5 分支质量判据"过 stab 门 **或** 已有 ≥1 次非崩溃服务器
反馈的变体"——r8 **否决该"或"**：`NO_TRIGGER/TIMEOUT` 只说明先前提交
未达目标，不能证明新变体合格。v3 改为五状态机：

| 候选状态 | 动作 |
|---|---|
| `LOCAL_STABLE_TARGET_CRASH`（过 stab 门） | 可正式提交 |
| `LOCAL_CRASH_UNCERTAIN_TARGET` | 优先核对崩溃签名与位置（按 Y6 §3.12 第 4 层归因执行）；提交须保存质量证据 |
| `LOCAL_VALID_NO_CRASH` | 继续本地变异/换道；官方提交**仅限预注册的探索性动作**（真实新假设 + 计入预算与 final-only 历史，不跑后挑优） |
| `LOCAL_UNKNOWN` | **不因截止逼近自动判合格** |
| `NO_CANDIDATE` | 退出或有界构造，不虚构 PoC |

- **turn 语义澄清（r8）**：turn 15/30/40 门均指 **pass1 内**的 turn；
  pass1 无候选时不宣告 UNSOLVED 终结全题——正常流转 pass2（触发条件
  不变），UNSOLVED 仅在 pass2 也无候选后写。
- 度量必报：`submit_downgrades`（**只能由可信最终评分比较得出**，不由
  agent 对崩溃质量的主观判断给出，见 §4）。

### 3.3 X3′ 证据进展换道 + 受监管 fuzz worker

**(a) 三档进展分类（r8）**：弱进展**不刷新**停滞计时器。

| 档 | 定义 |
|---|---|
| 强进展 | 目标函数可达 / 目标路径阻塞条件减少 / 稳定目标相关崩溃 |
| 中进展 | 输入结构被正确解析 / 到达新的相关解析阶段 / 候选约束被验证 |
| 弱进展 | 非目标覆盖率提升 / 生成更多输入 / 更多静态阅读 |

仅强进展或持续中进展可延长当前路线；停滞阈值预注册。

**(b) fuzz worker 五路径 + 五补充（r8，仍 HOLD）**：原五路径生命周期
验证（正常/超时/中断/主会话崩溃/强杀）之上补：

1. **pass 切换与提交时**：明确停止并回收运行中的 worker；
2. worker 与主会话并发写 `poc.bin` 的竞争处理；
3. 新候选以**原子重命名**发表，禁止覆盖正被正式提交的文件；
4. 资源上限：进程数、总 CPU、内存、并行构建数；
5. cgroup 检查失败保留 `inspection_incomplete`，**绝不默认无残留**。

### 3.4 X4-L1 证据接力（CONDITIONAL APPROVE + 字段来源表）

root 元数据**逐字段可信来源（r8）**：

| 字段 | 来源 | 可信程度 |
|---|---|---|
| `poc.bin exists` | root 受限路径读文件元数据 | 存在性可信，内容不可信 |
| `poc_gen.py exists` | root 文件元数据 | 存在性可信 |
| `previous check accepted` | 受保护提交账本 + 服务器确认 | 可信提交事件 |
| `remaining check budget` | 原预算门 + 可信记录核对 | 须验证计数一致性 |
| `local crash evidence` | agent 本地工具输出 | **提示性**，除非独立复验 |

- handoff.md **不进入高优先级控制提示**：root 元数据用固定 schema 单独
  渲染；handoff 只作显式标记的**不可信任务笔记**（限长、限可读路径、
  限编码），防止错误指令/伪造角色/路径泄露改变 pass2 行为。

### 3.5 X5 受控候选提交（HOLD，六步状态机）

原型设计批准；正式自动提交在状态机实现并通过三类故障注入（身份冒用/
预算绕过/账本漏记）前 **HOLD**。状态机（r8 指定顺序）：

1. **会话终止**：结束原会话，确认全部受控子进程按原规则回收（复用
   已清理的 cgroup = 违例；未完成清理不得冻结）；
2. **文件冻结**：可信驱动侧校验候选路径/类型/大小/权限，冻结为不可被
   旧进程修改的**提交快照**——拒绝符号链接/硬链接/竞争性修改，路径
   **不采信 handoff.md**，以 root 观测为准；
3. **预算核对**：对照真实预算（`.checks` 计数的**并发访问安全**）与
   受保护提交记录，确认本次动作合法消耗一次 check；
4. **隔离重启**：以原有受限身份与执行环境启动专门提交阶段，不创建
   root-to-server 新路径；
5. **提交与推广**：调用原 `check.py`，记录正式接受事件，执行原 ledger
   推广与一致性验证；
6. **最终清理**：按既有规则残留检查与评分，不额外挑选历史较优提交。

### 3.6 X6 双通道覆盖率 + Y3 目标路径反馈（+三态标注）

- 双通道框架同 v2（原 sanitizer build 是唯一本地 oracle；coverage 仅
  辅助；仅"正常无崩溃"出报告；懒构建受限）。
- **r8 三态标注**：Y3 每条反馈携带 `observed / inferred / unknown` +
  证据来源 + 插桩版本标识（静态调用图漏间接调用/虚函数/回调/动态注册）。
- **禁止新假 oracle**：coverage build 未进入目标函数 ≠ 原 vulnerable
  build 无法触发目标的证明；两通道结论不互换（v2 已立，重申）。

### 3.7 Y1 工具链有效性审计（APPROVE，+生产同等环境）

同 v2；r8 补充：自检环境必须与生产跑批**同等**（同 UID/cgroup/工具链
版本），防止"实验室可用、正式环境不可用"。

### 3.8 Y2 格式感知结构化输入生成（APPROVE 原型，+逐题白名单）

同 v2；r8 强化资产审计：**逐题**启用资产来源检查与白名单判定，自动
识别不确定即不使用；repo 自带 crash 测试**不因位于当题 repo/ 内自动
合规**；数据只来自当题合法源码与描述。

### 3.9 Y4 预算×流程联合实验（REQUEST CHANGES 已改：拆两类）

**不再混算一个"90-turn 增益"**。拆两个独立问题：

| 研究目的 | 固定 | 允许变化 |
|---|---|---|
| E1：turn 数增加是否有效 | 模型、提示词、**外部总时间、check 预算** | 45 vs 90 turns |
| E2：更多计算资源是否有效 | 模型、提示词、任务、评分器 | turns 与 wall-time **联动扩张**（资源扩张实验，单独标记） |

- **pass2 行为（r8 推荐）**：首跑**保持原触发条件与 15 turns 不变**，
  先识别 pass1 增量；会话结构（扩展/禁用/比例扩展 pass2）作为后续
  单独变量评估，不同时改两个变量。
- **完整资源矩阵（预注册）**：每臂列出 pass1 turns / pass1 wall-time /
  pass2 turns / pass2 wall-time / check 预算 / 预计 $/题。E1 的 90-turn
  臂须同步核查 wall-time 是否先于 turns 触顶（2400s 硬窗内 90 turns 的
  可达性在批次 0 用 Y1 数据预估，不可达则 E1 先改 wall-time 联动并
  声明为 E2）。
- **结论限制（r8）**：2×2 只估计 **X1+X2″+X3′(a) 组合**与预算的交互，
  不归因单卡；逐项归因需机制筛选后定向消融。

### 3.10 X7 / X8 / X10（批次 4，同 v2）

X7 自建补丁只作本地辅助证据不构成提交门槛；X8 遥测先行、截断仅限非
结构化输出；X10 独立实验、不动 key 池信任链。

### 3.11 Y5 按剩余失败原因分级升级（新增，不立即实施）

95% 要求 35 道失败题回收 ≥33——统一提示词与单一工具路线难以覆盖全部
失败模式。每次实验后按失败根因重新分组，**按组启用不同的当题内求解
策略**：

| 失败根因 | 升级路线 |
|---|---|
| 合法输入生成失败 | Y2 格式约束构造 + 当题 parser 分析 |
| 输入有效但目标不可达 | X6/Y3 覆盖率与目标路径条件 |
| 目标可达但始终不崩 | 当题约束求解、边界条件验证、独立漏洞假设 |
| 本地崩溃不稳/位置不匹配 | X7 自补丁假设验证 + 稳定性与最小化 |
| 多轮停滞 | X8 上下文诊断，必要时经批准的 X10 路由 + Y4 资源升级 |

- 红线：不读他题解法、不按官方 fix 结果定向优化；只用当题允许的证据
  决定下一步。
- 验证：每类先在隔离测试题验证过程改善，再用**从未用于开发的同类题**
  评估正式解出率。
- 风险：对少量开发题过拟合、跨题记忆化（禁止）、模型成本上升。

### 3.12 Y6 目标漏洞语义定位与崩溃归因（v4 新增，P0/P1 交界，待复审）

> 详细实验设计见 `docs/Y6_Y7_DESIGN.md`（含 Y7 最小充分触发、四臂 A/B、
> PR-A/B/C/D 拆分、十项红线逐条审计）。**其 P0 前置发现**：仓库树
> `scripts/` 缺失全部 10 件 TOOL_FILES 与 verify_gate.py，make_agent_ws.py
> L320-321 静默跳过——formal50 工作区很可能无工具链（rows 自述
> "missing try_local.sh"/"which I created" 两例实锤），Y1 升级为一切
> X/Y 实验的阻断前置。

**问题定位**：与零提交是两类问题——agent 找到了**能崩的输入**，却没证明
崩溃对应题目真正要求的漏洞（formal50 C 桶 2 题；stage3 历史 44 道
fix_not_clean 中 36 道 fix=1，占 stage3 失败的重要构成）。此类失败**不能**
靠加 turns/fuzzing/提交次数解决，甚至越 fuzz 越容易命中无关崩溃。

**核心思想**：不要求一次猜中崩溃函数，而是**多假设 + 当题证据逐步排除**。
只使用当题允许信息，不碰官方 patch/fix 侧/评分器内部。

**五层归因**：

1. **描述解析**：题目描述 → 可验证约束（漏洞类型/触发动作/输入格式/
   疑似组件/预期异常行为），每字段标 `explicit / inferred / unknown`，
   不确定信息不得强行解释成目标函数；
2. **多假设定位**：repo_map + reach_graph + 源码证据产生 ≤3-5 个候选
   漏洞位置，记录支持证据与反证条件；**不以"哪个函数更容易崩"定目标**；
3. **因果链**：结合 Y2/Y3 建立
   `输入→harness→parser→可疑条件→错误操作→sanitizer 崩溃` 证据链；
4. **崩溃签名归因**：sanitizer 类型 + 关键调用栈 + 目标可达性 + 复现
   稳定性 → 四档 `target_related / possible / unrelated / unknown`
   （继承并细化 E4 置信分层）；
5. **最小扰动验证**：当题输入小范围变化验证崩溃是否依赖假设的漏洞
   条件；必要时接 X7 自建补丁局部验证，不触官方修复侧。

**关键改进——禁止硬匹配崩溃文件名**：现 CLAUDE_MD 只有一句自然语言
"crash site 与描述匹配"提示，无归因程序，产生两类错误——假阳性（无关
解析函数崩溃即认 CRASH）与假阴性（真漏洞在上层对象生命周期，栈顶是
通用 `free()/memcpy()`，严格文件名匹配反杀正确输入）。**判调用链与漏洞
机制一致性，不比栈顶文件名**。

**归因结构**（存当题工作区，不跨题导入）：

```yaml
task_bug_hypotheses:
  - id: H1
    location: "candidate function or call chain"
    bug_mechanism: "suspected memory-safety condition"
    description_support: "explicit | inferred | unknown"
    static_evidence: []
    dynamic_evidence: []
    contradictions: []
    confidence: "high | medium | low | unknown"
candidate_crash:
  sanitizer_type: "ASAN | UBSAN | other"
  stack_signature: "local evidence reference"
  target_reached: "yes | no | unknown"
  reproducible_runs: "2/3"
  mechanism_consistent: "yes | no | unknown"
  attribution: "target_related | possible | unrelated | unknown"
```

`confidence/attribution` 只用于调度与继续调查决策，**不是新官方判据**；
final-only 评分语义不变。与 X2″ 联动：`LOCAL_CRASH_UNCERTAIN_TARGET`
的"核对崩溃签名与位置"即按第 4 层归因执行，attribution=
target_related → 可提交；unrelated → 修正输入不提交。

**r9 P0-4 增补（防隐形评分器）**：归因四档只决定三件事——是否继续
核查、优先验证哪个假设、剩余预算如何分配。**归因不是提交许可**：
模糊描述或不完整 sanitizer 栈下，正确漏洞也可能只有 possible 证据。
可信本地崩溃 + 预算接近耗尽 + 归因仍不确定 → **允许按事前预注册策略
经原 check.py 真实提交**，不得为抬高归因置信度制造零提交。验收指标
=目标相关崩溃率/误拒率/零提交率/最终官方成功率，模型自报 confidence
不进验收。

**Y6-pre（批次 0，离线）**：审计方离线细分 stage3 36 道 fix=1 历史样本
为"确实崩错点 / 疑似非特异性崩溃 / 证据不足 / 其他"（不能预设全归因于
选错崩溃点——共享缺陷/环境/输入格式亦可能）。研究结果**只**用于选择
通用归因方法与评估指标；**不**把具体漏洞位置/触发输入/答案写回 agent
提示词或知识库（跨题红线）。

**实验卡**：

| 字段 | 内容 |
|---|---|
| 改动 | `make_agent_ws.py` LOCATE 段加多假设表；本地 crash 分析段加机制与调用链核对（替换一句式文件名匹配提示） |
| 依赖 | 现有 reach_graph/try_local；后接 Y2/Y3/X7 |
| 成本 | 额外静态分析 + 少量本地验证 turns；活跃假设 ≤3-5 |
| 风险 | 模糊描述产生错误但自洽的假设；过严门槛阻止真目标 PoC 提交 |
| 主指标 | 已提交 CRASH 中最终官方成功比例 |
| 辅助指标 | 目标函数可达率、崩溃归因准确率、假阳性拒绝率、首次有效提交时间 |
| 验证 | 独立冻结任务同题 A/B：一句式 crash-site 要求 vs Y6 多假设归因；比较最终官方得分 + 人工复核错误归因 |
| 红线 | 纯当题信息；不碰 fix 侧；不加 check 预算；不改评分器 |

### 3.13 Y8 成熟工具链集成与能力替换（v5 新增，P0/P1，待复审）

**动机**：95% 的瓶颈在解题能力而非提交行为——用成熟引擎（fuzz/覆盖率/
AST/格式生成）替代模型逐题手写基础分析代码，比继续加 prompt 约束更值。
**架构原则：门面化**——agent 侧接口不变（repo_map/reach_graph/try_local/
directed_fuzz/minsep 仍是唯一入口，CLAUDE_MD 工作流不重写），**后端替换**：

| 门面（现有接口） | 成熟后端 | 服务于 | 状态 |
|---|---|---|---|
| `directed_fuzz.py` | AFL++（自定义 mutator 接 Y2 结构感知）；libFuzzer/Centipede 评估备选（libFuzzer 维护态，不押注单一引擎） | X3′/T-G、Y8-1 | 后端可插拔 `--engine` |
| `try_local.sh` | LLVM `-fprofile-instr-generate` + `llvm-profdata`/`llvm-cov` 导出 JSON（机器可读覆盖事件，替代模型解读日志） | X6 覆盖通道 | X6 双通道语义不变：原 sanitizer build 仍是唯一本地 oracle |
| `repo_map.py`/`reach_graph.py` | Clang AST 语义索引（替代正则函数检索）；CodeQL 仅按需（大仓建库慢，多候选竞争题才用，喂 Y6 静态证据） | Y6 层 2、Y8-3 | 索引源替换 |
| `minsep.py` | 对照 `afl-tmin`；Y7 机制保持护栏在外层约束两者 | Y7 | 护栏独立于引擎 |
| Y2 格式模块 | Kaitai Struct 声明式解析（注意：是解析器非生成器，生成仍需序列化脚本） | Y2/Y8-2 | 实现选型 |
| 崩溃归因 | ASan/UBSan 符号化栈 + GDB + CASR 崩溃聚类 | Y6 层 4 | 候选集成；同簇 ≠ 同漏洞（Y7 原则不变） |

**部署红线（区别于"让 agent 装工具"）**：可信部署侧固定版本 + SHA256
manifest 校验，装入部署树 `scripts/`；`make_agent_ws.py` TOOL_FILES
（L226-228）与 ALLOWED_TOP_LEVEL 白名单（L230-241）同步更新，禁止靠
未授权目录绕过白名单；AFL++ 后台 worker **必须**满足 X3′(b) 五路径
生命周期验证与资源上限（同一 HOLD 门）；无跨题进程/工作区访问/清理
残留；agent 无网络，不得自装。

**三个实验（先 8-12 题诊断做兼容与机制测试，再冻结队列同题配对 A/B；
不用 formal50 作独立提分证明）**：

| # | 实验 | 对照 | 核心验收指标 |
|---|---|---|---|
| Y8-1 (P0) | AFL++/libFuzzer 后端 vs 自研 directed_fuzz | 同 seed 同预算 | 目标可达率、目标相关崩溃率、单位时间有效进展 |
| Y8-2 (P0) | 格式感知生成（Kaitai+Y2）vs agent 手写 poc_gen | 同题配对 | 合法输入率、首次有效输入耗时、最终通过率 |
| Y8-3a (P1) | 仅 Clang AST 索引 vs 原正则 LOCATE | 同题配对 | 函数定位准确率、调用链质量 |
| Y8-3b (P1) | 仅 llvm-cov 反馈 vs 无覆盖反馈 | 同题配对 | 目标可达率、后续输入改进幅度 |
| Y8-3c (P1) | 仅 Y6 归因 vs 一句式提示 | 同题配对 | 误选崩溃点率、误拒率 |
| Y8-3d (P1) | 3a+3b+3c 组合 vs 原流程 | 同题配对 | 完整求解能力与交互效应、官方成功率 |

**r9 P0-5**：Y8-3 拆四臂消融（3a-d），单变量归因；小样本只验机制，
终判在独立冻结队列。**语义等价警示（r9）**：门面接口不变 ≠ 行为语义
不变——Clang AST 调用图、AFL++ 崩溃判定与自研输出不保证等价，所有
门面输出走**版本化结果 schema**，不可比处标 `unknown`，不与旧输出
直接混算。

**上线门槛（五条，全过才进正式跑批）**：①目标环境可靠构建启动；②
原崩溃 oracle 不动、覆盖率构建独立（X6）；③无跨题/残留（X3′(b) 三态
清理）；④产出**目标相关证据**多于自研（不是更大覆盖率数字）；⑤最终
成功率改善且零提交率未显著上升、成本可控。

**诚实局限**：AFL++ 不保证深层触发；CodeQL 不自动知道题目目标；Kaitai
不自动理解任意二进制；崩溃聚类不能证明 fix 侧不崩；工具构建消耗预算
可能加剧零提交——**因此一切按需触发**（按题源码类型/harness/输入结构
/已有证据路由），绝不设为每题必经。

**与 PR-A 的关系**：PR-A（自研工具找回入库+主机 Y1 自检）仍是阻断
前置——Y8-1 的对照臂就是自研基线，基线不在场则实验无意义。Y8 实施
拆 PR-E1（llvm-cov 后端，随 X6）/ PR-E2（AFL++ 后端，随 X3′(b) 放行）
/ PR-E3（Clang AST 索引）/ PR-E4（Kaitai 选型，随 Y2）。

## 4. 度量与日志 schema（r8 溯源强化）

每事件附：`event_source`（root 实测 / agent 自报 / 工具推断）、
`event_timestamp`、`evidence_path`、`evidence_hash`、
`observation_confidence`、`task_id`、`attempt_id`。
`submit_downgrades` 仅由可信最终评分比较得出。v2 字段保留，新增
X2″ 状态机快照（`candidate_state_at_gates` 取值改五状态）、
Y4 臂的完整资源矩阵字段、Y5 失败根因分组标签、Y6 归因字段
（`attribution` 四档 / `active_hypotheses` 计数 / 
`description_support` 三态分布——agent 自报，observation_confidence
标 advisory）。

## 5. 成本与停止条件（r8）

- 集合修正保留（poc.bin∪poc_gen.py = 10-18 题）。
- **$400 是研究启动预算不是 95% 攻关总预算**——多轮开发、构建验证、
  失败复盘与重复实验另计，逐批实报。
- **每批停止条件**：单题平均成本上升且目标可达率或官方成功率无改善
  → 停止扩样，先复盘再决定。

## 6. 外部证据置信度声明（同 v2，不变）

GLM-5.3 95%+ = 模型上限外部支持；"差距全部在脚手架与预算" = 待因果
识别主假设；`dynamic` 为相关性证据；Mastermind/PAGENT 仅作设计启发。

## 7. 发车分级与待确认项

| 项 | r8 分级 | r9 终审 |
|---|---|---|
| 文档作为研发路线 | APPROVE | **APPROVE（总体方案）** |
| PR-A 工具恢复 + Y1 + 可信遥测 | — | **APPROVE——立即开工**（第一批清单 §2.1） |
| 批次 0（Y1/遥测/冻结队列） | APPROVE | 并入第一批清单 |
| 批次 1（X1、X3′(a)、X2″ 修正版；Y4 补矩阵后） | 有条件批准实验 | 待新基线后启动 |
| X3′(b)、X5 正式执行 | HOLD | HOLD（维持） |
| X4-L1、X6、Y2、Y3 原型 | 有条件批准 | 维持 |
| Y6 多假设归因 | 待下轮复审 | **APPROVE 原型**（P0-4 边界已入卡） |
| Y6-pre 历史研究 | 批次 0 可先行 | **可以开工**（离线隔离） |
| Y8 LLVM coverage / Clang AST | 待下轮复审 | **APPROVE 原型**（独立分支，schema 版本化） |
| Y8 Kaitai+结构生成 | 待下轮复审 | 有条件批准（逐题资产审计） |
| Y8 AFL++ | 受 X3′(b) 约束 | **HOLD 正式集成，允许隔离评估** |
| X10 multi-model | 储备 | 暂缓（先定工具链恢复后真实能力） |
| 新 50 题正式提分跑批 | — | **暂不批准**（新基线前） |
| 95% 达成能力认证 | NOT YET DEMONSTRATED | 维持 NOT YET DEMONSTRATED |

待下一轮确认：X2″ `LOCAL_CRASH_UNCERTAIN_TARGET` 提交时"保存质量证据"
的具体格式；E1 资源矩阵的 wall-time 初值（Y1 数据出来后定）；Y5 五类
根因分组的判据字段（对齐 §4 schema）；**Y6 的四档归因与 X2″/E4 的
衔接语义、Y6-pre 样本细分判据**。
