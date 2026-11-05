# Stage-4 评审材料（防守闭环改动集）

这是 `stage4` 全量工作树的**评审子集**：只含本轮改动/新增的文件，加上运行
单元测试所必需的最小依赖。未列出的文件与 stage-3 基线相同，基线见
<https://github.com/jyw358/stage3->（`main`）。

## 为什么只传这些

- 改动集中在评分口径、提交账本、工作区隔离、结果落盘四块；
- 其余 300+ 个 stage-3 一次性脚本、宿主机数据、会话日志与凭据不参与评审；
- 清单与排除项见 [EXCLUDED.md](EXCLUDED.md)。

## 目录

```
cybergym-agent/
  plugins/
    evidence.py          提交账本 + final-only 判定（本轮核心）
    results_store.py     逐题原子行 + results.json 锁内重建（R2: rows/ 唯一权威）
    locks.py             跨平台文件锁（R2: 超时获取 + NFS 检测）
    cybergym_runner.py   提交客户端（提交时写账本；official_success 改 final-only）
    env_manager.py       未改，RunOutcome 依赖
    diff_verifier.py     未改，测试依赖
  scripts/
    make_agent_ws.py     工作区生成（--fresh / 无 intel / manifest / 白名单；R3: 路径自派生）
    run_agent_task.sh    单题 root wrapper（R3: 控制面绑定 + 每题唯一 uid + 私有 HOME/TMPDIR）
    stage4.py            队列驱动（阶段退出码传播 / 进程组清理 / run 锁 / 控制面 / uid 池）
    score_all.py         主机侧评分（最终提交 + ledger）
    score_task.py        单题评分（stage2/cleanpilot 路径）
    record_task_row.py   会话 + 账本 → 行；两段统计相加
    verify_stage4.py     数据完整性 + 执行覆盖率双维验收（R3: +控制面绑定/账本冲突）
    promote_ledger.py    root 侧账本收割（R3: 身份强制覆盖 / 坏行硬失败 / 冲突单列）
    validate_agent_uid.py wrapper 的 uid 裁决（R3-M2: 控制面为准，拒绝 0/prep/池外）
    stage2.sh            stage-3 遗留驱动：删 intel、硬门禁先于 worker 入口
    cleanpilot.sh        stage-3 遗留干净跑法：同上（S2_LEGACY_OK 门禁）
    agent_mission.txt    未改，mission 文本
  tests/                 stage-4 新增/改写的测试（154 项，可离线跑）
  agent/contract.py      未改，Verdict 依赖
root_code/
  stage4.sh              可信启动器（T1: 清 pycache → 树审计 → python3 -B exec）
  assert_code_tree.py    stdlib-only 树审计（部署 /root/，在 Python 启动前建立信任）
  score_all.sh           评分入口（薄包装）
  backfill_fix_side.sh   fix 侧回填
docs/
  STAGE4_CHANGES.md      改了什么、fail-closed 状态表、已验证/未验证
  STAGE4_DEPLOY.md       主机部署与 50 题 pilot 手册（/opt 部署 + 隔离检查清单）
  PROPOSAL_DEDICATED_USER.md  专用运行用户隔离提案（R3 已按评审裁决实施）
smoke_stage4.py          本地冒烟：工作区 + 行 + 验收（评分前验收必须失败）
smoke_score.py           本地冒烟：本地 HTTP /query-poc + final-only + ledger
```

## 快速验证

```bash
cd cybergym-agent
python3 -m unittest discover -s tests
# 预期：Ran 154 tests ... OK（含第二轮 B7-B12 回归、第三轮隔离回归、
#       R3/R3.1/R3.2/R3.3/R3.4 复审与运行时验收回归）

cd ..
python3 smoke_stage4.py
python3 smoke_score.py
# 预期：SMOKE SCORE OK（五个场景：最终失败=False / 最终成功=True / 账本不
# 覆盖最终=None / 哈希不一致=hash_mismatch / 混偏移时间戳按时刻排序）
```

`/home/naco/...`、`/data/cybergym/...`、`http://172.17.0.1:8666` 是跑批主机的
真实部署路径，原样保留，不是缺失文件。

## 本轮主张（供评审逐条证伪）

第一轮（B1–B5）：

1. 官方成功只按**最终提交**计算；`created_at` 严格解析为 UTC 后取最大值，
   缺失/无法解析/同刻并列一律 `success=None`，绝不给 `True`；agent 可写的
   账本**不参与**排序，也不消歧并列。
2. 提交瞬间写账本（哈希在构造请求体时对上传字节一次性计算，提交后不重读
   文件）；评分必须同时满足：服务器最终 `poc_id` == 账本最后一条，且服务器
   `poc_hash` == 账本 `poc_sha256`，否则 fail-closed。
3. 受保护账本 `root:root 0644 + chattr +a`，唯一写入者是 root 侧
   `promote_ledger.py`（B11：agent 进程无任何写路径，`CG_PROTECTED_LEDGER`
   出现在 agent 环境即被忽略并告警）；工作区账本仅作诊断。
4. 正式评分**没有**无账本降级参数（`--allow-missing-ledger` 已删除，
   `S4_REQUIRE_LEDGER` 已删除）；`score_all.py` 退出码区分
   完整/完整性失败/基础设施错误。
5. 跨题 intel 的两个写入点都已删除；生成器与 stage2/cleanpilot 都有硬门禁。
6. 每次尝试用 `--fresh` 工作区；生成后做 intel/预算/白名单断言并写 manifest。
7. 逐题 `rows/<task>.json` 原子落盘；`results.json` 在文件锁内原子替换；续跑按终态。
8. 第二段会话的 turns/tokens/checks/cost 与第一段相加；`CLAUDE.md` 渲染真实预算。
9. `verify_stage4.py` 能在提交缺证据、账本缺失、哈希不一致、intel 残留、
   run/agent 归属缺失、聚合不一致时报错。

第二轮（B7–B12）：

10. 评分/回填/验收任一阶段失败（或脚本缺失），`stage4.py` 整轮 exit 1；
    `batch_summary.json` 公布 execution/scoring/phase 三类失败（B7）。
11. `rebuild()` 只从 `rows/` 重建；`merge_row` 先写权威逐题文件再写派生聚合
    ——旧成功不可能从聚合复活（B8，评审复现命令 2 已验证）。
12. 终态拆分 execution/scoring：submitted+评分未决只进评分恢复，绝不重跑
    agent，也绝不计入完成（B9）。
13. 全 `gen_fail`/`worker_error` 的自洽轮必然验收失败（`coverage_infra`
    violation，评审复现命令 1 已验证 `verify_exit=1`）；聚合按字段比对、
    `scored/` 交叉核对、缺成本即 violation（B10）。
14. 每题独立进程组，超时 SIGTERM→SIGKILL 整组清理后才归档/重试（B12）；
    `stage2.sh`/`cleanpilot.sh` 的 legacy 门禁先于 worker 子命令（MAJOR）。

第三轮（专用用户隔离实施，R3）：

15. 代码树的信任由 root 侧启动器在 **Python 启动前**建立：`/root/stage4.sh`
    清 `__pycache__` 后运行 stdlib-only 的 `assert_code_tree.py`（部署在
    /root/，不 import 被审计树），拒绝符号链接 / go-w / 非 root 属主 /
    不可信用户可写祖先；`stage4.py` 内的 lstat 断言只是防御性兜底（T1）。
16. 身份三分离且 fail-closed：root（驱动/评分/晋升）、`S4_PREP_USER`（默认
    naco，仅 gen_task——绝不与 agent 同 uid）、每题**唯一数字 uid**
    （池内取号、run 内不复用、跳过 passwd 已有 uid）。
17. agent 会话以 `setpriv --reuid/--regid/--clear-groups` + `env -i` 显式环境
    运行（不用 sudo：本机 sudo 1.9.9 拒绝未注册数字 uid，见部署手册 §9.1）：
    全新 0700 的私有 HOME/TMPDIR/XDG_*/CLAUDE_CONFIG_DIR、umask 077、
    ws 属主=该 uid、ws 根 root:root 0711（不能列兄弟目录）；缺 setpriv 即
    拒跑，绝不回退 root。
18. 控制面绑定 `<out>/control/<task>/`（root:root 0700，O_NOFOLLOW+原子写）
    锁定 run_id/agent_id/submit.sh sha256/agent_uid；wrapper 消费前逐项校验，
    复用题目录必须哈希+agent_id 全对得上（marker 降级为遗留提示）。
19. `promote_ledger.py`：root 提供值强制覆盖（拒绝不可信预写值，rc=5）、
    坏行/缺 poc_id 硬失败（rc=2）、同 poc_id 不同哈希单列 conflicts 文件
    （rc=4）；受保护账本重述为“root 封存的不可信提交声明”，权威在服务器
    final 记录+哈希门禁；verify 报 `control_missing`/`control_mismatch`/
    `ledger_conflict`，控制面缺席即 warning（`control_plane_absent`）。

R3 复审修复：

20. 准备目录不再跨题可读：prep 期间 0700（仅 naco 写），prep 结束整树
    chown 给本题 uid + 0700，`task_base` root:root 0711；wrapper 复核
    属主/模式，verify 报 `taskdir_open`/`taskdir_unbound`（B1）。
21. UID 发号改为**宿主机级只增不减注册表**（`S4_UID_STATE`），跨 run 也不
    复用，注册表损坏 fail-closed（B2）。
22. 控制面祖先链逐级校验（root 所有 / 非 group·other 可写 / 无符号链接），
    不再只信任最后一级目录（M1）。
23. wrapper 的 uid 只信控制面：新增 `validate_agent_uid.py`（环境变量存在
    必须严格相等，拒绝 0/prep/池外）（M2）；promoter 失败立即记
    `evidence_error`、rc=6、不跑第二段（M3）；超时清理追加 `/proc` 会话
    扫描复查（M4，setsid 另开会话仍需 cgroup，见部署手册第 9 节）。

R3.1 增量复审修复：

24. `uid_alloc.json` 与宿主机注册表损坏一律 fail-closed（`SystemExit`），
    不再重置为空表继续发号（MAJOR-1）。
25. 题目录复用绑定从“只验 `submit.sh`”扩为整树：`task_inputs_sha256()`
    逐文件（相对路径 + 大小 + sha256，软链接记目标）生成 `task_sha256`，
    `validate_reuse()` 缺该字段或树变了都拒绝（MAJOR-2）。
26. wrapper 在入口处把题目录与 `<out>/tasks` 都 `readlink -f` 收敛，题目录
    越界即 `REFUSE`（MAJOR-3）；控制面 `agent_id` 为空一律拒绝正式任务
    （MAJOR-4）。
27. 上机负向测试（2026-11-04）**9 个场景 / 25 条断言全通过**：跨题读、跨 run
    uid、wrapper uid 冲突、空 agent_id、promoter 失败、降权身份与环境探针、
    ledger 0700/0600 跨 uid 不可读、真实 `lsattr` 的 append-only、setpriv 启动
    失败独立归因（`agent_launch_error`）；并据此修掉 sudo→setpriv、
    `evidence_error` 被改写两处缺陷（部署手册 §9/§9.1）。
28. 真实链路 smoke（2026-11-04）：`gen_task`（naco）→ `setpriv` 会话 → 真实模型
    调用 → 受保护账本 → 评分/验收。前几次失败暴露了只能在真机上出现的问题：
    agcli 装在 0750 家目录导致每题 uid 无法执行、agcli 依赖 `$HOME` 与私有 HOME
    冲突、`env -i` 清空后模型凭据无法注入（新增 `S4_AGENT_ENV_FILE` root 侧注入）；
    `--out` 祖先链新增"他人可遍历"校验（0700 的 `/root`、naco 属主的 `/data` 会被拒）。
29. 端到端 smoke 最终通过（2026-11-04，`arvo:10628`，uid 30005）：
    `status=submitted`、1 次 check、1 条真实提交、`cost=$0.42`；protected ledger
    目录 700 / 文件 600 / `lsattr` 含 `a`；`promote_ledger` 写服务器内部 id；
    `score_all` → `official_success=1/1`；`verify_stage4` → `violations=0
    warnings=0`。为此修掉五个现场缺陷，见部署手册 §9.2 S-5～S-10：会话内的
    `check.py` 因运行时树不可读而 `ModuleNotFoundError`（于是加了 agent uid 探针
    + 拒绝 group/other 可写的运行时树）、启动证据放在工作区会被删（改为 root
    侧已打开 fd）、默认评分脚本是 stage-3 遗留 wrapper、新 run 的行继承了旧 run
    统计、服务器 `created_at` 无时区导致全部 `timestamp_invalid`。
30. R3.3 复审两个 P1 修复（2026-11-05，WSL2 root/ext4 实测 5 场景/19 断言全过）：
    已有 append-only ledger 重入时 `chown`/`chmod` 会 EPERM（内核
    `setattr_prepare` 实测确认），改为先核验后原样复用、不符即拒
    （`evidence_setup_error` + rc=8，不改写证据）；`chattr` 不可用时从"降级
    跑完再被验收拒"改为**模型调用前 fail-closed**（`S4_ALLOW_UNPROTECTED=1`
    仅限开发降级）；并前移 `AGENT_ID` 赋值修复 `set -u` 下 fail-closed 路径
    丢行的潜伏 bug。详见 STAGE4_CHANGES.md §0.8.3 与部署手册 §9.3。
31. R3.4 复审两项加固（2026-11-05，真机 ext4 实测 23 断言全过）：正式入口
    **启动即拒** `S4_ALLOW_UNPROTECTED=1`（`root_code/stage4.sh` exit 4、
    `stage4.py` SystemExit），杜绝"先烧模型预算、后由验收拒绝"；ledger
    初始化（wrapper 只有 `set -u` 无 `-e`）改为**每个文件操作独立检查**，
    `mkdir/chown/chmod`/probe/建文件失败一律 `evidence_setup_error` + rc=8
    并清理半成品，清理失败显式 WARN。详见 STAGE4_CHANGES.md §0.8.4 与
    部署手册 §9.4。
32. R3.4 运行时验收三项（2026-11-05，真机 ext4，20/20）：跨 UID **写入**/
    删除/`chmod` 他题 workspace 与 prepared dir 全部 denied；他题
    `CLAUDE_CONFIG_DIR` 状态文件读取与覆写 denied；**setsid 逃逸闭环** ——
    原实现按会话号扫 `/proc`，逃逸后代存活却报 `residual=0`（对照组实测
    静默漏杀），改为 per-attempt token 扫 `/proc/*/environ` 精确兜杀；残留
    不为 0 时写 `process_residual_error` 且 `--retry-failed` 不重试（人工
    清理后才能重跑）。详见 STAGE4_CHANGES.md §0.8.5 与部署手册 §9.5。

评审请优先攻击这些主张，而不是重述它们。
（版本一致性提示：本材料对应仓库 `main` 单一 commit，见 `MANIFEST.sha256`；
请以该 commit 内容审计，勿混用不同来源/缓存的旧文件快照。）
