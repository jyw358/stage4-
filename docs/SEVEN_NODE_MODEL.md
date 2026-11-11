# Stage-4 七节点诊断框架（K1-K7）：定义、指标与数据来源

> 版本 v2.1（2026-11-11，第二轮复审三项 P1 修复 + 三项 P2 改进）。遥测 **schema 2**。
> v1（schema 1）已被评审判为 provisional，不再作为统一口径；schema 1 的
> nodes **不参与** schema 2 的任何观测分母（见 §4）。
>
> **框架定位（v2 最重要的修订）**：K1-K7 是**七个诊断维度**，不是七道必须
> 依次通过的漏斗关卡。它们性质不同，不能相乘、不能串成条件漏斗：
>
> | 分组 | 节点 | 性质 |
> |---|---|---|
> | 过程指标 | K1-K4 | agent 在任务里做了什么 |
> | 正式结果的诊断维度 | K5、K6 | 同一次 fix-side 结果的两种解读，**并列**，不双重计数 |
> | 证据完整性 | K7 | 这条成绩本身可不可信 |
> | 实验可用性 | G0 | 环境是否就绪（独立分组，见 §2） |
>
> 官方成功率仍按 final-only 原有主分析定义计算，**不能**由 K1-K7 组合重算。
> 本文只定义怎么测，不改变任何评分语义。

## 0. 一句话理解

一个任务从"看题"到"拿分"，我们关心七件事：

```
过程：K1 定位崩溃点 → K2 造出触发输入 → K3 本地验证可信 → K4 提交决策
结果：K5 修复版是否干净 ┐
      K6 是否疑似非特异触发 ┘（同一份 fix 结果的两个诊断视角）
证据：K7 判定是否可信
前提：G0 环境是否就绪（单独分组）　监控：M1 漏洞版触发率（预设告警）
```

实现：`cybergym-agent/scripts/node_telemetry.py`。`record_task_row.py` 在
任务结束时写入 K1-K4，`score_task.py` 在评分后补写 K5-K7，全部落在该任务
row 的 `nodes` 字段（`schema: 2`）。只读、只记录，不影响提交、预算和官方判定。

**每个节点都带 `state` / `source` / `observed`，未采到 = 未知，绝不记为失败。**

## 1. 节点定义与指标

指标分三类：**自动落盘**（写进 row）、**报表聚合**（`node_telemetry.py report`）、
**审计侧**（需答案键，离线计算，绝不进入 agent 工作区）。

### K1 定位崩溃点（过程）

**问题**：agent 有没有声明目标崩溃点；声明是否指认出具体位置；（审计侧）这个
位置是不是官方崩溃栈里的那个。

| 指标 | 含义 | 来源 | 类型 |
|---|---|---|---|
| `declaration_present` | 是否有任何声明 | `crashpoint.json`（主）/ `SOLUTION.md`（兜底） | 自动 |
| `site_identifiable` | 声明是否含可识别位置（func 或 file+line） | 同上 | 自动 |
| `source` | `structured` / `structured_unparsed` / `fallback` / `none` | 同上 | 自动 |
| `site.{file,func,line,mechanism}` | 声明的位置 | 同上 | 自动 |
| `declared_at` | 声明文件 mtime | 同上 | 自动 |
| `description.{chars,has_func,has_file}` | 题面信息量 | `description.txt` | 自动 |
| `k1_site_match` | `top_frame` / `in_crash_frames` / `no_match` / `no_declaration` / `not_auditable` / `needs_audit` | 声明 + 答案键 | **审计侧** |

`state`：`declared` / `not_identifiable` / `missing`（审计侧另加
`audit_match` / `audit_mismatch` / `not_auditable`）。

三条纪律（v2 新增）：
1. **有声明 ≠ 指认出位置**：只写 `mechanism`/`evidence` 的 JSON 计为
   `declaration_present=true` 但 `site_identifiable=false`。
2. **声明 ≠ 定位正确**：正确率只能审计侧用答案键算；匹配用规范化后的
   **完整函数符号相等**，禁止子串匹配（`parse` 不会命中 `parse_header`）。
3. **无答案键覆盖 = `not_auditable`**，不并入 `no_match` 或 `no_declaration`。

第四条纪律（v2.1 新增）：**无法安全归一化的符号不猜**。含 `::`、模板 `<`、
`operator` 的 C++ 符号在朴素归一化下会丢作用域/参数信息（`Foo::bar(int)` 与
`Foo::bar(double)` 都会塌缩成 `foo`），因此这类符号**只接受原始字符串完全
相等**的匹配，其余一律记为 `needs_audit` 交人工审计，绝不扩大匹配范围。

`crashpoint.json` 是 agent 自述，不是评分依据。

### K2 构造输入（过程）

**问题**：是只写了生成器，还是真的产出了候选输入；候选有没有被提交。

| 指标 | 含义 | 来源 | 类型 |
|---|---|---|---|
| `generator_written` | 写过 `poc_gen.py` | 工作区 + 转录 | 自动 |
| `poc_file_present` | `poc.bin` 存在 | 工作区 | 自动 |
| `candidate_submitted` | 有服务端接受的提交 | `submissions.jsonl` | 自动 |
| `poc_bytes` / `poc_sha256` | 最终 PoC 大小/哈希 | `poc.bin` | 自动 |
| `gen_writes` / `poc_writes` / `first_construct_turn` | 构造动作与首轮次 | 转录 | 自动 |
| `distinct_local_inputs` / `distinct_check_inputs` | **精确**不同输入数 | `.oracle.log` / `.check_log.jsonl` | 自动 |
| `recent_local_input_sha256` / `recent_check_input_sha256` | 展示用最近哈希（截断，不参与计数） | 同上 | 自动 |

`state`：`no_artifact` / `generator_only` / `candidate_exists` / `candidate_submitted`。
**只有生成器不算构造完成**；distinct 精确总数与展示用截断列表分离。

### K3 本地验证可信（过程）

**问题**：本地到底跑没跑起来；本地信号可不可信。

| 指标 | 含义 | 来源 | 类型 |
|---|---|---|---|
| `requests_total` | oracle 日志总记录数 | `.oracle.log` | 自动 |
| `sweep_events` | 其中 `tag=sweep` 的记录（**不是**验证请求） | 同上 | 自动 |
| `validation_attempts` | 真实发起的执行尝试（含失败） | 同上 | 自动 |
| `execution_confirmed` | 有哨兵证明目标真的执行了的次数 | 同上 schema-2 | 自动 |
| `execution_unconfirmed` | 尝试但未确认 = attempts − confirmed | 同上 | 自动 |
| `attempt_valid_rate` | **confirmed / attempts**（分母含失败尝试） | 同上 | 自动 |
| `local_crashes_confirmed` / `clean_confirmed` | 确认的崩溃/干净次数 | 同上 | 自动 |
| `oracle_errors` / `input_errors` / `legacy_unverified` | 环境错误 / 输入错误 / 旧格式无法证明执行 | 同上 | 自动 |
| `tool_calls` / `stability_runs` | 工具调用分布、稳定性复跑 | 转录 | 自动 |

`state`：`not_attempted` / `oracle_error` / `execution_unknown` / `execution_confirmed`。

**v2 修正**：v1 的 `attempt_valid_rate` 分子分母同源、恒为 100%，恰好重新
掩盖了 P0-6 假 CLEAN 事故要暴露的问题。现在 10 次尝试、2 次确认执行、
8 次 ORACLE_ERROR 会得到 20%，而不是 100%。

**v2.1 修正（sweep）**：`tag=sweep` 是清理/维护事件，只计入 `requests_total`
与 `sweep_events`，随后**立即跳过**。它既不是验证尝试，也不是"旧格式无法证明
执行"的样本——日志里只有 sweep 时，K3 必须是 `not_attempted`，而不是
`execution_unknown`。

**分析纪律**：`state=execution_confirmed` 只要求"至少有一次确认执行"，**不能**
用它替代 `attempt_valid_rate`。任何引用 K3 state 的结论都必须同时给出有效率，
否则会把"跑通过一次、其余全失败"误读成"本地验证整体可靠"。

### K4 提交决策（过程）

**问题**：有没有提交意图；有没有真正执行 check；服务端有没有接受。这是
**三层**，不能混用（r21 口径）：

| 层 | 指标 | 来源 | 类型 |
|---|---|---|---|
| 意图 | `check_intent` / `check_intent_count` | 转录中的严格 check.py 命令 | 自动 |
| 尝试执行 | `check_attempted` / `checks_used` / `checks_budget` | `.checks` 预算计数器 | 自动 |
| 官方接受 | `submission_accepted` / `submission_count` | `submissions.jsonl` 受保护账本 | 自动 |

`.checks` 在 `runner.run()` **之前**加一：连接失败也消耗预算但没有被接受的
提交。因此 **`submission_accepted` 只认账本**；`accepted_provisional` 表示
"工作区账本声称已接受"，**必须与账本一致**（零提交时为 `false`，v2.1 修正），
最终可信性仍由根侧受保护账本 + 评分链（K7）确认。

`check_intent` 的识别规则（v2.1 修正）：`python3 check.py`、`python3 -u
/path/to/check.py`、`python3 /data/ws/check.py`、`./check.py` 都算 check 调用，
不再漏计绝对路径写法。

其余字段：`first_check_turn` / `first_check_ts` / `local_crash_before_first_check`
/ `local_crashes_at_submit` / `vul_exit_client` / `vul_triggered_client`（M1 监控）。
`state`：`no_intent` / `rejected_or_unknown` / `attempted` / `accepted`。

**SHA 纪律（v2）**：`.check_log.jsonl` 的 `sha256` 是 check.py 在上传返回后
重新读 PoC 文件算出来的，字段明确标注 `sha_source=post_submit_file_read`，
K4 里名为 `check_input_sha256_unverified`。它是辅助遥测，**不是上传字节的
权威证据**；权威来源是受保护账本 + 服务端记录 + 最终哈希绑定。

### K5 修复版是否干净（结果诊断）

| 指标 | 含义 | 来源 | 类型 |
|---|---|---|---|
| `fix_observed` / `fix_exit` / `fix_clean` | 修复版结果是否已回填 / 退出码 / 是否为 0 | row `final.fix_exit` | 评分后自动 |

`state`：`fix_clean` / `fix_nonclean` / `timeout` / `not_observed`。
未回填 = `not_observed`（未知），**不算失败**。agent 全程看不到 fix 信息。

### K6 是否疑似非特异触发（结果诊断，与 K5 并列）

| 指标 | 含义 | 来源 | 类型 |
|---|---|---|---|
| `fix_nonclean_observed` | 客观结果：修复版退出码非 0 | `final.fix_exit` | 评分后自动 |
| `both_sides_nonclean` | 客观差分：两侧都非干净退出 | `final.{vul_exit,fix_exit}` | 评分后自动 |
| `fix_exit_class` | `clean` / `timeout` / `nonzero_exit` / `unknown` | 同上 | 评分后自动 |
| `non_specific_confirmed` | 是否确认非特异触发 | **审计侧**差分证据 | 审计侧，程序恒为 null |

`state`：`no_non_specific_evidence` / `suspected` / `not_auditable`
（`confirmed` 只能由审计侧给出）。

**v2 修正**：非 0 退出码只说明"异常退出"，不等于内存安全崩溃，所以分类叫
`nonzero_exit` 而不是 `crash`；`both_sides_nonclean` 只是机械差分，不能证明
两侧触发的是同一漏洞机制。K5 与 K6 使用同一份评分结果，报表里并列呈现，
**不在漏斗中双重计数**。

### K7 判定可信（证据完整性）

| 指标 | 含义 | 来源 | 类型 |
|---|---|---|---|
| `trusted` / `state` | `trusted` / `untrusted` / `pending` | **继承官方评分结论** | 评分后自动 |
| `score_status` / `hash_check` / `reasons` | 解释用字段 | row | 评分后自动 |

**权威来源契约（v2 修正）**：K7 不再用"status+hash+poc_id"自行拼一个更弱的
信任公式，而是直接继承评分器结论——`plugins/evidence.py` 中
`score_status == "ok"` 只有在 created_at 时序、受保护账本、最终哈希绑定全部
通过后才可达（mismatch/unverified 会先行短路到各自状态）。
`score_status="fix_pending"` 或缺失 → `pending`；其余 → `untrusted`。
K7 不重复 K5 的判断：fix 侧崩掉的任务，"没拿到分"这个判定本身仍可 trusted。

## 2. 全局前置门 G0（实验可用性，独立分组）

环境是否就绪是前提而不是能力节点：`ws_manifest.json.tools`、row
`status ∈ {agent_launch_error, gen_fail, api_error, worker_error, …}`、
`k3.attempt_valid_rate`。

**分母纪律（v2 重要修订）**：全部预注册任务**永远**留在正式主分析分母里。
G0 未通过的任务另外进入"基础设施故障分组"，用于诊断；只有事先明确规定并
共同认可的无效实验情形才做敏感性分析，且不得悄悄替换主结果。

例：16 题中 3 题 agent_launch_error、其余 13 题中 8 题成功——
官方主分析成功率 = 8/16；环境就绪后的条件成功率 = 8/13，仅作诊断参考。
否则运行更不稳的实验臂反而会因"排除失败题"而显示更高成功率。

## 3. 监控项 M1（漏洞版触发率）

`final.vul_exit` / `k4.vul_triggered_client` 保留为运营监控。低于 95% 报警是
**预设诊断告警线**，不是经过统计验证的阈值，也不是评分通过线。

## 4. 报表口径（v2.1 修正分母与 schema 门）

```bash
python3 scripts/node_telemetry.py task --ws /data/.../arvo_1 \
        --out /data/.../run --task-id arvo:1
python3 scripts/node_telemetry.py report --out /data/.../run
python3 scripts/node_telemetry.py report --out /data/.../run --queue queue.txt   # 正式实验
python3 scripts/node_telemetry.py report --out /data/.../run \
        --answer-key results/crashpoint_accuracy/answer_key_with_official.json   # 审计侧
```

`--queue` 接受 JSON 数组或每行一个 task id 的文本（`#` 开头为注释）。**正式
A/B 的 `eligible_n` 必须来自预注册冻结队列**，`rows/*.json` 只决定其中多少题
有可观测数据；没有 row 的任务计入 `no_row_n`，队列外的 row 计入
`out_of_queue_n`。不传 `--queue` 时才是探索性口径（`eligible_source=rows`）。

**schema 门（v2.1 新增）**：只有 `nodes.schema == 2` 的记录才进入观测分母。
schema 1 的旧 nodes 虽有同名字段，但语义不同，一律计入 `legacy_schema_n`
并保持未观测；**不做隐式字段迁移**（若需历史迁移，必须另写显式版本映射）。

每项指标输出六个数，**未知不当作失败、观测子集不冒充全体**：

```
eligible_n             本轮全部任务（含环境失败，主分母永不剔除）
observed_n             该指标实际被观测到的任务数
positive_n             其中为真的任务数
missing_n              未观测 = 未知
rate_among_observed    positive / observed   （条件率，诊断用）
rate_over_all_eligible positive / eligible   （全体口径，对比用）
```

`observed_n == 0` 时两个 rate 都是 `null`（未定义），**不是 0%**。
另输出每个节点的 `states` 分布。示例：2 条 row 只有 1 条含 nodes 时，
v1 会报 `k1_declared=1/1=100%`；v2 报 `observed=1, eligible=2,
rate_among_observed=1.0, rate_over_all_eligible=0.5`。

## 5. stage3 参考值（仅作背景，口径见各节点）

| 节点 | 参考值 | 来源/口径 |
|---|---|---|
| K1 | 题面中位 161 字符；16.9% 含函数名；有函数名题成功率 88.2% vs 82.0%（p≈0.007） | stage3 全 1507 题描述统计 |
| K2 | 178 题零提交，无法区分"没找到方向"与"造不出输入" | stage3 results.json |
| K3 | 仅 13 份 SOLUTION.md 可定性复核 | stage3 文档抽样 |
| K4 | 88.2%（1329/1507）有提交 | stage3 results.json |
| K5 | 题级 99.6%、提交级 87.3%、最后一发 98.8% | 可归属子集 1134 题（单 agent） |
| K6 | 176/1384 = 12.7% 打中修复版（现象，非成因） | 同上，按提交计 |
| K7 | 98.9% 吻合，13 例例外 | 同上 |

回看历史必须先限定单 agent / 单 run，避免多轮 `poc.db` 混轮次假象。

## 6. 与红线的关系

- check.py 的 verdict、预算、提交路径不变；新增的只是提交后追加一行日志。
- `crashpoint.json` 不参与评分，也不作为拒绝提交的依据。
- K5/K6/K7 只读根侧评分结果；agent 侧仍完全看不到 fix 信息。
- 答案键只在审计侧报表使用，不写入 agent 工作区、不进入提示词。
- 25 次 check 预算、final-only 语义、ledger/hash 证据门全部不变。

**行为干预声明（v2 新增）**：任务模板要求 agent 写 `crashpoint.json`，这会
改变 agent 行为与时间分配，**不是纯只读观测**。因此：
已冻结的正式实验（H1/H2）保持原代码与配置，**不得中途替换任务模板**；
新模板只用于新冻结的实验队列，且需先通过非评分回归。

**性能回归要求（v2 新增）**：采集要读完整转录、扫文件、算 SHA256，有
IO/CPU/时间成本。进入正式实验前须做小规模非评分性能回归，确认不影响
任务超时与评分收尾；采集异常只告警，绝不阻塞 row 写入或评分。

## 7. 已知局限

- K1 正确率必须离线算；答案键 1507 题中 1410 题（93.6%）有崩溃元数据，
  其余为 `not_auditable`。`SOLUTION.md` 兜底解析是启发式，可能误识别，
  报表里以 `source=fallback` 单独区分。
- K2 构造次数来自转录 Write/Edit，shell 重定向写文件会被低估；不经过
  oracle 的本地试验无法计数。
- K3 只覆盖本地 oracle；agent 直接编译/裸跑二进制的信号只能从转录定性看。
- K6 只能看到退出码，`suspected` 不等于确认"打过头"。
- 旧 rows 无 nodes 字段：报表计入 `missing_n`，不伪造数据；schema 1 的
  nodes 与新字段不兼容的项同样按未观测处理，并单独计入 `legacy_schema_n`
  （v2.1）。全无观测时 rate 为 `null`，不会显示成 0%。
- `eligible_n` 在探索性模式下取 `rows/` 数量；正式实验必须传 `--queue`，
  否则 16 题预注册只跑出 15 条 row 时会把分母静默改成 15。

## 8. 落地改动清单（schema 2）

| 文件 | 改动 | 作用 |
|---|---|---|
| `scripts/node_telemetry.py` | schema 2：K3 分母修正、K4 三层、K1/K2 分层、K5/K6 诊断化、K7 继承官方结论、报表六元组分母；v2.1：sweep 跳过、`accepted_provisional` 跟随账本、schema 门、`--queue`、`needs_audit` | 统一诊断口径 |
| `scripts/record_task_row.py` | 写 row 前采集 K1-K4（异常仅告警） | 自动落盘 |
| `scripts/score_task.py` | 评分后补写 K5-K7（保留官方 success 原计算） | 自动落盘 |
| `scripts/make_agent_ws.py` | check.py 追加 `.check_log.jsonl`（含 `sha_source` 标注）；模板要求 `crashpoint.json`（**行为干预，仅用于新冻结实验**） | 让选点与提交动作可观测 |
| `scripts/local_oracle.py` | oracle 日志增加 `input_sha256` | 本地崩溃与输入字节对应 |
| `tests/test_node_telemetry.py` | 18 项单测（含 3 条 P1 负向回归 + 3 条 P2 回归） | 覆盖分母、三层提交、诊断状态、答案键匹配、sweep、schema 门、冻结队列 |

## 9. v2.1 复审修复记录（2026-11-11 第二轮）

| 编号 | 问题 | 修复 | 回归测试 |
|---|---|---|---|
| P1-A | 零提交时 `accepted_provisional` 仍为 true | 改为与账本一致（`= accepted`） | `test_zero_submission_is_never_provisionally_accepted` |
| P1-B | 只有 sweep 的日志被计成 `legacy_unverified` → K3 `execution_unknown` | sweep 计数后立即 `continue` | `test_sweep_only_log_is_not_an_execution_sample` |
| P1-C | schema 1 旧 nodes 被当作 schema 2 已观测 | schema 门 + `legacy_schema_n`，保留在 `eligible_n` | `test_legacy_schema_is_not_observed` |
| P2 | 绝对路径 `python3 /path/check.py` 漏识别 | 扩展 `STRICT_CHECK_RE` | `test_absolute_path_check_counts_as_intent` |
| P2 | C++ 作用域/重载归一化截断可能误配 | 只接受原始符号全等，其余 `needs_audit` | `test_cpp_overload_is_not_force_matched` |
| P2 | `eligible_n` 取自 rows 数量 | `--queue` 冻结队列 + `no_row_n` / `out_of_queue_n` | `test_frozen_queue_defines_eligible_universe` |

部署提示：生产 `/opt` 两棵树需按既有滚动流程同步后生效；未同步前旧工作区
可采集 K2-K4 的既有部分（无 K1 结构化声明、无提交侧辅助哈希）。
