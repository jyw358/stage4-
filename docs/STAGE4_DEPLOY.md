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
# 基线账号（uid 必须落在池外）。显式指定 -u：R3.4 实测 `useradd` 在
# uid_registry 已有 30001/30002 的机器上会把新账号分到 30002 —— 正好与
# 已发放的任务 uid 撞车（同一 uid 既是被命名的账号又是某题的 agent）。
id s4agent || { useradd -u 4001 -U -m -s /usr/sbin/nologin s4agent 2>/dev/null \
  || useradd -u 4001 -U -M -s /usr/sbin/nologin s4agent; chmod 750 /home/s4agent 2>/dev/null; }
getent passwd s4agent | awk -F: '$3>=30001 && $3<=34096 {print "FAIL: uid in task pool"; exit 1}'
python3.12 --version
setpriv --version                      # R3.2 必需：降权不依赖 passwd 条目
test -f /root/.cybergym_env && echo env-ok
test -S /root/grader.sock && echo grader-ok
docker ps >/dev/null && echo docker-ok
df -h /data | tail -1
```

**降权方式（R3.2 上机结论）**：本机 `sudo 1.9.9` 拒绝未注册的数字 uid
（`sudo -u '#30001'` → `sudo: unknown user #30001`），而每题 uid 故意不是
passwd 账号，因此 sudo 无法启动任何 agent 会话。wrapper 现用
`setpriv --reuid --regid --clear-groups`（util-linux，无需 passwd 条目）
并以 `env -i` 传入显式环境。`setpriv` 缺失时 wrapper 直接 `REFUSE`（rc=4），
不会回退到 root。

stage-4 依赖与 stage-3 相同：rootless docker、agcli、`/home/naco/work/cybergym`
（gen_task 源码与 `.env`，保持 naco 属主——root 不执行它）、`/root/.cybergym_env`
（只有 root 能读的 `CYBERGYM_API_KEY`）。

**agcli 必须部署到 root 可控的全局路径**（不再放 `/home/naco/bin`：本机
`/home/naco` 是 0750，每题 uid 连遍历都不行），且**不得依赖 `$HOME`**：

```bash
install -o root -g root -m 0755 <you>/agcli /usr/local/bin/agcli
setpriv --reuid=30001 --regid=30001 --clear-groups \
  env -i PATH=/usr/local/bin:/usr/bin:/bin /usr/local/bin/agcli --version
```

R3.2 上机结论：`/home/naco/bin/agcli` 在脚本里把 `PATH="$HOME/opt/node/bin:…"`
并 `. "$HOME/work/cybergym-agent/.glm6.env"`，而 stage-4 给每题的是**私有
HOME**，因此原脚本在降权会话里必然找不到 `claude` 与 profile。部署版 agcli
须满足：node/claude 用绝对路径（如 `/opt/cybergym-agent-bin/node/bin`），
凭据从**进程环境**读取 `GLM_API_KEY/GLM_BASE_URL/GLM_MODEL`，缺失即拒绝（不要
静默落到默认后端）。

模型凭据由 root 侧注入（`env -i` 会清空环境，agent 不能自己去读密钥文件）：

```bash
install -o root -g root -m 0600 <profile.env> /etc/cybergym-agent/agent.env
# 只允许 GLM_API_KEY / GLM_BASE_URL / GLM_MODEL / AGCLI_CTX 四个键被转发
export S4_AGENT_ENV_FILE=/etc/cybergym-agent/agent.env
```

wrapper 以 root 读取该文件，仅把上述四个变量放进会话环境；agent 得到的是
**值**，拿不到文件本身。文件非 root 属主时 wrapper 直接 `REFUSE`（rc=4）。

### GLM key 池（stage-3 轮换同款，正式跑推荐；复审 r2 加固后语义）

stage-3 的 5 个可用 GLM key 引入 stage-4，按同样的策略轮换：固定优先级
`senary > quaternary > quinary > primary > secondary`（tertiary 沿用 stage-3
的 owner-retired，排除），probe 门控（非 200 不选），sticky（最优未变且
active 与源文件逐字节一致才不重写）。与 stage-3 的差别：

- **信任模型**：池文件、active.env、health.cache 一律 `root:root 0600`；
  使用前校验整条路径——普通文件、非 symlink、root 属主、无 group/other
  权限位，且每个祖先目录 root 属主且不可被 group/other 写。
- **凭据文件按数据解析，绝不 source**：只接受白名单键
  `GLM_API_KEY/GLM_BASE_URL/GLM_MODEL/AGCLI_CTX`，值域
  `[A-Za-z0-9._:/+=-]`（可选成对引号）；额外键、命令替换、不可解析行
  一律拒绝（选择器 rc=1、该题 REFUSE）。
- **三态模式**：池存在 → 选择器必须成功，active.env 为权威（与
  `S4_AGENT_ENV_FILE` 是否设置无关）；池不存在 → 单 key profile 必须设置
  且通过信任检查；两者皆无 → `model_credential_error` REFUSE，绝不无凭据
  启动。
- **写失败响亮**：active/标记/缓存每步写入都检查，写后复验与源文件一致
  （`cmp`）；sticky 前先验证一致性，不一致则原子修复并记日志。
- **预算策略**：全部 key 失败时，仅当**全部为 429**（限流）才保留上一
  active；任何非 429（认证/传输/5xx）且无健康 key → REFUSE 不烧预算。
- **探针缓存**：健康状态缓存 `health.cache`（`S4_KEY_POOL_TTL` 秒，默认
  120；0 = 禁用缓存强制探针；上限 3600，`S4_KEY_POOL_TIMEOUT` 1-120s，
  非法值启动即拒）。缓存逐键绑定 `glm-*.env` 的 sha256 指纹——TTL 内
  轮换任何 key 文件即自动失效重探，陈旧 200 不可能比它描述的配置活得久；
  缓存存在（含 symlink）但不可信 → 拒绝运行，只有缺失/空才视为无缓存。
- **状态一致性**：`glm_active` 标记读取前过完整信任检查（symlink/异常
  属主即拒），更新走 `atomic_write_text`（原子 + 回读校验）；全 429 保
  active 仅当 marker→源文件→active 内容三者一致，否则拒绝启动（无健康
  key 时绝不"顺手修复"）。
- **选择审计**：每次选择写 `last_selection`（`healthy:<name>` /
  `throttled_keep:<name>`，root 0600），任务日志
  `[keypool] active=<name> selection=<status>`——全 429 例外永远不被计为
  健康选择。
- 决策与审计：`/var/log/s4-keypool.log`（全 429 例外显式标注 NOT
  probe-verified）。

```bash
install -d -o root -g root -m 700 /etc/cybergym-agent/keys
install -o root -g root -m 600 <glm6.env> /etc/cybergym-agent/keys/glm-senary.env
install -o root -g root -m 600 <glm4.env> /etc/cybergym-agent/keys/glm-quaternary.env
install -o root -g root -m 600 <glm5.env> /etc/cybergym-agent/keys/glm-quinary.env
install -o root -g root -m 600 <glm2.env> /etc/cybergym-agent/keys/glm-primary.env
install -o root -g root -m 600 <glm.env>  /etc/cybergym-agent/keys/glm-secondary.env
install -o root -g root -m 700 key_pool_s4.sh /root/key_pool_s4.sh
# 池文件格式（严格）：[export ]KEY=VALUE，仅上述 4 键，值域见上
```

`/etc/cybergym-agent/keys/` 存在即启用（`S4_KEY_POOL_DIR`/`S4_KEY_POOL_SCRIPT`
可覆盖，覆盖路径同样过信任检查）。轮换策略基线对照：
`docs/reference/stage3_key_pool.sh`（stage-3 部署版原样 vendor，
sha256 `309d1cfa…`；stage-3 公开仓库无 `root_code/`，故以此副本为准）。

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

# 祖先链逐级 root 所有、他人不可写，且**他人可遍历**（R3.3）
# <out> 必须在 root 所有 + go-w + o+x 的目录下（0755 或 0711）：
# /root（0700）与 naco 属主的 /data/cybergym 都会被 driver 直接拒绝，
# 因为 prep 身份要写 <out>/tasks/<task>、每题 uid 要进 <out>-ws/<task>。
namei -l /opt/cybergym-agent/scripts/stage4.py
namei -l /var/lib/cybergym/runs/r_engineer
python3 - <<'PY'
import sys; sys.path.insert(0, "/opt/cybergym-agent")
from scripts.stage4 import ancestors_trusted
from pathlib import Path
print(ancestors_trusted(Path("/var/lib/cybergym/runs/r_engineer")))
PY
# 反例：根目录 0700 / naco 属主目录都必须被拒（返回 False + 原因）
python3 - <<'PY'
import sys; sys.path.insert(0, "/opt/cybergym-agent")
from scripts.stage4 import ancestors_trusted
from pathlib import Path
for p in ("/root/s4run", "/data/cybergym/stage4"):
    print(p, ancestors_trusted(Path(p)))
PY

# s4agent 不得有任何 sudo 权限（期望 "is not allowed to run sudo"）
sudo -l -U s4agent

# 每题数字 uid 的降权可用（R3.2：setpriv，不依赖 passwd 条目）
setpriv --reuid=30001 --regid=30001 --clear-groups id \
  && ! setpriv --reuid=30001 --regid=30001 --clear-groups ls /root \
  && echo setpriv-uid-ok
# 反例留档：本机 sudo 不支持未注册 uid
sudo -u '#30001' id 2>&1 | head -1     # 期望 "unknown user #30001"，说明必须用 setpriv

# 宿主机级 uid 注册表位置可写、只增不减（B2）
mkdir -p /var/lib/cybergym/stage4 && chown root:root /var/lib/cybergym/stage4 \
  && chmod 700 /var/lib/cybergym/stage4
# 跑批目录另放一处（注册表目录是 0700，不能同时当 --out）
mkdir -p /var/lib/cybergym/runs && chown root:root /var/lib/cybergym/runs \
  && chmod 755 /var/lib/cybergym/runs
python3 - <<'PY'
import sys; sys.path.insert(0, "/opt/cybergym-agent")
from scripts import stage4
print("uid pool floor:", stage4.AGENT_UID_BASE, "registry:", stage4.UID_REGISTRY)
PY

# 密钥只测属主/模式/可读性，绝不打印内容
stat -c '%U:%G %a' /root/.cybergym_env        # 期望 root:root 600
setpriv --reuid=30001 --regid=30001 --clear-groups test -r /root/.cybergym_env \
  && echo "KEY LEAK" || echo key-protected
stat -c '%U:%G %a' /home/naco/work/cybergym/.env   # naco 可读，他人不可读

# 代码自检
cd /opt/cybergym-agent
python3 - <<'PY'
from plugins import evidence, results_store
print("evidence.utcnow", evidence.utcnow())
print("final-only predicate:", evidence.official(1, 0), evidence.official(0, 0))
PY
python3 -m unittest discover -s tests     # 149 项（含 R3 / R3.1 / R3.2 / R3.3 回归）
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
  提交声明**，评分权威在服务器 final 记录 + 哈希门禁）；且
  `protected_ledger/` 为 root:root 0700、`*.jsonl` 为 0600（验收的
  `ledger_open`，任何 group/other 位即违规）；
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
  setpriv --reuid=$UID_A --regid=$UID_A --clear-groups cat /data/cybergym/stage4/pilot50-ws/<taskB>/CLAUDE.md
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
  setpriv --reuid="$UID_A" --regid="$UID_A" --clear-groups test -r "$OUT/tasks/<taskA>/description.txt"; echo "own_rc=$? (期望 0)"
  # 2. A 读 B 的 description.txt：必须非零
  setpriv --reuid="$UID_A" --regid="$UID_A" --clear-groups test -r "$OUT/tasks/$TASK_B/description.txt"; echo "cross_rc=$? (期望非 0)"
  # 3. A 读 B 的 submit.sh / 列 B 目录：必须失败
  setpriv --reuid="$UID_A" --regid="$UID_A" --clear-groups test -r "$OUT/tasks/$TASK_B/submit.sh"; echo "cross_submit_rc=$? (期望非 0)"
  setpriv --reuid="$UID_A" --regid="$UID_A" --clear-groups ls "$OUT/tasks/$TASK_B" >/dev/null 2>&1; echo "cross_ls_rc=$? (期望非 0)"
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
| `S4_AGCLI` | `/usr/local/bin/agcli` | agent CLI 绝对路径（必须每题 uid 可执行且不依赖 `$HOME`） |
| `S4_AGENT_ENV_FILE` | 空（关闭） | root 独占的模型凭据文件；仅 `GLM_API_KEY/GLM_BASE_URL/GLM_MODEL/AGCLI_CTX` 被转发进会话 |
| `S4_WSROOT` | `<out>-ws` | 工作区根（root:root 0711） |
| `S4_RUN_ID` | `<out>/run_id` | 本轮 id |
| `S4_PASS1_TURNS` | `45` | 第一段 turns |
| `S4_PASS2_TURNS` | `15` | 强制提交段 turns |
| `S4_CHECKS_BUDGET` | `25` | check.py 次数 |
| `S4_SCORE_EXTRA` | 空 | 传给 score_all.py 的额外参数 |
| `S4_SCORE_ALL` | `/root/score_all.sh` | 评分入口 |
| `S4_BACKFILL` | `/root/backfill_fix_side.sh` | fix 侧回填 |
| `S4_ALLOW_UNPROTECTED` | 空（关闭） | 仅开发：`chattr` 无法建立 protected ledger 时允许降级跑（verify 仍 fail-closed，行不得进正式成绩）。R3.4 起**正式入口直接拒绝**该变量（`/root/stage4.sh` exit 4、`stage4.py` SystemExit），只能用 `run_agent_task.sh` 单点调试 |

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
| `sudo -u '#<uid>'` 报 unknown user | **本机正常现象**：sudo 1.9.9 只接受 passwd 里存在的数字 uid，而每题 uid 故意不是账号 | 不要改 sudo，也不要批量建账号；wrapper 已改用 `setpriv` |
| `REFUSE: setpriv (util-linux) is required` | 目标机缺 util-linux/setpriv | 安装 util-linux；wrapper 不会静默回退到 root |
| agent 会话里 HOME 指向 /root | `env -i` 的显式变量被外层覆盖（异常） | wrapper 用 `env -i` + 显式 `HOME=`，正常不应出现；检查是否有人改回 sudo/`--preserve-env` |
| 全部 `no_ledger` | `check.py` 没写账本：旧 ws 里残留旧版 check.py | 确认 `<ws>/check.py` 顶部烘焙了 `LEDGER_PATH = r"<ws>/submissions.jsonl"`（生成器写入，env 重定向无效）；重跑该题 |
| 大面积 `hash_unverified` | 服务器 `poc_hash` 算法/字节范围与 SHA-256 不可比 | **pilot 前必须先做一次受控提交核对哈希语义**；不可比时全部 fail-closed，不会虚增分数 |
| `REFUSE: chattr +a unsupported on ...` + 行状态 `evidence_setup_error`（rc=8） | `$OUT` 所在 FS 不支持 append-only（如 NFS）；R3.3-M2 起正式模式在**模型调用前**拒绝 | 把 `<out>` 放 ext4/xfs 再跑；仅开发可用 `S4_ALLOW_UNPROTECTED=1` 降级（verify 仍 fail-closed） |
| `REFUSE: chattr +a did NOT take effect on ...` + 行状态 `evidence_setup_error`（rc=8） | `chattr` 返回成功但 `lsattr -d` 看不到 `a`（属性/路径异常） | 同上用 ext4/xfs；修属性后重跑该题（resume 会重跑，该状态不进 TERMINAL） |
| `REFUSE: existing protected ledger ... does not conform` + 行状态 `evidence_setup_error`（rc=8） | 重入时已有 ledger 的属主/模式/属性不符（含符号链接）；R3.3-M1 起不改写证据去迎合检查 | 按安全事件核查该题 ledger 为何被动过；确认后以 root 移除该文件再重跑 |
| `REFUSE: agent session did not start (setpriv uid=...)` | setpriv/环境注入失败，会话根本没起来（rc=7，行 `agent_launch_error`） | R3.2-M1：不进入第二段、不产生"正常完成"；按基础设施故障排查（`setpriv --version`、HOME/TMPDIR 可写、`--out` 祖先可遍历） |
| `REFUSE: ledger promotion failed rc=5` | 工作区账本里 run_id/task_id/agent_id 与 root 提供值不一致（篡改或串轮） | wrapper 以 rc=6 退出、行状态 `evidence_error`，不进入第二段；按安全事件核查，不要手工改受保护账本 |
| `REFUSE: ledger promotion failed rc=4` | 同 poc_id 不同哈希冲突，已单列 | `protected_ledger/<task>.jsonl.conflicts.jsonl` 有记录；验收报 `ledger_conflict`（fail-closed），wrapper 不再继续 |
| `another stage4 driver holds ...` | 同一 `--out` 已有 driver 在跑（run 级独占锁） | 不要并发两个 driver；确认前一个进程状态后等它结束或清理 |
| `--out is on nfs/...` | 多 worker + 网络文件系统，flock 语义不等价 | 把 `--out` 移到本地盘，或单 worker 并接受告警降级 |
| `task ids ... both map to rows/...` | 队列里两个 task id 在 `:`→`_` 映射后碰撞 | 清理队列（重命名任务或去掉冲突项）后重跑 |
| 全部 `fix_pending` | 没跑/跑失败 backfill | `/root/backfill_fix_side.sh --dry-run` 看待补数量；完成后再评分 |
| 个别 `ledger_mismatch` | 服务器有更晚提交，账本没有 | 看 `<out>/scored/<task>.json` 与 `<out>/protected_ledger/<task>.jsonl`；该题 fail-closed，不要手工改分 |
| 个别 `hash_mismatch` / `ledger_corrupt` | 上传字节与服务器哈希不一致 / 账本中间行被改 | 均按篡改处理：fail-closed 并人工核查该题 |
| `coverage_infra` violation | 队列里存在 `gen_fail`/`worker_error`/`api_error`/`evidence_error`/`agent_launch_error` 行 | 基础设施失败必须 `--retry-failed` 重跑清零，不允许平均掉 |
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

## 9. 上机负向测试记录（2026-11-04，内网 192.168.49.13）

在测试机上以 root 直接跑同一 commit 的树（部署副本 `/opt/cybergym-agent`，root:root
0755/0644）加负测。计数口径
（按 R3.2-N1 澄清）：**9 个测试场景、25 条断言**，全部 PASS；每个实际命令的
返回码随脚本输出留档（`/root/neg_run.log`、`/root/neg3_run.log`）：

| 场景 | 断言 | 结果 |
|---|---|---|
| T1（3） | uid 30001 读自己题目录 OK；读兄弟题目录 Permission denied；`tasks` 根 0711 不可列名 | PASS |
| T2（3） | 第二个 `--out` 首题拿到不同 uid；宿主机注册表损坏 → SystemExit；per-run `uid_alloc.json` 损坏 → SystemExit | PASS |
| T3（2） | `S4_AGENT_UID` ≠ 控制面 → rc=4；一致时通过 uid 门禁并进入会话 | PASS |
| T4（1） | 题目录在 `<out>/tasks` 之外 → rc=4 | PASS |
| T5（1） | 控制面 `agent_id` 为空 → rc=4 | PASS |
| T6（4） | promoter rc=5 → wrapper rc=6；失败不是 WARN；不进第二段；行 `evidence_error` | PASS |
| T8（4） | `protected_ledger` 目录 700 + 文件 600；其他 uid 读文件、列目录均 Permission denied | PASS |
| T9（3） | 真实 ledger 有 `a` 属性（`lsattr`）；wrapper 日志声明 verified；无虚假声明 | PASS |
| T10（4） | setpriv 启动失败 → rc=7；独立 REFUSE 文案；行 `agent_launch_error`；不进第二段 | PASS |

单测：`python3 -m unittest discover -s tests` → **149 项 OK**（Linux root 下 1 项
skip 为 Windows-only 用例）。

### 9.1 测试机暴露的问题（已修）

1. **`sudo -u '#<uid>'` 无法启动会话**：本机 sudo 1.9.9 对未注册数字 uid 报
   `unknown user`，而每题 uid 故意不是 passwd 账号 —— R3 用 sudo 降权的假设
   在本机不成立，会令所有题零执行。已改为 `setpriv --reuid/--regid/
   --clear-groups`，并把环境改为 `env -i` 显式注入（setpriv 不重置环境，
   否则 root 环境会泄漏）。缺少 setpriv 时 wrapper 拒跑而非回退 root。
2. **`evidence_error` 被改写成 `worker_error`**：`record_task_row.py` 在
   `--stage error` 时无条件写 `worker_error`，吞掉了 promoter 失败的具体状态。
   已改为优先采用调用方显式状态。

### 9.2 单题真实链路 smoke 暴露的问题（R3.3，已修）

按第四轮复审建议跑的 1 题真实链路（`gen_task` → `setpriv` 会话 → 真实模型
调用 → `promote_ledger` → `score_all` → `verify_stage4`）。以下四处**只有
真机才会暴露**：

| 编号 | 现象 | 原因 | 修复 |
|---|---|---|---|
| S-1 | 会话根本没起来：找不到 `claude` / 无法认证 | `/home/naco` 是 0750，每题 uid 不可遍历；`agcli` 脚本又用 `$HOME/opt/node/bin` 定位 node、`. $HOME/work/cybergym-agent/.glm6.env` 取凭据，而 stage-4 给的是**私有 HOME** | 部署侧把 node/claude 与 agcli 装到 root 属主全局路径（本机 `/opt/cybergym-agent-bin`、`/usr/local/bin/agcli`），部署版 agcli 不依赖 `$HOME`；凭据改为从进程环境读取，缺失即拒绝（第 1 节） |
| S-2 | 即使 agcli 可执行也拿不到模型凭据 | `env -i` 清空环境是隔离要求，但同时清掉了 `GLM_*` | 新增 `S4_AGENT_ENV_FILE`（root:root 0600）：wrapper 以 root 读取，只把 `GLM_API_KEY/GLM_BASE_URL/GLM_MODEL/AGCLI_CTX` 注入会话环境；非 root 属主直接 `REFUSE`（rc=4） |
| S-3 | `--out` 在 `/root/...` 时 `gen_task` 报 `PermissionError`，在 naco 属主目录时被 driver 拒绝 | `--out` 祖先链只校验"root 属主 + 无 group/other 写"，没校验**他人可遍历**；`/root` 0700 能通过却被 prep/agent 无法进入 | `ancestors_trusted()` 增加 `o+x` 校验（0700 的 `/root`、naco 属主目录均被拒并说明原因）；`control_dir_trusted` 的链校验改从 `<out>` 起算（`<out>/control` 是 0700 的 root 控制面，不适用该规则）；文档改为 `--out /var/lib/cybergym/runs/<name>` |
| S-4 | 真实提交被 promoter 判为身份不一致（rc=5 → `evidence_error`） | 队列标签是 `arvo:10628`，而 `check.py`/`CybergymRunner` 写入工作区账本的是 submit.sh 元数据里的**服务器内部 id**（如 `1e87eeeb8d69`），promoter 只认标签 | `promote_ledger.py` 新增 `--task-dir`：解析 `<tdir>/submit.sh` 元数据得到规范 id，接受"标签或规范 id"，并优先写规范 id（与 check.py 一致） |
| S-5 | 会话跑完 21 轮、真实提交，但 `checks=0 / submissions=0`，行仍是"无提交" | `check.py` 内嵌生成时的 AGENT HOME，用于 `import plugins.cybergym_runner`；本机树在 `/root/s4test`（`/root` 0700），每题 uid 读不到 → `ModuleNotFoundError`，白烧一次模型成本 | 部署树改 `/opt/cybergym-agent`（root:root 0755/0644）；wrapper 启动前**以 agent uid 探针** `test -r $AG/plugins/cybergym_runner.py`，并拒绝 group/other 可写的运行时树（否则会话可改写提交客户端），失败记 `agent_launch_error` + rc=7 |
| S-7 | 启动证据在 agent 工作区内消失，成功的 run 被判 `agent_launch_error` | 原实现把 `id -u` 写进 `$WS/.session_uid`：会话开始 14:10:40、agent 自己 14:10:44 的 `ls` 还看得到（6 字节），会话 14:14:01 正常结束后文件已不存在（删除者未定位） | 启动记录改为 root 在 `$LOGDIR/<task>.launch` 打开文件、沙箱内 shell 写**已打开的 fd**（`sh -c 'id -u >&9; 9>&-; exec "$@"'`），exec 前关闭 fd；记录必须等于 `AGENT_UID`，agent 无删改路径 |
| S-8 | 已提交但 `score_status=None`、`final.*` 空，verify 判"未评分" | 驱动默认 `S4_SCORE_ALL=/root/score_all.sh` 是 stage-3 遗留 wrapper，只写 `results.json` 的 `official_success` | 默认改用部署自带 `scripts/score_all.py`；`S4_SCORE_ALL` 仍可覆盖（按 `<out> <task_base> <ws_root>` 位置参数调用） |
| S-9 | 新 run 的错误行带着上一次 run 的 `turns/cost/result` | `record_task_row` 只在内存里按 `run_id` 重置，`results_store.merge_row` 又把磁盘上的旧行合并回来 | `merge_row` 检测 `run_id` 变化时丢弃磁盘旧行；新增回归测试 |
| S-10 | 所有真实记录被判 `timestamp_invalid`，任何任务都无法评分 | `/query-poc` 的 `created_at` 是**无时区**的 ISO（`2026-11-04T14:32:33.742270`），而账本同一提交为 `14:32:34.936Z`——服务器时钟是 UTC，只是序列化丢了偏移量 | 在 HTTP 边界（`query_poc`）只对"服务器原样无时区 ISO"补 `Z`；`evidence.parse_ts` 仍拒绝 naive 时间戳（客户端账本的标准未被放宽）；新增 4 项单测 |

本轮 smoke 结论（2026-11-04 14:30–14:34Z，`arvo:10628`，uid 30005）：
`status=submitted`、`subs=1 checks=1 turns=15`、`cost=$0.4159`（整轮 `$0.42`）、
`protected_ledger` 目录 700 / 文件 600 / `lsattr` 含 `a`、promoter `+1 entry`、
`score_all` → `official_success=1/1`、`verify_stage4` → `violations=0 warnings=0`。
启动记录 `$LOGDIR/arvo_10628.launch` = `root:root 0600` 内容 `30005`。


`--out` 的 `o+x` 校验同时解释了 R3.2 期间 `/var/tmp/s4neg` 变 700 的原因：
`allocate_agent_uid` 早前无条件把注册表**父目录**改成 0700，而负测把注册表放
在 scratch 根下，等于把整个夹具锁死（T6 因 uid 无法遍历而误报
`agent_launch_error`）。已改为**只在自建父目录时**才 chmod/chown；注册表文件
本身仍 0600。

### 9.3 R3.3 复审两个 P1（2026-11-05，WSL2 root / ext4）

复审确认 R3.2 三个修复静态 PASS 后新提两项，均已修复并做了 wrapper 级验证
（测试机 192.168.49.13 当日不可达；WSL2 为相同内核语义）：

| 编号 | 问题 | 修复 | 验证 |
|---|---|---|---|
| R3.3-M1 | 已有 append-only ledger 重入：`chown`/`chmod` 对带 `a` 属性的文件返回 **EPERM**（`setattr_prepare` 原语实测），旧初始化无条件重复属主/模式变更是脆弱路径 | 重入先核验（常规文件 + `root:root` + `0600` + `lsattr` 含 `a`）→ 符合**原样复用**、不符一律 `evidence_setup_error` + rc=8 拒绝，不改写证据 | 场景 B/C：重入 rc=0 且无 EPERM 噪声；丢失 `a` 的旧 ledger → rc=8 且 agcli 未被调用 |
| R3.3-M2 | `chattr` 不可用时只 WARN 降级，Agent 照常烧模型预算，验收才 fail-closed | 正式模式把失败**前移到模型调用之前**：`evidence_setup_error` + rc=8；`S4_ALLOW_UNPROTECTED=1` 仅保留开发降级 | 场景 D/E：chattr 注入失败时正式模式 rc=8、agcli 零调用；开发开关下 rc=0 且 WARN 留痕 |

新状态 `evidence_setup_error` 已加入 `verify_stage4.INFRA_FAILURE`（正式 pilot
出现即违规），不进 `results_store.TERMINAL`（主机修复后 resume 直接重跑）。
同段顺带修复：`AGENT_ID` 赋值前移到所有 fail-closed 记行路径之前（`set -u`
下原顺序会以 unbound variable 退出、丢行）。

wrapper 级负测合计 5 场景 / 19 断言，全 PASS；原语实测（root, ext4）：
`chown`/`chmod` 于 `+a` 文件 → EPERM，`chattr +a` 幂等 rc=0，追加允许、覆写拒绝。

### 9.4 R3.4 复审两项加固（2026-11-05，真机 nacocom）

复审确认 §9.3 两项 P1 静态 PASS，另提两项加固，均已实施并在真机验证：

| 项 | 修复 | 验证 |
|---|---|---|
| 开发降级开关可被误用 | 正式入口启动即拒 `S4_ALLOW_UNPROTECTED=1`：`/root/stage4.sh`（root 校验后，exit 4）与 `scripts/stage4.py`（参数解析后，SystemExit）各一处守卫。开发降级只能直接调 `run_agent_task.sh` | `/root/stage4.sh` + 该变量 → `REFUSE ... rc=4`（未触及 driver）；`stage4.py` 直调 → `REFUSE ... rc=1`；不设该变量 → 通过守卫进入树审计（控制组） |
| ledger 初始化缺逐步错误处理（`set -u` 无 `-e`） | 目录 `mkdir/chown/chmod`、probe 创建、受保护文件创建、`chown/chmod` 全部独立检查 → 失败即 `evidence_setup_error` + rc=8 并清理半成品；probe/未封存 ledger 清理失败 → 显式 WARN（不静默） | 注入 `chmod` 失败（场景 F）：rc=8、行 `evidence_setup_error`、agcli 零调用、消息 `cannot chmod`（4/4）；A–E 回归仍 19/19 |

部署现状：新代码在 `/opt/cybergym-agent-r33`（原 `/opt/cybergym-agent` 保持
原版不动）；root 侧入口 `/root/stage4.sh` + `/root/assert_code_tree.py` 已按
仓库 `root_code/` 同名文件安装（0755 / 0644，root:root）。单测 154 OK
（真机 skip 1）。

### 9.5 运行时验收三项：跨 UID 写入 / agcli 状态互访 / setsid 逃逸

2026-11-05 真机 `nacocom`（ext4），`/tmp/s4acc` 隔离夹具，**20/20 断言全过**。
两个独立 uid（30011 / 30012）分别持有自己的 workspace 与 prepared task dir
（均 0700，父目录 0711），以 `setpriv --reuid` 切换身份实测：

| 组 | 检查 | 结果 |
|---|---|---|
| G 跨 UID（30011 → 30012） | 读他题 workspace 文件 / `ls` 他题 ws / **创建** / 追加 / 删除 / `chmod` 他题目录 / 读他题 prepared input / 写他题 task dir —— 全部 denied；自己的 ws 可写（正向对照） | 9/9 |
| H agcli 状态 | 读他题 `.claude-config/state.json`、`ls` 该目录、**覆写**状态文件（会话劫持）均 denied；`/root/.cybergym_env` 不可读、`/root/grader.sock` 不可达；自己的状态可读（正向对照） | 6/6 |
| I setsid 逃逸 | 见下 | 3/3 |
| 收尾 | 两个任务 uid 名下无存活进程 | 2/2 |

**I 的关键证据（对照组）**：当 driver 不传 token 时，`setsid` 出去的后代在
`kill_task_tree` 之后**仍然存活**，而按会话号扫描仍返回 `residual=0` ——
即旧实现是**静默漏杀**，不是"记录并告警"。修复后（per-attempt token 注入
会话环境，root 扫 `/proc/*/environ` 精确匹配）：

```
control (no token): escaped pid survives   [residual=0 survivors=[3320259]]
token sweep sees escaped descendant         [before=[3320288,3320289,3320290] after=[] residual=0]
no task process survives                    [residual=0 after=[]]
```

配套代码：`stage4.token_members()` / `sweep_token()`（`/proc/*/environ` 全扫，
最多 3 轮 SIGKILL）、`kill_task_tree(proc, token)` 在会话清扫之后再扫 token；
wrapper 的 `env -i` 白名单新增 `S4_TASK_TOKEN`。**残留不为 0 时**驱动写
`process_residual_error`（故意**不在** `EXECUTION_FAILURE_STATUSES`，`--retry-failed`
不会碰它，必须人工清理主机后才能重跑——即"明确限制重试条件"），并已加入
`verify_stage4.INFRA_FAILURE`。

对抗性残余：后代若在 `exec` 时清空环境变量仍可逃出，需每题独立 cgroup 才
能彻底收口（§10）。


R3.4 上机另修两处环境问题（非代码）：`useradd s4agent` 在该机被分到
**uid 30002**（uid_registry 已把 30002 发给 `r_smoke/arvo_10628`，命名账号
与任务 uid 撞车）→ 改为显式 **uid 4001**（池外）；同时清掉负测遗留的
`s4neg4u`（uid 30001 未真正删除，见第 1 节命令）。`assert_code_tree` 现输出
`root-clean (s4agent,naco refused write access)`，无缺号 WARN。

### 9.6 R3.5 每题独立 cgroup 与三态进程清理（2026-11-05，真机 nacocom）

R3.4 复审 B1/M1 的最小修法（"每任务独立 cgroup，由可信控制进程终止整个
cgroup，并确认没有存活任务"）已落地，并做构造性负测。部署目录
`/opt/cybergym-agent-r33`，kernel 5.15，`stat -fc %T /sys/fs/cgroup` =
`cgroup2fs`：

| 组 | 构造 | 结果 |
|---|---|---|
| J-A 机制 | 后代 `setsid` + `env -i`（无 token） | token 扫、会话扫都看不到该 pid；`cgroup.kill` 后 `residual=0 complete=True`，pid 已不存在（6/6） |
| J-B 全链路 | `run_one`（stub wrapper）+ `--task-timeout 2` | rc=124、逃逸后代被杀、日志 `[kill] method=cgroup+session+token residual=0 complete=True`、`/sys/fs/cgroup/stage4-*` 无遗留、`select_todo --retry-failed` 允许重试（6/6） |
| J-C 不可探测 | 假 cgroup（`cgroup.procs` 不可读） | 行 `process_inspection_incomplete`、`--retry-failed` 不重试、逃逸进程确实存活（"未知"≠"已清空"）（4/4） |
| 启动门禁 | `S4_CGROUP_ROOT=/tmp/nope python3 scripts/stage4.py --queue /tmp/noqueue.json` | `REFUSE: per-task cgroup v2 is unavailable ...`；默认 root 下同一命令前进到 `queue not found` |
| NIT F | `$OUT/protected_ledger/.probe.777`（`chattr +a`）+ 无 ledger | rc=0；`WARN: removed stale probe ...`；新 ledger `lsattr` 含 `a`（6/6） |
| NIT G | 注入 workspace 的 `chmod` 失败 | rc=10、行 `workspace_setup_error`、agcli 零调用、`REFUSE: workspace setup failed`（4/4） |

**cgroup 可用性判定**：`cgroup.kill` 在 **root cgroup 中不存在**（root 不可杀），
所以 `cgroup_v2_usable()` 必须先创建子探针再检查 `<probe>/cgroup.kill`；直接
查 `/sys/fs/cgroup/cgroup.kill` 会把所有可用主机误判为不可用。若宿主机把
`/sys/fs/cgroup` 交给 systemd 统一管理，可用 `S4_CGROUP_ROOT` 指向一个
delegated（`Delegate=yes`）的可写子树，driver 只在该子树下建/删子 cgroup。

单测 163 OK（真机 root skip 1）；§9.3 的 A–E ledger 回归 19/19 仍全过。

## 10. 尚未覆盖的（上机后仍需补验）

以下只能静态实现 + 文档化，**部署后按顺序实测**（结果记入 pilot 复核记录）。
R3.2 一轮已消除四项（chattr +a 实测、protected ledger 属主/权限、agcli 在
setpriv + 私有 HOME 下的真实调用、umask 077 继承——见 §9、§9.2），R3.3 的
单题真实 smoke 又闭环了"真实提交 → promoter 封存 → 评分 → 验收"（§9.2 结尾），
R3.3 复审再闭环**已有 ledger 重入**与 **chattr 失败前移 fail-closed**（§9.3），
R3.4 复审闭环**正式入口拒绝开发降级开关**与 **ledger 初始化逐步错误处理**
（§9.4），并在真机跑完**跨 UID 写入 / agcli 状态互访 / setsid 逃逸**三项
运行时验收（§9.5，20/20）；R3.5 闭环**每题独立 cgroup + 三态进程清理**
（§9.6，B1/M1 的对抗性后代与"未知≠已清空"）；余下：

- assert_code_tree 的边界：ACL（`setfacl`）与 bind mount 不在 stat 可见范围，
  在真实挂载布局上跑一次 `namei -l` + 负向 touch 测试（第 2 节）；
- docker rootless 隔离的现场验证（沿用 stage-3 `scoring/smoke_isolation.sh`，
  本轮未改，也未在评审机执行）；
- cgroup 的**真实任务**观测：§9.6 已在真机构造性验证（`setsid` + 清环境 +
  超时 + 重试），仍要在 pilot 里看到一次真实超时的
  `[kill] ... residual=0 complete=True` 且 `/sys/fs/cgroup` 无遗留；若宿主机
  由 systemd 统一管理 cgroup，需确认 driver 建的子 cgroup 不被上层回收
  （必要时 `Delegate=yes` 或把 `S4_CGROUP_ROOT` 指向可写子树）；
- 服务器 `created_at` 已确认是**无时区 UTC、6 位微秒**（stage-4 在 HTTP 边界补
  `Z`，见 §9.2 S-10）；仍需在跑 5 题后观察同秒并列分布（并列会使
  `select_final` 判 `ambiguous`）。根治办法是服务端序列化带 `Z`，本仓库不改
  服务器。
