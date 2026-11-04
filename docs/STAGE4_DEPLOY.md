# Stage-4 部署与跑批手册

本手册按跑批主机的真实路径编写（`/home/naco/work/...`、`/data/cybergym/...`）。
在跑批主机**以 root 执行**；`naco` 只跑 gen_task / 工作区生成 / agent。

## 1. 部署前检查

```bash
id naco
python3.12 --version
test -f /root/.cybergym_env && echo env-ok
test -S /root/grader.sock && echo grader-ok
docker ps >/dev/null && echo docker-ok
df -h /data | tail -1
```

stage-4 依赖与 stage-3 相同：rootless docker、`/home/naco/bin/agcli`、
`/home/naco/work/cybergym`（gen_task 源码与 `.env`）、`/root/.cybergym_env`
（只有 root 能读的 `CYBERGYM_API_KEY`）。

## 2. 备份与部署

```bash
TS=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p /root/stage4-backup-$TS
cp -a /home/naco/work/cybergym-agent /root/stage4-backup-$TS/cybergym-agent
cp -a /root/score_all.sh /root/stage4.sh /root/backfill_fix_side.sh \
      /root/stage4-backup-$TS/ 2>/dev/null || true
```

从评审机（`stage4/` 所在机器）推送代码；排除密钥文件与本地测试残留：

```bash
rsync -a --delete \
  --exclude '.env' --exclude '.glm*.env' --exclude '.llm.env' --exclude '.pi.env' \
  --exclude '__pycache__' --exclude '.testtmp' --exclude '.smoke*' \
  stage4/cybergym-agent/ root@HOST:/home/naco/work/cybergym-agent/

scp stage4/root_code/score_all.sh          root@HOST:/root/score_all.sh
scp stage4/root_code/backfill_fix_side.sh  root@HOST:/root/backfill_fix_side.sh
scp stage4/root_code/stage4.sh             root@HOST:/root/stage4.sh
```

> `--delete` 会删掉主机上 stage-4 源码目录里多出来的文件；若不确定，先去掉
> `--delete` 跑一次，再补一次带 `--delete` 的同步。**不要**覆盖
> `/home/naco/work/cybergym/.env` 与 `/root/.cybergym_env`。

```bash
chown naco:naco /home/naco/work/cybergym-agent
chmod 755 /root/stage4.sh /root/score_all.sh
chmod 700 /root/backfill_fix_side.sh   # 含 DB/UDS 操作
```

部署后自检：

```bash
cd /home/naco/work/cybergym-agent
python3 - <<'PY'
from plugins import evidence, results_store
print("evidence.utcnow", evidence.utcnow())
print("final-only predicate:", evidence.official(1, 0), evidence.official(0, 0))
PY
python3 -m unittest tests.test_evidence tests.test_results_store tests.test_cybergym_runner
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

| 项 | 默认 |
|---|---|
| run root | `/data/cybergym/stage4/r_engineer` |
| 题目录 | `<run root>/tasks/<task>` |
| 工作区 | `/data/cybergym/stage4/r_engineer-ws/<task>`（即 `<run root>-ws`） |
| 日志 | `<run root>/logs/<task>.log` |
| agent 原始日志 | `/root/agent_logs/<run_id>/<task>.json` |

题目录是 **run-scoped** 的：gen_task 成功后写 `.stage4_run_id`。复用旧题目录
（没有本轮 marker）会被拒绝，因为 `submit.sh` 里的 `agent_id` 会把上一轮/上一波
的服务器记录混进本轮最终提交。换轮次请换 `--out`（题目录随之新建）。

## 4. 50 题 pilot（先跑这个）

```bash
cd /data/cybergym/stage4
/root/stage4.sh --queue /data/cybergym/stage4/queue.json \
    --out /data/cybergym/stage4/pilot50 --workers 3 --limit 50
```

driver 会自动：准备题 → 跑 agent → 补 fix 侧 → 评分 → 验收。也可分步：

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
python3 /home/naco/work/cybergym-agent/scripts/verify_stage4.py \
    --out /data/cybergym/stage4/pilot50 \
    --queue /data/cybergym/stage4/queue.json \
    --ws-root /data/cybergym/stage4/pilot50-ws
```

### pilot 验收门槛

```bash
python3 - <<'PY'
import json
v = json.load(open("/data/cybergym/stage4/pilot50/verify_stage4.json"))
print(json.dumps({k: v[k] for k in
      ("rows", "expected", "submitted", "official_success", "score_rate",
       "total_cost_usd", "rows_without_cost")}, indent=2, ensure_ascii=False))
print("violations:", len(v["violations"]))
for x in v["violations"][:10]:
    print(" ", x)
PY
```

必须全部满足才进入全量：

- `violations == 0`；
- 每个 `submissions > 0` 的行都有 `final.poc_id / poc_sha256 / vul_exit /
  fix_exit / submitted_at / agent_id`；
- `score_status` 只有 `ok`（或显式 `no_records` 对应 0 提交）；
- `results.json` 的 official 计数 == `rows/` 的计数；
- `find <ws-root> -name 'reference-intel*' | wc -l` 为 0；
- `rows_without_cost == 0`（agent 异常退出除外，且要有 `status` 解释）。

任何一项不满足：**不要扩大规模**，先按第 7 节定位。

## 5. 全量跑批与续跑

```bash
/root/stage4.sh --queue /data/cybergym/stage4/queue.json \
    --out /data/cybergym/stage4/r_engineer --workers 3
```

- 中断后原命令重跑：复用 `<out>/run_id`，只补未到终态的任务；
- 重跑失败题：`--retry-failed`（旧行存档在 `rows/attempts/`）；
- 只重算分：`/root/score_all.sh <out> <tasks> <ws>`；
- 换一批题/新一轮：必须换 `--out`（或显式 `--run-id`），否则会与旧 run 混在一起。

## 6. 关键环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `S4_AGENT_HOME` | `/home/naco/work/cybergym-agent` | 代码根 |
| `S4_REPO` | `/home/naco/work/cybergym` | gen_task 源码 |
| `S4_WSROOT` | `<out>-ws` | 工作区根 |
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
| 全部 `no_ledger` | `check.py` 没写账本：旧 ws 里残留旧版 check.py | 确认 `<ws>/check.py` 顶部烘焙了 `LEDGER_PATH = r"<ws>/submissions.jsonl"`（生成器写入，env 重定向无效）；重跑该题 |
| 大面积 `hash_unverified` | 服务器 `poc_hash` 算法/字节范围与 SHA-256 不可比 | **pilot 前必须先做一次受控提交核对哈希语义**；不可比时全部 fail-closed，不会虚增分数 |
| `WARN: chattr +a unsupported` | `$OUT` 所在 FS 不支持 append-only（如 NFS） | 受保护账本被禁用，评分退回工作区账本+哈希门禁；尽量把 `<out>` 放 ext4/xfs |
| 全部 `fix_pending` | 没跑/跑失败 backfill | `/root/backfill_fix_side.sh --dry-run` 看待补数量；完成后再评分 |
| 个别 `ledger_mismatch` | 服务器有更晚提交，账本没有 | 看 `<out>/scored/<task>.json` 与 `<out>/protected_ledger/<task>.jsonl`；该题 fail-closed，不要手工改分 |
| 个别 `hash_mismatch` / `ledger_corrupt` | 上传字节与服务器哈希不一致 / 账本中间行被改 | 均按篡改处理：fail-closed 并人工核查该题 |
| `REFUSE: workspace ... already exists` | 复用了旧工作区 | 用 `--fresh`（stage4 默认）或手工 `rm -rf` 该题 ws |
| `REFUSE: cross-task intel` | ws 里残留 `reference-intel*` | 直接删整个 ws 重跑；不要只删文件后复用 |
| `make_agent_ws failed` | tar/权限/磁盘 | 看 `<out>/logs/<task>.log` 与 ws 生成器 stderr |
| `refusing to reuse a pre-generated agent_id` | 指向了旧 `--task-base` | 换新 `--out`/`--task-base` 重新 gen_task；不要复用旧题目录 |
| `stage2.sh is LEGACY` | 误用 stage-3 入口 | 用 `/root/stage4.sh`；只有复现历史波次才 `S2_LEGACY_OK=1` |
| sudo `--preserve-env` 报错 | sudoers 限制 | 检查 wrapper 的 `AGENT_ENV` 列表；check.py 烘焙的账本路径不依赖 env，不受影响 |

## 8. 回滚

```bash
TS=<部署前的备份后缀>
systemctl stop <正在跑的 stage4 进程> 2>/dev/null || pkill -f run_agent_task.sh
rm -rf /home/naco/work/cybergym-agent
cp -a /root/stage4-backup-$TS/cybergym-agent /home/naco/work/cybergym-agent
cp -a /root/stage4-backup-$TS/score_all.sh /root/score_all.sh
```

stage-4 不修改 stage-3 的历史数据目录；回滚只影响代码入口。

## 9. 本轮没有覆盖的

- 第三波按失败类型重攻、崩溃点匹配门禁、预算重分配、模型路由（提分杠杆）；
- `/tmp` 残留与 docker 隔离的现场验证（沿用 stage-3 的 `scoring/smoke_isolation.sh`，
  本轮未改，也未在评审机执行）；
- 服务器 `created_at` 的实际精度与同秒并列分布（部署后跑 5 题即可观察
  `rows/scored/*.json` 与 `poc_records`）。
