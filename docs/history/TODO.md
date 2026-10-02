# 待议事项

- ~~**统一不可变计算镜像（后置，2026-09-13）**~~ **已落地（2026-09-21 核实）**：第二代暖池的
  `pool/config.json` 只有**一个** `runtime`，全部 worker——CPU 与 GPU 一样——跑
  `containers/rsi-science-<date>.sif`，`PYTHONPATH=/opt/rsi-control:/opt/rsi-python`。这一个镜像里
  同时装着科学栈（NumPy 2.2.6、SciPy 1.16.3、scanpy 1.12.4、cudf/cuml 25.12、cupy 13.6）和控制栈
  （temporalio 1.32、openai-agents 0.22、agent-harness-bridge 0.2.14），所以 CPU/GPU 不可能再分叉，
  `docs-gen2/WARM_POOL_V2.md` 说的"bridge 与 coordinator 环境尚未打进科学镜像"也已过时。
  原条目里的 NumPy 2.5.3 / 2.4.6 是第一代两个**宿主 venv**（`venvs/eca-ct` 与 `venvs/eca-ct-gpu`）
  的版本，它们不在第二代计算路径上：`eca-ct` 只剩 Periscope 在用（展示代码，不进身份），
  `eca-ct-gpu` 已无消费方。`~/.config/ecarsi/pool-runtimes.json` 随第一代 Dask 池一起作废
  （0.3.2 删除），全仓库无代码读取它，已移到 `~/.config/ecarsi/pool-runtimes.json.retired-20260921`。
  **仍未确立的是 GPU 的收益**：镜像里有 RAPIDS，但 GPU 路径从未实测过加速比，也没有调度证据表明
  GPU worker 拿到的是该用 GPU 的任务。那是一次测量，不是一次构建。

- **Stress population 保留／删除开关**：见 [eca-rsi#9](https://github.com/chansigit/eca-rsi/issues/9)(2026-09-11 转 issue,附 102 器官批跑的实测占比:MSP 3.5%、ZMIP 0.9%)。本轮仅记录,不修改当前判定规则或现有结果。
