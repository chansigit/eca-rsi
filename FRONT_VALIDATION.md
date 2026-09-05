# 前半程升级验收记录

日期：2026-09-04。范围：ECA-PP 文件接收、organize、实验样本映射、OSP 执行与续跑。
MSP / ZMIP 的内核和 crosssample / zoomin 适配未修改，未运行后续轮次。

## 已落实的交付

| 阶段 | 结果 |
| --- | --- |
| A | 当前输出发现、`.history` 排除、全来源状态清单、schema/counts/维度/物种检查、结果快照；组织过程按计划记录并恢复 |
| B | TSV 在原始 ID 上对齐、来源内实验决策、显式跨来源合池、完整实验池检查、全细胞样本映射；200+ 分组不硬拒绝 |
| C | 单/多样本统一 OSP worker、参数透传、锁、成功状态与内容指纹、QC 删除守恒、proposal/H5AD 一致性、失败分类、注释独立恢复 |
| D | 配套源码清单、独立测试、真实输入验证、复核展示、安装和迁移说明；最终真实运行的明细见下方 |

代码入口：[organize.py](ecarsi/organize.py)、[sample_mapping.py](ecarsi/sample_mapping.py)、
[persample.py](ecarsi/persample.py)、[osp_worker.py](ecarsi/osp_worker.py)、[osp_contract.py](ecarsi/osp_contract.py)。
使用方式：[FRONT_INTEGRATION.md](FRONT_INTEGRATION.md)。

## 自动检查

在已有 Slurm allocation `41891659` 内，使用 Python 3.12、`LC_ALL=C LANG=C` 运行。
没有新建或更改用户队列作业；测试与真实运行的生成文件均在仓库外。

- 前半程回归、驱动入口检查和 OSP 原有测试组合：**77 passed，18 warnings**。
  包含真实 OSP API 的 worker 调用测试，但测试中的模型提交使用 mock。
- 额外独立运行原有 `test_harness_sync.py`：**2 passed，1 failed**。
  当前 MSP 导入与 shim 身份检查通过；唯一失败是 ecarsi / msp `resources.py` 不同。
  保留失败暴露该冻结项，没有复制覆盖文件。
- `ruff check --target-version py312 --select F821,F822,F823` 通过；`git diff --check` 通过。
- 回归明确覆盖：空/损坏完成文件、非零子进程残留、QC 外来/重叠/重复 ID、标签不一致、
  输入/参数变化拒绝复用、注释失败后不重算 QC、组织到第二单元中断恢复、整体目录搬迁、
  稠密矩阵不同基因集合合并补零、新版 raw-expansion 指标/原因保存，以及模型如实提交“分组未知”。

测试日志：`/scratch/users/chensj16/eca-runs/_front-integration-20260904/tests.log`；
冻结项日志：同目录 `frozen-checks.log`。

## 真实输入

### Clayton 2025：三个完整来源内样本

输入使用现有 ECA-PP `uniChondro/18_Clayton_2025`，实际结果版本为 0.2.0，共 **1,766 个细胞**。
显式使用原始 `sample` 列，其三个值为 `r1_ctrl`、`r1_inj`、`r2_ctrl`。
organize 保留完整文件，没有先按器官或任意细胞数裁样。

最终运行目录：`/scratch/users/chensj16/eca-runs/_front-integration-20260904/clayton-verified`。
采用 Scrublet / DecontX 默认开启、resolution 1.0、真实注释开启、Chinese、effort low；
共享 bridge 为 OpenAI 后端，模型与源码的实际身份保存在 manifest 和每样本运行记录中。

最终脚本 **exit 0**，三个样本均为 complete / exit 0 / attempt 1：

| 原始样本 | 输入细胞 | OSP 保留 | QC 删除 |
| --- | ---: | ---: | ---: |
| r1_ctrl | 387 | 299 | 88 |
| r1_inj | 725 | 579 | 146 |
| r2_ctrl | 654 | 344 | 310 |
| 合计 | **1,766** | **1,222** | **544** |

`validation.json` 确认：输入到 organized 的原始 counts 完全相同；每个 clustered
输出在对应细胞/基因上的 counts 与来源一致；原始 `sample` 未被覆盖；所有细胞恰好归属一个实验；
逐样本“输入 = 保留 + 删除”，没有重叠或外来 ID。真实注释 proposal 与 H5AD 标签检查也通过。

随后再次执行 `persample`，再执行 `run --stop-after persample`，两次均 exit 0；
三个样本的 attempt 仍为 1，没有再次启动 worker。组织结果也经完整身份检查复用。
没有创建任何后续 round。

完整日志：`/scratch/users/chensj16/eca-runs/_front-integration-20260904/verified-validation.log`。
该目录的 `verified-validation.sh` 和 `verify-clayton.py` 保存了运行与逐细胞核验命令。
此前调试目录 `clayton`、`clayton-v2`、`clayton-final` 留作过程记录，以 `clayton-verified` 为最终验收。

### MCA1.1 AdrenalGland：真实派生 TSV 接入

输入同样为已有 0.2.0 结果，共 **11,815 个细胞**。完成 organize 与逐 ID 对齐验证：

| 派生分组 | 细胞数 |
| --- | ---: |
| AdultAdrenalGland_3 | 6,596 |
| AdultAdrenalGland_1 | 4,060 |
| AdultAdrenalGland_2 | 1,159 |

原始 cell ID 全部保留，`eca_pp_batch` 与 TSV 按 ID 对齐后的值逐项相同。
记录：`/scratch/users/chensj16/eca-runs/_front-integration-20260904/adrenal-final/derived-validation.json`。
此案例验证文件接入；TSV 对齐本身不证明三组是物理实验，未在这些分组上启动 OSP QC。

## 版本刷新与剩余边界

- 初审上游为 `9c3c8c2`；实施期间 ECA-PP 新增 `6c025a7` 的 `.raw` 扩展。
  已核对变更：文件/schema/counts 契约不变，新增 `metrics.raw_expansion` 及复核原因由完整快照保留。
  构造结果的回归覆盖新增字段；未把实际 0.2.0 数据结果声称为新版重新生成。
- OSP 仍为 `32bd68e`，bridge 仍为 `6a063c2`。精确记录见 [FRONT_COMPATIBILITY.json](FRONT_COMPATIBILITY.json)。
- 跨器官拆开同一实验时，新驱动会阻止局部池 QC；共享实验级 QC 尚未设计。
  没有实验依据的跨来源合池、派生 barcode 分组仍需明确实验信息，不能靠校正指标自动推断。
- OSP 零幸存/不可聚类样本当前使 unit 未完成；其后续纳入、整合 batch 与物理样本的区别，继续等 MSP 定约。
- ECA-PP 自有 harness 的迁移仍由上游/bridge 协调。本次未修改任何同级仓库。
- 仅验收 Python 3.12。仓库原有 index/serve 含 3.12 的 f-string 写法，不能用本次检查宣称
  `requires-python >=3.10` 的全范围已验证；打包最低版本或旧 Python 兼容另需收口。

- 实际 Chinese 注释运行出现 Matplotlib 缺少 CJK 字体的 warning，部分 PNG 中文标签可能缺字；
  JSON 与 HTML 文本保留中文。字体配置属展示欠账，本次未修改 OSP 渲染器。

真实结果证明调用、数据身份与台账衔接；不把模型注释文本或单次 QC 过滤比例作为生物学准确性的证明。

## 2026-09-04 后续 ECA-PP 增量审查

当前配套上游更新为 **0.5.1 / `b461c46`**，包含 `0576683`：扩展到更完整的 `.raw`
后直接信任它，不再与旧 HVG counts 比对；`raw_expansion.reference_source`、`counts_check`
及该比对产生的复核原因不再生成。其他检查仍可能产生 `needs_review`。
schema 2、标准化 H5AD/counts 层、identify-columns 与 TSV 接口没有变化。
本地两个 tracker 脚本的未提交修改只涉及重复命令行参数的收集，不改变接入契约。

RSI 接收代码不依赖上述可选字段，本次没有修改运行代码。回归改用旧版真实字段名
`counts_check`（替换原构造测试中的 `reference_check`），并新增 0.5.1 无比对、状态 ok 的情况；
两种情况均验证 organize 接收成功，manifest 和独立 JSON 快照完整保留原结果，包括状态与原因。
新版重新生成后若输入内容发生变化，现有身份检查会阻止静默复用旧目录，应使用新 RSI 输出目录。
本次没有重新执行 ECA-PP 标准化或真实模型注释，上面的实际数据验收仍对应原记录。

当前运行 `tests/test_front_integration.py`：**32 passed、1 failed**。
新增的新旧 raw 结果回归均通过；失败是 `test_front_bridge_identity`：
bridge 已另行更新为 `7e658bc`，新增 `DEFAULT_FORMAT`、`configure_logging`、`ensure_logging`
公共导出，RSI 现有 shim 缺少 `DEFAULT_FORMAT`，首次对象检查即报 AttributeError。
这项属于独立的 bridge 接口同步欠账，不是 ECA-PP 输入失败；原先的 bridge 验收基线保留，
不能将当前组合宣称为全部通过。本次未修改 bridge、OSP、MSP/ZMIP 或其适配。

## 2026-09-04 bridge 0.2.0 适配与发布

- bridge 源码未修改：从干净提交 `7e658bc069fc0ccc164f91a7278b7a2ab4311dcd` 构建 0.2.0，
  wheel 与源码逐文件一致，wheel/sdist 均通过 `twine check`，已上传 PyPI 并校验线上 SHA256。
- 共享 bridge 自身测试：50 passed。真实 OpenAI agent 经 RSI shim 调用，共享 ToolSpec
  工具提交 `bridge-0.2.0-ok` 成功；这不是全量生物学分析重跑。
- RSI CLI 与 OSP worker 显式配置 stderr 日志；子进程测试验证连续两次初始化、
  ensure_logging 共存、每条日志只出现一次且 stdout 内容未混入日志。异常对象可跨 shim 捕获。
- 前段、worker、agent selection、日志、旧 harness 检查和 OSP 测试共 82 passed、1 failed。
  唯一失败 `test_resource_copies_still_match` 属于已知资源副本欠账，未改 MSP/ZMIP。
  旧接口测试固定原公共接口，不再把 bridge 新增接口强制加入 shim 契约。
- OSP 工作区仅改 pyproject.toml 两条 bridge 依赖声明；OSP 未发布新版本。
- 发布和真实调用记录在 `/scratch/users/chensj16/eca-runs/_bridge-release-20260904/`：
  `dist/`、`sha256.json`、`smoke.py`、`smoke.json`、`smoke.stdout`、`smoke.stderr`。

- GitHub Release 与远端 `v0.2.0` 标签已同步到相同提交：
  https://github.com/chansigit/agent-harness-bridge/releases/tag/v0.2.0
  附相同 wheel/sdist 和 SHA256 清单。PyPI simple 索引更新后，普通 pip 下载成功；
  下载的 wheel 在全新 venv 安装并导入 0.2.0 及新增日志接口成功。
