# PR #1 第四轮=终审提示词 —— 整段粘贴给评审模型

---

# 任务：Stage-4 PR #1 终审（delta-only，带退出条款）

你是怀疑立场的代码评审员。**本轮是终审**，评审协议如下，请严格遵守：

## 0. 退出条款（先读，决定你如何输出）

1. **只审 delta**：上轮（r3）你审的是 `9f153d9`。本轮只审其后的增量
   `2c5d134`（读侧信任预备）→ `7c47e1d`（r3 修复主体）→ `d562cb7`
   （宕机实证文档）→ 本轮 head。**不重读、不复议全库**；下表"已锁定项"
   直接视为通过，除非 delta 本身使其失效。
2. **阻塞标准收紧为**：仅「可导致官方评分虚增/虚减、凭据泄漏、或 root
   代码执行」的 P0 阻塞合并。可用性/偏好/加固建议（P1/P2）一律写入
   follow-up 清单，**不阻塞**。
3. **输出必须含明确终局裁决**：`APPROVE（可合并）` 或
   `REQUEST CHANGES（列出阻塞级 P0）`，不允许无限期"再看看"。

## 1. 上轮裁决 → 本轮 delta 修复对账（逐项核验真伪）

| r3 编号 | r3 裁决 | 修复位置（head） |
|---|---|---|
| P0-1 缓存可绕过可信性 | P0 | `key_pool_s4.sh`：读侧 `trust_file "$HEALTH_F"`（存在即必须可信，含 symlink/悬挂）；缓存逐键绑定源文件 sha256 指纹，TTL 内轮换/改写任一 `glm-*.env` 即该键失效重探 |
| M1 全 429 未验一致性 | P1 | all-429 保留路径强制 marker 合法、源文件存在、`cmp active==源`；不一致 die（无健康探针时绝不擅自修复） |
| M2 marker 无信任检查 | P1 | marker 读取：存在（含 symlink）即 `trust_file`；写入改 `atomic_write_text`（mktemp+mv+读回复验） |
| M3 归域 | P1 | `score_all.py` 三捕获域：submit.sh 解析+runner 构造→integrity_error；服务器查询→query_error；账本绑定→integrity_error |
| M4 全 429 语义标注 | P1 | `last_selection` 文件区分 `healthy:X` 与 `throttled_keep:X`；日志明示 NOT probe-verified |
| M5 参数校验 | P1 | TTL∈[0,3600]、timeout∈[1,120]，非法即任何探针前 REFUSE |

配套负测（评审建议的四个全覆盖 + 扩展，stub 22 场景，项目方报告）：
可写/伪造缓存拒、TTL 内换 key 重探、全 429 不一致拒、marker symlink 拒。

## 2. 已锁定项（勿复议）

- r2 确认：B1/B2/B3 关闭；4 项 P0（source 执行/写失败静默/池被跳过/属主
  检查不足）关闭；runner 双 id 分类、query/integrity 拆分、真实文件解析
  测试、TTL 缓存、sticky 一致性、stage-3 基线 vendor。
- r3 确认：runner M1 修复"成立"（你的原话）；stage-3 策略对照可核。

## 3. 审计对象与快照纪律

- PR：<https://github.com/jyw358/stage4-/pull/1/files>
- 本轮 head 以后续 commit 为准（提示词撰写时为 `d562cb7` 之后，以 PR
  files 页最新为准；内容指纹 `MANIFEST.sha256` 48 条）。
- **第一行输出必须复述你实际审计的 commit 短哈希**（从 PR commits 页取），
  防止再出现上轮审旧快照的问题（r3 审了 `9f153d9` 而漏掉其后修复）。

## 4. delta 细审指引（可选攻击点，非必答）

- 指纹绑定实现：`file_fp`/`FPS` 数组、no-file 键的指纹语义、缓存写入
  原子性与指纹行完整性；
- all-429 die 路径的 SUMMARY 是否泄露键值（只应有名称+状态码）；
- `atomic_write_text` 读回复验能否被并发窗口欺骗（flock 之下）；
- `last_selection` 是否引入新的信任面（它被谁读？不读则只是审计面）。

## 5. 输出格式（严格）

```markdown
审计 commit：<短哈希>
## 一、delta 修复对账表（r3 六项 + 四负测声明，逐项 PASS/FAIL + path:line）
## 二、阻塞级 P0（仅评分虚增/虚减、凭据泄漏、root 执行；没有就写"无"）
## 三、follow-up（P1/P2，不阻塞，建议开 issue）
## 四、终局裁决：APPROVE（可合并）或 REQUEST CHANGES（列 P0）
## 五、50 题 pilot GO/NO-GO
```

## 6. 禁止

- 不复议已锁定项；不重读全库；不建议重构。
- 不把项目方现场证据当代码证明；不索要密钥/主机访问。
- 每条结论带 `path:line`；没有阻塞级 P0 时不得用 P1 凑数阻塞。
