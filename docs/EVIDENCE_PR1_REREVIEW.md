# PR #1 复审证据材料（2026-11-05，跑批主机 8.130.138.155）

评审 AI 只能读仓库与 PR。本文把**代码里证明不了、只能现场取得**的事实原样
存档：服务端 404 契约源码、真实探针输出、端到端正/负向打分记录、GLM key 池
指纹与测试记录。所有内容不含密钥明文（密钥一律 sha256 前 12 位指纹）。

口径与上一轮评审一致：本文全部属于 **项目方现场报告**，复审可采信度以
"需现场验证"计；代码层面的问题请以仓库 `path:line` 为准。

---

## 1. 服务端 /query-poc 404 契约（B1 修复依据）

部署的服务端源码在 `/home/naco/work/cybergym/src/cybergym/server/__main__.py`
（不在本仓库，评审无法读到，故全文摘录关键两处）。

认证层（私有路由网关，坏/空 key 都走这里，**不进入 handler**）：

```python
# __main__.py:119 附近（私有路由鉴权依赖）
raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
```

业务层（query-poc handler，真正"无记录"）：

```python
# __main__.py:198-205
@private_router.post("/query-poc")
def query_db(db: SessionDep, query: PocQuery):
    logger.info("query-poc: agent=%s task=%s", query.agent_id, query.task_id)
    records = get_poc_by_hash(db, query.agent_id, query.task_id)
    if not records:
        raise HTTPException(status_code=404, detail="Record not found")
    return [record.to_dict() for record in records]
```

查询语义（`server/pocdb.py:71-88`）：`filter_by(agent_id=..., task_id=...)`
精确匹配，两个条件都参与过滤。

请求链路：root 评分器 → `http://172.17.0.1:8666`（grader_proxy，转发响应体
不变）→ `/root/grader.sock`（uvicorn，`run_grader.py` 启动）。

**契约要点：两种 404 的状态码相同、`detail` 字符串不同**
（`"Not found"` = 认证层；`"Record not found"` = 业务层）。评分器
`score_all.py` 的 `BUSINESS_404_DETAIL` 只认后者，其余一律 fail-closed 为
`query_error`。

### 1.1 真实探针输出（2026-11-05，agent=b796276460794a938307651ef11f5be3）

```
good_key + real_task  -> (200, b'[{"agent_id":"b796276460794a938307651ef11f5be3","task_id":"arvo:10628","poc_id":"c6fff4f02dcb404f851...')
good_key + fake_task  -> (404, b'{"detail":"Record not found"}')
bad_key  + real_task  -> (404, b'{"detail":"Not found"}')
empty    + real_task  -> (404, b'{"detail":"Not found"}')
```

坏 key 与空 key 的网关响应**完全同型**——这正是首轮 pilot 全部误判
`no_records` 的机制；也只有 body 能区分。

---

## 2. 端到端打分正/负向记录（pilot5b，run s4-20261105T074234Z-224903）

评分器：`/opt/cybergym-agent/scripts/score_all.py`（部署 sha256
`9d4751b461dd4895dbf1793bb573e92623d1f5a6defbfd3e0014fd2443c47867`，
与 PR head `ebab608` 中该文件一致）。

```
== POSITIVE: formal rescore (root key file, no env) ==
[scored] official_success=5/5 decided=5 by_status={'ok': 5} integrity_fail=0 infra_err=0
positive rc=0
== NEGATIVE B2: stale env key WITHOUT override flag must be ignored ==
env CYBERGYM_API_KEY=deadbeef... (无 S4_KEY_ENV_OVERRIDE)
[scored] official_success=5/5 decided=5 by_status={'ok': 5} ...   -> rc=0（root 文件赢）
== NEGATIVE B1: authorized override with a WRONG key must be query_error ==
env CYBERGYM_API_KEY=deadbeef... S4_KEY_ENV_OVERRIDE=1
[scored] official_success=0/5 decided=0 by_status={'query_error': 5} infra_err=5
negative-b1 rc=2        <-- 原缺陷路径：现在是 infra error，不再 no_records
== RESTORE ==
[scored] official_success=5/5 ... by_status={'ok': 5}   -> rc=0
```

恢复后落盘状态：5 行 `score_status=ok`（pilot5b 数据未被负向实验污染）。

### 2.1 凭据文件现状（指纹，非明文）

```
/root/.cybergym_env            CYBERGYM_API_KEY len=32 sha=ef5785bf3038   <- 评分器权威源
/home/naco/work/cybergym/.env  CYBERGYM_API_KEY=<empty>                    <- 首轮事故根源
/etc/cybergym-agent/agent.env  GLM_API_KEY    len=49 sha=506194b53d4b     == 池中 senary
```

---

## 3. GLM key 池（ebab608）

### 3.1 池清单（root:root 0600，/etc/cybergym-agent/keys/）

| 名称 | 来源(stage3) | key sha256[:12] | 探针 |
|---|---|---|---|
| senary | .glm6.env | 506194b53d4b | 200 |
| quaternary | .glm4.env | 21c03df554ea | 200 |
| quinary | .glm5.env | f1643d974345 | 200 |
| primary | .glm2.env | b3e127f1c3c3 | 200 |
| secondary | .glm.env | ac5bf0a00cce | 200 |
| （tertiary，.glm3，owner-retired，不入池） | 16ed8e5181b7 | 200 |

### 3.2 确定性轮换测试（stub curl，5/5 PASS）

```
PASS A-throttle-skips-top-two (rc=0 active=quinary)     # senary/quaternary 429 -> quinary
PASS B-all-throttled-keeps-active (rc=0 active=quinary) # 全 429 + 有 active -> 保持
PASS C-all-throttled-no-active-fails (rc=1 active=none) # 全 429 + 无 active -> REFUSE
PASS D-recovery-switches-up (rc=0 active=senary)        # senary 恢复 -> 上切
PASS E-only-secondary-alive (rc=0 active=secondary)     # 只剩末位 -> 用末位
```

### 3.3 真实选择器 + 端到端

```
2026-11-05T09:51:50Z pool switch unknown -> senary (senary=200 quaternary=200 quinary=200 primary=200 secondary=200 )
第二次运行 STICKY-OK（active.env mtime 不变）
active.env/root:root 600；选择器 /root/key_pool_s4.sh root:root 700
```

单题端到端（pool-smoke1，run s4-20261105T095309Z-78d967）：

```
[keypool] active=senary run=s4-20261105T095309Z-78d967 task=arvo:10628
[stage4] arvo:10628: submitted
[stage4] done ... execution_failures=0 scoring_pending=0 phase_failures=none
row: submitted / official_success=True / score_status=ok / cost $0.506358
```

### 3.4 可信入口新增拒绝（本 PR 最后一个 commit）

`/root/stage4.sh` 现与 `S4_ALLOW_UNPROTECTED` 同型拒绝
`S4_DEV_LEGACY_KEYFILE=1`（正式入口不接受 naco 可写凭据文件的 dev 旗标）。

---

## 4. 评审无法从仓库验证、只能标"需现场验证"的清单

1. 服务端源码与 404 body 契约（本文 §1 摘录即全部依据）；
2. §1.1/§2/§3 的现场输出（项目方报告）；
3. 池文件的密钥真实有效性（探针 200 是唯一现场证据）；
4. grader_proxy 是否对所有路径都透传 body（§1.1 四组合实测过 query-poc 一条）。

## 5. 交叉验证基线

- stage-3 轮换策略基线（公开仓库）：<https://github.com/jyw358/stage3->
  `root_code/key_pool.sh`、`cybergym-agent/scripts/key_affinity.sh`、
  `cybergym-agent/scripts/window_budget.py`。
- 本地+部署机测试套件：176 OK（7 项既有 skip），含新增
  `tests/test_score_all.py` 13 例。
