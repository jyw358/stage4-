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
    results_store.py     逐题原子行 + results.json 锁内重建
    locks.py             跨平台文件锁
    cybergym_runner.py   提交客户端（提交时写账本；official_success 改 final-only）
    env_manager.py       未改，RunOutcome 依赖
    diff_verifier.py     未改，测试依赖
  scripts/
    make_agent_ws.py     工作区生成（--fresh / 无 intel / manifest / 白名单）
    run_agent_task.sh    单题 root wrapper（账本 env、两段会话、写行）
    stage4.py            队列驱动（准备→跑→回填→评分→验收）
    score_all.py         主机侧评分（最终提交 + ledger）
    score_task.py        单题评分（stage2/cleanpilot 路径）
    record_task_row.py   会话 + 账本 → 行；两段统计相加
    verify_stage4.py     5 项跑后验收
    stage2.sh            stage-3 遗留驱动：删 intel、加硬门禁、默认拒绝主入口
    cleanpilot.sh        stage-3 遗留干净跑法：加 intel 硬门禁
    agent_mission.txt    未改，mission 文本
  tests/                 stage-4 新增/改写的测试（52 项，可离线跑）
  agent/contract.py      未改，Verdict 依赖
root_code/
  stage4.sh              入口
  score_all.sh           评分入口（薄包装）
  backfill_fix_side.sh   fix 侧回填
docs/
  STAGE4_CHANGES.md      改了什么、fail-closed 状态表、已验证/未验证
  STAGE4_DEPLOY.md       主机部署与 50 题 pilot 手册
smoke_stage4.py          本地冒烟：工作区 + 行 + 验收
smoke_score.py           本地冒烟：本地 HTTP /query-poc + final-only + ledger
```

## 快速验证

```bash
cd cybergym-agent
python3 -m unittest tests.test_evidence tests.test_results_store \
    tests.test_cybergym_runner tests.test_make_agent_ws
# 预期：Ran 52 tests ... OK

cd ..
python3 smoke_stage4.py
python3 smoke_score.py
# 预期：SMOKE SCORE OK（五个场景：最终失败=False / 最终成功=True / 账本不
# 覆盖最终=None / 哈希不一致=hash_mismatch / 混偏移时间戳按时刻排序）
```

`/home/naco/...`、`/data/cybergym/...`、`http://172.17.0.1:8666` 是跑批主机的
真实部署路径，原样保留，不是缺失文件。

## 本轮主张（供评审逐条证伪）

1. 官方成功只按**最终提交**计算；`created_at` 严格解析为 UTC 后取最大值，
   缺失/无法解析/同刻并列一律 `success=None`，绝不给 `True`；agent 可写的
   账本**不参与**排序，也不消歧并列。
2. 提交瞬间写账本（哈希在构造请求体时对上传字节一次性计算，提交后不重读
   文件）；评分必须同时满足：服务器最终 `poc_id` == 账本最后一条，且服务器
   `poc_hash` == 账本 `poc_sha256`，否则 fail-closed。
3. 账本镜像到 root 侧 `chattr +a` 追加只读文件（`protected_ledger/`）；
   工作区账本仅作诊断。
4. 正式评分**没有**无账本降级参数（`--allow-missing-ledger` 已删除，
   `S4_REQUIRE_LEDGER` 已删除）；`score_all.py` 退出码区分
   完整/完整性失败/基础设施错误。
5. 跨题 intel 的两个写入点都已删除；生成器与 stage2/cleanpilot 都有硬门禁。
6. 每次尝试用 `--fresh` 工作区；生成后做 intel/预算/白名单断言并写 manifest。
7. 逐题 `rows/<task>.json` 原子落盘；`results.json` 在文件锁内原子替换；续跑按终态。
8. 第二段会话的 turns/tokens/checks/cost 与第一段相加；`CLAUDE.md` 渲染真实预算。
9. `verify_stage4.py` 能在提交缺证据、账本缺失、哈希不一致、intel 残留、
   run/agent 归属缺失、聚合不一致时报错。

评审请优先攻击这些主张，而不是重述它们。
