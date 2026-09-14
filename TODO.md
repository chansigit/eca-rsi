# 待议事项

- **统一不可变计算镜像（后置，2026-09-13）**：CPU/GPU 共用经过 OSP/MSP/ZMIP 验证的固定依赖，将虚拟环境直接打包进镜像。当前保留 CPU NumPy 2.5.3 / GPU NumPy 2.4.6，通过默认 `~/.config/ecarsi/pool-runtimes.json` 公布并核验各运行环境；本次漏配问题已修复，但不承诺未来环境变更永不出错。用户要求先解决 warm pool 排队和低利用率，暂不迁移正在运行的任务。

- **Stress population 保留／删除开关**：见 [eca-rsi#9](https://github.com/chansigit/eca-rsi/issues/9)(2026-09-11 转 issue,附 102 器官批跑的实测占比:MSP 3.5%、ZMIP 0.9%)。本轮仅记录,不修改当前判定规则或现有结果。
