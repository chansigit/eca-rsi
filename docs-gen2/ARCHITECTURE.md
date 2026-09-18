# ecarsi 第二代架构（分支 `gen2`）

第一代（`main`：`eca-rsi run` 单进程流水线，含 0.3.0 的 Slurm pool / batch 准入）原样保留，本分支不改它的模块位置。
第二代把同一套内核包装成可持久化、可并行的批量系统；2026-09-14 到 09-17 之间它以平铺在 `ecarsi/` 顶层的
二十几个 `agent_*` / `*_workflow` / `*_v2` 文件存在，2026-09-17 收进下面的子包。模块名以代码为准。

## 分层

```
运维            container/control-plane.sh（模板；部署副本在运行目录）· worker-keeper.sh
────────────────────────────────────────────────────────────────────────────────
控制面          ecarsi.control       Temporal 工作流树：dataset → unit → persample / crosssample / zoomin → agent
────────────────────────────────────────────────────────────────────────────────
Bridge          ecarsi.agent        模型回合的持久收件箱：session 契约、dispatch、evidence / parallel 批读
Warm Pool       ecarsi.warm_pool     有界计算请求：state（文件协议）、backend（HyperQueue）、worker、budget
────────────────────────────────────────────────────────────────────────────────
Stage 程序      ecarsi.stages        organize / persample / crosssample / zoomin / release：在科学镜像里跑，
                                     包装内核并在 host 侧校验每个提案；crosssample_v3 / zoomin_v3 只改模型看到的契约
────────────────────────────────────────────────────────────────────────────────
内核（独立仓库） osp · msp · zmip · standissect-lite        模型运行时：agent-harness-bridge（`harness_bridge`）
────────────────────────────────────────────────────────────────────────────────
观测            ecarsi.observatory（页面 + `status` 命令行）· ecarsi.serve（Periscope，第一代，只读）
```

依赖方向自上而下：control 调 bridge 和 warm_pool，bridge 调 warm_pool，stages 只依赖 warm_pool.state
的文件原语和第一代的库函数（校验、写盘、发布），不反向依赖 control。已知的两处反向耦合见"接缝"。

## 模块对照

| 包 | 模块 | 原名 | 职责 |
|---|---|---|---|
| `ecarsi.control` | `__init__` | work_coordinator | 活动、AgentWorkflow、CLI（`worker` / `start-*` / `resume-*` / `status-*`）、POLL_RETRY / SHORT |
| | `temporal` | temporal_service | 托管 Temporal server + PostgreSQL，`service.json` 端点 |
| | `dataset` `persample` `crosssample` `zoomin` | *_workflow | 各阶段工作流；`persample.saved_module` 让已存请求保留原程序 |
| `ecarsi.agent` | `__init__` | agent_bridge | 回合请求的 submit / status / serve / reconcile；`adapter_path` 指向被 pin 的 host 代码 |
| | `dispatch` | agent_dispatch | 把回合派到 Pool，inode 键的 finished 缓存 |
| | `session` | agent_session | 会话契约：create / reset / validate_turn / continuation，重复拒绝停机（REPEAT_LIMIT） |
| | `evidence` `parallel` `tool_execution` `tool_errors` | agent_* | 证据批读、并行工具、工具执行请求、参数拒绝 |
| `ecarsi.warm_pool` | `state` `backend` `worker` `allocation` `provision` | 不变 | 文件协议（含 `reference` / `verified` / `immutable`）、HQ 适配、worker |
| | `budget` | operation_budget | 按上游实测定预算、measured ceiling、resume 时回放 |
| `ecarsi.stages` | `organize` `persample` `crosssample` `zoomin` | *_v2 | host 程序：计算、校验、发布 |
| | `release` | dataset_release | 数据集发布（ledger / review / umap） |
| | `crosssample_v3` `zoomin_v3` | 同名 | 协议 v4 包装：prompt 内联证据路径、无参数分页工具合并、宽松 JSON |
| `ecarsi` | `observatory` (+ `.html`) | dev_observatory | 状态页、时间线、`status` 文本报告（含 GPU 列） |
| | `round_policy` | 同名 | 两代共用的轮次停机规则（第一代 loop 也用） |
| | `prompts/` | 同名 | 两代共用的 prompt 与 checklist |

## 入口

```bash
python -m ecarsi.control.temporal --root <control> --postgres-bin … --temporal-dir … --schema-dir … --bind <ip>
python -m ecarsi.warm_pool --root <pool> scheduler --host <node>          # HyperQueue 调度器；add-worker 加节点
python -m ecarsi.agent serve <bridge>                                     # 回合收件箱
python -m ecarsi.control --service-root <control> --task-queue <q> worker  # 协调器（可多份）
python -m ecarsi.observatory serve --root <base> --bind <ip> --port 8765 --temporal-service-root <control>
python -m ecarsi.control --service-root <control> --task-queue <q> start-dataset|resume-dataset|status-dataset <run_id>
```

`container/control-plane.sh` 把这六条封装成 `start|stop|restart|status|report`。`eca-rsi` 命令行仍是第一代入口。

## 不变量

- **按内容 pin。** control 提交的每个请求都把它依赖的程序文件写进 `inputs`（`stages.program()`），bridge 把
  `bridge/session.py` 的哈希写进会话（`adapter_path`）。会话在飞时这些文件不能改；改契约要么加新文件（v3 的做法），
  要么等没有会话在飞时切换。
- **请求身份是重放键。** Pool / Bridge 里已存在的请求 id 一律回放已存内容，差异只记进 `resubmitted.json`；
  所以旧布局的已存请求（`-m ecarsi.zoomin_v3` 等）在新布局下会回放失败——切换前让批次跑完，或把相关请求归档后 resume。
- **只有轮询。** Pool / Bridge 是文件协议，协调器用活动轮询（POLL_RETRY 约 35 分钟容忍）；控制面节点上不要
  无节制扫描 requests 目录，它会拖垮同一 Lustre 客户端上的协调器。
- **部署参数不进包。** 节点、镜像、路径、并发上限都在 `control-plane.sh` / 环境变量 / 请求 spec 里。

## 和最初设计的差别

最初的设计只有三块：agent 的 bridge、warm pool、以及 OSP / MSP / ZMIP 内核。现在多了：

1. **Temporal 控制面**——每个数据集是一棵持久工作流树，resume 靠请求身份回放而不是重算。
2. **Stage 程序层**——内核不直接暴露给模型；v2 程序做 host 校验，v3 包装只改模型看到的东西。这一层的分裂
   完全是 pin 机制的产物。
3. **资源策略**——`warm_pool.budget` / `driver_budget` / `compute_policy` 按实测定内存，OOM 翻倍重试。
4. **观测**——`observatory` 与第一代的 Periscope 并存。

## 接缝（下一步整理的候选）

- `stages.crosssample_v3` / `stages.zoomin_v3` 应折回各自的 v2 程序：本分支没有在飞会话要保护，合并后每个阶段
  只剩一个程序、一份契约。
- `bridge.tool_execution` 和 `bridge.evidence` 直接 import `stages.persample` / `stages.crosssample`
  （用 stage 的工具实现来构造 Pool 请求），是 bridge → stages 的反向耦合。
- 第一代的 `batch.py`（Slurm 节点上的数据集准入）和 Temporal 的 dataset 工作流职责重叠；Periscope 的
  `serve.py` 还读 `batch.py` 的队列。
- `ecarsi.agent`（回合收件箱）和外部包 `agent-harness-bridge`（SDK 适配）都叫 bridge。
