# ECA-PP / OSP 集成审查与升级规划

审查日期：2026-09-04。状态：用户已授权实施，A/B/C 已落地，D 已完成前半程验收。

下文第 2–6 节保留实施前的审查证据。新版行为见 [FRONT_INTEGRATION.md](FRONT_INTEGRATION.md)，
实际验收与剩余边界见 [FRONT_VALIDATION.md](FRONT_VALIDATION.md)。MSP/ZMIP 冻结要求继续有效。

本次覆盖 `eca-pp → organize → persample → osp`，以及共享 `agent-harness-bridge` 的接入边界。
MSP、ZMIP 及其 crosssample / zoomin 调用适配暂缓，等用户通知其开发完成后再审查和实施。

结论：当前输出文件名和主要标签仍能衔接，但上游归档、质量状态、实验样本身份及外层完成判断存在实质欠账。应先修这些接口，再验证新版组合；只升级依赖版本不够。

## 1. 本次核对的版本与证据边界

| 项目 | 本地提交 | 声明版本 | 核对范围 |
| --- | --- | --- | --- |
| eca-rsi | `ccade434f907d4d9a2306b2c028135e3c7d7a500` | 0.1.0 | 主线源码、调用参数、manifest、现有测试 |
| eca-pp | `9c3c8c20ae173acc4a0d02e5accbc39c09bc43fd` | 0.5.0 | standardize / identify-columns 的实现、结果格式、归档方式 |
| osp | `32bd68ee9d12ec3753c9109f32def1ec33d88044` | 0.1.1 | CLI / Python API、QC、注释发布、输入输出约束、测试 |
| agent-harness-bridge | `6a063c2439cdc14b81bd49f8f2fa2ee3eed24d8c` | 0.1.0 | 公共 API、shim、安装依赖和责任边界 |

OSP 当前提交还包含 `docs/NEWS.md` 中的 Unreleased 修改；仅写 `osp-sc==0.1.1` 不能证明安装了本次审查的全部代码。以上是本地 checkout 基线，不是远端最新版本声明。

实际解释器为 `/scratch/users/chensj16/venvs/dl2025/.venv/bin/python`，Python 3.12。
ecarsi、osp、bridge 均从对应同级源码导入；该环境没有安装 eca-pp。后者不是文件交接方式的缺陷，建议适配层继续消费结果文件，不为读取 JSON/TSV 强制安装整套上游计算依赖。

eca-pp 原有脚本修改和未跟踪文件未改动；本次只新增此规划文件。小型复现使用 `/tmp`，未启动模型调用、实际数据分析或新的 Slurm 作业。

## 2. 已兼容的部分

- ECA-PP 仍写 `standardized.h5ad` 和 `result.json`，`schema_version` 仍为 2；矩阵使用 `layers['counts']` 保存 counts、`X` 保存 lognorm。OSP 默认优先读取 counts 层，正常输入不需要反归一化，也不需要恢复被上游移除的 `.raw`。
- OSP 的 `clustered.h5ad`、`report.html`、`annotation_proposal.json`、`qc_summary.csv`、`qc_removed.csv` 没有改名。`_ann_coarse`、`_ann_fine`、`_qc_action` 仍沿用现有语义：QC 真实过滤，注释的 drop/flag 是建议。
- persample 当前生成的多样本 CLI 参数仍被 OSP 接受；整文件路径调用的 Python API 仍存在。问题是两条路径缺少统一配置和完成记录，不是入口已经被删除。
- OSP 已自行实现单文件原子写入、H5AD 回读，以及注释最后发布 proposal；这些能力无需在 RSI 复制实现，但 RSI 要验证它调用的那次任务是否成功。
- ecarsi / osp 已经使用共享 bridge 的 identity-preserving shim。超时、后端重试和 MCP 生命周期修复应继续放在 bridge；样本分组、质量判据、文件和续跑状态仍由业务模块负责。
- 新的 Scrublet 分数统计和 `decontx_degenerate` 进入 `qc_summary.csv` 后，现有 `_sample_inventory()` 会把全部键传给样本纳入 agent。不能把这部分描述成“新指标完全丢失”；欠账主要在配置、持久化状态和集中复核展示。

对应实现：[ECA-PP build](../eca-pp/src/eca_pp/standardize/build.py)、[OSP CLI](../osp/osp/__main__.py)、[OSP 发布注释](../osp/osp/annotate.py)、[RSI inventory](ecarsi/crosssample.py)、[共享 shim](ecarsi/harness.py)。

## 3. 集成欠账

本文 P0 表示新版批量接入前必须解决的阻断或错误处理风险；P1 表示可靠运行和续跑所必需；P2 表示后续协调或展示完善。

### U1 · P0：上游正常重跑后的归档会阻断 organize

ECA-PP `archive_outputs()` 把旧结果移到 `standardize/.history/standardize-*/`。RSI `find_ecapp_units()` 对根目录执行 `rglob('*.h5ad')`，只承认当前标准路径；归档中的旧 H5AD 会进入 violations，导致整个 organize 返回 3。

已用上游实际归档函数复现：一个合法当前结果，加一次历史归档，得到 1 个有效来源和 1 个不合规文件。无需假设用户手工混入文件。

实施：只从当前步骤结果发现来源，明确排除上游约定的 `.history` 子树；当前目录内未声明的额外 H5AD 继续报错，不能把所有未知文件都静默忽略。

证据：[organize.py](ecarsi/organize.py)，`find_ecapp_units` 第 40–61 行；[run_outputs.py](../eca-pp/src/eca_pp/core/run_outputs.py)，第 12–42 行。

### U2 · P0：入口没有消费上游质量状态，也没有完整的来源清单

现在只要求两个文件同时存在。`profile_unit()` 读取 species 和维度，不检查 `status`、`exit_code`、`step`、schema、output、counts 层及其与实际矩阵的对应关系。

两个已复现的后果：有 H5AD 的 `status=error, exit_code=1` 仍能进入 profile；同一根目录中只有 rejected result、没有 H5AD 的来源会消失在发现结果里。后者即使最终允许排除，也应明确记录，不能被“已发现来源内细胞守恒”掩盖。

还复现了缺少 counts 层仍通过入口。OSP 随后会把 X 当作 counts；若该 X 实际为 lognorm，有限且非负检查不能识别这个语义错误。这是输入门槛缺失，不是说正常 ECA-PP 输出缺少 counts。

建议状态规则：

| 上游结果 | RSI 行为 |
| --- | --- |
| `ok`、exit 0、有可验证输出 | 接受 |
| `needs_review`、exit 0、有可验证输出 | 接受并保留 reasons，进入复核记录 |
| `rejected`、exit 2、无输出 | 明确列为上游拒绝；不作为分析输入、不重试；汇总排除数和原因 |
| `needs_review`、exit 3 | 上游尚待决，当前输入集合未准备完成 |
| `error`、exit 1；状态矛盾；文件不完整 | 当前输入集合未准备完成，不静默缩成成功子集 |

检查顺序是结果 schema / 终态 → H5AD 可读、维度和索引 → counts 与来源信息。旧结果走显式兼容分支；未知 schema 不猜。路径应支持目录搬迁，不能仅拿上游旧绝对路径做字符串相等比较。

另外，文档声称每分析单元只有一个物种，但 `plan._validate()` 没有校验；混合人鼠的 profile 通过了 host 校验。实施时加入合并前的同物种检查，避免只依赖 prompt。

证据：[organize.py](ecarsi/organize.py)，第 45–59、81–112 行；[plan.py](ecarsi/plan.py)，`_validate`；[result.py](../eca-pp/src/eca_pp/core/result.py)；[standardize CLI](../eca-pp/src/eca_pp/standardize/cli.py)，第 304–349 行；[OSP counts 读取](../osp/osp/qc.py)，第 430–438 行。

### U3 · P1：identify-columns 的新证据没有进入 organize / persample

organize 仅保存可选 `identify_columns_result` 路径；profile 没有读取其结论。persample 重新调用 agent，仅看合并后 obs 的简化画像。较晚的 crosssample 才读取原始路径里的 `columns.batch`，主要留作记录。

因此以下信息没有形成有效的前置交接：`classification`、候选排序及分组层级、`columns.cell_type`、`correction`、结构化 warnings、`columns.batch.kind='derived'` 对应的 `batch.tsv`。

新版 `batch=null` 也不是统一的“只有一个实验样本”：它可能代表无候选、未做 probe、数据太小或证据不足。`correction='unnecessary'` 同样不代表可合并物理样本。上游还允许带 warning 的 condition / other fallback，不能直接用来跑每样本 doublet / ambient 分析。

实施：在来源 barcode 尚未因合并改名时解析既有列或 TSV，按 cell ID 对齐，检查重复、缺失和覆盖；保存原始结论快照和规范化证据。向 persample 提供候选、分类、警告及来源映射，保留其独立判断实验样本粒度的责任。

兼容性也需要覆盖旧 `step_version=0.2.0`：当前磁盘上的 MCA1.1 AdrenalGland 结果已经有 derived `batch.tsv`，不是只在未来 0.5.0 才需要支持。此次抽查到的若干真实结果仍是 0.2.0，不能按当前源码版本给它们贴新版标签。

证据：[organize.py](ecarsi/organize.py)，第 55–57、91–112 行；[persample.py](ecarsi/persample.py)，第 440–466 行；[crosssample.py](ecarsi/crosssample.py)，`ecapp_batch_designations`；[identify-columns CLI](../eca-pp/src/eca_pp/identify_columns/cli.py)，第 578–693 行；[TSV 对齐契约](../eca-pp/src/eca_pp/core/colspec.py)。

### S1 · P0：来源内样本名没有转换为全局实验样本身份

`execute_plan()` 用 `source_unit` 和 barcode 后缀区分来源，却原样保留 `sample` 等列值；`list_samples()` 只按选中的一个列值分组。

已复现：来源 A 的 3 个细胞和来源 B 的 3 个细胞都写 `sample=S1`，合并后 cell ID 唯一、细胞数守恒，但得到一个 `S1: 6` 样本。若这两个来源对应不同实验，会错误地联合运行 OSP；OSP 的“只有一个标签”检查也识别不了这种混池。

反方向同样要处理：一个物理实验可能被拆成多个文件，不能无条件把 `source_unit + sample` 当作最终实验身份。来源限定的键可消除意外同名合并；跨来源合并必须有共同实验的正面证据和显式映射。

建议建立 `eca_sample_id` 及原始 cell ID / 来源映射，保留原始 metadata。分组决策依次使用显式配置、上游证据、必要的窄 agent 判断；“确认为单样本”与“分组未知”分别记录。不要把未知信息默认转换为整个合并文件一个样本。

补充两个规模限制：现有校验硬拒绝超过 200 个分组，profile 对超过 50 个值的列不再提供 value counts。升级时用分组大小摘要和层级关系替代固定上限；缺失值处理也应识别空字符串等占位值。

若 organize 的器官拆分切断了同一物理实验的细胞池，应记录并阻断错误的独立 doublet 计算，或另行设计共享的实验级 QC 前置阶段。不能声称“每 unit 中的一份子集”自动等于完整实验池。

证据：[execute.py](ecarsi/execute.py)，第 136–143 行；[persample.py](ecarsi/persample.py)，`profile_obs`、`_validate_sample_column`、`list_samples`、`write_subsets`；[OSP 单样本校验](../osp/osp/qc.py)，第 297–309 行。

### S2 · P0：完成检查不满足新版 OSP 的外层驱动要求

`_is_done()` 只看 report、H5AD、可选 proposal 是否存在，还接受 `.pruned` 标记。不要求 QC 汇总或删除台账，也不读文件内容和运行身份。

已复现：三个空文件被判 complete。crosssample 随后另行要求 `qc_summary.csv`，而 ledger 只在 `qc_removed.csv` 存在时读取；因此“persample 完成”和“下游输入完整”的判断并不一致。

还有一个独立错误：`drive()` 返回的失败列表被 `main()` 丢弃，最后只重查文件。模拟驱动报告 1 个失败但目录留有契约文件时，persample 最终 exit 0。不能用末尾存在性检查覆盖子进程失败状态。

实施：每样本持久化输入/配置身份、attempt、exit code、状态和验证结果；exit 0 后回读 H5AD 和 proposal，要求 QC 汇总与删除台账，按 cell ID 验证“输入 = 幸存 + 删除”、两者互斥及无未知细胞。注释需验证实际 `cluster_key`、proposal 覆盖与标签列一致。失败列表必须影响退出状态。

旧目录、成功后被 prune 的目录、需要重新计算的目录使用不同状态。已有 release 的清理标记不能作为新一轮计算成功的依据。OSP 无目录级锁，RSI 也需确保同一样本目录只有一个 writer。

证据：[layout.py](ecarsi/layout.py)，第 52–58、179–186 行；[persample.py](ecarsi/persample.py)，第 252–254、349–364、490–496 行；[ledger.py](ecarsi/ledger.py)，第 85–103 行；[OSP 完成规则](../osp/docs/input-output.md)，Reruns and completion。

### S3 · P1：严格输入检查后的失败处理与配置透传尚未适配

新版 OSP 在空输入、非法 counts、QC 后不足 3 个细胞、HVG 不足和 primary clustering 少于 2 簇时明确失败。RSI 对这些确定性失败仍原参数重跑完整流水线一次；注释失败也会重跑 QC、Scrublet 和 DecontX，没有单独恢复注释阶段。

另外，persample 无法传入 OSP 的 `--no-decontx`、`--no-scrublet`、`--resolution`、`--language`、`--effort`；整文件 Python 路径默认计算两个 resolution，多样本 CLI 路径默认只计算 1.0。选择 null 样本列会导致配置路径发生变化，还会覆写工作副本中的 `obs['sample']`。

实施：统一到一个子进程入口和显式配置对象，完整记录参数。输入/确定性失败直接记录原因，临时基础设施失败才重试；计算完成而注释失败时，在输入和配置匹配的条件下仅恢复注释。若需要稳定的机器可读失败分类，应与 OSP 约定接口，不能只靠错误文本猜测。

“全被 QC 删除”和“保留细胞但无法聚类”要区分。当前阶段仍按 unit 未完成处理；不要为继续进入冻结中的 MSP 临时制造假 clustered H5AD 或静默排除样本。零幸存样本如何进入后续流程，留待 MSP 完成后联合定约。

证据：[OSP 输入输出约束](../osp/docs/input-output.md)；[OSP cluster.py](../osp/osp/cluster.py)，第 336–440、766–827 行；[OSP CLI](../osp/osp/__main__.py)，第 21–44、65–89 行；[persample.py](ecarsi/persample.py)，第 171–203、354–364 行。

### R1 · P1：外层续跑缺少输入、配置、版本及完整组织结果的身份校验

`eca-rsi run` 在 `L.is_root()` 为真时跳过 organize；该判断接受仅存在 `units/` 目录的情况。已复现空 `units/` 就被视为 organized。多 unit 组织到一半中断时，续跑有只处理已写出部分的风险。

persample 只校验 harness/model，重用旧分组和计数，不校验 H5AD、OSP 版本、计算参数或已有 subset 的身份。恢复时覆盖 species / tissue / annotate 的参数也没有形成新的持久化配置记录。更换输入或新版 OSP 后，旧输出仍可能被跳过。

实施：organize 完整完成记录必须包含所有计划单元及验证结果；样本 manifest 明确 schema 和输入/配置身份。对大 H5AD 采用首次交接时的内容指纹及可复用清单，避免每个子进程重复哈希全文件。旧 manifest 可读、可展示，但不能凭空补造已验证身份；升级默认使用新输出根目录。

证据：[__main__.py](ecarsi/__main__.py)，第 61–69 行；[layout.py](ecarsi/layout.py)，第 63–85 行；[execute.py](ecarsi/execute.py)，第 151–185 行；[persample.py](ecarsi/persample.py)，第 281–300、425–466 行。

### I1 · P1/P2：配套版本声明和公共基础设施边界需要补齐

- **P1，RSI 负责**：`kernels` extra 仍允许 `osp-sc[agent]>=0.1.0`，不能保证具备新版输入检查和发布语义。声明已验证组合，记录解释器、模块实际路径、distribution 版本和源码提交；对于尚未发布的 OSP 修复使用明确提交基线。
- **P1，RSI 负责**：将 ecarsi / osp 的 bridge 身份测试与活跃开发的 msp 分开，前两条集成链应能独立验证。运行记录应包含 bridge 版本；样本池的失败/完成不能转移给 bridge 负责。
- **P2，上游协调项**：eca-pp 仍保留自己的 `harness.py` 和三份 `_harness_*`，尚未迁入共享 bridge。它有较短墙钟预算、typed errors 和确定性降级策略，不能机械替换导入后套用 RSI 默认预算。该迁移由 eca-pp / bridge 独立推进，不应成为本次文件对接的阻塞条件。
- **P2，展示与审计**：合并时 `ad.concat` 没有保留 `uns['eca_pp_standardize']`，已在小型样例确认；外层应有独立的逐来源快照，不依赖合并后的 uns 保留单一上游对象。上游 reasons/warnings 及 OSP 的退化标志应汇总到本项目报告；现有 `review.collect()` 只汇总后续 rounds。

证据：[pyproject.toml](pyproject.toml)、[现有跨仓库测试](tests/test_harness_sync.py)、[eca-pp harness](../eca-pp/src/eca_pp/harness.py)、[bridge 责任边界](../agent-harness-bridge/README.md)、[review.py](ecarsi/review.py)，第 218–225 行。

## 4. 建议实施顺序及验收

按以下四个可独立审查的改动包推进。前两包完成后，前半程的输入与分组才具备可信基础；第三包保证执行和续跑；第四包完成组合验收。

| 阶段 | 改动范围与交付 | 前置依赖 | 验收条件 |
| --- | --- | --- | --- |
| A：上游结果接入 | organize 的当前结果发现、状态清单、版本解析、counts/物种检查、结果快照；覆盖 U1/U2 和 U3 的读取部分 | 无 | `.history` 不阻断；不误读失败结果；rejected 来源可追踪；exit 0 review 可保留警告；旧 schema 2 结果可按兼容规则读取；混物种合并被拒绝 |
| B：实验样本映射 | 在来源 ID 未变更时对齐 batch.tsv；保留 cell-type 与分组证据；建立 `eca_sample_id` 和来源映射；统一单样本/多样本路径；覆盖 U3/S1 | A | 两来源同名样本不意外合池；同一实验分散多文件可凭明确证据合并；TSV 乱序可对齐、重复/缺失报错；null 不自动代表单样本；合法 200+ 分组可处理 |
| C：OSP 执行状态 | 统一参数透传；样本级运行记录和单 writer；验证 QC 台账/注释；失败分类和注释独立恢复；组织完成与输入身份检查；覆盖 S2/S3/R1 | A；与 B 的映射格式对齐 | 非零退出不能成功；坏文件或缺台账不能完成；输入、参数或源码变化不能静默复用；同配置恢复不重复已成功的 QC；部分 organize 可正确恢复全部计划单元 |
| D：组合验证和文档 | OSP/bridge 精确版本基线、独立接口测试、上游/OSP 复核信息展示、安装与续跑说明；覆盖 I1 的 RSI 部分 | A+B+C | 通过确定性边界测试；在新输出根做真实小数据验证至 persample 完成；保存输入数、分组数、过滤数、台账守恒和运行记录 |

建议 A、B、C 分别形成小 PR，D 提供验收记录。此处不对工期作未经验证的承诺；主要不确定性是跨文件实验身份，以及 OSP 是否提供稳定的失败状态接口。

测试分三层：

1. 小型构造数据：发现/状态/TSV/同名样本/缺失值/物种/恢复/损坏输出/非零退出，完全不调用模型。
2. 调用契约：实际 OSP CLI/Python 入口，mock agent 提交；验证完成文件、动态 `cluster_key`、QC 删除台账，以及 `--no-decontx`、resolution 等参数真正传入。
3. 真实前半程：一个已知多样本数据集，再加一个确有 derived TSV 的来源集合；验证至 persample。模型文字不逐字比较，检查细胞身份、counts、分组边界和输出约束。选定实验分组后才计算 doublet。

兼容迁移保留已有字段和主要 OSP 文件名。新的身份/状态字段应增加 schema 版本；保留旧结果浏览能力，不自动改写或重算历史 release。MSP/ZMIP 未稳定期间不把整条 `run → release` 作为本阶段的完成声明。

## 5. 冻结记录：待 MSP / ZMIP 完成后处理

此次只保留接缝清单，不修改内核或 `ecarsi/crosssample.py`、`ecarsi/zoomin.py` 的调用策略：

- 已有外层“文件存在即跳过”与新版内核输入/配置/锁/发布恢复检查之间的接缝。
- `persample.sample_column` 同时影响后续 batch 列，而实验样本分组与整合校正因子未必相同；`correction='unnecessary'` 和 biological fallback 如何进入正式整合，等 MSP 定约。
- OSP 动态 `cluster_key`、QC 建议、退化指标、零幸存或不可聚类样本，在正式纳入与删除台账中的语义。
- 现有 tests 中 MSP 导入失败，以及 ecarsi / msp 的 `resources.py` 不再逐字节相同。保留测试暴露的问题，不复制覆盖开发中的文件。

ZMIP 本次未深入复审；以上不是其缺陷的最终认定，也没有把它的开发状态当作本次前半程升级的阻塞。

## 6. 本次验证结果

| 检查 | 结果与解释 |
| --- | --- |
| OSP 当前 checkout 的现有 pytest | **34 passed，18 warnings**；其中包含 mock/小数据测试，不代表真实 agent 或全部生物学流程已验证 |
| ecarsi / osp 独立 bridge 身份检查 | **14 个公共导出全部通过对象身份检查**；不依赖 MSP 导入，也不代表三个后端已做真实模型验证 |
| RSI 文档列出的三个测试文件 | **6 passed，2 failed**；失败分别是导入 `msp.integrate.integrate_adata` 和 resources 副本相等检查；单独重跑 shim 测试仍因 MSP 导入失败，未修冻结中的模块 |
| 合成接口复现 | U1 归档冲突、U2 error/缺 counts 接受与 rejected 消失、S1 同名合池、S2 空文件完成与失败返回丢失、R1 不完整组织目录，均观察到预期的当前缺陷 |
| 实际数据结果只读抽查 | MCA1.1 AdrenalGland 有 derived TSV；Bladder 为 batch null；uniChondro 两份结果为 existing sample 列；抽查记录的 step_version 为 0.2.0 |
| 真实新版前半程 / 全流程 | 本次未执行，属于阶段 D 及后续 MSP/ZMIP 联合验收 |

测试在已有 allocation 内执行。初始环境的 `C.UTF-8` 不可用，pytest 启动在 readline locale 初始化处崩溃；把测试进程的 `LC_ALL` / `LANG` 设为 `C` 后得到上述结果，未修改持久环境配置。

复现脚本：`/tmp/eca-rsi-integration-audit-20260904.py`。本次结果：`/tmp/eca-rsi-integration-audit-wkskswl5/results.json`。临时目录可能被清理，关键条件和观察值已记录在本文；正式实施时应将这些边界行为转为仓库内回归测试。

实施已启动并落地 A → B → C；D 的结果另记 FRONT_VALIDATION.md。
MSP/ZMIP 联合升级仍等用户通知。
