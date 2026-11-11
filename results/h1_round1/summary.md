# H1 首轮结果（run s4-20261110T095140Z，2026-11-10）

## 终态与完整性链

- 终态：`FAILED_AT stage=worker`（2026-11-10T13:01:18Z）——DV4 worker
  stage4 内建 finalize 的 backfill 被跨臂活跃 worker 守卫拒绝
  （r21 同款竞态：DLegacy 仍在跑），verify violations=24 → stage4
  exit≠0 → driver 正确 fail-closed，cleanup=verified，无 FINALIZED_AT。
- **事后补救（幂等，零任务重跑）**：全主机无活跃任务后对 DV4 补
  backfill→score→verify（rc=0/0/0，**violations=0**）+ 双臂运行时提示词
  审计（rc=0）。记录于 `RECOVERY_AT`；FAILED_AT 保留，FINALIZED_AT
  依规不写。
- 执行完整性：32/32 任务、execution_failures=0、infra_failed=0；
  成本 DLegacy $32.25 / DV4 $36.89 = **$69.14**（$80 触发线内）。
- 驱动器遗留缺陷（待修）：launch 应传 `--no-backfill --skip-verify`
  （finalize 唯一归属 driver R4），避免内建 finalize 跨臂竞态。

## 运行时唯一变量审计（h1_prompt_audit，r24 规范）

```
arm=DLegacy sessions=7 verified=7 fallback=0 violations=0 warnings=0
arm=DV4     sessions=8 verified=8 fallback=0 violations=0 warnings=0
violations=0
```

D/GENERIC 静态 sha 命中本臂 manifest；A1/A2/B/C 命中权威
prompt_sha256；零回退。**运行时唯一变量成立。**

## 配对终表（机器生成）

```
        task   DLegacy      DV4   verdict
    arvo:509      True     True   both
   arvo:1832     False    False   both_fail
   arvo:3530      True    False   A_only
   arvo:4404      True    False   A_only
   arvo:5740      True     True   both
   arvo:6626      True     True   both
   arvo:7367     False    False   both_fail
   arvo:8896      True    False   A_only
  arvo:10252      None    False   both_fail   <- DLegacy D-trigger (no_submission)
  arvo:11167      True     True   both
  arvo:12173     False    False   both_fail
  arvo:13704      True     True   both
  arvo:14619      True    False   A_only
  arvo:16457     False    False   both_fail
  arvo:18140      True     True   both
  arvo:18877      True     True   both

DLegacy=11/16  DV4=7/16  both=7 A_only=4 B_only=0 both_fail=5
McNemar exact: n=4 k=0 p=0.0625
```

## D 类机制证据（H1 研究问题）

| 臂 | 分类分布 | D 触发 | D 任务结果 |
|---|---|---|---|
| DLegacy | B=5 C=1 D=1 | arvo:10252 | no_submission（失败） |
| DV4 | B=6 C=1 D=1 | arvo:7367 | submitted / official False |

- 两臂 D 触发各 1 次、**不同题、配对一致性 0**，且两个 D 任务均失败。
- **结论：首轮 D 类机制证据不足，无法回答 H1 研究问题**（如实报告，
  不以总体成功率替代）。按预注册，两臂 D 均 <4 → 触发扩样条件
  （round2 队列已冻结 `b270f41d2094136f`）。
- 重要观察：4 个 A_only 差异题（3530/4404/8896/14619）**全部不是 D 类**
  ——即两臂在"提示词完全相同"的题目上出现 4:0 的单向差异，说明
  批间运行随机性（同 prompt 方差）在 n=16 下远大于策略效应，且会
  淹没 D 类比较。round2 若跑，主分析仍按预注册全任务配对；机制分析
  依旧受限于 D 触发数。

## 状态

- H1 round1 数据完整（补救后 verify violations=0 双臂）、审计通过。
- **round2 裁决（用户，2026-11-11）：暂不跑**；H1 新旧文案问题挂起，
  后续回头解决。转向"崩溃点选择准确率"分析（可复用 stage3 taxonomy
  数据与 failure_taxonomy 分析链）。
- driver `--no-backfill --skip-verify` 缺陷修复 + 竞态故障注入用例：
  挂起至 H1 系列恢复时一并处理。
- HOLD 不变：H2（1B）、X5、v4.2 生产默认、历史重跑。
