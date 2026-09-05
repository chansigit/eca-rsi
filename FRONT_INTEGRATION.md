# ECA-PP → organize → persample 接入与续跑

本次升级止于 OSP 完成。MSP / ZMIP 及 crosssample / zoomin 适配仍待联合升级；
新组合验证请使用分步命令，或 `run --stop-after persample`。

## 输入与组织

```bash
eca-rsi organize /path/to/eca-pp-output /path/to/new-run
# 已有明确的分析单元划分时，可跳过规划模型：
eca-rsi organize /path/to/eca-pp-output /path/to/new-run --plan-json plan.json
```

`plan.json` 沿用 `ecarsi.plan.PLAN_SCHEMA`。每个单元必须只有一种已解析物种，
所有接受来源的细胞必须恰好分配一次。主流程也会在真正写出结果前校验模型提交。

- 读取 schema 2，包括磁盘上已有的 0.2.x 和当前 0.5.x 结果；未知 schema 拒绝。
- `ok/0`、`needs_review/0` 接受，后者的原因随来源保存。
  ECA-PP `0576683` 起直接信任扩展后的 `.raw`，不再生成 HVG counts 交叉比对及其复核原因。
  RSI 不依赖这些可选字段，也不补做该比对；旧结果已有的 `counts_check` 和复核原因仍原样保留。
- `rejected/2` 且无输出，列入 `organize/source_inventory.json`，不纳入计算。
- error、blocked、状态矛盾、缺文件、缺 counts 层或非法矩阵，会阻止整个输入集合继续。
- 只跳过 ECA-PP 步骤目录内约定的 `.history`；其他未声明 H5AD 仍报错。
- `input/upstream/<source>/` 保存完整结果 JSON、派生 TSV 和完整来源 obs。
  TSV 在合并改名之前按原始细胞 ID 对齐；重复、缺失、额外 ID 均拒绝。
- `organized.h5ad` 保留原始 metadata，并新增 `source_unit`、`eca_source_cell_id`、
  可选 `eca_pp_batch`、`eca_pp_cell_type`。这些列名保留给 RSI；输入发生重名会报错。

`organize/manifest.json` 先记计划和 running 状态，逐单元登记输出指纹，全部完成后才记 complete。
同输入、同适配代码的中断可继续完成剩余单元；旧目录或已修改的输入、计划、代码要求新目录。
内容 SHA-256 每次驱动入口计算一次，不在每个样本子进程重复哈希整个来源大文件。
每单元保留完整来源 obs 以检查实验池是否被器官拆分，因此多单元时会增加 metadata 存储。

## 实验样本映射

```bash
# 明确知道 sample 是物理实验列；同名值只在同一来源内分组。
eca-rsi persample /path/to/new-run/units/UNIT --sample-column sample

# 确认单个来源就是一套完整实验时：
eca-rsi persample /path/to/new-run/units/UNIT --single-sample

# 只保存/检查映射，不运行 OSP：
eca-rsi persample /path/to/new-run/units/UNIT --sample-map samples.json --plan-only
```

省略映射参数时，逐来源向窄决策模型提供 obs 画像及上游分类、候选、嵌套、校正和 warning 证据。
批次列不直接当作实验列，`batch=null` 或 `correction=unnecessary` 不推导为单实验。
模型选择 null 时必须有 `confirmed_single=true` 及依据；分组未知会停止，需提供明确映射。
不设 200 个实验的硬上限。空字符串和常见缺失占位不能成为伪样本。

显式配置优先；`sources` 必须完整覆盖当前单元来源。跨来源合池必须单独声明：

```json
{
  "sources": {
    "source-A": {"sample_column": "library", "rationale": "原始文库编号"},
    "source-B": {"sample_column": "eca_pp_batch", "rationale": "已核对 TSV 值为原始文库编号"}
  },
  "merges": [
    {
      "sample_id": "library-7",
      "evidence": "A 的 L7 和 B 的 run7 是同一 GEM well 分成的两个细胞文件",
      "members": [
        {"source": "source-A", "value": "L7"},
        {"source": "source-B", "value": "run7"}
      ]
    }
  ]
}
```

合并后的实验 ID 只能使用字母、数字、点、下划线和连字符。一个来源分组只能参与一个合并；
同一实验内重复的原始 cell ID 拒绝，需先解决重叠来源。没有显式 merges 时，不同来源的 `S1` 始终分开。
完整映射写入 `persample/sample_mapping.csv.gz`，包括当前 cell ID、原始 ID、来源、原始分组值、`eca_sample_id`。
OSP 子集才新增 `eca_sample_id`，不覆写原始 `sample` 列。

若完整来源 obs 表明该实验还有细胞在另一个组织单元，拒绝在局部池独立运行 QC。
当前不实现跨器官共享的实验级 QC；完整实验池含义仍依赖输入数据与明确实验信息。

## OSP 配置与状态

```bash
eca-rsi persample /path/to/new-run/units/UNIT --sample-column sample \
  --resolution 0.8 --language Chinese --effort high

# 调试时可显式关闭；默认 QC 两项均开启，注释也开启。
eca-rsi persample /path/to/another-run/units/UNIT --sample-column sample \
  --no-scrublet --no-decontx --no-annotate
```

单实验与多实验统一通过 `ecarsi.osp_worker` 子进程调用 OSP 公共 Python API，
显式传入 Scrublet、DecontX、resolution、species、tissue、language、effort、model。
resolution 默认 1.0；harness 继承环境。`OSP_PYTHON` 可选择内核解释器。
RSI 不复制 OSP 的 QC、聚类或注释实现，也不修改共享 bridge 的预算和重试策略。

每样本 `request.json` 和 `run_state.json` 记录输入/配置身份、解释器、包版本、源码提交与源码内容指纹、
attempt、阶段、退出码、失败类别、校验结果及输出指纹。一个驱动和一个样本目录各有进程锁。
正常完成需要成功状态、零退出，并校验：

- 可读的 `clustered.h5ad`、有效 HTML、QC 汇总、`qc_removed.csv`；注释开启时还要 proposal。
- 输入细胞 = 幸存细胞与删除细胞的不重叠并集，无重复、无外来 cell ID，汇总数量相同。
- proposal 实际 `cluster_key` 存在，簇覆盖、coarse/fine 标签和 QC action 与 H5AD 一致。

确定性错误不自动重算；明确的临时连接/超时错误最多再试一次。未分类错误保持失败，不能从文字猜测可重试。
QC 零幸存与不足三细胞分别记录；都使单元未完成，不制造占位 H5AD，也不静默排除以强行进入 MSP。
注释失败保留经校验的计算快照，同身份恢复只执行注释。成功后清理 subset 和恢复用计算快照。

相同配置续跑验证内容后跳过成功样本；输入、映射、计算参数、模型、解释器或源码变更要求新输出目录。
`--allow-agent-change` 不覆盖此新版前半程身份要求。旧输出和 `.pruned` 仍可浏览，不能作为新计算的成功依据。
整体目录可搬迁；结果文件中的旧绝对路径只作来源记录，读取使用当前目录内相对定位。

上游 review/warnings 和 OSP 退化、失败信息写入 `persample/needs_review.{json,md}`，
单元页面和后续既有 review 汇总会显示。页面读取已记录的成功状态；实际续跑另做内容校验。

## 独立验证

```bash
LC_ALL=C LANG=C PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider \
  tests/test_front_integration.py tests/test_osp_worker.py tests/test_agent_selection.py
```

这些测试独立于 MSP/ZMIP。原有跨内核 harness/resources 检查保留，仍单独暴露冻结中的不一致。
已测配套源码见 [FRONT_COMPATIBILITY.json](FRONT_COMPATIBILITY.json)，实际运行结果见
[FRONT_VALIDATION.md](FRONT_VALIDATION.md)。ECA-PP 仍自带 harness 的迁移由上游独立协调。

## bridge 0.2.0 适配

`ecarsi.harness` 保留拆分前 14 个公共接口及旧常量的对象身份，作为旧导入路径的兼容层。
新代码直接从 `harness_bridge` 导入；shim 不承诺转出上游未来新增的每个接口。
测试明确固定旧接口集合，同时允许兼容层增加导出；删除旧接口、遗漏声明或对象不一致仍应失败。

RSI CLI 在入口调用 `configure_logging("ecarsi", stream=sys.stderr)`，OSP worker 入口配置
`ecarsi`、`osp` 和共享 bridge 的日志。重复初始化替换 bridge 自己的 handler；
库函数不主动重配日志，bridge `run_agent` 的 `ensure_logging` 尊重调用方已有 handler。
bridge 保留向 root logger 传播，应用自己额外安装的 root handler 仍会收到记录。
这里只配置 logging，旧代码原有 print 不会自动改道；worker 状态仍写 JSON 文件。

RSI/OSP 源码依赖 bridge `>=0.2.0,<0.3`。bridge 0.2.0 已发布 PyPI，OSP 依赖修改
仍在本地源码，见 INSTALL.md。输入/内核/bridge 身份检查保留，升级后使用新运行目录。
