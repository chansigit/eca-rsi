# 异步执行单元与多级存储草案

状态：设计候选，尚未实现或部署。共 58 个候选 primitive；清单覆盖面不代表第一版全部独立执行。
适用分支：feature/durable-workflows。现有 main、服务、配置和生产数据保持运行。

## 设计基准

organize / per-sample / cross-sample / zoom-in 是展示与科学语义上的阶段，
不再是资源准入容器。每个样本、整合单元、lineage 使用有版本的输入依赖。
没有全局 round barrier；一个数据集等待模型不影响另一数据集的就绪计算。

区分科学迭代和故障重试：新参数或新输入产生新 operation generation；
同一任务因节点消失重试则产生新 attempt。有效的模型决定及输入都必须保留。
运行过程可动态追加任务；任务本身依赖已经存在的产物版本，跨代迭代由协调器推进。

Hybrid 是 model.request → tool.validate → 数值/证据操作 → tool.resume 的组合。
模型等待不能持有 pool 计算名额或完整表达矩阵。轻量 control 操作可在协调进程执行。
模型结果必须经过现有科学校验，不能为了并发跳过 sample mapping、注释或删除保护。

## 候选清单

执行类别是资源性质而非硬件保证；每个任务仍按规模估算 CPU/RAM/VRAM、临时磁盘和耗时。
“内部提取”表示现有函数包含该逻辑，但尚未具备独立恢复接口；不是可直接调用的新 API。
标注 GPU 的项目只表示发现现有实现路径，不代表所有数据规模、OSP 参数或新拆分接口已验收。
CPU/GPU 算法、随机种子、精度与运行环境身份必须记录，不能悄悄混用科学产物。

### 输入与组织

| 操作 | 执行类别 | 输入 → 输出 | 现有入口或设计边界 |
|---|---|---|---|
| input.inspect | I/O | 输入文件 → 形状、类型、稀疏度、元数据概况 | organize.profile_unit / prepare_osp.source_profiles |
| input.fingerprint | I/O | 输入版本 → 内容身份与校验记录 | run_state.file_identity；不在每次调度时重扫大文件 |
| input.validate | CPU/I/O | 输入与规则 → 数据契约检查结果 | upstream.validate_matrix |
| metadata.profile | CPU/I/O | obs/var → 分组、缺失、常量列统计 | organize / sample_mapping |
| organize.propose | AI | 输入概况 → 组织方案候选 | organize agent |
| organize.validate | control | 候选方案 → 接受方案或错误 | 保留现有科学约束 |
| sample_mapping.propose | AI | 样本元数据 → 样本来源候选映射 | sample_mapping agent |
| sample_mapping.validate | control | 候选映射 → 确认映射 | 不得用实验条件自动冒充独立样本 |
| sample.extract | I/O | 确认映射与矩阵 → 样本子集 | persample.write_subsets |
| matrix.merge | CPU/I/O | 匹配版本的样本产物 → 合并矩阵 | msp.integrate.pipeline.load_and_merge |

### 样本质量计算

| 操作 | 执行类别 | 输入 → 输出 | 现有入口或设计边界 |
|---|---|---|---|
| qc.metrics | CPU | 样本计数 → QC 指标 | osp.qc.qc_one_sample 内部提取 |
| qc.thresholds | CPU | 指标与明确规则 → 阈值和标记 | osp.qc._mad_bounds 等；可与 metrics 合并 |
| qc.dissociation_score | CPU | 计数与基因集 → 应激指标 | osp.qc；保留归一化语义 |
| qc.doublet | CPU | 样本计数 → doublet 分数与标记 | osp.qc 的 Scrublet 调用 |
| qc.coarse_clusters | CPU | 计数 → DecontX 初始化标签 | osp.qc._coarse_clusters_for_decontx |
| qc.decontaminate | CPU | 计数与初始化 → 去污染矩阵和诊断 | osp._decontx；不得默认宣称有 GPU 内核 |
| qc.decide | control | 指标、标记与规则 → QC 决定 | 保留当前规则；不要额外强加新的 AI 门槛 |
| cells.filter | CPU/I/O | 输入与已验证决定 → 保留/删除集合和新矩阵 | OSP/MSP 已有过滤逻辑 |

### 共享数值操作

| 操作 | 执行类别 | 输入 → 输出 | 现有入口或设计边界 |
|---|---|---|---|
| matrix.normalize_log | CPU | 计数 → 归一化表示 | OSP cluster / MSP _preprocess |
| features.hvg | CPU | 表达与批次 → HVG 表 | MSP _preprocess；保留小批次处理 |
| matrix.scale | CPU | HVG 矩阵 → 标准化表示 | OSP/MSP embedding 内部；注意稠密化峰值 |
| embedding.pca | CPU；GPU候选 | 表示 → PCA 与参数记录 | OSP/MSP embedding 内部；GPU 需单独验证 |
| integration.harmony | CPU/GPU已有 | PCA、批次、参数 → 校正表示 | MSP _run_harmony / _run_harmony_gpu |
| graph.neighbors | CPU/GPU路径已有 | 表示 → 邻居图 | MSP _run_cluster / _run_cluster_gpu 内部提取 |
| clusters.leiden | CPU/GPU路径已有 | 图与 resolutions → 聚类标签 | MSP _run_cluster / _run_cluster_gpu 内部提取 |
| embedding.umap | CPU/GPU路径已有 | 图/表示与参数 → UMAP | OSP/MSP cluster 内部提取 |
| graph.paga | CPU | 图与标签 → PAGA 汇总 | OSP/MSP；具体调用按契约复用 |
| clusters.subcluster | CPU；GPU候选 | 指定子集、表示、参数 → 子聚类 | msp.evidence.subcluster_once |
| clusters.dissect | CPU | 矩阵与聚类 → 质量诊断 | MSP _dissect / standissect-lite |
| markers.rank | CPU/GPU已有 | 表达与标签 → DEG/marker 表 | msp.integrate.deg；按现有 GPU 路径验证 |
| markers.compare | CPU | 指定两组 → 对比证据 | osp.cluster.deg_two_groups |
| lineage.subset | CPU/I/O | 接受的 lineage 划分 → 子集 | zmip.lineage.subset_for |
| lineage.markers | CPU | 表达与 lineage → lineage marker 表 | zmip.foreign.lineage_markers |
| lineage.foreign_score | CPU | 子集与 marker → 异源评分 | zmip.foreign.score_foreign |

### 证据与模型交互

| 操作 | 执行类别 | 输入 → 输出 | 现有入口或设计边界 |
|---|---|---|---|
| evidence.summary | CPU/I/O | 指标与标签 → 小型证据表 | msp.evidence；批次组成、counts 等 |
| evidence.gene_table | CPU/I/O | 基因/细胞选择 → 表 | msp.evidence.gene_table |
| evidence.islands | CPU | 嵌入与标签 → 岛/邻接证据 | zmip.plan.lineage_evidence / umap_islands |
| evidence.plot | CPU/I/O | 指定数据与图参数 → 图像 | 现有报告/UMAP 绘图；保留模型必需图片 |
| evidence.pack | control/I/O | 证据引用 → 大小受控的模型输入 | 不把完整 AnnData 放到 bridge |
| model.request | AI | 不可变提示、工具模式、上下文 → 单次模型回复 | Bridge：计划/注释/审核/lineage 方案是 purpose 参数 |
| tool.validate | control | 模型工具请求 → 合法 primitive 请求 | 白名单、权限、参数、输入版本与科学规则 |
| tool.resume | control | 工具结果引用 → 下次模型调用上下文 | 上下文持久化；不保持计算进程等待 |
| decision.validate | control | 模型提案 → 接受决定/退回错误 | MSP/OSP/ZMIP 既有校验规则 |
| annotation.apply | CPU/I/O | 接受标签与输入 → 注释产物 | msp.annotate._apply / ZMIP 注释应用 |
| lineage.plan_validate | control | 接受提案与证据 → lineage 计划 | zmip.plan.validate_plan |
| convergence.evaluate | control | 版本化指标、决定与政策 → 继续/收敛/停止 | 新协调逻辑；上限停止不等于收敛 |

### 结果、恢复与存储

| 操作 | 执行类别 | 输入 → 输出 | 现有入口或设计边界 |
|---|---|---|---|
| artifact.validate | CPU/I/O | 候选产物 → 大小/hash/数据契约验证 | osp_contract / MSP checkpoint / ZMIP cache |
| artifact.publish | control/I/O | 已验证的尝试结果 → 接受的不可变版本 | 原子元数据发布与尝试任期校验 |
| ledger.update | CPU/I/O | 接受的细胞变更 → 细胞账本 | ecarsi ledger；按版本幂等 |
| report.render | CPU/I/O | 接受结果 → 用户报告 | 与模型必需证据分开，低优先级但纳入交付契约 |
| result.export | I/O | 有效结果 → 用户指定目的地 | 现有 mirror/export；区分导出失败与计算失败 |
| checkpoint.reconcile | control/I/O | 提交记录、执行状态、收据 → 恢复行动 | 适配层；通知只能加速发现 |
| storage.probe | I/O | worker 可见挂载 → 存储域、权限、容量观测 | 与内存/CPU 探查独立 |
| artifact.stage | I/O | 源副本与目标存储 → 已验证副本 | 预留空间、临时文件、完成验证；显式复制任务 |
| artifact.promote | I/O | 临时或局部产物 → 满足持久化政策的副本 | 机时到期前迁出；成功后才推进需要可靠输入的阶段 |
| artifact.evict | I/O | 可再生且无使用租约的副本 → 删除确认 | 仅用户授权的 scratch/cache；不删原始/唯一/保留结果 |
| artifact.replicas | control/I/O | 副本观测 → 目录状态 | 同路径不是同一副本；失联与已丢失区分 |
| transfer.reconcile | control/I/O | 中断复制 → 恢复/重做/清理临时文件 | 不把半拷贝文件当完成 |

## 执行契约

每个 OperationSpec 保存：op_id、kind/version、dataset/unit/sample/lineage scope、
输入 artifact IDs、parameters、科学运行身份、依赖、输出角色、资源候选、优先级、
重试分类、结果持久化要求和迭代 generation。不得只用可变目录路径当输入身份。
请求、模型回复、运行凭据和产物不含 API key。

资源候选是经过验证的实现，例如 harmony.cpu / harmony.gpu，各自带 CPU/RAM/VRAM
与耗时估计。GPU 优先但不是无限等待；比较预计排队+搬运+执行+结果落盘时间。
不能仅凭 HQ 分配到 GPU 就把 CPU 算法当成 GPU 算法。模型使用 runtime backend 决策记录。

OperationRun 的逻辑状态：blocked → ready → submitted → running → validating → succeeded。
失败另外区分 retry_wait、input_invalid、unknown_external_result、cancelled、failed。
产物搬运也有自己的 operation 与依赖；I/O 排队不能伪装成 GPU 计算。
每个 attempt 有独立目录、后端执行标识、开始/结束、失效任期和错误分类。
completed 通知不是结果真相；读取已验证、已接受的持久收据才可推进下游。

执行资源不是数据库租约的替代：worker 的总配额不能超过 Slurm allocation/cgroup 或
用户给本地 runner 的限制。一个 allocation 内多个 worker 必须有不重叠的额度。
先按请求预算预留，结合实测峰值修正估计，不能因为瞬时 RSS 低就反复超卖未来峰值。

## 拆分尺度与数据驻留

优先拆：跨 AI/计算边界、CPU/GPU 切换、耗时且可独立重试、可以并发的样本/lineage。
normalize/log1p 与部分小表校验等可融合；邻居图/Leiden/UMAP 先沿用现有计算包，
独立收据和数值等价验证完成后再拆。不应为了列出 primitive 重写科学算法。

融合只是一种执行优化：保留内部操作身份与失败位置。显式声明最晚持久化边界，
节点损失时允许重做尚未持久化的融合段，不允许凭内存缓存宣称可跨节点恢复。
任务结束必须释放 CPU/GPU；数据缓存保留所占 RAM/磁盘仍须计入资源账本并可回收。

小数组/表在配置的 payload 上限内可以直接传输；大矩阵采用 artifact 引用。
不规定所有数组必须走文件，但必须保存足够的输入身份/内容以支持恢复。
模型输入中的图和小表也应有可追溯版本，不能读到下一代覆盖的文件。

## 本地运行与调度后端

已确认统一使用 pool，不保留绕过调度器的 local-direct 路径：
1. 单机 pool：四个组件部署在本机，一个额度受限的 Warm Pool Worker 执行计算。
   提供一条命令启动，明确 CPU/RAM/GPU/磁盘预算，不要求用户逐一启动服务。
2. 分布式 pool：普通机器和/或用户申请的 Slurm allocation 上启动 Worker 并加入。
   不自动申请 allocation，不因空闲退还；到期停止派活并在机时内收尾。

两种拓扑使用同一 operation/产物契约、资源准入和恢复路径。轻量控制操作仍在所属
服务内执行，数值子进程执行库归 Worker 使用，不是另一条绕过 pool 的入口。
同机组件独立监督与恢复，Work Coordinator 崩溃不能连带终止健康计算进程。
工作流持久化引擎与 pool 调度引擎仍待选型；统一 pool 不等于已经选定 HQ/Temporal。
单机全失联不保证服务连续可用；恢复后按持久记录接续。
配置不在包导入时要求 Slurm，也不默认连接生产 scheduler。

## 多级存储：速度等级与可见范围分开

不能用一条 local → scratch → archive 链表达所有机器。配置任意多个存储域：

| 例子（不是固定路径） | 可见范围 | 生命周期 | 推荐用途 |
|---|---|---|---|
| 进程 RAM / GPU VRAM | 单进程/单设备 | 进程或任务 | 连续计算热数据；计入预算 |
| node-local SSD | 单节点 | 节点/机时 | 可再生缓存、scratch、同节点接续 |
| group-shared fast disk | 指定节点组 | 按配置 | 小集群共享中间结果 |
| cluster shared scratch | 集群节点 | 可能受清理政策影响 | 活跃产物、跨节点接续 |
| durable project store | 指定站点或可访问客户端 | 长期保留政策 | 原始输入、可靠检查点、最终结果 |
| archive / remote store | 经显式搬运可到达 | 长期 | 归档或低频读取；可晚于首版实现 |

每个存储域配置 store_id、可见 worker/group、节点侧 root 映射、读写权限、
容量/配额、保留余量、临时/持久属性、吞吐观测和清理授权。同名路径不意味着共享。
探查需验证 store 身份与实际可读性；节点独有 /tmp 不能误标成集群共享。

Artifact = 不可变内容/语义身份；Replica = store_id + 相对路径 + 节点/域 +
大小/hash + 状态 + 验证时间 + 保留/使用租约。一个 artifact 可以有多个副本。
迁移副本不改变科学输入身份；参数/数值实现改变才产生新计算身份。

读取顺序是选择最低预计完成成本的可用副本，而非一律强制复制到最快盘。
单次顺序读可以直接用共享盘；反复复用才可能值得提前搬到 SSD。
集群外机器看不到共享盘时，先明确传输路径和身份校验，不假设 HQ 负责传文件。

artifact.stage 先预留目标空间（含临时文件、展开后大小与输出余量），限并发复制，
目标临时路径完成大小/hash验证后才标 ready。同文件系统可原子 rename；跨文件系统
必须先复制并验证，再发布副本记录，不能将跨盘 move 假定为原子操作。
同一共享盘的容量及带宽账本只能算一次，不能给每个 worker 各发完整容量。
每个 worker 上的 HQ disk 数值资源只能表达本地额度，不能直接解决跨 worker 共享配额。

产物策略显式分为 regenerable-cache / checkpoint / final：cache 可按授权清理，
checkpoint/final 满足要求的持久副本后才能发布成功。下游可在同节点先用临时产物，
但只有对应可恢复边界完成后才能声称容错保证成立。机时不足需包含 stage-out 余量。

缓存淘汰仅针对已授权 scratch、无活跃读租约且可重建或有合格副本的内容。
不自动删除原始输入、唯一可靠副本或用户保留结果。磁盘满返回 storage_wait，
释放尚未执行的计算预留并退避；禁止转成密集重试或继续写半成品。
传输失败不得污染有效副本；避免多个 worker 同时搬运同一大文件。

## 简单调度政策

1. 就绪与可执行筛选：科学依赖有效、输入可达、runtime匹配、CPU/RAM/VRAM和磁盘可容纳，
   剩余机时能覆盖输入搬运、执行、结果迁出及安全余量。
2. 基本公平：优先级+等待时长；大任务暂不适配时允许小任务递补，避免队首阻塞。
   持续有小任务也不能永久饿死大任务，可逐步为等待过久的大任务留出可满足的容量。
3. 同级任务优先复用已有可达副本，但不能因无限等待缓存节点而放空其他节点。
4. 模型请求、计算、I/O分别限流；按资源或字节设置有界积压，不按整个 driver 峰值卡入口。
5. 实测再调整估算：记录排队、stage-in、compute、model、stage-out、publish 各段耗时。
   不把更多 running 或更高 CPU 当最终验收；同时记录有效产物/小时、数据集完成/小时、
   重算比例、模型等待及 I/O瓶颈。

HQ 可承担 worker 内资源匹配和任务队列。RSI 适配层负责数据可达性、运行身份、提交去重
和产物恢复，不能与 HQ 同时独立分配同一批 CPU。第一版先保证输入在已知共享域可达，
复杂跨域软亲和与动态搬运须通过原型确认 HQ 接口是否足够；不足时才考虑上游扩展或 fork。

## 第一轮实施与验收

先接现有 OSP compute / model / publication 三段和 MSP Harmony / 聚类 / DEG 边界，
以现有数值实现保证输入输出契约。并行样本与 lineage 优先；其它 primitive 逐项解包。
不用一次同时更换科学内核、工作流引擎、调度器、数据格式。

最小验证矩阵：单机pool；HQ本地并发；HQ用户提供Slurm worker；GPU优先与CPU回退；
跨共享存储域复制；node-local丢失；磁盘满；copy中断；server journal丢最后状态；
旧worker迟到结果；AI等待时另一个数据集完成计算；动态增加/退出worker；迭代局部失效。

验收必须展示：有效结果可复用、没有重复发布、共享磁盘不超卖、传输等待不占GPU名额、
断线不改动有效科学结果。尚未进行本设计对应的 HQ 或跨盘运行测试。
