# 提案：stage-4 专用运行用户（s4agent）——隔离加固，待评审

状态：**提案，未实施**。先请评审 AI 评估方向与完备性，通过后再改代码。
基线：第二轮修复后的 `main`（commit `28053f0`，B7–B12 已修）。

## 1. 现状信任模型（root / naco 双用户）

| 资产 | 属主 | 谁执行 | 谁可写 |
|---|---|---|---|
| `stage4.py` / `run_agent_task.sh` / `promote_ledger.py` | naco | **root** | naco（含 agent 会话进程） |
| `plugins/`（evidence / results_store / locks 评分逻辑） | naco | root（评分）/ naco（check.py import） | naco |
| agent 工作区 `<out>-ws/<task>` | naco | naco（agcli + check.py） | naco |
| 受保护账本 `<out>/protected_ledger/` | root 0644 | root 侧 promoter | 仅 root |
| `~/.claude/projects`（stage-1/2/3 历史轨迹） | naco | — | naco（agent 可读） |
| gen_task 源码 `/home/naco/work/cybergym` + `.env` | naco | naco | naco |

## 2. 本提案要切断的威胁路径

- **T1 评分代码可替换（最严重，B7–B12 评审未覆盖）**：agent 会话以 naco 运行，
  而 `cybergym-agent/` 代码目录 naco 可写。agent 可以在会话内改写
  `plugins/evidence.py`（评分判定）或 `scripts/run_agent_task.sh` /
  `stage4.py`（**root 执行的脚本**）——后者等价于植入 root 代码。B11 只封了
  "账本可追加"，没有封"评分代码可替换"。
- **T2 历史轨迹可读**：`CLAUDE_CONFIG_DIR=$WS/.claude-config` 只隔离 agent 的
  写入位置；agent 仍可读 naco 的 `~/.claude/projects`（stage-1/2/3 各波的完整
  解题轨迹 = 跨波污染源）。
- **T3 sudo 面**：naco 出现在 sudoers（gen_task / agcli 链路）；agent 进程
  继承 naco 的全部 sudo 能力，未做最小化。
- **T4 跨波数据**：naco 可读同属主的其他工作区（auditor/ctf 波、旧 run）。

**与已修复项的关系**：工作区账本、`.checks`、poc.bin 等 agent 可写资产在
现行评分链路里已按"不可信"处理（服务器定序 + 哈希门禁 + root 侧账本收割），
本提案不改变该模型，只把"agent 能触碰的代码与历史"也纳入只读。

## 3. 提案设计

### 3.1 用户与权限矩阵

```bash
useradd -m -s /bin/bash s4agent        # 无 sudo、无附加组、空 home
chown -R root:root /home/naco/work/cybergym-agent
chmod -R go-w,a+rX /home/naco/work/cybergym-agent   # 所有人只读
```

| 资产 | 变更后属主/权限 | s4agent | naco |
|---|---|---|---|
| cybergym-agent 代码树 | root:root 0755 | 只读 | 只读 |
| 工作区 `<out>-ws/<task>` | s4agent:s4agent 0700 | 读写 | 不可见 |
| 题目录 `<out>/tasks/<task>` | s4agent 0755（marker 仍 root:root） | 读写 | 不可见 |
| gen_task 源码 + mask_map | root:naco（按现状，仅必要时调整） | 只读 | 只读 |
| GLM 凭据 | 0640 root:s4agent | 读 | 不可见 |
| 受保护账本 / rows / out 根 | root（不变） | 不可见 | 不可见 |

### 3.2 代码改动点（若评审通过）

1. `stage4.py`：`chown_naco()` → `chown_agent()`（`S4_AGENT_USER` 环境变量，
   默认 `s4agent`）；gen_task 的 `sudo -u naco` 同步替换；
2. `run_agent_task.sh`：三处 `sudo -u naco` / `chown naco:naco` 换为
   `S4_AGENT_USER`；
3. `STAGE4_DEPLOY.md`：新增建户与属主检查步骤；
4. 可选加固：每次 run 前断言代码树对 `S4_AGENT_USER` 不可写（启动即拒）。

### 3.3 明确不解决的

- 服务器侧排序 / `poc_hash` 语义（与本地账号无关）；
- B7–B10 状态机与验收逻辑（已由第二轮修复覆盖）；
- B12 进程树清理（POSIX 进程组已覆盖；用户隔离不替代进程清理）；
- agent 的网络访问（提交服务器与 GLM API 是功能必需，不能断网）；
- s4agent 名下的工作区内部互害（同用户并发任务之间；如需隔离升级为
  每任务独立 UID 或 systemd-run --uid，成本更高，见问题 3）。

## 4. 请评审 AI 重点回答的问题

1. T1（root 脚本对 agent 可写 = 提权路径）的定性是否成立？严重级别？
2. 单一 `s4agent` 用户是否足够？还是必须每任务/每 worker 独立 UID？
3. 是否应同时叠加 cgroup / namespace / systemd-run --uid（如 `PrivateTmp`、
   `ProtectHome`）？哪些是 pilot 前必须、哪些可以后置？
4. gen_task 继续以 naco 跑、还是也迁到 s4agent？（gen_task 写题目录，题目录
   归属需随之决定）
5. `.stage4_run_id` marker 的 root 属主校验（已实现）在该模型下是否仍充分？
6. 该提案与现行 B11 受保护账本模型之间是否有评审认为的缺口？
