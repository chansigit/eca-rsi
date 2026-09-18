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
Agent 回合服务  ecarsi.agent         模型回合的持久收件箱：session 契约、dispatch、parallel 批读（原 agent_bridge；
                                     叫 agent 是为了让 bridge 只指外部包 agent-harness-bridge）
Warm Pool       ecarsi.warm_pool     有界计算请求：state（文件协议）、backend（HyperQueue）、worker、budget
────────────────────────────────────────────────────────────────────────────────
Stage 程序      ecarsi.stages        organize / persample / crosssample / zoomin / release：在科学镜像里跑，
                                     包装内核并在 host 侧校验每个提案；contract 是共享的模型契约（v4），
                                     evidence / execution 是它们在 Pool 上的执行计划（session 按 planner 名字调用）
────────────────────────────────────────────────────────────────────────────────
内核（独立仓库） osp · msp · zmip · standissect-lite        模型运行时：agent-harness-bridge（`harness_bridge`）
────────────────────────────────────────────────────────────────────────────────
观测            ecarsi.observatory（页面 + `status` 命令行）· ecarsi.serve（Periscope，第一代，只读）
```

依赖方向自上而下：control 调 agent 和 warm_pool，agent 调 warm_pool，stages 只依赖 warm_pool.state
的文件原语和第一代的库函数（校验、写盘、发布），不反向依赖 control 或 agent；agent 需要 stage 的执行计划时，
按 session 里登记的 `planner` 模块名 import，没有静态依赖。

## 模块对照

| 包 | 模块 | 原名 | 职责 |
|---|---|---|---|
| `ecarsi.control` | `coordinator` | work_coordinator | 活动、AgentWorkflow、CLI（`worker` / `start-*` / `resume-*` / `status-*`）、POLL_RETRY / SHORT |
| | `temporal` | temporal_service | 托管 Temporal server + PostgreSQL，`service.json` 端点 |
| | `dataset` `persample` `crosssample` `zoomin` | *_workflow | 各阶段工作流；`persample.saved_module` 让已存请求保留原程序 |
| `ecarsi.agent` | `__init__` | agent_bridge | 回合请求的 submit / status / serve / reconcile；`adapter_path` 指向被 pin 的 host 代码 |
| | `dispatch` | agent_dispatch | 把回合派到 Pool，inode 键的 finished 缓存 |
| | `session` | agent_session | 会话契约：create / reset / validate_turn / continuation，重复拒绝停机（REPEAT_LIMIT） |
| | `parallel` `tool_errors` | agent_* | 并行只读工具、参数拒绝 |
| `ecarsi.warm_pool` | `state` `backend` `worker` `allocation` `provision` | 不变 | 文件协议（含 `reference` / `verified` / `immutable`）、HQ 适配、worker |
| | `budget` | operation_budget | 按上游实测定预算、measured ceiling、resume 时回放 |
| `ecarsi.stages` | `organize` `persample` `crosssample` `zoomin` | *_v2 | host 程序：计算、校验、发布 |
| | `release` | dataset_release | 数据集发布（ledger / review / umap） |
| | `contract` | crosssample_v3 / zoomin_v3 | 协议 v4 的共享件：无参数列表工具、deg_lookup 阈值、宽松 JSON、checklist |
| | `evidence` `execution` | agent_evidence / agent_tool_execution | Pool 上的执行计划：证据批读、单工具精确计划、实测预算 |
| `ecarsi` | `observatory` (+ `.html`) | dev_observatory | 状态页、时间线、`status` 文本报告（含 GPU 列） |
| | `round_policy` | 同名 | 两代共用的轮次停机规则（第一代 loop 也用） |
| | `prompts/` | 同名 | 两代共用的 prompt 与 checklist |

## 入口

```bash
python -m ecarsi.control.temporal --root <control> --postgres-bin … --temporal-dir … --schema-dir … --bind <ip>
python -m ecarsi.warm_pool --root <pool> scheduler --host <node>          # HyperQueue 调度器；add-worker 加节点
python -m ecarsi.agent serve <bridge>                                      # 模型回合服务（运行目录里仍叫 bridge）
python -m ecarsi.control --service-root <control> --task-queue <q> worker  # 协调器（可多份）
python -m ecarsi.observatory serve --root <base> --bind <ip> --port 8765 --temporal-service-root <control>
python -m ecarsi.control --service-root <control> --task-queue <q> start-dataset|resume-dataset|status-dataset <run_id>
```

`container/control-plane.sh` 把这六条封装成 `start|stop|restart|status|report`。`eca-rsi` 命令行仍是第一代入口。

## 不变量

- **按内容 pin。** control 提交的每个请求都把它依赖的程序文件写进 `inputs`（`stages.program()`），agent 把
  `agent/session.py` 的哈希写进会话（`adapter_path`）。会话在飞时这些文件不能改；改契约要等没有会话在飞时切换
  （v3 那种叠新文件的做法已经折回，不再保留）。
- **请求身份是重放键。** Pool / Bridge 里已存在的请求 id 一律回放已存内容，差异只记进 `resubmitted.json`；
  所以旧布局的已存请求（`-m ecarsi.zoomin_v3` 等）在新布局下会回放失败——切换前让批次跑完，或把相关请求归档后 resume。
- **只有轮询。** Pool / Bridge 是文件协议，协调器用活动轮询（POLL_RETRY 约 35 分钟容忍）；控制面节点上不要
  无节制扫描 requests 目录，它会拖垮同一 Lustre 客户端上的协调器。
- **部署参数不进包。** 节点、镜像、路径、并发上限都在 `control-plane.sh` / 环境变量 / 请求 spec 里。

## 和最初设计的差别

最初的设计只有三块：agent 的 bridge、warm pool、以及 OSP / MSP / ZMIP 内核。现在多了：

1. **Temporal 控制面**——每个数据集是一棵持久工作流树，resume 靠请求身份回放而不是重算。
2. **Stage 程序层**——内核不直接暴露给模型；每个阶段一个程序做 host 校验，`contract` 是它们共享的模型契约。
3. **资源策略**——`warm_pool.budget` / `driver_budget` / `compute_policy` 按实测定内存，OOM 翻倍重试。
4. **观测**——`observatory` 与第一代的 Periscope 并存。
5. **准入**——第一代的 batch 准入（节点代理、OSP compute-ahead、driver 内存租借）在本分支撤掉，数据集只由控制面准入。

## 接缝（还剩的）

- Periscope（`serve.py`，第一代）和 `observatory` 是两套观测；Periscope 现在只看数据集和 Slurm pool。
- `ecarsi.pool`（第一代的 Slurm pool）仍被 `warm_pool` 用来读节点清单，两套 pool 并存。
