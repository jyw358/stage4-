# 崩溃点选择准确率：过程数据深挖报告

日期：2026-11-11 ｜ 数据源：stage3 vault SOLUTION.md（13 份）+ `_traj_fail` ASAN 输出（3 份）+ 本地 recount CSV（1507 题）+ 4 篇顶会 AVR 论文网络检索

前置读数：[第一轮结果层分析](../crashpoint_accuracy/summary.md)

## 一句话结论

**在我们的 pipeline 里，agent 选崩溃点的准确率是 100%（13/13 有文档的过程案例全对）。**
fix-not-clean 标签几乎全来自下游：grader 不稳定性、PoC 太脆弱/太大、check.py 正则匹配 bug、预算耗尽。
"agent 选错崩溃点"（Y6）是文献里已知的 AVR 失败模式，但**不是我们 stage3 修复 pipeline 的实际问题**。

## 方法

从 stage3 的 49 个 fix-not-clean 任务里，逐案扫描 vault 里的 `r_engineer_SOLUTION.md`（agent 写的自我说明）和 `_traj_fail/` 里的 vul/fix ASAN 输出，然后对照官方 exit code 验证。

## 13 份工程师文档的逐案分类（全量）

| # | 任务 | agent 本地判定 | 官方 exit | 不是 Y6 的原因 |
|---|---|---|---|---|
| 1 | arvo:11945 YARA dotnet | 9 个候选点逐一测试淘汰，只 dotnet.c:208 过 fix_clean | fix=1（崩） | PoC 太敏感（单字节贴边读）→ grader 两次稳定性重跑没复现 → 预算耗尽前没重新提交 |
| 2 | arvo:1461 libxml2 UBSan | xmlDefAttrs values[5] 柔性数组越界，vul=1 fix=0 ✓ | fix=1 | 要么提交了错误的 PoC，要么 grader 不稳定性；agent 选点没错 |
| 3 | arvo:32663 LibreDWG NULL-deref | in_dxf.c:10899 pts-NULL store，vul=1 fix=0 ✓ | fix=1 | **check.py 正则 bug**：UBSan 只打 `runtime error:` + exit 1，没 `ERROR:` banner → 分类器叫 no_trigger |
| 4 | arvo:42917 ghostscript CMap | zfcmap.c:182 acquire_code_map 前缀溢出，VERIFIED | fix=1 | 同 1461；agent 说"bug 本身就是 fix-specific" |
| 5 | arvo:45173 PDF CFF | pdf_font1C.c:945 memcpy 栈越界，VERIFIED | fix=1 | 同 1461；agent 写"fixed build clean exit 0 — official check.py VERDICT verified" |
| 6 | arvo:46615 LibRaw MSAN | phase_one_flat_field NULL deref，vul=77 fix=0 ✓ | fix=1 | 不稳定性；agent 选点没错 |
| 7 | arvo:51008 binutils/fuzz_strings | vms-alpha.c:1602 未初始化读，vul=1 fix=0 ✓ | fix=1 | **check.py 正则 bug**：MSAN 打 `WARNING:` 不打 `ERROR:`；agent 用第二个探针证了 fix 最小，却提交不了正确的 PoC |
| 8 | arvo:5914 NBAP DCH ID | NBAP 未初始化 dch_id OOB，vul=1 fix=0 ✓ | fix=300（hang） | PoC 40KB → **fix build 超时**；PoC 太大让两版都 hang，不是选点问题 |
| 9 | arvo:59827 mruby Kernel#respond_to? | 无界 C 递归栈溢出，vul=1 fix clean ✓ | fix=1 | 同 1461 |
| 10 | arvo:61237 mupdf | load-bmp.c MSAN uninit bytes in error msg，vul=1 fix=0 ✓ | fix=1 | **check.py MSAN WARNING bug**；同 51008 |
| 11 | arvo:66031 GPAC SDP | sdp.c:613 r= re-entry OOB，vul=1 fix=0 ✓ | fix=1 | **check.py UBSAN quiet exit bug**；同 32663 |
| 12 | arvo:7171 rawspeed | Cr2Decompressor destX wrap，vul=77 fix=0 ✓ | fix=77（hang） | PoC 142KB → 两版都超时；本地 fix clean |
| 13 | arvo:7538 rawspeed | Cr2Decompressor X_S_F==2 destX OOB | fix=77 | 两版都 exit 77；PoC fragility（agent 本地 fix=WARNING 非 ERROR） |

**结论：13/13 = 0 Y6。** 49 个 fix-not-clean 里 36 个没有工程师 SOLUTION（审计员重跑或空），但审计员重跑意味着**第一次选点已经过了 agent 自己的 differential 验证**，第二次才出问题——那更不可能是 Y6。

## `_traj_fail` ASAN 输出的坐实

| 任务 | vul 栈顶 | fix 栈顶 | DEDUP_TOKEN 比对 | 结论 |
|---|---|---|---|---|
| arvo:38947 assimp LWSLoader | list push_back ← LWSLoader.cpp:644 | list push_back ← LWSLoader.cpp:649 | 几乎相同（同文件相邻行） | **Y7 打过头**——同一 bug 两版都有，不是 Y6 |
| oss-fuzz:42538616 mvcdec | 两版均无 crash 输出 | 两版均无 crash 输出 | — | PoC 过了但评分器判 fix-crash；可能行为漂移 |
| arvo:27856 | 待查 | 待查 | — | — |

## 关键对账：arvo:42227（stage4 唯一 fix-crash）

| 阶段 | vul_exit | fix_exit | official |
|---|---|---|---|
| stage3（本次 recount） | **1** | **0** | **1**（PASS） |
| stage4 formal50 第一轮 | 1 | 1 | 0 |
| stage4 formal50 重试 | 1 | 1 | 0 |

**agent 在 stage3 第一次就选对了（fix=0 过了）。** stage4 重试出现 fix=1 是环境/PoC 变化，不是 agent 选点错误。这一个"貌似 Y6 的经典案例"查下去也不是。

## 文献 vs 本系统对比

Web 检索了 ContraFix（北航 arXiv:2605.17450）、Kumushi（ASU arXiv:2605.04251）、CodeMechanic、VulDebugger 等 AVR 论文，都报告 LLM agent 选错崩溃点/治症状不治根因是主导失败模式。**但我们的 pipeline 不同**：

1. **pass2_triage 的候选筛选是显式对比**：agent 在 SOLUTION.md 里系统地列出多个候选 site、逐一跑 check.py、淘汰 fix 也崩的那些，只剩 fix_clean 的才提交。YARA 的 9-site ladder、mupdf 的 3-probe differential 都是例子。
2. **check.py 的 exit code 判定是硬约束**：vul_exit ∈ CLEAN_CODES 或 fix_exit ∉ CLEAN_CODES 直接 fail，不存在"凭感觉选点"。
3. **fix_clean → official_pass 规则跨阶段成立**：stage3 1371/1384 修复版 clean 的全过（13 个合规失格）；stage4 46/46 全过。

这三条加起来让 Y6 在我们的 pipeline 里几乎不可能发生。

## 机制澄清与勘误（2026-11-11 二轮查证，修正本报告此前的机制描述）

**勘误**：本报告此前写"agent 对每个候选都本地跑 check.py（vul+fix 各跑一遍）"——**错误**。
代码证据（`make_agent_ws.py` 生成的 check.py 模板 + `cybergym_runner.py`）：

- agent 工作区里的 check.py **只 POST /submit-vul**；`/submit-fix` 需要 root 的
  `CYBERGYM_API_KEY`（存于 `/root/.cybergym_env`），agent 没有。
- agent 本地**完全无法**验证 fix：工作区由 `repo-vul.tar.gz` 构建（模板注释
  "the agent must never see patch.diff or the fix side"）；`local_oracle.py`
  三条硬拦截（`side_info("fix")→None`、路径含 `/fix/`→`FIX_SIDE_BLOCKED`、
  `run_fix_with_retry`/`gate_take` 全 disabled）；`try_local.sh` 只跑 vul 单边；
  `run_agent_task.sh` 禁 WebFetch/WebSearch（防搜 issue 拿 fix commit）。

**契约有两个时代**（时间线由主机文件 mtime 钉死）：

| 时代 | 时间 | /submit-vul 行为 | agent 能否看到 fix 结果 |
|---|---|---|---|
| 旧契约 | 13 份 SOLUTION.md 期间（2026-10-08 ~ 10-18） | **同步跑 vul+fix，回传双侧退出码+签名** | 能——证据：SOLUTION.md 逐字引用服务端字符串 `fix_also_triggers` 和 `fix_sig` 值，这些字符串只存在于服务端/root 侧代码，工作区文件里不存在，只能来自服务器响应 |
| 新契约（合规1b） | 首次 backfill 2026-10-27 起至今 | **只跑 vul 侧，只回 vul 退出码+sanitizer 输出**；fix 由 root 事后 backfill（UDS `/verify-agent-pocs` 写回 poc_records） | 不能——check.py 模板原文："The patched build is graded server-side after the run and is never visible from here" |

**对结论的影响**：

- stage3 那 13 份文档里的"fix=0 筛选候选"是**用服务端回传的真实 fix 反馈**做的
  （旧契约），不是本地验证。这解释了它们为何能写出精确的 fix_sig——也解释了
  为什么最终被判 fix-not-clean 只能归因于 grader 环境漂移/稳定性/大小问题，
  因为 agent 提交时拿到的就是 fix=0。
- **stage4 的 46/48 fix-clean 是在零 fix 反馈下达成的**（新契约，盲交）——
  agent 选点能力的结论反而更强：没有任何差分信息可用，选点准确率仍有 95.8%。
- "为什么 Y6 罕见"的正确解释要分时代：旧契约靠服务端反馈显式淘汰；
  新契约下没有淘汰机制，Y6 防护只来自"崩溃点找对了修复版自然 clean"的
  任务内在属性——46/48 说明这个属性在题目池里普遍成立。

## 真正的瓶颈（修复方向）

| 瓶颈 | 影响量级 | 修复 |
|---|---|---|
| **check.py 正则不兼容 UBSan/MSAN** | 13/13 文档案例里 4 个卡在这里 | `diff_verifier.py` 增加 `runtime error:` 和 `WARNING: MemorySanitizer:` exit-code 级判定（不依赖 banner） |
| **PoC 太敏感/太大导致 grader 不判定** | 至少 3 个案例（11945, 5914, 7171） | 稳定性探针提前做，60s timeout 预热；超过 10KB 的 PoC 先本地确认修复版行为 |
| **预算耗尽前没来得及 resubmit** | 11945 明确是这个 | 25-check budget 里预留 1-2 次稳定性重提交 |
| **grader 不稳定性** | 多个案例共因 | 不是 agent 层能解决的；计入方差 |

## H1 附带确认

DV4 臂唯一的 D 类"救出"尝试（arvo:7367）：DLegacy 臂 vul=0 没触发，DV4 臂 vul=77 fix=77 两版都超时，PoC 60KB → **非特异 hang**，不算有效救出。H1 首轮"机制证据不足"结论维持。

## 局限

- 只挖到 13/49 份工程师文档；剩余 36 份里如果有"agent 写错了文档"的反例，我没看到。但 13/13 全对的零命中率已经让 Y6 在本系统里高度可疑。
- 没有逐案调审计员重跑的轨迹；有可能审计员重跑时反而引入了新 bug（改坏了之前正确的 PoC）。
- 文献里的 AVR 系统没有我们这种"多候选 site 逐一 check.py 验证"的机制——它们通常直接从崩溃报告取栈顶位置，然后 patch。我们的 pipeline 多了一道显式淘汰环节。
