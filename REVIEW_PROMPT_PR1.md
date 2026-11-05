# PR #1 复审提示词 —— 整段粘贴给评审模型

---

# 任务：Stage-4 PR #1 复审（凭据加固 + GLM key 池）

你是一名怀疑立场的代码评审员。上一轮你评审了 PR #1 的单提交 `be9e69e`，
裁决 **REQUEST CHANGES**（B1 BLOCKER：非空错误 key 的认证层 404 仍会被判
`no_records`；B2：声称 root 凭据优先、实际 env 优先；B3：naco 可写历史
`.env` 自动回退扩大信任边界）。项目方随后推送了两个新提交：

- `1c92d8a` — 针对你 B1/B2/B3 的修复 + 13 项离线回归测试
- `ebab608` — 新增：GLM 模型 key 池（stage-3 五 key 轮换移植；**此前完全未审**）

**只审 PR #1 分支 head**（以 <https://github.com/jyw358/stage4-/pull/1/files>
为准；内容指纹见仓库根 `MANIFEST.sha256`，47 条，含 `REVIEW_PROMPT_PR1.md`
自身——即本提示词也在被审 commit 内，属预期）。截至本提示词定稿，代码改动
分布于 4 个 commit：`be9e69e`（首版 api_key 修复）→ `1c92d8a`（B1/B2/B3）
→ `ebab608`（GLM key 池）→ `c8b9859`（复审材料 + `stage4.sh` 拒绝
`S4_DEV_LEGACY_KEYFILE`；本 commit 不含评分/池逻辑变更）。

仓库 `docs/EVIDENCE_PR1_REREVIEW.md` 是项目方现场证据存档（服务端 404 契约
源码摘录、探针输出、端到端正/负向记录、key 池指纹与测试）——全部按
**项目方报告 / 需现场验证**采信，不得当作代码证据。先读代码；文档只作背景。

**引用格式必须是 `path:line`。** 没有代码位置的结论会被退回。

## 1. 上轮裁决 → 本轮待证伪的修复主张

| # | 上轮裁决 | 项目方主张 | 主要位置 |
|---|---|---|---|
| C1 | B1 BLOCKER：认证 404 ≠ 无记录未区分 | 仅 body 为 `{"detail":"Record not found"}` 的 404 映射 `no_records`；认证层 `"Not found"`、空 body、非 JSON body、非 404 一律传播为 `query_error`（infra，exit 2）；契约固定在 `BUSINESS_404_DETAIL`，写入模块 docstring | `scripts/score_all.py` `query_records`/`_is_business_not_found`/`_error_body` |
| C2 | B2：优先级名实不符 | 次序为 显式授权覆盖（`S4_KEY_ENV_OVERRIDE=1` + env）> `/root/.cybergym_env` > env（仅 root 文件缺失时）> legacy（仅 `S4_DEV_LEGACY_KEYFILE=1`）；代码/docstring/测试三处一致 | `scripts/score_all.py` `api_key` |
| C3 | B3：naco 可写文件入信任边界 | 正式路径彻底不读 `/home/naco/work/cybergym/.env`；且可信入口 `root_code/stage4.sh` 与 `S4_ALLOW_UNPROTECTED` 同型拒绝 `S4_DEV_LEGACY_KEYFILE=1` | `scripts/score_all.py`、`root_code/stage4.sh` |
| C4 | 回归测试缺失 | `tests/test_score_all.py` 13 例覆盖你给的 4 行矩阵 + dev 旗标 + 404 body 各形态，含你上轮的 FakeRunner 负例 | `tests/test_score_all.py` |

## 2. 新增未审改动：GLM key 池（`ebab608`，全部当作首次评审）

背景：stage-4 的模型凭据此前钉死在 `/etc/cybergym-agent/agent.env` 单 key。
本提交把 stage-3 的 5 个可用 GLM key 引入并轮换。**轮换策略基线是公开仓库
<https://github.com/jyw358/stage3-> 的 `root_code/key_pool.sh`**（优先级
senary > quaternary > quinary > primary > secondary，tertiary owner-retired
排除；probe 门控非 200 不选；sticky；全限流保上一个 active）——请自行拉取
对照，验证"策略一致"是否属实。

| # | 主张 | 主要位置 |
|---|---|---|
| P1 | 轮换策略与 stage3 `key_pool.sh` 完全一致（优先级/probe 门控/sticky/全限流保 active），仅实现载体不同 | `root_code/key_pool_s4.sh` vs stage3 `root_code/key_pool.sh` |
| P2 | 信任边界：池文件与 active.env 全部 root:root 0600，位于 `/etc/cybergym-agent/keys/`；dir/selector/active.env 任一非 root 属主 → wrapper REFUSE rc=4；agent 只经 `env -i` 重注入拿值、永远拿不到文件 | `cybergym-agent/scripts/run_agent_task.sh`（S4_KEY_POOL_DIR 段）、`root_code/key_pool_s4.sh` |
| P3 | 并发正确性：选择器 flock 串行；active.env 用同目录 mktemp+mv 原子替换；3 worker 并发启动时行为确定 | `root_code/key_pool_s4.sh` |
| P4 | 预算保护：全部 key 限流且无历史 active → 选择器 rc=1 → 该题 REFUSE，不启动 agcli；池目录不存在 → 回落 agent.env 单 key，行为与改动前一致 | `root_code/key_pool_s4.sh`、`run_agent_task.sh` |
| P5 | 无密钥泄漏：日志/输出只含 key 名称与探针状态码，不含密钥值；`[keypool] active=<name>` 逐题可审计 | 两个文件全部 echo/日志行 |
| P6 | 文档：`docs/STAGE4_DEPLOY.md` 新增小节与实现一致（安装命令、自动启用条件、覆盖变量、REFUSE 语义） | `docs/STAGE4_DEPLOY.md` §GLM key 池 |

## 3. 必须逐项回答的攻击面

### 3.1 C1 的 404 分类
- `_error_body` 对 `e.read()` 的所有失败路径（fp=None、已消费、编码错）是否
  全部落入 fail-closed？`plugins/cybergym_runner.py` 的 `query_poc` 先 catch
  HTTPError 存 `last_err` 再 re-raise——它在传播前有没有消费 body？两个
  task_id 形态各试一次后 `raise last_err`，score_all 读的是哪次请求的 body，
  两次的 body 是否可能不同型？
- 服务器将来把 detail 字符串改掉（升级/本地化）：后果是全部 `query_error`
  exit 2（响亮失败）还是悄悄退回 `no_records`？`BUSINESS_404_DETAIL` 的
  相等比较是 `==` 还是模糊匹配？
- 非 JSON body（代理错误页）、JSON 但无 `detail` 键、`detail` 为非字符串：
  各走哪条分支？`.get("detail")` 对 list/dict 的返回值？
- runner 对非 404 直接 `raise`：score_all 侧 `except Exception` 归为
  `query_error`——有没有把本应是 integrity failure 的异常也吞进 infra？

### 3.2 C2/C3 的凭据次序
- env key 带引号/空白/BOM：`strip().strip('"').strip("'")` 处理顺序的边界；
  `S4_KEY_ENV_OVERRIDE` 设为 `"true"`/`"yes"`（非 `"1"`）时的行为？
- `api_key()` 抛 RuntimeError → `main()` 写 `scoring_report.json` 并 return 2：
  此路径在 `--only-unscored`、空 rows、`--run-id` 过滤后 rows 为空时是否仍成立？
- `stage4.sh` 拒绝 `S4_DEV_LEGACY_KEYFILE=1`：绕过面——直接调
  `scripts/score_all.py`、经 `S4_SCORE_ALL` 包装脚本、cron/手工重打分，这些
  路径本 PR 是否声明为 dev/运维域？`S4_KEY_ENV_OVERRIDE` 为什么**不**同样
  在可信入口拒绝，这个不对称是否成立（提示：上轮评审你自己要求"显式授权
  覆盖模式"，区别在哪）？

### 3.3 key 池（全部首次评审）
- `key_pool_s4.sh` 的 `set -a; . "$envf"`：池文件是 root:root 0600，但文件
  内容若有恶意/失误的额外 export（如 `PATH=`、`S4_*=`），会进入哪个进程的
  环境？`run_agent_task.sh` 里 `set -a; . active.env` 之后这些变量会不会被
  转发进 agent 会话（对照 CRED_ARGS 白名单只有 4 个变量）？
- 每题启动都跑 5 次探针（各 8 token）：50 题 × 5 key 的配额代价与 stage3
  的 cron 每 5 分钟一次相比是否可接受？探针超时 20s × 5 串行（flock 下跨
  worker 排队）最坏会把单题启动拖多久？有没有 probe timeout 可配？
- sticky 判定用 `glm_active` 标记文件内容 vs 重新计算的最优：标记文件被
  手工改坏/删掉、active.env 与标记不一致（半个写坏）时行为？
- `mktemp "$POOL_DIR/.active.XXXXXX"` + `mv`：同目录原子性成立；但
  `chmod 600` 在 mv 前对 mktemp 文件——中间态权限？崩溃残留 `.active.*`
  文件会堆积吗，下一次运行会清吗？
- 全限流保 active（B 场景）：active 里的 key 也限流时任务仍会带着它启动、
  agcli 内部 429 失败——这是 stage3 同款行为还是 stage4 新引入的预算浪费？
  REFUSE 只在"无历史 active"时发生，这个设计是否自洽？
- `run_agent_task.sh` 的池段在 `if [ -n "${S4_AGENT_ENV_FILE:-}" ]` 块内：
  agent.env 缺失但池存在时会怎样（静默单 key？REFUSE？）与文档是否一致？

### 3.4 测试是否同义反复
- 13 例中哪些 mock 了 `_key_from_file` 本身——这类测试还能发现真实文件解析
  bug 吗（引号、CRLF、`export ` 前缀）？`_key_from_file` 对
  `export CYBERGYM_API_KEY=x` 格式（stage3 池文件即此格式！）返回什么？
  这对 GLM 池文件解析是**可用**的、对 root 凭据文件格式是否留有假设？
- FakeRunner 直抛 HTTPError：绕过了 runner 真实的两 id 重试逻辑，`query_poc`
  的 `last_err` 路径有没有被任何测试覆盖？没有的话标记为盲区。

## 4. 输出格式（与上轮相同，严格）

```markdown
## 一、实际改动范围
## 二、BLOCKER（P0）
## 三、MAJOR（P1）
## 四、修复确认表
| 检查项 | 结果 | 依据(path:line) |
## 五、可被利用/退化路径（若有）
## 六、最终裁决
| 维度 | 裁决 |
含：B1/B2/B3 是否关闭、key 池可否进正式跑、PR #1 可否合并、50 题 GO
```

## 5. 禁止

- 不要重述项目方主张；逐条证伪。
- 不要把 EVIDENCE_PR1_REREVIEW.md 的现场输出当代码证据；标"项目方报告"。
- 不要索要密钥明文、poc.db、会话日志、宿主机访问。
- 不要建议"加强测试/完善文档"这类空话；每条落到 path:line 与最小修法。
- 不要混用旧快照：只审 PR #1 分支 head（对照 `MANIFEST.sha256` 47 条指纹）。

## 6. 建议读序

1. `scripts/score_all.py` 全文（4 个 commit 的改动都在这）→ 出 C1-C3 结论
2. `tests/test_score_all.py` → 3.4
3. `root_code/key_pool_s4.sh` + stage3 仓库 `root_code/key_pool.sh` 对照 → P1/P3
4. `run_agent_task.sh` 的 S4_KEY_POOL 段 + `root_code/stage4.sh` 新拒绝 → P2/P4、3.2
5. `docs/STAGE4_DEPLOY.md` §GLM key 池 → P6
