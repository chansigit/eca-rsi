# Warm Pool 生产运行审计：2026-09-13

这套系统目前属于**能运行真实分析、仍需要有人值守的试运行系统**。尚无证据支持“可以无人值守处理整批输入”的承诺。此前把局部测试通过、worker 上线、任务开始运行表述成生产稳定，是验收标准使用错误。

**硬性要求（用户再次明确）：批量运行需要有人值守不可接受。本文是在描述未完成的缺陷，不是在提出可接受的降级交付。目标必须是无人手工推队列或重启进程的整批执行；数据本身确实无法处理时要明确终止原因并隔离该数据集，不能因此阻塞其他任务。**

以下判断来自本次源码检查、Slurm/进程状态、计算请求和真实数据输出；不是根据页面颜色推断。运行证据在 `/scratch/users/chensj16/eca-runs/production-audit-20260913/`，worker 故障注入证据在相邻的 `pool-resilience-20260913/`。

## 空闲的原因

审计初期，持久队列有 234 个数据集：10 个 running、219 个 queued、3 个 paused、2 个 failed；5 个 compute worker 曾同时空闲，compute queue 为零。正在运行的 driver 大多在模型检查、注释或规划阶段。它们的日志仍在前进，不能一概判成死锁。

这 10 个 driver 当时合计约 14 GiB RSS，却预留了 120 GiB。每个 driver 在整个数据集流程中持有 CPU 和内存预算，模型等待也不释放。新数据集无法取得 driver 容量，自然也没有机会生成新的计算请求。“数据集排队很多、compute queue 为空”在现有设计下可以同时成立。

CPU 数值是 Dask worker 进程用量除以它获得的 CPU 数量，不是整机使用率。一个 24 CPU worker 若只忙一个核心，约显示 4%。短 GPU 计算也可能落在约 30 秒的采样间隔之间。现有指标不保证覆盖外部子进程，RSS 也不等于整个 Slurm cgroup 的总内存。本次另从宿主机采样了完整进程树；新增 OSP 计算期间，一个 worker 达到约 80% 的已分配 CPU 使用率。

GPU worker 曾从约 03:40 离线至 12:13。22_Bianetal 的 19 个 OSP 样本已经完成，后续 Harmony 等待 GPU，最终因等待超时失败。恢复 worker 的进程缺失，是这段长时间停顿的基础设施原因。

## 实际计算边界

| 层次 | 当前实际工作 | 是否属于计算 pool |
|---|---|---|
| Dataset driver | 数据准备、模型交互、检查点、阶段衔接，以及部分本地矩阵操作 | 不是一个 compute task |
| OSP | 一个样本的计算和计算报告作为一次远端任务；样本之间可以并行 | 是 |
| MSP Harmony | 单独的 Harmony 任务 | 是 |
| MSP 图与聚类 | neighbors、Leiden、UMAP 合成一次任务 | 是 |
| MSP DEG | 邻居图/PAGA/差异分析合成一次任务，可要求 GPU | 是 |
| 模型检查/注释 | 外部模型服务和工具调用 | 不占 compute worker，但占着 driver 预算 |

“按具体计算调度”的方向正确。相邻计算合并成一个任务可以避免重复传输，没必要机械地把每个函数拆开。但是整数据集的执行状态不应冒充 compute task；原先把两者混放在 Warm Pool 页面会误导使用者。

## 本次已实施

1. 给已有 Slurm grant 内的 worker 增加宿主机监督：子进程退出后重新验证资源、退避重启；旧进程组退出前不启动替代进程。真实 GPU 故障注入验证了同一个排队请求在 worker 自动恢复后完成。没有重新申请或释放 Slurm allocation。
2. 将 driver 活动移到 Overview 的 Workflow activity。Warm Pool 保留 worker 和计算请求。展示最近日志阶段、内存和日志时间；刷新失败保留旧数据并明确标记，短暂监控失败不会立即冒充整个 pool 离线。
3. 增加输入元数据资源策略，按未压缩 counts 和 dense HVG 工作空间估算预算。219 个尚未启动的数据集已重新估算，活动任务预算保持原样。配置适用于后续提交，显式预算仍优先。估算尚未经过所有大输入的峰值验证，不能作为内存一定足够的保证。
4. 增加通用、有次数上限的检查点恢复。必须有匹配的终止记录；仅已确认的 worker 丢失、OSP 标记可重试的失败、driver 时间到期可以进入退避和正常排队。不自动重跑分组不明、科学检查失败、用户主动暂停或执行状态不确定的工作。当前配置最多两次 attempt。

这些改变已让队列自行补入新数据集，并产生新的 OSP 计算；不代表所有排队数据都已运行。终止任务重新进入 queued，也不代表已经恢复计算或完成。

12:56 的进一步读取显示：自本次持续采样开始后，scheduler 记录了 25 次计算返回，包括 GPU Harmony、图与聚类、DEG，以及 4 次 OSP compute attempt。其中 GPU Harmony 约 0.06 秒、图与聚类约 0.4–0.6 秒、DEG 约 2–8 秒；30 秒 GPU 采样很容易漏掉这些短计算。OSP attempt 约 67–79 秒。scheduler 的 `done` 只表示函数返回，OSP 还要以计算检查点和输出验证判断成功，不能把 25 次返回称为 25 个数据集完成。证据为 `pool-tasks.latest.json`。

## 距离稳定生产还缺什么

| 优先级 | 未完成的问题 | 验收条件 |
|---|---|---|
| P1 | driver 仍长期持有整流程资源，部分重矩阵操作在本地；这限制吞吐 | 先测阶段耗时与峰值内存，再确定需下放的重计算边界；排在执行恢复闭环之后，不先重写阶段 DAG |
| P0 | CPU compute worker 的 inventory 显示 `step_extern`，其内存主要由 Dask Nanny 管理；不等于每个 worker 及其可能启动的子进程都有独立的 Slurm 内存边界 | 用已有 allocation 内的独立 step/cgroup 约束整个 worker 进程树，并验证超限只影响该 worker；监控同时覆盖子进程 |
| P0 | scheduler、队列 controller、node agent 的故障恢复没有形成完整闭环；node agent 重启会受子任务继承整块 CPU 锁影响，不确定旧执行目前仍保留资源 | 分别中断这些服务，验证不会重复写结果、不会永久扣住容量；节点退出/到期后，其他仍有资源的节点继续接收工作 |
| P0 | 缺少整批、长时间无人干预的验收；已观察到 AdultIleum 完整 release，但不足以证明整批可靠 | 代表性单样本、多样本、大矩阵和 GPU 流程各自完整发布；连续运行至少一个完整批次并跨过一次节点退出，过程中无需手工改队列 |
| P0 | running compute 没有独立的执行硬期限，driver 断开后也可能留下不返回的计算 | 将执行上限与排队估时分开；先隔离并终止旧 worker 进程树，确认后归还槽位。不能仅把任务改为 expired 或取消 future 就分发新工作 |
| P1 | 运行版本和运维/UI 代码耦合，CPU/GPU 依赖也有差异 | 以可复现部署清单区分科学身份和服务版本；保留兼容性验证，不能靠关闭 hash 检查解决 |
| P1 | 计算历史只保留近期内存记录，部分阶段靠文本日志解释；旧 MSP 的可重试原因仍需从最后异常兼容读取 | 任务有 dataset/sample/stage 身份、持久终止原因、开始/结束时间和进度时间；能区分模型等待、计算等待、执行中与失联 |
| P1 | worker 一次执行一个计算任务，driver/worker CPU 切片固定；GPU 开关对部分阶段是强制 GPU，并非“有 GPU 就优先” | 优先验证每节点多个隔离 worker 的简单资源配置；仅在性能测量需要时再做单 worker 并发和阶段后端选择 |

模型调用时长也是实际吞吐的一部分，例如新增输入的一次 sample-column 判定用了约 121 秒。它不能靠提高本地 GPU 利用率解决。需要把模型等待、数据准备和 kernel 时间分开记录，再决定优化何处。

## 此后的完成标准

分别报告“代码测试通过”“已部署”“真实任务完成”和“无人值守验收通过”。每次修复必须给出触发条件、共同调用路径、自动恢复或明确终止的证据，以及还未覆盖的情况。看到 worker online 或进程 running，只能证明那一层正在运行。

本次测试和观察结果以证据目录的 `tests.log`、`browser.json`、`observations.jsonl` 为准；最终观察摘要另存 `observation-summary.json`。本文列出的 P0 项尚未全部完成，因此不能把本次改动称为生产就绪。

## Opus 5 复核与本轮后续结果

已按用户要求在本目录实际运行 `claude -p --model claude-opus-5`，只给 Read/Glob/Grep 权限。原报告、针对意见的复核及修订答复在证据目录的 `claude-opus5-review.md`、`claude-followup-prompt.txt`、`claude-opus5-followup.md`。模型名称也在 CLI 的实际返回中确认。

采纳的优先级是：执行记录、终止原因、重启接管、超时计算回收、服务监督，再优化吞吐。未采纳直接把 worker 线程数改为 4、在已用满 8 CPU 的 GPU 节点再加 6 CPU worker、原地统一 NumPy、从已单独划出的 driver 内存再次扣除 worker 内存。Opus 在复核后撤回了这些建议。仍需纠正两处实现细节：只缩小继承的 CPU 锁集合，不足以解决 agent 阻塞获取锁；先把 running 标成 expired 而未确认旧计算结束，会过早释放槽位。实施时必须同时覆盖这些边界。

进一步完成并部署到管理服务的修复：

- supervisor 在启动前先写执行记录；启动器不存在等尚未创建子进程的异常写明确的 `launch_failed` 终止记录，释放该次预算。已经创建子进程后的未知异常不能冒充安全终止；这部分还需要 Slurm step/PID 身份核实。
- 修复到时限正常退出码 3 被恢复逻辑漏掉的问题，仍排除用户主动暂停；重试清除旧 PID 和开始/分配时间，旧 attempt 信息保存在历史中。
- controller 遇到临时共享存储读取错误，放弃本次未提交状态并在下一轮重读，不再直接退出。该修复不等于已经实现 controller 进程被杀后的自动重启。
- 上述管理逻辑回归 10 项通过。部署只更新 `service-site` 和队列 controller，未变更正在运行的科学快照，也未重启正在分析的数据集。

13:07 AdultIleum 完成两轮分析并正常发布，原始 2,095 个细胞都有唯一台账，最终 1,194 个细胞；已只读核对 final.h5ad 的细胞身份、必需注释列以及 Oak 镜像一致性。随后队列自动将 AdultAdipose 的第二次 attempt 分配并启动。证据为 `adult-ileum-release-validation.json` 和 `observations.jsonl`。这证明该次完成与补位链路实际发生，不代表节点退出、scheduler 重启或整个代表批次验收已经通过。

另修复了 Overview 的统计口径：Cells in 和 Cells over time 共同使用已开始事件，排队输入单独列为 Cells awaiting start；缺少开始时间的历史输入单列说明，不编造日期。数据集筛选同时更新图表和统计卡，实际浏览器验证无脚本错误且 tooltip 与统计值相符。此 UI 修复已上线，科学数据未修改。

## 14:25 的升级与验收进度

新增证据目录 `/scratch/users/chensj16/eca-runs/production-upgrade-20260913/`。
第三次 Opus 5 代码复核保存在 `claude-review.md`。其中指出的 allocation
结束后无法回收、旧 supervisor 无接管证据、异常启动不分类和硬超时依赖粗估时
均已在共同路径修复。当前回归为 29 项通过，包含真实进程和 Dask 故障注入；
尚未用生产 allocation 到期验证全部恢复链路。

管理服务已合并同一节点内的 driver 切片，bigmem 为 24 CPU / 96 GiB，
CPU17 为 9 CPU / 72 GiB；与 compute worker 的联合内存预算分别为
240/256 GiB 和 120/128 GiB。旧 driver 通过安全检查点交接，新 supervisor
只继承自己的 CPU 锁。controller 和两个 node agent 均由进程监督器托管，
两个 node agent 被中断后实际自动恢复。scheduler 已在原地址重新上线。
科学代码和依赖快照没有原地修改，运维包放在单独的 `pool-service/eca_services`。

部署也暴露了三个问题，不能从验收记录中略去：混用旧准入进程曾生成未实际启动的
重复容量分配；第一个替换 worker 曾继承错误 CPU mask；新运维模块的 RPC 函数
无法被旧 scheduler 导入。这些已分别通过启动前接管回收、保留原 mask 和稳定
RPC 导入路径处理。未实际启动的 attempt 不消耗科学失败重试额度，未修改科学输出
来掩盖错误。滚动切换会短暂减少可调度 worker，应从稳态吞吐窗口中分开统计。

14:25 的快照为 12 completed / 22 running / 199 queued / 1 failed；四个
CPU worker 正在做 OSP，GPU worker 正在做 DEG。这是计算开始流入的证据，
不是长期利用率或生产就绪的证明。那一个历史失败是 Aynaud2020 输入无法确认
样本分组，日志中的真实原因与旧队列的通用 supervisor 错误文案不同，不能盲目重试。

持续观测进程已启动，15 秒保存一次 worker 指标、计算状态和 driver 日志阶段，
每十分钟汇总阶段计算次数、耗时、CPU 用量和队列变化。文件为 `utilization.jsonl`
和 `ten-minute-windows.jsonl`。Dask CPU 指标仅统计 worker 进程，另用
`host-usage.py` 抽查完整宿主机进程树。14:23 一次 OSP 采样中 CPU17 worker 的
进程树约使用其 8 CPU 配额的 48%，不能用其它空闲 worker 的指标替代它。

仍未完成的生产验收包括：完整批次跨节点退出的无人干预运行、compute worker
及其可能启动的子进程的独立 Slurm 内存边界、长期吞吐，以及大型输入的峰值预算验证。
driver 的整流程预留和强制 GPU 后端仍是后续吞吐分析重点；不能根据模型等待阶段
的低 RSS 直接撤销内存预算，也不能未经科学验证就把 GPU 算法改成 CPU 算法。

### 关于 R 子进程的更正

本次使用的 OSP 在 `osp/qc.py:368` 明确采用 `osp._decontx` 的纯 Python DecontX 实现；检查 OSP 源码未找到 `Rscript` 或 `rpy2` 调用。此前关于这批 OSP 运行 R 子进程的表述没有依据，现已更正。进程指标的覆盖范围是需要核实的问题，不能据此断言本次低利用率由遗漏 R 子进程造成。

## 15:03 持续观测与后续修正

资源观测已从临时脚本移入 `ecarsi.pool.observe`，由宿主机服务监督器运行。
15 秒记录一次，30 秒探查一次 worker 的完整进程树，十分钟汇总一次。
全部五个 worker 已实测接入；界面区分进程树指标与 Dask 单进程回退，
RSS 明确标为进程 RSS 之和，不冒充 Slurm cgroup 内存。队列文件读取失败
不会中断计算监控。验证见 `production-upgrade-20260913/observer-ui-verification.json`。

此前 Warm Pool API 会读取不再使用的数据集及运行记录，实测耗时 11.39 秒，
超过前端 8 秒请求期限；删掉这条依赖后，连续五次请求中位数为 18.49 毫秒，
新增进程树指标后一次复测为 19 毫秒。图表不更新确有这一原因，但它不能解释
真实空闲：完整进程树采样仍显示低 CPU 使用率。

14:35—14:45 窗口记录 28 次计算完成，14:45—14:55 窗口记录 17 次，
两个窗口的计算队列在全部采样点均为空。后一窗口混合了两种 CPU 指标，
不能将其均值直接当作升级前后利用率对比。bigmem 上 14 个 driver 的一次
快照预留了全部 96 GiB，而 RSS 合计约 18.5 GiB；CPU17 的 driver CPU 配额
被用满。多数日志处于模型标注或审核阶段。不能因此直接撤销内存预留；
下一计算阶段的峰值仍需保障。

### StepMgr 退出确认缺陷

现场核实两台 driver allocation 均为 `StepMgrEnabled=Yes`。
`squeue --steps` 只返回 batch/extern；`scontrol -o show step JOB` 能列出全部
23 个正在运行的 driver 数字 step，并与进程 cgroup 对得上。Slurm 官方说明
也记录了这一差异：<https://slurm.schedmd.com/SLUG24/Step-Management.pdf>。
此前把 squeue 未列出当作 step 消失的证据是不充分的。

现在新式及旧式执行清理、allocation 结束恢复共用 `allocation_steps()`，
按 job 查询 step manager，再匹配准确的执行名称或已登记数字 step。
只允许对数字 step 发信号；查询失败保持预留。只有已有终止 accounting
证据时，才接受 controller 已清除 job 的明确回复。管理层已经部署此修正，
未重启 compute worker。已经启动的旧 supervisor 仍持有旧 Python 代码；
新 node agent 的孤儿恢复及以后启动的 supervisor 使用修正版。
验证见 `step-manager-verification.json`，以及 batch/recovery 20 项通过的测试。

### OSP 计算与标注分阶段：已测试，未切换生产科学环境

`persample.drive` 原先将计算和标注占在同一个样本并发名额内，模型等待会
阻止后续样本的计算。新实现复用同一个事件循环，给计算和标注分别限制
名额、共同限制 driver 内存，并限制等待标注的计算结果数量。
`osp_worker --compute-only` 写计算检查点和 `computed` 状态，不能冒充样本完成；
标注仍需经过原有内容验证。暂停保留计算检查点，标注失败重试复用它。
真实子进程测试验证了有内存时计算与标注交叠、无余量时等待，以及暂停后
不启动标注。此项只在工作区实现；运行中的固定科学环境尚未包含它。
它解决多样本 OSP 内部的阻塞，不能单独解决跨数据集 driver 的整流程预留。

07_Swahnetal 和 21_Lawrenceetal_invitro 的旧失败确认是 GPU admission timeout。
原数字 step 的终止 accounting、step manager、全部输出 writer 锁和运行版本
均已核对；两者现已通过正常 submit 接口进入持久队列，沿用原 output/mirror。
08_Yanetal 的 exit 137 尚未查明来源，仍未作为确定的瞬时故障补交。

14 个完成单位的检查通过：输入 29,723 个细胞，最终 16,677 个；原始 cell ID、
去除记录、最终标签及镜像一致，见 `release-validation.json`。这仍是小单位验证，
不是全批次生产验收。

15:05 的下一窗口全部使用进程树指标，共完成 22 次计算，计算队列仍在所有采样点为空；
五个 worker 的平均 CPU 使用率为约 0.08%—5.3%。这是实际空闲的证据。
真实 Slurm 孤儿 step 测试也通过：`43173336.46` 存活时保留预算，确认其消失后
才写终止收据；只清理这一命名测试 step，未取消 allocation。首次本地启动器测试
被正常快速清理，没有形成预期的孤儿存活窗口，因此另做了远端存活 step 的验证。
结果见 `step-manager-probe.json`。

另从 37 个运行中或已完成数据集的日志尾部各取最近结束的 agent 阶段：墙钟时间
合计 19,399 秒，工具时间合计 143.8 秒。样本不是同步时间窗口，也不是全流程
耗时占比；其余部分包括模型交互、网络及框架开销，不能全部称为网络等待。
原始日志行保存在 `agent-time-observation.json`。

### Opus 5 本轮复核后的修正

复核文件为 `stage-review.md`。采纳并实现：allocation step 查询逐 job 隔离故障，
允许保留无关的特殊 step 标识但只给匹配的数字 step 发信号；启动和周期恢复共用
异常保护；step 查询与发送信号之间自然结束时重新确认消失，不丢弃成功收据；
清理期间的查询异常保持锁和预算继续观察。收紧了扫描到的 launcher 消失后仍
发送进程组信号的分支，只有另行确认的孤儿进程组才允许组长缺席。

对于被 SIGKILL 杀死的子进程，没有将未知原因自动归为可重试 OOM；这需要真实
故障证据。保留失败样本的计算快照是恢复与调查所需，未按复核建议直接删除。
新分阶段实现现在也用已生成的 computed/clustered H5AD 大小提高本地标注预算，
但它仍是估算，不是峰值内存证明。活进程未退出时必须保留预算，不能靠固定
等待期限把它当作已经停止。

另将 driver 最长运行时间与入场所需机时分开：`hours` 仍为每次运行上限，
`min_attempt_hours` 可显式配置最低剩余机时。现场设为 2 小时，原 6 小时上限
保留。正常 allocation 到期单独记录 `allocation_end`，确认退出后从检查点
重新排队，不消耗错误重试额度；主动暂停与不可重试科学错误仍阻止自动恢复。
上述管理层更新已部署，OSP 分阶段代码仍未替换正在运行的科学版本。

### 15:25：以 AI 与计算独立准入作为下一轮验收条件

本轮管理与 OSP 分阶段回归 45 项通过；随后补充了内存读取和队首阻塞修复，
前后段契约、OSP 子进程与阶段调度共 92 项通过。16 个已完成单位的只读复核
通过：输入 35,241 个细胞，最终 18,512 个，台账、标签、镜像一致。
第二次真实 StepMgr 故障回收测试 `43173336.50` 通过，终止确认后释放预算。

用户要求的目标是跨数据集推进：A 等模型时，只要资源容许，B 的计算就应进入
pool。当前计算内核已经在 worker，模型在 driver，但资源准入仍然耦合，不能
把这种进程位置的分离当成目标已经完成。15:05—15:15 窗口返回 27 次计算，
全部采样时刻计算队列为空，五个 worker 平均 CPU 使用率约 0.07%—2.34%。
这仍不满足吞吐要求。

| 环节 | 实际阻塞位置 | 下一步需要改变的边界 |
| --- | --- | --- |
| OSP | 旧 `persample.drive` 的样本名额覆盖计算和标注；标注载入整个 clustered 矩阵 | 计算检查点完成后释放计算名额；标注独立准入；跨数据集使用同一资源账本 |
| MSP | Harmony、图/聚类、预计算 DEG 已远程执行；inspect/annotate 等模型时保留 integrated 矩阵；预处理和部分交互工具仍本地运行 | 把模型会话与矩阵计算进程分开；本地预处理和模型调用的重工具也必须取得当前阶段预算 |
| ZMIP | lineage 从整合到标注共用名额，父进程还持有完整数据；`ZMIP_PARALLEL=1` 另走进程内串行路径 | 同时覆盖串行入口和并行入口；按 lineage 交付计算检查点、独立排标注；保留 merge 的完整性校验 |
| 外层 batch | `capacity()` 在整次 dataset execution 期间保留 CPU 与峰值内存 | 从整流程准入改为阶段准入，等待模型不能占用下一阶段的计算预算 |

本次还发现 OSP `validate_outputs` 用 AnnData backed 读取，却仍会载入 counts
层。已复用 downstream 已有的按需读取器，避免为核对标签与台账载入表达矩阵；
保留 counts 存在性、维度、原始 ID、标签和 QC 校验。测试禁止读取表达数组，
并验证缺失 counts 仍拒绝。分阶段队列也改为扫描可放入当前内存预算的任务，
避免前面一个大任务挡住后面可执行的小任务；真实子进程测试覆盖这一场景，
同时核对并发预算没有超出。这些科学包修改仍是候选版本，未热替换现场代码。

下一轮采用以下顺序，不再以扩大并发数代替解耦：

1. 复用现有 JSON 队列、writer 锁、阶段检查点和 supervisor 收据，在同一
   持久调度入口登记待运行阶段。先把 OSP 的 compute/annotation 接入，再扩展
   MSP integrate/inspect/annotate 和 ZMIP lineage；轻量 driver 只推进依赖。
   `waiting for model`、`waiting for compute`、`running compute` 必须来自阶段
   事件，日志末行只能辅助排查，不能成为释放预算的依据。
2. 阶段交接后，持有矩阵的子进程确认退出，才释放它的 CPU/内存预算。模型
   会话保存文本、图像引用及已接受判定；需要矩阵的工具按文件路径重新取得
   计算执行权。便宜的查表保留本地，重计算交给 pool。等待中的模型会话仍计
   自身真实内存和并发预算，不能凭 CPU 闲或瞬时 RSS 低直接超卖内存。
3. 样本和 lineage 的计算结果形成有界待标注队列，模型变慢时背压只约束相应
   积压；仍可推进其他符合全局资源预算的数据集。当前样本的 QC/纳入判定若
   是下游输入的必要条件，下游必须等待，不能对未经接受的输入抢算。
4. 用显式版本发布解决上线：每个 attempt 固定执行环境并记录来源；新版本
   在独立输出和匹配 worker 上验证后用于未开始任务。旧检查点继续按旧环境
   恢复，或经过有证据的兼容迁移。不得改写旧身份、关闭 runtime 校验，或向
   已运行进程注入未记录的科学代码。

验收必须同时包含：A 的模型调用被刻意阻塞，B 的实际计算仍启动并完成；
模型恢复后的重工具重新排队并遵守内存/GPU/机时限制；模型故障只阻断自己的
依赖链；恢复不重复计算已验证的检查点、不重复发布或丢失细胞。OSP、MSP、
ZMIP 都需覆盖，且跨数据集覆盖。最后用连续 10 分钟窗口比较完成单位数、
计算等待与模型等待时间、失败和守恒检查；CPU/GPU 利用率只是其中一个指标。
详细源码定位另存运行目录 `phase-ownership-audit.json`。

15:26 窗口复查：完成 28 次计算，计算队列仍在所有采样点为空，五个 worker
平均 CPU 使用率约 0.06%—3.37%。持久队列为 16 completed、24 running、
195 queued、1 failed；bigmem driver 预留 96/96 GiB，进程 RSS 合计约 25.3 GiB；
CPU17 driver 预留 56/72 GiB，driver CPU 已分配完。瞬时 RSS 低不能直接用作
降低峰值预算的证据。监控服务继续采样并每 10 分钟写一份窗口报告。

### 2026-09-13 15:57 部署进展

此前的阶段拆分候选现在已以独立 `eca_stages` 包部署在
`production-upgrade-20260913/stage-service`，批量配置通过
`osp_dispatch_module` 选择它。科学代码仍来自 `pool-recovery-20260913/site`，
ecarsi / OSP 的 runtime source SHA 与部署前逐字一致；没有修改原身份或开启
开发模式。新 attempt 先由原 organizer 准备输入，再由分阶段 OSP dispatcher
执行，最终交还原 RSI 校验、MSP 和 ZMIP 继续。每个样本仍必须通过原输入、
输出及细胞账本验证；调度代码另记 `orchestration.json`。

已经完成的部署和证据：

- 中央 controller 现在执行持久队列的 assign；`driver_cpu_slots_per_core=2`
  已生效，允许同一 driver CPU 上有两个任务，完整内存预留和 CPU 锁仍保留。
  FetalLung 与 Olalekan2021_ovarian 通过此路径自动进入运行。
- 跨数据集验收 `live-shared-cpu-probe.json`：A 模拟阻塞模型等待，B 使用同一
  driver CPU 提交真实 pool 计算并完成，此时 A 仍在等待。它证明调度机制，
  不代表实际生物流程的提速倍数。
- bigmem 的大 worker 从 112 调至 96 GiB，确认无执行或待领取结果才重启；
  driver 预算从 96 增至 112 GiB。AdultCerebellum / AdultBoneMarrow 自动
  获得两个新名额，并已进入新的 `eca_stages.osp_dispatch` 进程。
- 小 worker 仍运行旧 MSP DEG，保持 drain，等待计算和结果交接完成后再缩小
  其预算；未强杀计算，未申请或归还 Slurm allocation。
- OSP、批量和前处理相关测试 70 通过；另一个进程识别回归批次 24 通过。
  新 dispatcher 的资源探查识别已同步到后续 supervisor。

此时队列为 17 completed、29 running、189 queued、1 failed。上一完整十分钟
窗口记录 22 次成功计算，其中 1 次是明确标记的调度验收探针，21 次为实际任务；
worker 平均 CPU 仍约 0.06%—8.1%。不能把并发数增加当作已证实的吞吐提升。
MSP/ZMIP 的模型期间矩阵保留、整个数据集峰值内存长期预留仍未解决；该限制
与已上线的 CPU 共享及 OSP 样本阶段拆分必须分开报告。

15:59 现场验收补充：AdultBoneMarrow 两个样本分别进入 sh03-06n50 和
sh03-08n39 的 pool worker；第一个样本完成计算进入 annotation 时，第二个
仍在 compute，状态文件与日志一致。独立 namespace 配合原 pinned RSI 的
准备、完成、再次恢复合同测试也通过。17 个完成单元重新验证：输入 36,901
细胞、最终 19,799 细胞，账本覆盖及 Oak 镜像一致。

小 worker 的旧 DEG 已自行完成且无待领取结果，安全重启为 8 GiB；driver
随后扩展到 136 GiB，五个 worker 仍保留。队列自动再接入 AdultCervix、
AdultLiver、AdultGallbladder，共 32 running、186 queued、17 completed、
1 failed。最新已结束窗口（约 15:46—15:56）有 26 次实际计算完成，worker
平均 CPU 约 0.06%—10.09%；它大部分覆盖上线之前，不能用于宣称升级提速倍数。
原始验收证据见运行目录 `stage-live-verification.json`，十分钟观察服务仍运行。

### 2026-09-13 16:37 模型等待期间释放 driver 内存

MSP/ZMIP 控制流程现以独立 `eca_stage_v4` 包发布。模型会话保留 obs/var/uns，
表达矩阵在工具执行时加载并在退出时清除；表达查询、按需 DEG、局部聚类可
直接进入 pool。ZMIP 主进程不再持有整个 atlas 等待 lineage 子进程。
批量 controller 接受显式释放请求，结合新鲜进程 RSS 保留至少 2 GiB 余量，
每个节点另保留一个任务恢复完整计算预算的空间。本地重计算必须先取得完整
预算的持久化确认。未知旧进程或仅仅 RSS 偏低均不能触发释放。

真实 pool 闭环探针 `live-memory-loan-probe.json` 通过：两个各预留 8 GiB 的
模型等待会话各降至约 2.26 GiB，第三个原本无法进入的 4 GiB 任务完成 pool
计算；两个会话随后都完成 pool 表达查询并恢复完整预算。这是机制验收，
不是生物流程吞吐倍数。CPU17 的 CPU worker 已安全切到与 MSP/ZMIP 相同的
数值环境，使这些 CPU 工具也能使用它；未改变 Slurm allocation。

中央 controller、两个 node agent 和监控均已加载新管理代码，批量配置开启
`driver_memory_lending`。31 个旧 attempt 已请求安全检查点交接，由现有恢复
机制自动重新入队。16:37 已有 FetalSpinalCord、Tang-Huau2018_ovarian、
Dost2020_othermodels_Patients 进入新配置，尚未以真实任务证明内存释放增益。
有旧 agent progress 且无新版本凭据的阶段继续使用原实现；后续新阶段才使用
新控制流程。监控每 15 秒采样，并持续生成 10 分钟窗口。

本次升级也暴露并实际造成一次部署错误：开始时修改了仍被生产进程以 editable
方式导入的 ZMIP 源码，触发 runtime 不一致。原 MSP/ZMIP 目录已恢复原代码，
候选改动移到独立 worktree，原 ZMIP 已复制固定到生产 site，数值 runtime
重新核验一致。FetalRib、NeonatalAdrenalGland、Tang-Huau2018_ovarian 已通过
正常 retry 恢复。另三个在漂移期间创建的 ZMIP 阶段，在确认写进程停止并取得
writer lock 后移到同轮 `.history/zoomin-source-drift-*`；其上游结果保留。
这三个数据集为 FetalSpinalCord、AdultFallopiantube、Dost2020_othermodels_Patients，
都已通过正常 retry 重新排队。没有改写旧 runtime 身份或禁用校验。
证据：`archived-drift-stages.json`、`canonical-zmip-runtime.json`。

最近一次发布检查覆盖 21 个完成单元：输入 56,944、最终 29,543 细胞，细胞
账本、输出标识及 Oak 镜像一致。尚未宣称生产吞吐已达到目标；后续观察需
区分旧阶段、真正释放预算的新阶段、探针计算和实际生物计算。

### 2026-09-13 17:04 未启动数据集进入 OSP 的实测验收

已发布独立 `eca_prep_v1` 准备流程。原本尚未开始的数据集先以 1 CPU、最多
4 GiB 准入；输入来源、organize 计划和样本划分仍按原流程确认。准备进程
只读元数据，整理、写子集和 OSP 数值计算交给 pool。每个数据集最多提前
计算 4 个样本，正在准备与准备完成待接续的数据集合计上限 8 个。计算
结果保留原 identity、QC 账本与检查点；模型注释和下游判断没有跳过。

部署前状态快照确认 AdultThyroid、AdultArtery 都是 queued、attempt 0，
完整 driver 预算各 12 GiB。二者自动以 4 GiB 准备预算进入，实际 OSP
计算分别送到 sh03-08n39 和 sh03-13n22。校验时另有 23 个运行数据集处于
model_wait。两数据集共 3 个样本、18,731 个输入细胞，计算后保留 13,990、
QC 移除 4,741；逐细胞账本守恒，计算产物哈希、request 和样本 manifest
身份一致。样本仍是 computed，必须继续模型注释。

两个准备 attempt 已正常结束并自动回到完整 driver 队列；空出的准备名额
随后自动接入 AdultOmentum 和 FetalBrain。验收未手工指定数据集运行节点，
也未申请新的 allocation。已有控制 allocation 内登记了两个准备名额，
其节点角色禁止接入完整 driver。证据保存在运行目录
`osp-preparation-live-proof.json` 与 `preparation-deploy-baseline.json`。

上线时另修正了两项部署问题：准备包构建最初遗漏 `sample_column.md`，
在 AdultArtery 的正常重试期间补齐原始提示文件，构建测试现验证其内容；
node agent 恢复时错误地把“新任务准入才需预留的内存恢复窗口”再次加到
已存活任务预算上，导致拒绝接管。现恢复只核对已授予预算与实测 RSS，
新准入仍保留恢复窗口；未终止存活科学计算。相关测试共 29 项通过。

最新完整发布检查覆盖 23 个单元：输入 72,242、最终 42,337 细胞，账本、
输出标签字段与 Oak 镜像一致。这次已用真实数据证明新的 OSP 入口可用，
尚不能据此给出稳定吞吐倍数；准备积压达到上限后仍需正常 driver 接续，
完整流程完成率和各阶段耗时继续由持续观察服务记录。

### 2026-09-13 17:22 持续吞吐复查：利用率仍低

17:07—17:17 的完整窗口中，五个 worker 的平均 CPU 利用率为 3.84%、
7.49%、7.09%、0.09%、0.30%。完成数据集从 24 增至 27；窗口内完成
12 次 OSP 计算，另有大量很短的查询操作。不能用 177 次总操作数宣称
资源已充分利用。AdultThyroid、AdultArtery 已自动进入普通 driver，并从
验证后的 compute checkpoint 继续必要的模型注释。

此时的限制仍是：8 个准备积压名额已满；每个 Dask worker 只有一个
执行线程和一个 pool_slot，调度器按整个 worker 互斥；CPU 数值环境
不同又进一步限制了 OSP 可选 worker。需要保留进程隔离，不能简单
删除 busy 检查让多个会修改进程环境的科学函数共享线程。CPU9 allocation
已在 17:20 到期，未申请或归还用户资源。

管理层还有共享存储问题：node/controller 会在队列锁内反复写同样的
grant.json，现场多个进程在等待 Lustre 锁，节点心跳出现超过 90 秒的
延迟。已让相同确认不再重复写，同时保留确认丢失后的补写；6 项相关
测试通过，管理进程已加载。科学计算没有重启。此项减少无效 I/O，
不等于已解决全部队列锁竞争，也不等于已提高 worker 计算并发。

### 2026-09-13 资源日志升级

在原独立 observer 中加入 schema_version 2 资源记录：CPU 百分比与实际
核数并列，RSS 与调度预留并列，记录 GPU/显存、任务 ID、数值环境、
Slurm 剩余机时、driver 模型等待状态及节点心跳年龄。新增 events.jsonl
保存观察到的 worker、计算任务、数据集状态变化和采集异常/恢复。
采样保留实际耗时与间隔；目标间隔 15 秒，SSH 进程树探查目标间隔 30 秒。

历史文件只在完成任务发生变化时写入，最新快照保留完整状态。三类 JSONL
分别按 64 MiB 轮转，各保留 7 份备份；旧的超大历史日志先保留为备份。
不复制模型回复或进程环境，仅记录数据集日志路径和更新时间。

6 项相关测试通过，真实 pool 单次采样核验 4 个 worker 均有完整进程树
CPU/RSS、请求预留资源和任务记录，探查无错误。发布仅重载 observer，
科学计算与调度配置未重启或更改。证据目录：
`telemetry-validation-20260913`，部署记录 `resource-logging-deploy.json`。

### 2026-09-13 17:46—17:55 利用率与失败恢复复查

日志确认利用率偏低：检查窗口内总 CPU 约 9%—10%，GPU 约 5.5%。
一段约十分钟的窗口完成 199 个计算请求，其中 13 个 OSP 请求共运行
约 550 秒；大量请求是不到几秒的证据查询，不能按请求数判断吞吐量。
39 次采样仅 10 次有计算请求排队。主要问题是计算任务供应不足。

入口有四项具体限制：准备积压上限 8 个，当时为 2 个准备中、6 个等
完整 driver，159 个未开始的数据集因此被挡住；driver 在部分模型等待
阶段仍保留整套预算，实测约 49 GiB、预留约 168 GiB；每个 worker
仍整进程互斥；两个 NumPy 2.4.6 worker 不能承接要求 2.5.3 的 OSP。
24 CPU 的 OSP worker 在 32/39 次采样中有任务，CPU17 节点的 worker
仅 4/39 次有任务。各节点约一半采样的心跳年龄超过准入阈值 60 秒，
实测 node/controller 在等待 Lustre 文件锁，会间歇产生离线等待原因。

部分 ZMIP 主日志长期无输出，但子 lineage 的注释进度仍更新；这是旧
v4 运行器日志转发缺失，不能据此认定模型或计算已经卡死。

失败恢复复查确认当前三项顶层失败，扫描持久队列已尝试的数据集，
未发现仍处于 failed 状态的 OSP 样本。09_Sunetal 的旧客户端报告
`ConnectionError: pool task done:`，源码确认是执行完成后、取回结果前
worker 丢失。08_Yanetal 是进程退出 137，日志本身不能证明一定是 OOM。
二者遗漏在已退出的旧批次中，未进入新 controller 的恢复范围。

已确认旧 runner 与原进程退出、取得输出写锁、核对 OSP manifest 和
用户暂停控制，再迁入持久队列；输出和检查点保留。输入资源探查分别
给出 28 GiB、32 GiB driver 预算，替代旧批次小预算。17:55 两项仍是
queued，原因是 driver CPU/内存容量不足，尚不能称为恢复运行。
控制器已补上上述结果丢失错误的有限重试分类，并让最终 receipt 覆盖
旧的临时失败原因；Periscope 对迁移前后的同一输出只统计一次。

Aynaud2020_othermodels_CellLines 的实际失败是样本划分未知。598 个
细胞仅有 d0_Xeno、d7、d7+2 等实验条件标签，缺少已确认的实验/样本
对应关系；未将条件标签擅自当作样本，也未重复请求模型猜测。

这些修复补齐了失败恢复入口，尚未解决低利用率。下一优先级应是继续
解除阶段计算对完整 driver 峰值预算的依赖，并缩短控制队列锁内 I/O；
随后在进程隔离下增加同节点计算并发，保留数值环境匹配约束。
现场证据：`utilization-and-recovery-audit.json`、`osp-failure-audit.json`、
`legacy-failure-recovery.json`、`failure-recovery-deploy.json`。

### 2026-09-13 18:57：并发、运行环境与新节点

这一轮把一个 worker 的计算任务改为各自独立的容器内 Python 子进程。调度器按每个请求的 CPU、内存、GPU 和数值环境累计预留；任务获得互不重叠的 CPU/GPU 身份。CPU17 节点曾同时完成 3 个真实 MSP/ZMIP 请求；新加入的 43316333（64 CPU/256 GiB）曾同时运行 6 个 OSP 请求，43316331（8 CPU/32 GiB）曾同时运行 2 个。新节点初次接入错误地使用了旧版单任务入口，已等它们的已有任务完成后逐台重启到并发入口；运行证据在 `production-upgrade-20260913/new-workers-live.json`。不能把“6 个请求”解释成 6 个满核计算。

所有计算 worker 的传输层运行在 GPU 容器环境。每个数值任务再根据配置文件 `pool-runtimes.json` 选择容器内解释器，校验实际安装包指纹；CPU OSP 的 NumPy 2.5.3 和 GPU MSP 的 NumPy 2.4.6 均已在真实 worker 上完成独立计算探针。Dask 仍报告传输层与 scheduler/client 的 NumPy 版本警告；这是传输层版本提示，数值任务的版本在子进程内单独验明。子进程及后代的 RSS 已纳入 worker 自身的内存探查，调度器也以实际 RSS 拒绝超额新任务。超时或内存超额仍按整个 worker 进程组围栏，兄弟任务要依靠原 driver 检查点恢复，尚无单任务故障隔离。

新 GPU 作业 43316407 在检查时仍是 Slurm `PENDING (Priority)`，尚无计算节点可接。已有 43173346 GPU worker 正常在线。近一小时 101 个 GPU 请求均为 MSP 的 GPU Harmony、图/聚类、DEG；它们实际 GPU 阶段总共约 391 秒。最近十分钟 GPU 请求累计运行约 74 秒，低平均使用率主要来自上游阶段和模型判定无法持续供应 GPU 请求。调度器为 GPU 请求必须匹配 GPU；CPU 请求优先放 CPU worker，CPU 忙时可使用 GPU 节点的 CPU，但 CPU OSP 不会因运行在 GPU 节点而变成 GPU 算法。新 GPU 作业开始后，`watch-gpu-43316407.py` 将在已获批的作业内启动 worker，不提交或取消 Slurm 作业。

准备入口已让原先未启动的数据集自动完成真实 OSP 计算，同时其他数据集仍在模型等待。随后暴露出纯数字细胞 ID 被 pandas 读成整数、AnnData 当作位置索引的缺陷：Yuan2018_brain 的细胞守恒校验拦截了错位结果，Azizi2018_breast_InDrop 在越界索引处失败。新的 `eca_prep_v2` 将细胞 ID 按字符串读取并验证子集标签，两个终止任务已在修复配置下重新入队；仍须检查真实 OSP 计算检查点，不能把重入队写成已修复完成。

旧版 v4/v5 的 MSP 模型安全暂停还暴露出 AnyIO 非守护线程未关闭，Python 已退出分析函数却挂在 `threading._shutdown`，长时间占着 driver。已经用不调用模型的最小程序复现；在启动钩子中只对事件循环已关闭的 AnyIO 线程发送正常停止并等待，回归测试通过。15 个已写出安全暂停日志、没有科学写入句柄且退出阶段不再处理 SIGTERM 的旧进程，在身份校验后解除线程退出阻塞，交还给原有退出码 3 和检查点恢复。其余旧版运行仍在模型等待或阶段中，必须在安全边界交接；不能修改有已接受科学决定的阶段来源校验来强制换版。

当前仍未达到无人值守生产验收。未完成的主要事项是：验证 v2 数字 ID 修复后的两个真实任务；让旧版 11 个模型等待中的 driver 完成安全交接；解决 32 个准备名额已满而完整 driver 容量不足的持续背压；核对新增 driver 容量后的完成率与连续 10 分钟资源曲线；等待并实际验证 43316407 GPU 节点启动；确认新旧日志完整性与中途失败的自动检查点恢复。已完成的数据集当时为 32 个，仍在运行或排队的很多，不能据计算请求数宣称整批流程完成。

### 2026-09-13 19:39：增设准备资源，按样本并行供给计算

19:31 的十分钟窗口里，六个 CPU worker 平均使用率约 1.9%—10.1%，GPU 平均约 6.2%；当时 pool 只有 3—4 个正在执行的计算请求且没有排队请求。约 150 个数据集在 driver/准备准入前等待。这里的低使用率主要是计算任务供应问题，不能归因于 worker 每次只接一个任务：当前 worker 已支持多任务累计预留 CPU/内存。

已在现有 `warmpool-bigmem` 43316333 的 64 CPU/256 GiB 配额内划分不重叠的三个服务：32 CPU/128 GiB 计算 worker、16 CPU/88 GiB driver、16 CPU/32 GiB 准备节点，总内存预留 248 GiB。切分前计算 worker 先 drain 至没有运行任务或保留结果，随后停止旧进程并在同一 Slurm 作业内启动新进程。`node-sh04-01n13.json`、`preparation-sh04-01n13.json`、`warmpool-43316333/inventory.json` 记录了三个新服务的身份与资源。新节点上线后，正在运行的数据集从 54 增至 72，pool 同时运行计算请求从约 3 增至约 20；这只是瞬时变化，尚须用完整窗口判断持续收益。

准备任务现在一次读取标准化 H5AD 并写出同一 unit 的多个已确认样本子集，然后按样本独立提交 OSP 计算，默认每个准备进程并发 2 个样本，继续遵守原样本映射、写锁与计算检查点。没有跳过模型确认，也没有修改 OSP 数值算法。准备积压上限从 48 经 64 调至 128，避免新增准备节点刚上线就被积压限额挡住；它仍是有界队列。相关准备与 per-sample 测试 10 项通过，新的运行包 `eca_prep_v2` 已更新，现存进程会按已加载版本完成或按原检查点恢复。

新准入的准备任务每个数据集预计算样本上限从 4 增至 8；现存 attempt 保留其启动时的配置。现场已确认多个正在准备的数据集有 10—55 个独立样本，因此原 4 样本上限会过早停止计算供给。每个样本仍独立产生请求和检查点，8 只是计算提前量，不改变后续完整 driver 对全部样本的职责。

19:46 将新准入任务的 `PERSAMPLE_PARALLEL` 上限从 2 调至 4，以便同一数据集的不同 OSP 样本同时向 pool 申请资源；pool 仍逐任务检查 CPU、内存和运行环境。此值只影响新 attempt 的配置快照，正在运行的 attempt 不会被中途改变。

当前不能宣称生产稳定：新准备进程是否持续写出 `compute_state.json`、数据集完成率和 CPU 5/10 分钟均值仍需连续验证。约 30 个 driver 处于模型等待，尽管已返还部分预算，剩余 driver 容量仍会限制后续 MSP/ZMIP 进入速度。旧 GPU 作业只有间歇性 MSP GPU 任务，新 GPU 作业 43316407 此时仍在 Slurm 排队；OSP 是 CPU 算法，不能把它放在 GPU 上就算成 GPU 加速。

19:39:30—19:49:38 的十分钟运行记录给出了第一段完整后验窗口：pool 完成 60 个 OSP 请求（窗口前一段约 35 个），仍有 16 个 OSP 正在执行；四台 CPU 计算主机的 OSP 完成数为新 bigmem 26、新 normal 11、旧 bigmem 13、CPU17 10。CPU17 的 8 CPU worker 平均使用率 41.9%，新 bigmem 32 CPU 为 32.0%，新 normal 8 CPU 为 42.5%；旧 bigmem 两个 worker 分别只有 12.2% 和 12.9%，仍未充分利用。窗口内 OSP 请求时长累计约 11,419 秒；样本规模不同，不能把请求数比值直接当成运算量加速比。GPU 十分钟平均约 2.8%，仍由 MSP GPU 阶段供应不足主导。

真实数据验收：Young2018_kidney 的新准备进程一次写出 4 个已确认样本请求，并同时派出 2 个 OSP 计算；4 个 `compute_state.json` 均与请求身份一致，四类输出文件哈希校验全部通过。Ji2020_skin 的一个新样本也完成相同校验。Yuan2018_brain 在数字细胞 ID 修复后已有两个样本计算完成，后续模型注释尚未验收。该窗口结束时顶层账本为 32 completed、73 running、130 queued、1 failed、1 assigned，说明计算供应已改善，但整条流水线完成率没有在这十分钟内上升。

唯一 failed 的 Zilionis2019_lung 是样本划分冲突：已获模型确认的 organize 计划把同一实验拆进不同单元，per-sample 入口在科学计算前拒绝了计划。它不是暂时性 worker 错误，不能自动重试同一计划；需要补上有依据的计划修正流程。准备进程中的模型等待仍会占用准备节点预算，后续应在保持样本判定的前提下把这段等待与计算名额分开。以上仍不能构成无人值守完整生产验收。

19:53 又修正了准备积压满后的资源闲置：当积压达到配置上限时，内存至少 16 GiB 的准备节点可以按原 driver 预算准入已准备的数据集；8 GiB 控制节点仍只做准备。积压低于上限时优先继续准备，计算/driver 仍共用该节点的真实 CPU 和内存账本，不额外超配。相关准入与批处理测试 20 项通过，管理代码发布后只重启了 controller 子进程，独立节点服务和科学计算未被中断；controller 新进程健康，队列年龄约 1 秒。此规则尚未在真实积压满额场景触发，不能列为实测的吞吐收益。

19:54 的后续快照中，18 个正在运行的准备 attempt 已携带新配置（8 个提前样本、最多 4 个并发），pool 一度出现 8 个等待请求。此时不是缺 worker 进程：新 bigmem 的 128 GiB 计算预算已预留约 113 GiB，旧 bigmem 的 96 GiB 预算预留约 90 GiB，normal 的 24 GiB 预算预留约 20 GiB；等待的三个 OSP 样本分别要求约 58、62、62 GiB。大内存任务使节点 CPU 闲着而内存账本无法再安全准入，这是本轮供给扩张后的下一瓶颈。不能只按当前 RSS（分别远低于预留）直接超卖内存，计算峰值尚未发生。GPU 仍只对有 GPU 实现的 MSP 请求有实际用处。


### 2026-09-13 20:17：下一瓶颈、准备 OOM 与 GPU 试点

20:00—20:13 的线上采样持续显示大 OSP 任务受内存预约限制：新 bigmem worker 有 32 核/128 GiB，其中一次只用约 6 核却已预约 127 GiB；约 21—25 个计算请求排队，主要理由为内存及旧节点剩余机时。已完成 OSP 中至少 5 项实际峰值超过原预约，且 >40 GiB 请求尚无足够完成样本，因此未按瞬时 RSS 下调大样本内存。独立真实 7,461 细胞 OSP 对照在同一 worker 上：1 核 174.60 秒、4 核 133.93 秒，峰值约 1.75/1.73 GiB，四个主要 CSV 逐字节一致。它支持选择性多核试点，不支持声称大样本也有相同比例收益；代码新增 `OSP_POOL_TASK_CPUS` 与 `OSP_POOL_TASK_CPUS_MIN_CELLS`，默认行为不变，尚未部署科学运行环境。

`gWAT_Rag` 的准备 step 43316333.80 在 2 GiB 限额下 OOM（sacct MaxRSS 2,087,964K）；上游组织和样本子集已完成，错误发生在准备进程等待 pool OSP 计算时，并非 pool worker OOM。管理层现在按 `counts_bytes` 增加较大输入的初始准备内存，并只对确认属于当前 attempt 的准备 OOM 逐次提高预算、从检查点恢复，上限为原数据集完整预算；普通科学错误不套用。管理代码原子复制到 `fleet-20260913/service-site/ecarsi/batch.py`（备份 `production-upgrade-20260913/batch-before-prep-oom-1789355705.py`），仅重启 controller 子进程；`gWAT_Rag` 已由 failed 自动转入 retry_wait，再变为 queued，下次预算 4 GiB，尚未确认重新启动或完成。`Zilionis2019_lung` 的样本划分冲突仍为 failed。相关管理、OSP、pool 测试合计 61 项通过。

GPU 科学核查：ZMIP 每个 lineage 的重嵌入复用 MSP 的 GPU Harmony/图计算，`AdultTrachea` 日志证明实际 `_run_cluster_gpu` 送至 RTX 3090；OSP 当前仍申请 `gpus=0`。RAPIDS 发行包实际名称 `rapids-singlecell-cu12`，旧指纹错误地查询 `rapids-singlecell`；源码已修复并加 worker 必须含 RAPIDS/CuPy 版本的调度约束及可配置 GPU 节点主存保留，未部署到旧科学快照或驻留 worker。独立 OSP `X_pca` 图计算对照脚本和 10,999 细胞输入已准备；旧 GPU worker 被一项约 51 GiB 的线上 CPU OSP 占据，环境指纹仍旧，预检拒绝抢算；新 GPU 作业 43316407 仍因 Slurm Priority 排队。准备阶段 AI 等待时的内存贷款源码已测试，尚未部署；旧科学快照不得原地覆盖，需要版本化 runtime 与安全 worker 滚动切换。

20:25 在线验证补充：旧 node-agent 曾抢在新 controller 前给 `gWAT_Rag` 的第 2 次准备尝试再次分配 2 GiB，因而之前的“下次 4 GiB”并未在该任务上兑现。已暂停新派发、逐一重载五个 node-agent 管理子进程并恢复派发；已有科学子进程未中断。随后 `brain_APOE_p2of2` 的准备 OOM 自动恢复尝试实际获配 4 GiB，五个活跃节点心跳保持新鲜。`gWAT_Rag` 第 2 次尝试仍按已记录的 2 GiB 运行，不能中途改其 Slurm 限额；若它再次确认 OOM，应由同一有界恢复路径提高下次限额。

新科学环境已另建 `production-upgrade-20260913/science-site-v2`，旧科学快照保留。普通 worker `43316331` 在无运行任务和保留结果时排空并原位滚动重启，重登记的四种解释器包括旧 CPU/GPU 与新 CPU/GPU；新 GPU 解释器报告 `rapids-singlecell-cu12==0.17.0`，新旧 `ecarsi` 代码指纹不同。新 CPU 环境从驱动端向该 worker 提交一次真实 pool 任务并成功取回主机名，证明任务准入、解释器选择和返回路径可用；这不是 OSP 端到端验收。其余 worker 仍用旧运行配置，新环境尚未给整批数据集启用。大 OSP 请求仍受预约内存约束，GPU OSP 图算法试点仍待可用 GPU worker。ZMIP 的 GPU 已覆盖 MSP 重嵌入中的图/Leiden/UMAP，其他 lineage 注释和模型等待不是 GPU 计算任务。

`AdultTrachea` 的 Epithelial lineage 日志显示 17:56:12 进入 GPU 图/聚类、17:56:15 分配 GPU、17:56:54 进入后续 lineage 分析；单次模型注释记录约 547 秒。该输入只有一个样本，Harmony 被跳过，因此不能把这次实例写成“GPU Harmony 已执行”。当时 3 个待 zoom 的 lineage 因 `ZMIP_PARALLEL=1` 串行；ZMIP 源码本已有独立子进程、内存估计及总预算检查。20:27 将配置中**新 attempt** 的 lineage 并发上限调至 2，旧 `AdultTrachea` attempt 仍保持原快照值 1。尚无新 attempt 进入 ZMIP 可用于验证这一改动的实际吞吐收益；现有 GPU 任务也不能代替模型判定。

新 `cpu-v2` worker 上的独立 7,461 细胞 OSP 实算成功两次，耗时 125.11/126.65 秒，主存峰值约 1.74 GiB。两次在同一 `sh04-14n18` 上所得六个主要 CSV 逐字节一致，DecontX 初始分组与最终 Leiden 聚类逐细胞一致。它们与旧 `sh03-13n22` 上的旧环境运行相比，QC 删除集合一致，但 DecontX 分组 ARI 为 0.878、最终 Leiden ARI 为 0.810，六个 CSV 哈希均不同。旧节点为 AMD EPYC 7742，新节点为 EPYC 8224P；跨节点 CPU 数值行为可能相关，但目前没有同一节点新旧环境对照，不能证明差异由硬件还是代码快照引起。因此此试点仅证明新环境能运行并可在同节点重复，**不构成科学输出等价验收**；整批 OSP 仍保持旧运行配置，不能把这个 worker 的试点速度当成整体提速。
