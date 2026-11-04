# Stage-4 部署与跑批手册

本手册按跑批主机的真实路径编写（`/opt/cybergym-agent`、`/data/cybergym/...`）。
在跑批主机**以 root 执行**。R3（专用用户隔离实施）后的身份模型：

| 身份 | 账号 | 只允许做什么 |
|---|---|---|
| 驱动/评分/晋升 | `root` | 跑 `stage4.py`、`score_all`、`backfill`、`promote_ledger`；写 `<out>/control`、受保护账本、rows |
| 准备身份 | `naco`（`S4_PREP_USER`） | 仅 gen_task 与题目录准备（`<out>/tasks/<t>` 属主），**与 agent 不同 uid** |
| 执行身份 | 每题一个数字 uid（`#30001` 起池） | 仅本任务工作区（0700，私有 HOME/TMPDIR/XDG），**run 内不复用** |
| 兜底账号 | `s4agent`（`S4_AGENT_USER`） | 仅作存在性基线与不可信名单成员，不应拥有任何代码/数据 |

## 1. 部署前检查

```bash
id naco                                # prep 身份必须存在
id s4agent || useradd -M -N -s /usr/sbin/nologin s4agent   # 基线账号（uid 必须落在池外）
python3.12 --version
test -f /root/.cybergym_env && echo env-ok
test -S /root/grader.sock && echo grader-ok
docker ps >/dev/null && echo docker-ok
df -h /data | tail -1
```

stage-4 依赖与 stage-3 相同：rootless docker、agcli、`/home/naco/work/cybergym`
（gen_task 源码与 `.env`，保持 naco 属主——root 不执行它）、`/root/.cybergym_env`
（只有 root 能读的 `CYBERGYM_API_KEY`）。

**agcli 必须部署到 root 可控的全局路径**（不再放 `/home/naco/bin`，那样要求
开放 /home/naco 的目录遍历）：

```bash
install -o root -g root -m 0755 <you>/agcli /usr/local/bin/agcli
/usr/local/bin/agcli --version        # 真跑一次；能打印版本才算部署成功
```

## 2. 备份与部署

```bash
TS=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p /root/stage4-backup-$TS
cp -a /opt/cybergym-agent /root/stage4-backup-$TS/cybergym-agent 2>/dev/null || true
cp -a /root/stage4.sh /root/assert_code_tree.py /root/score_all.sh \
      /root/backfill_fix_side.sh /root/stage4-backup-$TS/ 2>/dev/null || true
```

从评审机（`stage4/` 所在机器）推送代码；排除密钥文件与本地测试残留：

```bash
rsync -a --delete \
  --exclude '.env' --exclude '.glm*.env' --exclude '.llm.env' --exclude '.pi.env' \
  --exclude '__pycache__' --exclude '.testtmp' --exclude '.smoke*' \
  stage4/cybergym-agent/ root@HOST:/opt/cybergym-agent/

scp stage4/root_code/stage4.sh             root@HOST:/root/stage4.sh
scp stage4/root_code/assert_code_tree.py   root@HOST:/root/assert_code_tree.py
scp stage4/root_code/score_all.sh          root@HOST:/root/score_all.sh
scp stage4/root_code/backfill_fix_side.sh  root@HOST:/root/backfill_fix_side.sh
```

> `--delete` 会删掉主机上 stage-4 源码目录里多出来的文件；若不确定，先去掉
> `--delete` 跑一次，再补一次带 `--delete` 的同步。**不要**覆盖
> `/home/naco/work/cybergym/.env` 与 `/root/.cybergym_env`。

R3：代码树必须由 **root 独占**（T1：root 跑的代码不能有其他写者）。旧手册的
`chown naco:naco /home/.../cybergym-agent` **禁止再执行**——那会把 T1 防线整个
交出去：

```bash
chown -R root:root /opt/cybergym-agent
chmod -R go-w /opt/cybergym-agent
chmod 755 /root/stage4.sh /root/assert_code_tree.py /root/score_all.sh
chmod 700 /root/backfill_fix_side.sh     # 含 DB/UDS 操作
```

### 部署后自检（每条都要真跑）

```bash
# T1 树审计：真实启动路径 = /root/stage4.sh（内部先跑 assert_code_tree.py）
/root/stage4.sh --help >/dev/null && echo launcher-ok

# 可信引导必须能拒绝坏树（negative test，预期输出 violation 并退出 1）
sudo -u naco touch /opt/cybergym-agent/scripts/__wtest && \
  /root/stage4.sh --help >/dev/null 2>&1; rc=$?
rm -f /opt/cybergym-agent/scripts/__wtest
[ "$rc" -ne 0 ] && echo "T1-negative-ok (rc=$rc)" || echo "T1-NEGATIVE-FAILED"

# 树内零符号链接、零 go-w、零非 root 属主（应无输出）
find /opt/cybergym-agent \( -perm /022 -o -type l \) -print
find /opt/cybergym-agent ! -user root -print

# 祖先链逐级 root 所有、他人不可写
namei -l /opt/cybergym-agent/scripts/stage4.py
namei -l /data/cybergym/stage4

# s4agent 不得有任何 sudo 权限（期望 "is not allowed to run sudo"）
sudo -l -U s4agent

# 每题数字 uid 的 sudo 语法可用（R3 依赖 '#<uid>'，需 sudo >= 1.8.x）
sudo -u '#30001' id && ! sudo -u '#30001' ls /root && echo sudo-uid-ok

# 宿主机级 uid 注册表位置可写、只增不减（B2）
mkdir -p /var/lib/cybergym/stage4 && chown root:root /var/lib/cybergym/stage4 \
  && chmod 700 /var/lib/cybergym/stage4
python3 - <<'PY'
import sys; sys.path.insert(0, "/opt/cybergym-agent")
from scripts import stage4
print("uid pool floor:", stage4.AGENT_UID_BASE, "registry:", stage4.UID_REGISTRY)
PY

# 密钥只测属主/模式/可读性，绝不打印内容
stat -c '%U:%G %a' /root/.cybergym_env        # 期望 root:root 600
sudo -u '#30001' test -r /root/.cybergym_env && echo "KEY LEAK" || echo key-protected
stat -c '%U:%G %a' /home/naco/work/cybergym/.env   # naco 可读，他人不可读

# 代码自检
cd /opt/cybergym-agent
python3 - <<'PY'
from plugins import evidence, results_store
print("evidence.utcnow", evidence.utcnow())
print("final-only predicate:", evidence.official(1, 0), evidence.official(0, 0))
PY
python3 -m unittest discover -s tests     # 137 项（含 R3 / R3.1 复审回归）
```

## 3. 数据目录与 queue

```bash
mkdir -p /data/cybergym/stage4
# queue 支持两种格式：
#   JSON 数组：["arvo:10400", "arvo:12345", ...]
#   纯文本：每行一个 task id，# 开头为注释
ls -l /data/cybergym/stage4/queue.json
```

运行目录（默认值，可用参数覆盖）：

| 项 | 默认 | 属主/模式 |
|---|---|---|
| run root | `/data/cybergym/stage4/r_engineer` | root（祖先链必须 root 所有、非 go-w） |
| 控制面 | `<run root>/control/<task>/` | root:root 0700（run_id/agent_id/submit_sha256/agent_uid） |
| UID 注册表 | `/var/lib/cybergym/stage4/uid_registry.json` | root:root，目录 0700、文件 0600；**只增不减** |
| 题目录 | `<run root>/tasks/<task>` | prep 期间 naco 0700；prep 后归**本题 uid** 0700 |
| 题目录父级 | `<run root>/tasks` | root:root 0711（可遍历、不可列） |
| 工作区 | `<run root>-ws/<task>` | 每题独立 uid，0700；ws 根 root:root 0711 |
| 日志 | `<run root>/logs/<task>.log` | root |
| agent 原始日志 | `/root/agent_logs/<run_id>/<task>.json` | root 0700 |

题目录是 **run-scoped** 的，但 R3 起信任锚在控制面：`<out>/control/<task>/`
由 root 以 O_NOFOLLOW+原子替换写入，绑定本轮 `run_id`、期望 `agent_id`、
`submit.sh` 的 sha256 与分配的 `agent_uid`。复用题目录时三者都要对得上；
对不上（包括 submit.sh 在 gen 之后被改过一字节）就拒绝复用。
换轮次请换 `--out`。

**R3 复审补充**：题目录本身在 prep 结束后会整树 chown 给本题 uid 并 chmod
0700（B1）——`uid_A` 无法读取 `task_B` 的 description.txt / submit.sh；父级
`tasks/` 为 0711，Sibling 无法列目录。UID 发号记入宿主机级注册表（B2）：
一个 uid 一旦发出，**任何后续 run 都不会再拿到它**（旧 run 残留的 0700 文件
因此不可达）。控制面及其全部祖先按 M1 逐级校验 root 属主 + 非 go-w。

## 4. 50 题 pilot（先跑这个）

```bash
cd /data/cybergym/stage4
/root/stage4.sh --queue /data/cybergym/stage4/queue.json \
    --out /data/cybergym/stage4/pilot50 --workers 3 --limit 50
```

driver 会自动：准备题（naco）→ 分配每题 uid → 跑 agent（`#<uid>`，0700 私有
HOME/TMPDIR/XDG/CLAUDE_CONFIG_DIR，umask 077）→ 补 fix 侧 → 评分 → 验收。
也可分步：

```bash
# 只跑，不评分
/root/stage4.sh --queue ... --out ... --limit 50 --no-score --no-backfill
# 补 fix 侧
/root/backfill_fix_side.sh --dry-run
/root/backfill_fix_side.sh
# 评分（final-only + ledger）
/root/score_all.sh /data/cybergym/stage4/pilot50 \
    /data/cybergym/stage4/pilot50/tasks \
    /data/cybergym/stage4/pilot50-ws
# 验收
python3 /opt/cybergym-agent/scripts/verify_stage4.py \
    --out /data/cybergym/stage4/pilot50 \
    --queue /data/cybergym/stage4/queue.json \
    --ws-root /data/cybergym/stage4/pilot50-ws
```

### pilot 验收门槛

`stage4.py` 的退出码已经承载 B7/B9/B10 语义：**exit 0 才算整轮通过**（执行
失败、评分未决、回填/评分/验收任一阶段失败都会 exit 1）。人工复核门槛：

```bash
python3 - <<'PY'
import json
v = json.load(open("/data/cybergym/stage4/pilot50/verify_stage4.json"))
print(json.dumps({k: v[k] for k in
      ("rows", "expected", "submitted", "official_success", "score_rate",
       "total_cost_usd", "rows_without_cost", "coverage")}, indent=2,
      ensure_ascii=False))
print("violations:", len(v["violations"]))
for x in v["violations"][:10]:
    print(" ", x)
PY
cat /data/cybergym/stage4/pilot50/batch_summary.json
```

必须全部满足才进入全量：

- `violations == 0`（R3 起包含 `control_missing`/`control_mismatch`/
  `ledger_conflict`——控制面绑定与账本冲突都按篡改处理）；
- `coverage.infra_failed == 0`（gen_fail/worker_error/api_error 为 0——
  基础设施失败必须重跑，不允许"平均掉"）；
- 每个 `submissions > 0` 的行都有 `final.poc_id / poc_sha256 / vul_exit /
  fix_exit / submitted_at / agent_id`（双侧 agent_id 非空且相等）；
- `score_status` 只有 `ok`（或显式 `no_records` 对应 0 提交）；
- `results.json` 的 official 计数 == `rows/` 的计数，且逐字段一致
  （status/submissions/cost/score_status/run_id）；
- 每个 `submissions > 0` 的行有非空、逐行可解析的
  `protected_ledger/<task>.jsonl`（root 侧 promote 产物；它封存的是**不可信
  提交声明**，评分权威在服务器 final 记录 + 哈希门禁）；
- `find <ws-root> -name 'reference-intel*' -o -name 'reference_intel*' \
  -o -name 'ref-intel*' -o -name 'ref_intel*' | wc -l` 为 0；
- 隔离抽查（R3 新增，任一失败即 NO-GO）：
  ```bash
  # a. 同一波内不得出现两个工作区共享同一个属主 uid
  stat -c '%u' /data/cybergym/stage4/pilot50-ws/* | sort | uniq -d
  # b. 每个工作区 0700 且属主 = 其控制面 agent_uid（抽 3 题）
  stat -c '%a %u' /data/cybergym/stage4/pilot50-ws/<task>
  cat /data/cybergym/stage4/pilot50/control/<task>/agent_uid
  # c. uid A 无法读 uid B 的工作区（拿两题的 uid 实测）
  sudo -u '#<uidA>' cat /data/cybergym/stage4/pilot50-ws/<taskB>/CLAUDE.md
  #   必须报 Permission denied
  ```

- **跨任务准备目录负向测试（R3-B1，必须真跑，先证明路径存在再断言拒绝）**：
  ```bash
  OUT=/data/cybergym/stage4/pilot50
  UID_A=$(cat "$OUT/control/<taskA>/agent_uid")
  UID_B=$(cat "$OUT/control/<taskB>/agent_uid")
  TASK_B=<taskB>

  # 0. 先证明文件真实存在（否则“路径不存在”会伪装成通过）
  test -e "$OUT/tasks/$TASK_B/description.txt" && echo path-ok
  # 1. A 读自己的材料：必须成功
  sudo -u "#$UID_A" -- test -r "$OUT/tasks/<taskA>/description.txt"; echo "own_rc=$? (期望 0)"
  # 2. A 读 B 的 description.txt：必须非零
  sudo -u "#$UID_A" -- test -r "$OUT/tasks/$TASK_B/description.txt"; echo "cross_rc=$? (期望非 0)"
  # 3. A 读 B 的 submit.sh / 列 B 目录：必须失败
  sudo -u "#$UID_A" -- test -r "$OUT/tasks/$TASK_B/submit.sh"; echo "cross_submit_rc=$? (期望非 0)"
  sudo -u "#$UID_A" -- ls "$OUT/tasks/$TASK_B" >/dev/null 2>&1; echo "cross_ls_rc=$? (期望非 0)"
  # 4. 属主/模式断言
  stat -c '%a %u' "$OUT/tasks/$TASK_B"            # 期望 700 + UID_B
  stat -c '%a %u' "$OUT/tasks"                    # 期望 711 + 0
  ```

- **跨 run UID 复用负向测试（R3-B2）**：
  ```bash
  # 两个不同 --out 的新 run，各自第一题拿到的 uid 必须不同
  grep -h '"' /var/lib/cybergym/stage4/uid_registry.json | sort -u
  stat -c '%u' /data/cybergym/stage4/pilot50-ws/* | sort | uniq -d   # 期望无输出
  ```
  旧 run 工作区若仍存在，用新 run 的任意 uid 读旧 run 的 0700 目录必须
  Permission denied（旧 uid 永不再发，命中概率为 0 而非"通常不会"）。

- **wrapper UID 绑定负向测试（R3-M2）**（在一道已完成 prep 的题上直接调 wrapper）：
  ```bash
  C=$OUT/control/<taskA>
  S4_AGENT_UID=30002 S4_RUN_ID=$(cat "$OUT/run_id") \
    bash /opt/cybergym-agent/scripts/run_agent_task.sh <taskA> "$OUT/tasks/<taskA>" "$OUT"
  # 期望 rc=4 + "S4_AGENT_UID=30002 != control agent_uid=..."
  S4_AGENT_UID=0 ... 同上   # 期望 rc=4（uid 0 一律拒绝）
  ```

- **promoter 失败负向测试（R3-M3）**：把某题的 `protected_ledger/<task>.jsonl`
  写入一行坏 JSON（或让工作区账本出现串 run 的 run_id），再跑该题：
  期望 rc=6、行状态 `evidence_error`、**不出现第二段日志**、verify 报
  `coverage_infra`。测完把该行作废重跑。
- `rows_without_cost == 0`；
- `batch_summary.json` 中 `phase_failures == []`、`scoring_pending == []`。

任何一项不满足：**不要扩大规模**，先按第 7 节定位。

## 5. 全量跑批与续跑

```bash
/root/stage4.sh --queue /data/cybergym/stage4/queue.json \
    --out /data/cybergym/stage4/r_engineer --workers 3
```

- 中断后原命令重跑：复用 `<out>/run_id`，只补未到终态的任务；
- 重跑失败题：`--retry-failed`（旧行存档在 `rows/attempts/`；注意 retry 会
  为该题分配**新的**每题 uid——旧 uid 不回收）；
- 只重算分：`/root/score_all.sh <out> <tasks> <ws>`；
- 换一批题/新一轮：必须换 `--out`（或显式 `--run-id`），否则会与旧 run 混在一起。

## 6. 关键环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `S4_AGENT_HOME` | `/opt/cybergym-agent` | 代码根（root 独占，T1 审计对象） |
| `S4_REPO` | `/home/naco/work/cybergym` | gen_task 源码（prep 身份所有） |
| `S4_PREP_USER` | `naco` | 准备身份（gen_task；不得与 agent 同 uid） |
| `S4_AGENT_USER` | `s4agent` | 不可信基线账号（树审计名单成员） |
| `S4_UNTRUSTED_USERS` | `s4agent,naco` | assert_code_tree 拒绝其写访问的账号 |
| `S4_AGENT_UID_BASE` | `30001` | 每题 uid 池起点 |
| `S4_AGENT_UID_SPAN` | `4096` | 池大小（run 内与跨 run 都不复用） |
| `S4_UID_STATE` | `/var/lib/cybergym/stage4/uid_registry.json` | 宿主机级 uid 注册表（root 0700/0600，只增不减） |
| `S4_AGCLI` | `/usr/local/bin/agcli` | agent CLI 绝对路径 |
| `S4_WSROOT` | `<out>-ws` | 工作区根（root:root 0711） |
| `S4_RUN_ID` | `<out>/run_id` | 本轮 id |
| `S4_PASS1_TURNS` | `45` | 第一段 turns |
| `S4_PASS2_TURNS` | `15` | 强制提交段 turns |
| `S4_CHECKS_BUDGET` | `25` | check.py 次数 |
| `S4_SCORE_EXTRA` | 空 | 传给 score_all.py 的额外参数 |
| `S4_SCORE_ALL` | `/root/score_all.sh` | 评分入口 |
| `S4_BACKFILL` | `/root/backfill_fix_side.sh` | fix 侧回填 |

## 7. 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| `assert_code_tree: N violation(s)` | 树内有 go-w/符号链接/非 root 属主/可写祖先 | 看 200 行内清单定位文件；重新 rsync 部署（root 属主 + go-w），**不要**手工放宽权限迁就 |
| `REFUSE: no control-plane binding` | 题目录由 R3 之前的 driver 生成，或 `<out>/control` 被删 | 换 `--out` 重新跑整轮；不要手工补 binding |
| `REFUSE: submit.sh sha256 mismatch` | gen 之后题目录内容被改过 | 删除该题目录（或换 `--task-base`）重新 gen_task |
| `REFUSE: control run_id mismatch` | wrapper 的 `S4_RUN_ID` 与绑定不一致 | 不要手工回放 wrapper；用 `/root/stage4.sh` 正常驱动 |
| `REFUSE: no numeric per-task agent uid` | driver 非 root 或为旧版（未分配 uid） | 确认用 `/root/stage4.sh` 以 root 启动 |
| `REFUSE: S4_AGENT_UID=<n> != control agent_uid=<m>` | 直接调用 wrapper 或环境串了别的 run | 用 driver 启动；不要手工回放 wrapper（控制面 uid 才是权威） |
| `REFUSE: <tasks>/<task> is mode=… uid=… expected 700 owned by …` | 题目录未 hardening（旧版 prep 或手工建） | 删掉该题目录让 driver 重新 gen，或换 `--out` |
| SystemExit `uid registry ... is unreadable/corrupt` | 宿主机级注册表被改/损坏 | 以 root 修复注册表（保持只增不减）；修复前不得继续发号 |
| `agent uid pool exhausted` | 池耗尽或 passwd 里占用过多 | 调大 `S4_AGENT_UID_SPAN` 或上移 `S4_AGENT_UID_BASE` |
| `REFUSE: ledger promotion failed rc=<n>` + 行状态 `evidence_error` | promoter 硬失败（坏行/身份串轮/冲突） | 按安全事件核查该题账本；不要手工改受保护账本，重跑该题 |
| SystemExit `prep user 'naco' does not exist` | `S4_PREP_USER` 指向的账号不存在 | 建账号或改 `S4_PREP_USER` |
| `sudo -u '#<uid>'` 报 unknown user | sudo 版本过旧（不支持 `#uid`） | 升级 sudo，或为池内 uid 批量建无 shell passwd 条目（不推荐） |
| agent 会话里 HOME 指向 /root | sudoers `env_reset` 覆盖了显式 env（罕见） | 检查 `/etc/sudoers` 是否有 `always_set_home`/`env_delete=HOME`；wrapper 已显式传 `HOME=`，正常不应出现 |
| 全部 `no_ledger` | `check.py` 没写账本：旧 ws 里残留旧版 check.py | 确认 `<ws>/check.py` 顶部烘焙了 `LEDGER_PATH = r"<ws>/submissions.jsonl"`（生成器写入，env 重定向无效）；重跑该题 |
| 大面积 `hash_unverified` | 服务器 `poc_hash` 算法/字节范围与 SHA-256 不可比 | **pilot 前必须先做一次受控提交核对哈希语义**；不可比时全部 fail-closed，不会虚增分数 |
| `WARN: chattr +a unsupported` | `$OUT` 所在 FS 不支持 append-only（如 NFS） | 受保护账本被禁用：评分退回工作区账本+哈希门禁，且验收将报 `ledger_missing`（fail-closed）。必须把 `<out>` 放 ext4/xfs 再跑正式 pilot |
| `WARN: ledger promote rc=5` | 工作区账本里 run_id/task_id/agent_id 与 root 提供值不一致（篡改或串轮） | 看 wrapper 日志与该行内容；按安全事件处理，不要手工改受保护账本 |
| `WARN: ledger promote rc=4` | 同 poc_id 不同哈希冲突，已单列 | `protected_ledger/<task>.jsonl.conflicts.jsonl` 有记录；验收会报 `ledger_conflict`（fail-closed） |
| `another stage4 driver holds ...` | 同一 `--out` 已有 driver 在跑（run 级独占锁） | 不要并发两个 driver；确认前一个进程状态后等它结束或清理 |
| `--out is on nfs/...` | 多 worker + 网络文件系统，flock 语义不等价 | 把 `--out` 移到本地盘，或单 worker 并接受告警降级 |
| `task ids ... both map to rows/...` | 队列里两个 task id 在 `:`→`_` 映射后碰撞 | 清理队列（重命名任务或去掉冲突项）后重跑 |
| 全部 `fix_pending` | 没跑/跑失败 backfill | `/root/backfill_fix_side.sh --dry-run` 看待补数量；完成后再评分 |
| 个别 `ledger_mismatch` | 服务器有更晚提交，账本没有 | 看 `<out>/scored/<task>.json` 与 `<out>/protected_ledger/<task>.jsonl`；该题 fail-closed，不要手工改分 |
| 个别 `hash_mismatch` / `ledger_corrupt` | 上传字节与服务器哈希不一致 / 账本中间行被改 | 均按篡改处理：fail-closed 并人工核查该题 |
| `coverage_infra` violation | 队列里存在 `gen_fail`/`worker_error`/`api_error` 行 | 基础设施失败必须 `--retry-failed` 重跑清零，不允许平均掉 |
| `REFUSE: workspace ... already exists` | 复用了旧工作区 | 用 `--fresh`（stage4 默认）或手工 `rm -rf` 该题 ws |
| `REFUSE: cross-task intel` | ws 里残留 intel（含 `reference_intel*`/`ref-intel*` 等改名拼写） | 直接删整个 ws 重跑；不要只删文件后复用 |
| `make_agent_ws failed` | tar/权限/磁盘 | 看 `<out>/logs/<task>.log` 与 ws 生成器 stderr |
| `stage2.sh is LEGACY` / `cleanpilot.sh is LEGACY` | 误用历史入口（worker 子命令同样被拒） | 用 `/root/stage4.sh`；只有复现历史波次才 `S2_LEGACY_OK=1` |

## 8. 回滚

```bash
TS=<部署前的备份后缀>
pkill -f run_agent_task.sh 2>/dev/null || true
rm -rf /opt/cybergym-agent
cp -a /root/stage4-backup-$TS/cybergym-agent /opt/cybergym-agent
cp -a /root/stage4-backup-$TS/score_all.sh /root/score_all.sh
chown -R root:root /opt/cybergym-agent; chmod -R go-w /opt/cybergym-agent
```

stage-4 不修改 stage-3 的历史数据目录；回滚只影响代码入口。回滚到 R3 之前的
备份时，注意旧 `stage4.sh` 不做树审计、旧 wrapper 用 `sudo -u naco` 跑 agent
——那是已知弱隔离形态，不要用于正式跑批。

## 9. 本轮没有覆盖的（上机后必须补验）

以下只能静态实现 + 文档化，**部署后按顺序实测**（结果记入 pilot 复核记录）：

- sudo `'#<uid>'` 语义实测：`sudo -u '#30001' env` 打印完整 env，确认
  HOME/TMPDIR/XDG_*/CLAUDE_CONFIG_DIR 与 wrapper 传入一致、无 root 泄漏变量；
- 跨任务准备目录/工作区负向测试（第 4 节 4c 与 B1 四步，必须证明"自己的能读"
  才排除假通过）；
- **setsid 逃逸（R3-M4 残留）**：`kill_task_tree` 现在会按会话号扫描 `/proc`
  补杀，但一个自己调用 `setsid()` 另开新会话的后代仍会逃出。若正式跑批要求
  "重试前所有 agent 进程必不存在"，需要把每题包进独立 cgroup（systemd
  `systemd-run --scope --slice=stage4-<task>`，结束时 `systemctl stop`），
  本轮未实现；当前实现只能保证进程组 + 原会话被清空并记录残留数；
- umask 077 经 sudo 透传：agent 会话内 `umask` 与新建文件模式抽查；
- 跨任务负向测试：uid A 读/写 uid B 工作区、读 `/root`、读其他任务
  `CLAUDE_CONFIG_DIR` 全部 Permission denied（第 4 节 4c 已列一条，建议 3 对）；
- assert_code_tree 的边界：ACL（`setfacl`）与 bind mount 不在 stat 可见范围，
  在真实挂载布局上跑一次 `namei -l` + 负向 touch 测试（第 2 节）；
- agcli 在 `#<uid>` + 私有 HOME 下能真实连上服务器（跑 1 题 smoke）；
- chattr +a 在 `<out>` 实际文件系统上的行为（第 7 节已列告警分支）；
- docker rootless 隔离的现场验证（沿用 stage-3 `scoring/smoke_isolation.sh`，
  本轮未改，也未在评审机执行）；
- 服务器 `created_at` 的实际精度与同秒并列分布（部署后跑 5 题即可观察
  `rows/scored/*.json` 与 `poc_records`）。
