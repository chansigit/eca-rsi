# HyperQueue 复用评估

2026-09-14。结论：优先试验外部适配，不立即 fork，不立即替换生产 Dask。
官方仓库审查快照：680857bdfa45f92c50535cdef525f5a26bbcf55c，Cargo 包版本 0.26.2。
stable 文档与 main 快照可能不同；实际试验要固定发布版本并验证 API。
目前完成文档和源码检查，尚未运行 HQ 故障注入或跨节点试验。

## 适配位置

工作流协调器（Temporal仍为候选）→ RSI operation适配层 → HQ server/worker →
Apptainer中的primitive命令。Agent Bridge处理AI请求。产物/副本目录与搬运逻辑
供三者使用，第一版作为RSI模块而非新增一个必须常驻的第五服务。

资源分配只由一个后端负责。不要保留Dask和HQ同时管理同一份CPU/GPU。
同一个operation可以选local-direct或HQ，选择必须显式配置并有独立测试端点。
无需Slurm的本地HQ部署是官方支持的；完全不需要HQ服务的本地direct模式由RSI保留。
[仓库说明](https://github.com/It4innovations/hyperqueue)

## 已核实的能力与边界

| 要求 | 证据 | 对RSI的含义 |
|---|---|---|
| 手动加入本地或Slurm worker | worker start；Slurm内手动启动会检测机时 | 用户管理allocation，不使用hq alloc自动申请；不启用idle timeout |
| 单节点多任务 | CPU和通用 indexed/sum resources | 复用资源匹配；准确传入allocation实际额度 |
| CPU/GPU不同资源组合 | resource variants，job definition接口 | 适配层仍要选对数值实现；不是自动科学算法转换 |
| 剩余机时筛选 | worker time limit 与 task time request | 把输入搬运、stage-out、安全余量纳入估计 |
| 动态追加任务 | open jobs 支持不断提交新task | 循环在coordinator中展开；不需要预先列出全部迭代 |
| worker丢失 | server重新调度，执行从头开始，instance ID递增 | 现有有效检查点要由RSI复用；instance ID只在具体task内有意义 |

来源：[worker](https://it4innovations.github.io/hyperqueue/stable/deployment/worker/)、
[resources](https://it4innovations.github.io/hyperqueue/stable/jobs/resources/)、
[open jobs](https://it4innovations.github.io/hyperqueue/stable/jobs/openjobs/)、
[failure](https://it4innovations.github.io/hyperqueue/stable/jobs/failure/)。

资源账面约束与物理隔离是两回事。HQ文档明确通用资源是逻辑数字；
不能据此声称已强制限制任务RSS或已经隔离GPU显存。实际CPU绑定、内存限制、
进程树终止、GPU可见性、容器环境和Slurm步骤行为都要单独验证。
共享磁盘也不能仅在每个worker上声明完整容量，这会重复计算全局额度。

## 恢复方面的关键缺口

启用 --journal 后，server 可从持久化历史恢复jobs；worker连接不会自动恢复。
最后几秒尚未写入journal的完成状态可能丢失，恢复后会重算。
worker默认on-server-lost=stop；finish-running允许正在运行的任务结束后退出，
不是原worker自动重连到新server。[server](https://it4innovations.github.io/hyperqueue/stable/deployment/server/)、
[worker](https://it4innovations.github.io/hyperqueue/stable/deployment/worker/)。

因此采用HQ仍须设计：
- operation ID ↔ server generation/job/task/instance ID的持久映射；提交应答丢失后先核对再提交。
- 每个attempt输出独立目录；只有当前有效generation可以接受发布。
- 对已存在的有效产物先做收据检查，避免journal丢完成记录引发重复科学计算。
- node supervisor仅在用户allocation仍有效时重新启动worker并绑定当前server。
- server接力必须有明确主实例任期；共享同一个journal路径不等于自动选主。
- finish-running下旧任务和新server重调度可能重叠。先完成失效尝试隔离/租约设计，
  再启用该策略；不得依赖任务ID字符串当fencing。
- 失败重试归属清楚：HQ负责其worker失联重执行，RSI决定科学失败/参数变化的后续动作；
  避免coordinator的提交重试与HQ内部重试形成重复job。

Task notifications不持久化，也不重放，不能作为唯一完成依据。
[通知文档](https://it4innovations.github.io/hyperqueue/stable/jobs/notify/)。

## 多级共享存储

HQ的“without shared filesystem”主要说明如何分发连接元数据access.json；
它不是通用的数据分层、搬运、复制或配额管理承诺。
[非共享文件系统部署](https://it4innovations.github.io/hyperqueue/stable/deployment/cloud/)。

RSI需独立描述artifact及replica、存储可见范围、读写权限、配额、可靠性和清理政策。
初版通过可达的共享输入与显式stage任务保证正确性，再验证数据亲和调度接口。
若HQ缺少按副本位置选择worker的合适扩展点，优先向上游提小补丁；
只有试验确认适配层无法满足必要约束才维护fork。不要先写复杂全局数据调度器。

## 原型验收

1. 非Slurm本地启动server和两个额度受限worker，执行不同CPU/RAM请求的并发任务。
2. CPU/GPU variants在固定版本实际可用，选择与数值实现一致；无GPU时CPU继续。
3. 用户allocation worker加入/退出；机时不足不启动必然无法收尾的任务。
4. kill worker、kill server、journal未flush、丢通知、丢提交应答、旧任务迟到结果：
   均可恢复且结果只接受一次；明确哪些计算会重做。
5. 三个存储域：共享A、仅部分worker可见的共享B、每节点local SSD。验证复制、
   配额、缓存失联、半文件清理，以及任务不能被分到不可读输入的worker。
6. 真实OSP样本和MSP小任务通过同样契约，与本地直接执行的科学结果对比。

先验证这些边界，再决定HQ承担多少功能以及是否正式采用Temporal。
