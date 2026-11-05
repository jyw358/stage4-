# PR #1 第三轮复审提示词 —— 整段粘贴给评审模型

---

# 任务：Stage-4 PR #1 第三轮复审（r2 修复核验 + 新实现首次细审）

你是一名怀疑立场的代码评审员。前两轮结论：

- 第一轮（`be9e69e`）：B1 BLOCKER（认证层 404 误判 `no_records`）、B2（凭据
  优先级名实不符）、B3（naco 可写 `.env` 自动回退）→ REQUEST CHANGES。
- 第二轮（`c8b9859`）：**B1/B2/B3 已 CLOSED**（评分器修复获批）；但当时
  新增、首次受审的 GLM key 池被判 **REQUEST CHANGES**：4 项 P0（P0-1
  凭据文件被 root `source` 执行；P0-2 选择器写失败仍可 rc=0；P0-3 池逻辑
  嵌在 `S4_AGENT_ENV_FILE` 条件内、env 未设即静默跳过；P0-4 属主检查不
  构成信任边界）+ M1-M8（runner 双 id 回退可掩盖首次认证 404；query 与
  证据异常混入同一 `query_error`；`_key_from_file` 真实解析无测试；双 id
  序列无测试；每题 5 探针排队/配额；sticky 不验证 active 一致性；全失败
  保 active 与 "probe-gated" 矛盾；stage-3 基线不可得）。

项目方随后推送 `9f153d9`（r2 修复）+ 读侧信任加固。本轮任务：
**A. 逐项核验 r2 修复是否真实成立、是否引入新洞；B. 对修复中的新实现
（严格解析器、信任检查链、三态 wrapper、TTL 缓存、s4_business_404 标记、
integrity_error 拆分）做首次细审。**

**只审 PR #1 分支 head**（以 <https://github.com/jyw358/stage4-/pull/1/files>
为准；内容指纹见仓库根 `MANIFEST.sha256`，48 条）。commit 链：
`be9e69e` → `1c92d8a`（B1-B3）→ `ebab608`（key 池 v1）→ `c8b9859`/
`face58e`（复审材料）→ `9f153d9`（r2 修复）→ 读侧信任加固（本轮 head）。
r2 修复全部代码在 `9f153d9` 及之后。

`docs/EVIDENCE_PR1_REREVIEW.md` §6 是 r2 修复的现场验证存档（stub 15 场景、
e2e、负向打分）——按**项目方报告 / 需现场验证**采信，不得当代码证据。
先读代码；文档只作背景。**引用格式必须是 `path:line`。**

## 1. 本轮待证伪的主张（r2 修复声明，逐条攻击）

| # | 上轮裁决 | 项目方主张 | 主要位置 |
|---|---|---|---|
| R1 | P0-1 | 凭据文件（池文件/active.env/agent.env）全部按**严格数据**解析：白名单 `GLM_API_KEY/GLM_BASE_URL/GLM_MODEL/AGCLI_CTX`、可选 `export ` 前缀、可选成对引号、值域 `[A-Za-z0-9._:/+=-]`；额外键/命令替换/不可解析行拒绝该文件；任何路径都不再 `source` | `root_code/key_pool_s4.sh` `parse_env_strict`/`load_env_strict`、`run_agent_task.sh` `s4_parse_env_strict`/`s4_load_cred_env` |
| R2 | P0-2 | 写路径每步 `|| die`；切换后 `cmp` 复验 active 与源一致；sticky 前同样验证，不一致原子修复；`.tmp.*` 残留清理 | `key_pool_s4.sh` `atomic_write`/主流程 |
| R3 | P0-3 | wrapper 三态互斥：池存在→选择器必须成功、active.env 权威（与 `S4_AGENT_ENV_FILE` 无关）；无池→单 key 文件必须设置且可信；皆无→`model_credential_error` REFUSE | `run_agent_task.sh` 凭据段 |
| R4 | P0-4 | 读侧与写侧同强度信任检查：普通文件、非 symlink、root 属主、**零 group/other 位**、祖先链 root 且不可 go-w；选择器对全部池文件预检；`S4_KEY_POOL_SCRIPT` 覆盖路径同检；**health.cache 与 glm_active 读取也过 `trust_file`**（r3 补强） | 两文件 `trust_*` 系列 |
| R5 | M1 | runner 双 id 回退仅限业务 404（body 精确 `"Record not found"`）；认证/歧义 404 首次即抛；业务 404 在异常对象上标 `s4_business_404`，评分器优先看标记、body 已消费也不误判 | `plugins/cybergym_runner.py` `query_poc`/`is_business_not_found`、`scripts/score_all.py` `query_records` |
| R6 | M2 | `score_all` 拆双捕获域：服务器查询→`query_error`（exit 2）；submit.sh 解析/账本/`bind_evidence`→`integrity_error`（exit 1）；docstring 状态清单已含 `integrity_error` | `scripts/score_all.py` 主循环 |
| R7 | M3/M4 | 真实文件解析测试（plain/quoted/CRLF 过；export 前缀/BOM/缺键拒）+ 真 runner 双 id 序列测试（business×2、auth-first、business→success） | `tests/test_score_all.py` `TestRealFileParsing`/`TestRunnerDualIdSequences`/`TestIntegrityVsQuerySplit` |
| R8 | M5 | 探针结果缓存 `health.cache`（`S4_KEY_POOL_TTL` 默认 120s；0=禁用）；锁内读缓存选择，二次启动 0 探针 | `key_pool_s4.sh` 缓存段 |
| R9 | M6/M7 | sticky 需 active 与源逐字节一致；全失败仅**全 429** 保 active，任何非 429 且无健康 key → rc=1（REFUSE） | `key_pool_s4.sh` 主流程尾部 |
| R10 | M8 | stage-3 部署版 `key_pool.sh` 已 vendor 为 `docs/reference/stage3_key_pool.sh`（sha256 `309d1cfa…`），策略可比对；文档明示触发点差异（每题启动 vs cron 5min） | `docs/reference/`、`docs/STAGE4_DEPLOY.md` |

另有两个项目方自报并已修的缺陷（验证修复真实性）：
- probe 子 shell `die` 被吞 → 调用点 `|| die`（`key_pool_s4.sh` 探针循环注释处）；
- `TTL=0` 同秒缓存仍可用 → 0 彻底禁用。

## 2. 本轮重点攻击面（新实现首次细审）

### 2.1 严格解析器能否被绕过
- 值内含 `=`（`KEY=a=b` 取 `a=b`？）；`export  KEY=`（多空格）；`export<TAB>`；
  行内注释；同一键重复出现（谁赢？）；空值（`KEY=`）通过与否、通过后后果；
- 引号成对剥离后值含空格/`$`/反引号：charset 正则是否兜住全部残余形态？
  `[[ =~ ]]` 在 bash 各版本对 locale 的行为（`[A-Za-z]` vs `[[:alpha:]]`）；
- `run_agent_task.sh` 与 `key_pool_s4.sh` 两份解析器是否逐行为一致？任何
  分叉即轮换用键 A、wrapper 校验键 B 的窗口。

### 2.2 信任链与时序
- TOCTOU：`trust_file` 通过后、`cat/cmp` 使用前，文件被换（root 内威胁
  模型下是否可接受？请在威胁模型一节明确表态）；
- 选择器（flock 内）写完 active.env 到 wrapper（无锁）读之间发生切换：
  wrapper 读到的是哪份？是否原子（`mv` 同目录 rename 语义）？
- `S4_KEY_POOL_SCRIPT` 默认 `/root/key_pool_s4.sh`：祖先链检查是否覆盖
  `/root` 本身（0700 root）？攻击者可控的覆盖路径能通过哪些检查？
- health.cache 被篡改（若绕过目录权限）最坏后果是什么？缓存毒化能否让
  "全部 429 保 active" 分支被伪造触发？（rc 只影响启动，但请评估预算面）

### 2.3 评分链新语义
- `s4_business_404` 属性：除 runner 外还有谁会设置？评分器
  `getattr(e, ...) or is_business_not_found(e)` 的短路顺序在 body 已消费
  与未消费两种情况下各等价于什么？伪造该属性需要什么能力？
- `integrity_error`：与 `verify_stage4.py` 的状态白名单交互（submitted 行
  非 ok 即 violation；非 submitted 行的 (None/no_records/no_ledger) 集合）
  是否会把 integrity_error 行报成别的类别或漏报？
- 双捕获域边界：`parse_submit_sh`/runner 构造被归入哪域？与 M2 最小修法
  的归类建议一致吗？

### 2.4 三态 wrapper
- mode-2（无池、单 key）：`S4_AGENT_ENV_FILE` 设置但文件不可信（0644/
  symlink/非 root）各拒绝路径；mode-3 REFUSE 是否在任何预算消耗之前；
- `CRED_ARGS` 只含 4 白名单变量：池/agent.env 都解析后，AGCLI_CTX 的
  合并次序（池文件无该键时是否保留 agent.env 值）与文档声明一致？

### 2.5 测试反同义反复
- 24 例中仍 mock 掉什么？`TestIntegrityVsQuerySplit` 的 stub runner 绕过
  了哪些真实路径？e2e 只覆盖 mode-1，mode-2/3 的 REFUSE 是静态确认
  ——请按"代码确认/需现场验证"分级。

## 3. 输出格式（与前两轮相同，严格）

```markdown
## 一、实际改动范围
## 二、BLOCKER（P0）
## 三、MAJOR（P1）
## 四、r2 修复确认表
| 上轮编号 | 是否关闭 | 依据(path:line) |
覆盖 R1-R10 与两个自报缺陷
## 五、可被利用/退化路径（若有）
## 六、最终裁决
| 维度 | 裁决 |
至少含：R1-R10 逐项、key 池可否进正式跑、PR #1 可否合并（整只/拆分）、
50 题 pilot GO/NO-GO
```

## 4. 禁止

- 不要重述项目方主张；逐条证伪。
- 不要把 `docs/EVIDENCE_PR1_REREVIEW.md` 的现场输出当代码证据。
- 不要索要密钥明文、poc.db、会话日志、宿主机访问。
- 不要"加强测试/完善文档"空话；每条落到 path:line 与最小修法。
- 不要混用旧快照：只审 PR #1 head（对照 `MANIFEST.sha256` 48 条指纹）。

## 5. 建议读序

1. `root_code/key_pool_s4.sh` 全文 → R1/R2/R4/R8/R9 + 2.1/2.2
2. `cybergym-agent/scripts/run_agent_task.sh` 凭据段 → R3/R4 + 2.4
3. `plugins/cybergym_runner.py` `query_poc`/`is_business_not_found` +
   `scripts/score_all.py` → R5/R6 + 2.3
4. `tests/test_score_all.py` → R7 + 2.5
5. `docs/reference/stage3_key_pool.sh` 对照 → R10
