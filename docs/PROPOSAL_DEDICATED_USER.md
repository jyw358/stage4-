# 提案：stage-4 专用运行用户（s4agent）——隔离加固

> ## 实施状态（R3，第三轮评审后更新）
>
> 状态：**已按第三轮评审裁决实施**（本文件保留原始提案文本作历史记录；
> 真实实现见代码与 [STAGE4_CHANGES.md](STAGE4_CHANGES.md) 第 0.5 节、
> [STAGE4_DEPLOY.md](STAGE4_DEPLOY.md) 第 2/4/9 节）。评审结论与落地对照：
>
> | 评审裁决 | 落地 |
> |---|---|
> | T1 成立（P0），且 3.2 的 `assert_code_tree_readonly()` 太晚太窄——信任必须由 root 拥有的外部启动脚本在 Python 启动前建立 | `root_code/stage4.sh` + `root_code/assert_code_tree.py` 部署到 `/root/`，exec 前审计全树+祖先；`stage4.py` 内仅留防御性早断言 |
> | 单一 s4agent UID 不批准为唯一隔离措施（chmod 700 对同 UID 无效） | 每题唯一数字 uid（池 30001 起、run 内不复用），0700 私有工作区/HOME/TMPDIR/XDG |
> | gen_task 不与 agent 同 UID | 保留独立准备身份 `S4_PREP_USER`（默认 naco）；`chown_prep` fail-closed |
> | marker 绑定不可替换（软链接窗口/父目录可替换/未绑定 submit.sh） | 控制面 `<out>/control/<task>/` root:root 0700，O_NOFOLLOW+原子写，绑定 run_id+agent_id+submit.sh sha256+agent_uid；wrapper 逐项校验 |
> | 账本信任描述失实；promoter 保留不可信预写值 | 信任模型重述为“root 封存的不可信提交声明”；promoter 强制覆盖身份值、坏行硬失败、同 ID 异哈希单列 conflicts |
> | 50 题 pilot NO-GO，修复+smoke 通过后重评 | 154 项单测 + 双 smoke 全过；上机负向测试 2026-11-04 于 192.168.49.13 跑 **9 个场景 / 25 条断言**全过（部署手册第 9 节），并据现场证据把降权从 sudo 改为 setpriv |
>
> **R3.2 复审（第四轮）**：裁决 CONDITIONAL PASS（代码修复），50 题 pilot 仍
> NO-GO（真实 agcli / 评分闭环 / 进程残留未验收）。已修复三个 P1：启动失败
> 独立归因（`agent_launch_error` + rc=7，不进第二段）、真实 ledger 的
> `chattr +a` 由 `lsattr` 验证、protected ledger 改 0700/0600 并在验收中新增
> `ledger_open`。随后按 P0 跑单题真实链路 smoke，暴露并修掉 agcli 部署位置、
> 私有 HOME 与凭据注入（`S4_AGENT_ENV_FILE`）、`--out` 祖先链"他人可遍历"
> 校验三处真机问题。详见 STAGE4_CHANGES.md 第 0.8/0.8.1 节。
>
> **R3.3 单题 smoke 结果**：`arvo:10628`（uid 30005）真实跑通
> `submitted → promote → score → verify`（`official_success=1/1`、
> `violations=0`、`cost=$0.42`）。现场又暴露并修掉五项：会话内 `check.py`
> 因运行时树对每题 uid 不可读而 `ModuleNotFoundError`（新增 agent uid 探针 +
> 拒绝 group/other 可写的运行时树）、启动证据放在工作区会被删（改为 root
> 侧已打开 fd 记录）、驱动默认评分脚本是 stage-3 遗留 wrapper、新 run 的行
> 继承旧 run 统计、服务器 `created_at` 无时区导致全部 `timestamp_invalid`。
> 详见 STAGE4_CHANGES.md 第 0.8.2 节与部署手册 §9.2 S-5～S-10。
>
> **R3.3 复审（第五轮）**：裁决 CONDITIONAL PASS（原 M1/M2/M3 静态 PASS），
> 新提两个 P1 均已修复：已有 append-only ledger 的重入改为"先核验、符合即
> 复用、不符即拒"（`chown`/`chmod` 对 `+a` 文件在内核层即 EPERM，实测确认）；
> `chattr` 不可用时从降级跑完改为模型调用前 fail-closed（新状态
> `evidence_setup_error` + rc=8；`S4_ALLOW_UNPROTECTED=1` 仅限开发）。
> WSL2 root/ext4 wrapper 级负测 5 场景 / 19 断言全过。详见
> STAGE4_CHANGES.md 第 0.8.3 节与部署手册 §9.3。
>
> **R3.4 复审（第六轮）**：裁决 CONDITIONAL PASS，两项 P1 静态确认修复
> （并撤销"chattr 失败仍烧预算"的旧判断），另提两项加固，均已实施并在真机
> ext4 验证 23 断言全过：正式入口启动即拒 `S4_ALLOW_UNPROTECTED=1`
> （`root_code/stage4.sh` exit 4、`stage4.py` SystemExit）；ledger 初始化
> 改为每个文件操作独立检查（`set -u` 无 `-e`，原实现会静默跳过失败，
> 留下半初始化证据目录），失败即 `evidence_setup_error` + rc=8 并清理。
> 详见 STAGE4_CHANGES.md 第 0.8.4 节与部署手册 §9.4。
>
> **R3.5 复审（第八轮）**：裁决 B1（对抗性后代 `setsid` + 清环境可逃出 token
> 扫描）与 M1（`residual=0` 只是"成功观测到的残留数"）最小修法已实施：每次
> attempt 独立 cgroup（先入组再 exec，结束由 `cgroup.kill` 终止并轮询
> `cgroup.procs` 至空），清理结果改三态（clean / `process_residual_error` /
> `process_inspection_incomplete`），后两者不自动重试；driver 正式运行前
> 检查 cgroup v2 可用性。真机构造性负测 26 断言全过（`setsid` + `env -i` +
> 超时 + 重试全链路；另修两项 NIT）。详见 STAGE4_CHANGES.md §0.8.6 与部署手册
> §9.6。
>
> **R3.4 运行时验收（第七轮）**：真机 ext4 20/20 —— 跨 UID **写入**/删除/
> `chmod` 他题 workspace 与 prepared dir 全部 denied；他题 `CLAUDE_CONFIG_DIR`
> 状态文件读取与覆写 denied；**setsid 逃逸闭环**。关键对照：旧实现只按会话号
> 扫 `/proc`，逃逸后代存活却报 `residual=0`（静默漏杀）；改为 per-attempt
> token 扫 `/proc/*/environ` 后 `before=3 → after=[] residual=0`。残留不为 0
> 写 `process_residual_error` 且 `--retry-failed` 不重试（必须人工清主机后才能
> 重跑）。详见 STAGE4_CHANGES.md 第 0.8.5 节与部署手册 §9.5。
>
> **R3 复审（第二轮）**：裁决 REQUEST CHANGES，唯一 P0 是“准备目录仍可跨题
> 读取”（R3-B1）。已修复：题目录 prep 后整树归本题 uid + 0700、父级 0711、
> wrapper 复核属主/模式、verify 报 `taskdir_open`；同时处理 uid 跨 run 复用
> （宿主机只增不减注册表）、控制面祖先链校验、wrapper uid 强绑定、promoter
> 失败硬停、`/proc` 会话扫描。详见 STAGE4_CHANGES.md 第 0.6 节。setsid
> 另开会话的后代仍需 cgroup（未实现，已列为上机残留项）。
>
> 与原提案的差异：部署根从 `/home/naco/work/cybergym-agent` 改为
> `/opt/cybergym-agent`（root 独占，文档 `chown naco:naco` 一行是 T1 反模式，
> 已删除）；agcli 改 `/usr/local/bin/agcli`（无需开放 /home/naco 遍历）；
> 3.2 的补丁预览已被真实实现取代（见下文标记）。

原始提案文本（历史记录，未再逐字维护）：

状态：~~提案，未实施~~（已被上文实施状态取代）。
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

### 3.2 实施补丁预览（**已废弃——被 R3 真实实现取代**，保留原文以对照评审意见；与实现的差异：路径、chown 方向、单一 UID 模型均按评审意见修正，见顶部实施状态表）

#### 补丁 A — `cybergym-agent/scripts/stage4.py`

A1. 用户参数化 + 启动断言（切断 T1）：

```diff
 AG = os.environ.get("S4_AGENT_HOME", "/home/naco/work/cybergym-agent")
+AGENT_USER = os.environ.get("S4_AGENT_USER", "s4agent")
 REPO = os.environ.get("S4_REPO", "/home/naco/work/cybergym")
```

```diff
-def chown_naco(path) -> None:
-    """gen_task and the workspace generator run as naco: the dirs they write
-    into must be owned by naco even though this driver runs as root."""
+def chown_agent(path) -> None:
+    """gen_task and the workspace generator run as AGENT_USER (dedicated,
+    unprivileged, no home history): dirs they write into are owned by that
+    user even though this driver runs as root."""
     if pwd is None or euid() != 0:
         return
     try:
-        pw = pwd.getpwnam("naco")
+        pw = pwd.getpwnam(AGENT_USER)
         os.chown(path, pw.pw_uid, pw.pw_gid)
     except (KeyError, OSError):
         pass
```

```python
# NEW: called first thing in main() on POSIX, before any task starts.
def assert_code_tree_readonly() -> None:
    """T1: root-executed code must be writable by root only.  Group/other
    write on the agent code tree is how an agent session could rewrite the
    scorers (or these very root-run scripts); refuse to start instead."""
    import stat as st
    if pwd is None or euid() != 0:
        return
    try:
        pwd.getpwnam(AGENT_USER)
    except KeyError:
        raise SystemExit(f"agent user {AGENT_USER!r} does not exist "
                         "(create it, see STAGE4_DEPLOY.md s1)")
    base = Path(__file__).resolve().parents[1]          # .../cybergym-agent
    for d in (base, base / "scripts", base / "plugins"):
        m = d.stat().st_mode
        if m & (st.S_IWGRP | st.S_IWOTH):
            raise SystemExit(f"{d} is group/other-writable; refusing to run "
                             "(T1: agent must not be able to rewrite root-run code)")
```

A2. gen_task 与目录属主改用 AGENT_USER：

```diff
     tdir.mkdir(parents=True, exist_ok=True)
-    chown_naco(tdir)
+    chown_agent(tdir)
     cmd = [
-        "sudo", "-u", "naco", "env", f"PYTHONPATH={REPO}/src",
+        "sudo", "-u", AGENT_USER, "env", f"PYTHONPATH={REPO}/src",
         "/usr/bin/python3.12", "-m", "cybergym.task.gen_task",
```

```diff
     for d in (out, out / "rows", out / "logs", task_base, ws_root):
         d.mkdir(parents=True, exist_ok=True)
-    chown_naco(task_base)
-    chown_naco(ws_root)
+    chown_agent(task_base)
+    chown_agent(ws_root)
+    assert_code_tree_readonly()        # NEW: start-gate, before the run lock
```

#### 补丁 B — `cybergym-agent/scripts/run_agent_task.sh`

```diff
-# Runs as root. The agent and workspace generator still run as naco.
+# Runs as root. The agent and workspace generator run as $S4_AGENT_USER:
+# a dedicated account with no sudo entry, no home history, and no write
+# path to the code tree or <out> (root keeps rows/, ledgers, reports).
+AGENT_USER="${S4_AGENT_USER:-s4agent}"
```

```diff
-# the generator runs as naco and must be able to create <ws_root>/<task>
-[ "$(id -u)" -eq 0 ] && chown naco:naco "$WSROOT" && chmod 755 "$WSROOT"
+# the generator runs as the agent user and must create <ws_root>/<task>
+[ "$(id -u)" -eq 0 ] && chown "$AGENT_USER:$AGENT_USER" "$WSROOT" && chmod 755 "$WSROOT"
```

```diff
-if ! sudo -u naco env S4_RUN_ID="$RUN_ID" python3 "$AG/scripts/make_agent_ws.py" \
+if ! sudo -u "$AGENT_USER" env S4_RUN_ID="$RUN_ID" python3 "$AG/scripts/make_agent_ws.py" \
```

```diff
 export CLAUDE_CONFIG_DIR="$WS/.claude-config"
-mkdir -p "$CLAUDE_CONFIG_DIR"; chown naco:naco "$CLAUDE_CONFIG_DIR"; chmod 700 "$CLAUDE_CONFIG_DIR"
+mkdir -p "$CLAUDE_CONFIG_DIR"
+chown "$AGENT_USER:$AGENT_USER" "$CLAUDE_CONFIG_DIR"; chmod 700 "$CLAUDE_CONFIG_DIR"
```

```diff
 # pass 1
-sudo -u naco --preserve-env="$AGENT_ENV" \
+sudo -u "$AGENT_USER" --preserve-env="$AGENT_ENV" \
     timeout 2400 /home/naco/bin/agcli -p "$(cat "$AG/scripts/agent_mission.txt")" \
 ...
 # pass 2
-sudo -u naco --preserve-env="$AGENT_ENV" \
+sudo -u "$AGENT_USER" --preserve-env="$AGENT_ENV" \
     timeout 900 /home/naco/bin/agcli -p "$PASS2_PROMPT" \
```

#### 补丁 C — `docs/STAGE4_DEPLOY.md`（部署前检查新增）

```bash
id s4agent                                            # 存在、无 sudo、独立主组
sudo -u s4agent test ! -w /home/naco/work/cybergym-agent/scripts \
  && echo code-readonly-ok                            # T1 断言的离线版
sudo -u s4agent test ! -r /home/naco/.claude/projects 2>/dev/null \
  && echo history-hidden-ok || echo "WARN: naco history visible"   # T2
stat -c '%U:%G %a %n' /home/naco/work/cybergym-agent  # root:root, 0755
```

#### 实施时的验证矩阵（上机执行）

| 检查 | 命令（以 s4agent） | 期望 |
|---|---|---|
| 代码树只读 | `sudo -u s4agent touch /home/naco/work/cybergym-agent/scripts/x` | Permission denied |
| out/protected 不可见 | `sudo -u s4agent ls /data/cybergym/stage4/r_engineer/protected_ledger` | Permission denied |
| naco 历史不可读 | `sudo -u s4agent ls /home/naco/.claude/projects` | No such file or directory |
| agcli 可执行 | `sudo -u s4agent test -x /home/naco/bin/agcli && echo ok` | ok |
| GLM 凭据可读 | `sudo -u s4agent cat <key-file>`（0640 root:s4agent） | 输出非空 |
| 提交链路 | 单题 dry 跑 1 个任务，观察 ledger/promote/verify 全绿 | 0 violations |

#### 已知迁移注意点（评审请补充）

1. agcli 若在 `~/.config` 或 `$HOME` 下有状态，需要为 s4agent 预置（只读分发）；
2. `/home/naco/bin/agcli` 需 `o+x`；GLM key 文件 `0640 root:s4agent`；
3. `naco` 用户保留给 gen_task 源码树的日常维护（只读运行不受影响）；
4. `record_task_row.py`/`promote_ledger.py`/评分链路全部仍以 root 运行，无改动。

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
