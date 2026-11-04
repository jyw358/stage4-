# 刻意未包含的内容

本仓库是评审子集，不是完整工作树。以下内容**有意缺失**，请按说明处理，
不要当成缺陷上报。

## 凭据与私密配置

- `.env`、`.glm*.env`、`.llm.env`、`.pi.env`
- `/root/.cybergym_env`（评分用的私有 API key）
- 任何 token、cookie、auth.json

## 跑批主机数据

- `/data/cybergym/**`：题目录、工作区、结果目录、回填日志
- `/root/pocserver-trusted/poc.db`（服务器判定库）
- `/root/agent_logs/**`、`~/.claude/projects/**`（会话轨迹）
- stage-3 的 `final_submission_exitcodes_stage3.csv`、`stage3_scores.xlsx`、
  `reference_intel_inventory.csv` 等历史证据

这些只存在于跑批主机；本轮评审针对代码，不针对那一轮的数据。

## 与 stage-4 主路径无关的 stage-3 遗留脚本

完整工作树里有 300+ 个一次性脚本（`root_code/q*.py`、`rescore*.py`、
`stage1.sh`、`wave*.sh`、`agent50.sh`、`agent200.sh`、`fullrun_*.sh` 等），
以及 `legacy/stage3_root/` 的隔离/回放脚本。它们不在 `stage4.py` 的运行路径上，
且大量写死 stage-3 路径，带上只会放大评审面。需要时可单独提供。

具体排除：

- `cybergym-agent/scripts/` 下除本仓库列出的 9 个脚本与 `agent_mission.txt` 外的全部文件；
- `root_code/` 下除 `stage4.sh`、`score_all.sh`、`backfill_fix_side.sh` 外的全部；
- `legacy/`（stage-3 隔离脚本，原 `stage3_root/`）；
- `cybergym-agent/{docs,data,api,bin,kb,roles,agents,sdk,synthetic,supervisor,ui}/`
  中与评测口径无关的内容（`agent/contract.py` 因 `diff_verifier` 依赖而保留）。

## 一个“改了但未纳入”的文件

- `cybergym-agent/scripts/run_llm_task.py`：本轮也改成了 final-only + 账本，
  但它属于 **LLM-worker 路径**（不是 stage-4 clean run 的 `agcli` 路径），
  依赖未包含的 `agent/*` 脚手架，放进来会无法导入。若评审需要，可单独提供
  `run_llm_task.py` + `scaffold/agent/`。

## 本地产物

- `__pycache__/`、`*.pyc`、`.testtmp/`、`.smoke*/`、`.tmp*/`
- 本仓库自己的 `MANIFEST.sha256` 之外没有二进制产物

## 需要主机才能验证的

- 服务器 `/query-poc` 的真实返回结构、`created_at` 精度与排序
- 服务器 `poc_hash` 的算法与字节范围（是否为上传字节的 SHA-256）——
  pilot 前先做一次受控提交核对，否则评分会大面积 `hash_unverified`
- `<out>` 所在文件系统是否支持 `chattr +a`（ext4/xfs 支持，NFS 不支持；
  不支持时受保护账本自动禁用并打印 WARN）
- `/submit-vul` 是否产生 `vul_exit_code=NULL` 的 fix-only 记录
- `backfill_fix_side.sh` 对 `fix_exit IS NULL` 的实际回填行为
- `/tmp` 残留、rootless docker、fix 侧可读性
- bash 脚本的真实执行（评审机可能没有 bash）
