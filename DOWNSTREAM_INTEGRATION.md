# MSP / ZMIP 升级审查与 RSI 对接规划

审查日期：2026-09-05（本地时间）。用户已解除 MSP / ZMIP 适配冻结。
本文保留实施前的源码审查证据。用户已授权多代理协作实施，当前进度见下节及文末；
历史欠账描述不代表当前实现仍有同样行为。

## 当前实施状态

- RSI 输入、状态、细胞台账、续跑身份及跨轮发布保护已落地；资源副本已同步。
- 发布组合：bridge **0.2.3**、OSP **0.1.2**、MSP/ZMIP **0.3.2**，均已同步
  PyPI 与 GitHub Release；安装命令见 [INSTALL.md](INSTALL.md)。
- bridge 0.2.2 的 Responses 长度中断恢复、MSP/ZMIP 的有界状态/表达查询和批次删除
  保护已测试。0.2.3 仅将版本改为单一来源，修复 0.2.2 模块仍显示 0.2.1 的打包遗漏；
  隔离 wheel 全部 **84 tests** 通过，运行代码比较确认只有模块版本字符串变化。
- RSI 在新鲜 Pandas 3 / AnnData 0.13 环境 **129 passed / 2 skipped**；MSP 两个
  Python CI 环境各 **179 passed**，ZMIP **117 passed** 且 CI 通过。
- Clayton 两轮及幂等重跑已通过，最终 850 个细胞与总台账一致；原共享 allocation
  OOM 标记和后来独立复核返回码 0 均保留在验证记录。
- **19Liu 全尺寸尚未验收完成**：前两次标注失败已归档；当前使用独立固定环境
  bridge 0.2.2 / MSP、ZMIP 0.3.2 重新标注，完成后才进入全尺寸 ZMIP。
  固定环境不因版本显示修正而更改，也不把发布或单元测试称为生物学准确性验证。


## 基线与结论

- RSI `82007ca`、OSP `1b3b2ab` 已推送各自 origin/main。
- MSP 0.3.0：`d4e7927795b4ee509bdc1f3153415dac59ee458c`。
- ZMIP 0.3.0：`c63d270545adf1f27393f768058d05c6ef144e70`。
- 实际计算解释器：`/scratch/users/chensj16/venvs/dl2025/.venv/bin/python`；
  运行时包版本为 MSP 0.3.0、ZMIP 0.3.0、harmonypy 2.0.0、bridge 0.2.0。
- 审查期间 MSP 新增 d4e7927，仅更新 TODO 文档，未改变被测试的运行代码。

主要调用路径仍兼容，但 RSI 外层完成校验、样本身份和跨轮恢复需要升级。
不能把新版内核自己的保护机制当作 RSI 已经自动接入。

## 内核发生了什么变化

| 范围 | 当前行为及 RSI 影响 | 源码证据 |
| --- | --- | --- |
| MSP 公共 API | 新增 `msp.evidence`、`msp.agent_tools`；旧 inspect/annotate 私有导出被删除。ZMIP 已迁移；RSI 当前 CLI 包装未直接依赖这些被删接口 | `../msp/CHANGELOG.md`、`../msp/msp/evidence.py`、`../zmip/zmip/runtime.py` |
| MSP integrate 拆包 | `msp.integrate` 从文件改成包，公开 `integrate_adata` 等仍可导入；wheel 包含子包的打包修复已落地 | `../msp/msp/integrate/__init__.py`、`../msp/pyproject.toml` |
| Harmony | 使用 `harmonypy>=2,<3`，CPU/C++ 实现；取消 torch/GPU 路径及 `MSP_DEVICE`。默认数值参数随 Harmony 版本变化，旧结果不能视为同一计算身份 | `../msp/msp/integrate/pipeline.py::_embed` |
| 调色和日志 | 使用 PyPI stanhue，取消技能目录和 `MSP_PALETTE_DIR`；MSP/ZMIP/bridge 使用 logging，CLI 默认 stdout | `../msp/msp/log.py`、`../zmip/zmip/__main__.py` |
| MSP 恢复 | `.msp-state/*.pending` 标记未完成步骤，`.msp-history` 归档受影响结果；报告可独立重建。CLI 检查整合参数及输入路径，但不比对输入内容哈希和实际源码 | `../msp/msp/steps.py`、`../msp/msp/__main__.py::_integration_matches` |
| MSP 输入约束 | 每份 OSP 输入必须只有一个 batch 值、基因轴完全一致、counts 存在、细胞 ID 全局唯一；不自动改名或补造 QC 阴性值 | `../msp/msp/integrate/pipeline.py::load_and_merge` |
| ZMIP 恢复 | 单目录写锁、输入/参数/运行时代码身份、plan/markers/lineage 内容校验；旧输出无记录必须用新目录或显式 force | `../zmip/zmip/cache.py`、`../zmip/zmip/lineage.py` |
| ZMIP 发布 | 全局输出先暂存，日志记录多文件替换事务，完成凭证最后发布；中断恢复、删除/转移台账及谱系覆盖校验 | `../zmip/zmip/publication.py`、`../zmip/zmip/merge.py` |
| Agent 续跑 | ZMIP 将模型、effort、预算等记入审计，不用它们使已完成计算失效；RSI 当前仍有自己的 agent 配置检查 | `../zmip/zmip/cache.py::prepare_run`、`ecarsi/__init__.py` |

MSP 的 pending/归档和 ZMIP 的恢复强化包括 0.2 阶段累积改动，不全是 0.3 新增。
`msp.harness` 目前仍兼容，但已弃用并计划在 0.4 删除；RSI 的跨仓库测试应明确版本边界。
资源副本差异是文件读取方式、测试路径注入和格式变化，当前 diff 未显示资源计算公式改变。

## 集成欠账（按优先级）

### P0：失败和不完整输出可能被当成完成

1. `crosssample.load_persample` 只看文件存在（甚至认可 `.pruned`），没有接住前段新增的
   manifest 失败列表、逐样本成功状态与内容身份。复现：manifest 为 failed，空契约文件齐全，仍被接收。
2. `crosssample.main`、`zoomin.main`、`loop._run_msp_from_h5ad` 的提前返回绕过内核验证。
   复现：MSP `annotate.pending` 存在且契约全为空文件，后续轮包装仍返回 0；
   ZMIP 存在发布日志且原生 `publication.complete` 为 False，包装也返回 0，未调用内核。
3. `layout.MSP_CONTRACT` 没要求 `annotation_removed.csv`；ZMIP 契约没有删除、转移台账和发布凭证。
   `ledger` 对缺少的删除 CSV 直接跳过，不能作为发布前的细胞守恒证明。
4. loop 还会凭 `decision.txt + stats.txt` 跳过整轮，凭既有 round input 复用上一轮输入。
   只修两个包装入口不能覆盖跨轮缓存和最终 release。

### P0：实验样本 ID 和目录名混用

`persample.build_entries` 在目录名附加哈希；`crosssample` 的纳入决定使用 manifest `value`，
`ledger._persample_frames` 却使用 `d.name`。因此同一个样本排除决定无法匹配台账中的 sample。
必须以 manifest 的稳定样本 ID 为唯一业务键，目录名只负责定位文件。
已复现：排除一个含两细胞的样本后，总台账仍把两细胞标为 `kept`，预期均为 `excluded-sample`。
同时删除 `build_ledger` 静默丢弃重复细胞 ID 的行为，改成显式报错并验证各阶段集合守恒。

### P1：实验池与整合校正因子仍混为一谈

RSI 默认把 `eca_sample_id` 直接交给 MSP 的 `--batch-col`。它适合保证 OSP 完整实验池，
但并不证明每个实验差异都应被 Harmony 校正。上游 `correction` 和派生 TSV 证据需要进入明确的
整合配置；保留现有行为作为显式记录的兼容策略，不能凭空推断生物学条件应消除。
MSP 多文件入口要求每文件一个 batch 值，若校正列在单实验内有多值，应先验证并走适当的
合并入口，不能直接替换命令参数。`correction=unnecessary` 应有明确的跳过校正机制和记录。

全被 QC 删除、少量幸存但不能聚类、annotation 失败必须分别表示。目前前段按失败阻断，
不能为了继续整合伪造 H5AD 或把这些情况交给纳入 agent 当生物学排除理由。

### P1：输入身份、锁、参数和记录缺口

- MSP 内部记录输入路径和主要参数，不能发现原路径文件被替换或 Harmony/源码变化。
  RSI 应记录实际 MSP_PYTHON / ZMIP_PYTHON 的版本、模块路径、源码哈希、输入内容和配置。
- MSP steps 要求调用者保证单写者，源码本身没有提供 ZMIP 那样的父进程运行锁。
  RSI 应有 unit/round 写锁，锁覆盖纳入决定、内核调用、状态发布；不重复持有 ZMIP 同一把锁。
- 纳入决定目前不核对样本清单/证据变化，不原子写入；续跑需验证证据身份和完整决定 schema。
- MSP/ZMIP 的 resolutions、HVG/PCA/neighbors、Harmony、language、effort、预算应统一配置，
  首轮和后续轮必须一致透传。不要把 RSI `--force-reopen` 偷换成内核 `--force`。
- `cost.run_streamed` 已合并 stdout/stderr，兼容新日志；但后续轮 MSP 使用普通 subprocess，
  未经过费用记录路径。这是既有遗漏，不是 logging 引入的丢失。

### P2：安装说明与资源副本

`INSTALL.md` 仍写 MSP 0.2、Harmony 0.2、torch 和 MSP_DEVICE；kernels extra 的 MSP/ZMIP
版本范围过宽。应按本轮验证组合限制至 0.3 系列，清理旧安装指引。
资源代码可先同步已验证的实现并通过现有测试；是否进一步抽成公共基础设施应单独定范围，
这次不为资源检测而扩大 bridge 的 agent-runtime 职责。

## 实施顺序和验收

| 阶段 | 交付范围 | 验收条件 |
| --- | --- | --- |
| E1 | 下游运行状态与统一验证入口；前段交接、MSP/ZMIP 完成检查；样本台账 ID 修复 | 本文四类最小复现转为回归测试；空文件、pending、发布中断、失败前段均不能成功；整样本排除准确记账 |
| E2 | unit/round 锁、内容和运行身份、纳入决定及输入快照；贯通 loop/release | 同路径输入变更、参数或内核变更拒绝旧结果；双写被阻止；中断恢复不混用代次；已有 stats/decision 不绕过验证 |
| E3 | 配套版本、统一参数、整合校正策略、日志费用、资源副本与文档 | 两种 MSP 入口参数一致；Harmony 2 运行验证；实验池不被校正分组重切；原始 counts 和 ID 不变 |
| E4 | 新目录真实全流程与恢复验收 | 多样本真实数据走到 release，至少验证第二轮；含不下钻谱系、实际下钻、删除/转移台账；重复运行不重调已完成 agent；核对细胞集合、counts、删除并集和发布凭证 |

验收时单样本跳 Harmony、多样本 Harmony、无下钻和有下钻都应覆盖。
带 `.pruned` 的历史结果仍可浏览，不能凭标记冒充可继续计算；迁移/force-reopen 必须从可验证的
保留数据建立新一轮身份，不自动重算或覆盖历史发布。

## 实施前验证与边界

- RSI 指定检查：`test_crosssample_cwd.py` + `test_harness_sync.py`：3 通过、1 失败。
  失败仅资源副本逐字节比较；原先 MSP 导入错误在当前组合已消失。
- MSP：step_recovery、evidence_contracts、resources；ZMIP：resume_and_runtime、
  publication_runtime、pipeline_guards，合计 **122 项通过**。
- `zmip.runtime.check_runtime()` 通过；实际包版本与上述基线一致。
- 外部复现目录：`/scratch/users/chensj16/eca-runs/_downstream-audit-20260905/`，
  包含 `reproduce.py`、`results.json`、`ledger_reproduce.py`、`ledger_results.json`。
- 测试使用已核实运行的 allocation 41891659，节点 sh03-01n42；没有变更队列或提交新作业。
- 本次未运行真实模型的完整 crosssample → zoomin → release，不宣称整条升级已验收。
- 本文只修改 RSI 规划记录，未修改 MSP/ZMIP 或 RSI 后段运行代码。


## 2026-09-05 统筹实施记录

用户追加的维护清单与 E1–E4 同步安排，不将内核维护测试冒充 RSI 全流程验证。

| 工作线 | 已落地内容 | 验证/边界 |
| --- | --- | --- |
| MSP | Scanpy DEG 特定 log2 警告按调用汇总，其他警告/异常保留；Harmony 默认关闭迭代日志；提供 ZMIP 所需 7 个公开 API；修复子簇删除后仅剩一个细胞的 Wilcoxon 异常 | 本地 commit `2e036c87`，版本准备为 0.3.1，未发布；137 tests 通过，覆盖率 89.63%，annotate/inspect/evidence 分别 85%/75%/81%；wheel 外部导入通过 |
| ZMIP | Ruff/格式化、Python 3.10/3.12 CI（测试/覆盖率报告/wheel 导入）；兼容层优先 MSP 公共 API，集中保留已发布 0.3.0 的回退；TODO 与变更记录 | 本地 commit `75a44c0`；105 tests 通过，wheel 导入通过；Actions 尚未远端执行，0.3.0 本地 wheel 只用于验证，不能覆盖 PyPI 已发布版本 |
| RSI E1 | 接住 schema-2 persample 成功状态、失败清单与实际样本输出；拒绝空文件、未完成标记、篡改结果；直接分块读取 HDF5 counts 对比；修复样本目录和 ID 混用、not-zoomed 跨轮丢失 | 原最小复现已转换为回归测试；台账验证输入/幸存/删除/排除集合，拒绝重复和缺台账，保留字面 ID 001/NA |
| RSI E2 | unit 写锁、纳入证据身份、阶段内容与运行身份、跨轮输入快照、round/release 收据；保留历史 decision 重开；可恢复 release 目录发布事务 | 包含 fork/独立进程锁检查和真实子进程崩溃故障注入；报告/台账改变不能当已完成；prune 中断累计汇总、postflight 身份变化和完成轮次改批次列的补丁均已合入 |
| RSI E3 | 0.3 系列依赖范围、Harmony 2 安装指引、统一两种 MSP 入口参数和费用收集；同步资源副本；显式 MSP_BATCH_COL 与实验池字段分开；修复既有 Python 3.10 f-string 语法不兼容 | 默认校正策略仍明确记为 compatibility_default；每个 OSP 完整实验内须只有一个 batch 值，不凭空把生物学条件消除 |
| E4 大数据 | 19Liu 新目录接 existing integrate → inspect → annotate → ZMIP，明确 mouse | 81,079 × 27,763、6 样本；既有 counts/ID 与原输入逐元素一致；原报告 5 节、14 表、17 图已核对；全链运行中，不能提前宣布通过 |
| E4 RSI | Clayton 新目录 organize → 完整实验 OSP → 两轮 MSP/ZMIP → release → 原命令幂等 | 1,766 细胞，实验池 387/725/654；已完成两轮与幂等复验，release 850 细胞；使用 PP 0.2 历史输入，不是新生成的 PP 0.5 输入 |

全尺寸验证目录：`/scratch/users/chensj16/eca-runs/_downstream-integration-20260905/19liu`。
RSI 验证目录：同级 `clayton`；运行日志在同级 `clayton-logs`。
共享 dl2025 环境的 `pip check` 存在跨项目旧依赖冲突，完整输出已由 19Liu 验证任务归档；
本链 MSP/ZMIP/bridge 的直接依赖和实际公开 API 检查通过，不能据此声称全环境无冲突。

### 有意保留的维护项

- MSP harness 删除属于 0.4 的既定兼容性变更，不在 0.3 补丁提前删除。
- adjustText 的逐字节确定性不只靠 seed：还受 RNG 与按时间停止的迭代预算影响，需单独验证策略。
- DegTables 懒加载、report 共用 helper、旧快速 monkeypatch 测试替换仍在 MSP TODO，
  属于性能/可维护性工作，不阻断本轮修复；没有性能证据时不进行大范围重构。
- ZMIP schema-1 的 torch=None 只是身份字段；直接删除会使旧运行身份不匹配，随有迁移说明的
  schema 升级再清理。此字段不代表仍依赖 torch。
- `correction=unnecessary` 自动决策和单实验含多个技术校正因子的高级策略尚未实现；
  当前提供显式校正列并拒绝不满足内核约束的分组，不静默重切 OSP 实验。

### 全尺寸运行暴露的新问题与独立修复候选

19Liu inspect 已完成（约 19 分 50 秒，峰值约 4.27 GiB），47 个簇中原建议
29 keep / 2 flag / 16 drop，共 490 个细胞进入 inspect 删除候选。审查发现其中
6 个 `artifact-batch` 簇、147 个细胞仅凭样本偏倚等证据被建议删除；本份输入的
sample 与 condition 一一对应，不能据此区分条件特异生物学与技术批次。这不是对
原研究是否具有重复的判断。

MSP 独立候选增加宿主规则：`artifact-batch` 的 drop 转为 flag，保存原建议和调整原因；
已有不符合新规则的 inspect 结果不能直接进入 annotate。保存的真实 inspect 提案经正常
完成路径重放后，inspect 删除候选由 490 降为 343；与 38 个预注释候选取并集后由
528 降为 381。该变更及回归测试在隔离副本验证，143 tests 通过，尚未替换正在运行的源码。
基线 annotate 完成后，在新的 `19liu-guarded` 目录重放已保存的 inspect 提案（明确记录
不重调 inspect 模型），重新运行真实 annotate 和全尺寸 ZMIP。

基线 annotate 还因读取 81,079 行细胞台账而两次耗尽模型上下文。bridge 0.2.1 候选
为宿主 Read 增加 UTF-8 安全的字节分页：默认 8 KiB、最大 32 KiB、明确续读位置。
62 tests 通过；真实 OpenAI 三页读取并提交结果的 smoke 通过。Claude 自带 Read 不由
此接口实现。0.2.0 已发布内容保持不变。

验证用独立 wheel 安装目录为同级 `guarded-runtime`：bridge 0.2.1、OSP 0.1.2、
MSP 0.3.1、ZMIP 0.3.1。51 个 Python 文件在候选源码、wheel、安装目录间逐个一致，
8 个发布文件通过 twine check；这些是本地候选验证，尚非 PyPI/GitHub 发布。
OSP 0.1.2 同时解决已发布 0.1.1 对 bridge 0.1 的依赖约束与新版 RSI 不可共同解析的问题。

后续基线 annotate 运行约 26 分 23 秒后明确失败（exit 1）：两次上下文重置后，第三次
请求仍因图像与文本总 token 超限被 provider 拒绝。前两次超限紧随大条形码 CSV 读取；
最后会话已避开该 CSV，但图像、77 簇证据等累积后仍超限，不能把全部失败归因于单次 Read。
基线没有最终 annotation_proposal/annotated.h5ad，保留 annotate.pending 和失败日志；
没有启动基线 ZMIP。新的 guarded 检查重放已完成，真实 annotate 正在运行，分页是否足够
仍以这次运行结果为准。

外部 RSI 源码副本合入待应用的 prune 中断汇总修复、计算结束后输入/运行身份复核、
完成轮次显式 MSP_BATCH_COL 变更检查后，全套测试 **125 passed、2 skipped**。
当前 live 源码仍为 Clayton 已开始运行的版本；这些结果不表示延后补丁已合入 live。

### 标注阶段的补充边界（隔离候选）

- MSP/ZMIP 的标注 schema 原本也允许 `remove_reason=batch`，因此 inspect 保护不能代表
  整条链已阻止仅凭批次疑点删除。独立候选将该请求转为 keep，保留 requested action/reason、
  host adjustment 和 review 标记；不会把保留细胞混入删除台账，也不覆盖独立 QC 删除。
  转换后继续验证合并关系和标签，旧的未规范化 batch/remove 提案不能直接应用。
- bridge 重置保留任务与提交闭包，但 MSP 原来没有查询已提交标签的工具。候选
  `annotation_status` 提供待办 ID、已提交标签/action/merge 的小页摘要及单簇详细分页；
  恢复时先查状态，避免重新收集全部 77 簇证据。响应上限 16 KiB。
- `check_genes` 原来每次返回所有簇的表达表。候选支持显式 comparison clusters，超过
  16 KiB 时明确报错并要求缩小查询，不返回部分数值表冒充完整证据。Python `gene_table`
  默认行为保持不变。真实 OpenAI 验证完成“大表被拒绝→指定两个簇→提交准确均值”，
  3 次模型请求，输入 3,239 / 输出 574 tokens；记录在 `gene-query-smoke/`。
- 合并 MSP 候选共 167 项测试：首次 166 通过，唯一失败是旧测试要求工具说明精确等于
  DOC；加入新查询帮助后更新该断言，相关 8 项再次通过。ZMIP 候选全套 116 项通过。
  这些补充候选尚未替换正在运行的 Clayton 或 19Liu guarded 环境。


### Clayton 最终结果与源码合入

真实流程：organize 1,766 → OSP 1,222 → 第一轮 MSP 1,009 / ZMIP 1,002 →
第二轮 MSP 858 / ZMIP 850。两轮实际下钻分别为 918 和 785 细胞；最终 850 个 ID
与 1,766 行总台账中的幸存者完全一致，counts 对比、原生完成凭证、两轮及 release
收据均通过。原命令的 organize / persample / loop 三项重跑没有新增 agent 调用，
10 个关键状态与输出哈希一致。

原长 Slurm step 在应用层全部正常退出后仍被共享 allocation 的 4 个 OOM 事件标记，
该调度器异常不隐去。另一个轻量步骤重新完成三条幂等命令、哈希及台账复核，
**Slurm 返回码 0**。证据：`clayton-logs/final-audit.json` 和 `clayton-final-verify.log`。
完成后已解除 live 源码冻结并合入最终候选及 RSI 延后补丁；该真实运行使用的是冻结前的
实现，新增保护另有候选/最终源码回归证据，不伪称整套最终源码重新调用模型跑了两轮。

最终发布 packet：`releases-final/`。四包 8 个产物通过 twine，RSI front/kernels ×
Python 3.10/3.12 的四种依赖解析通过，实际导入通过；51 个 Python 文件与独立
`guarded-runtime-v2` 一致，仅依赖元数据等打包信息有变。最低版本采用 bridge 0.2.1、
OSP 0.1.2、MSP/ZMIP 0.3.1，待逐包发布后再验证纯 PyPI 解析。


### 发布与最终本地回归

2026-09-05 已发布并同步 GitHub Release：bridge 0.2.1（47f5177）、OSP 0.1.2
（b1e2bdf）、MSP 0.3.1（1a4660a）、ZMIP 0.3.1（a8c329c）。八个 PyPI 文件的
SHA256 均与 `releases-final/dist` 一致，逐包记录见同目录 `*-pypi-verification.json`。
RSI 运行代码保存为 `2abca32`；安装说明已改用真实存在的发布版本。

最终 live 组合：RSI **125 passed / 2 skipped**，MSP **167 passed**，ZMIP
**117 passed**；测试进程和 Slurm 步骤返回码均为 0。bridge 分页 **62 passed**
及真实 OpenAI 分页 smoke 已完成，最终 51 个包内 Python 文件与发布 wheel 一致。
远端 Actions 与纯 PyPI 解析另行核对。19Liu 的较早 guarded 标注仍在运行，
不能把包发布和本地回归当作全尺寸 ZMIP 已通过。

### 新鲜依赖与大数据失败后的补充修正

远端 Python 3.12 首次运行使用 Pandas 3.0.5 / AnnData 0.13.3.post0，发现 MSP 将
字符串扩展类型排除在先验标签之外；RSI profile 也有同一判断。MSP 0.3.2（5dea759）
已修复并发布，远端 Python 3.10/3.12 各 179 tests 通过。RSI 同类修复增加四个真实
H5AD 回归，在隔离 Python 3.12.14 / Pandas 3.0.5 / AnnData 0.13.3.post0 环境
全套 **129 passed / 2 skipped**，Slurm 返回码 0；正常解析安装且 pip check 无冲突。
记录见 `pandas3-verification.json`、`pandas3-rsi-tests.log`、`pandas3-freeze.txt`。

较早的 19Liu guarded annotate 最终仍失败：56 分 14 秒、峰值约 4.37 GiB；一次上下文
重置后，provider 返回 `response.incomplete(reason=length)`。没有有效最终提案或
annotated.h5ad，ZMIP 没有启动。失败归档见 `19liu/VALIDATION_STATUS.md`。
正在准备 bridge 0.2.2 的精准有限恢复，再使用有状态查询和表达结果限制的新运行目录验证。
跨进程保存部分标注需要绑定输入/配置和安全恢复协议，作为后续独立工作；不能从截断日志
重建提案，也不通过无限重试掩盖失败。
