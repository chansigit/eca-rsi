# 2026-09-05/06 夜间批量跑(mca1.1/mca2.0/mca3.0/tabula-muris)问题记录

跑 102 个器官(mca1.1 30、mca2.0 11、mca3.0 29、tabula-muris-drop 12、tabula-muris-facs 20)时,
自主监控循环发现并修复的真实 bug、还没修的已知问题、以及暴露出的架构/流程弱点。目的是给未来改进提供依据,
不是运行记录(运行记录见 `_eca-rsi-jobs/submitted.tsv` 和 Google Sheet)。

## 已修复的真实 bug

各自在独立 worktree 完成(不动 primary checkout,避免打断正在跑的 job 的 runtime identity),
批量跑期间通过 `gen_rsi.sh` 的 `HVG_ORGANS`/`BRIDGE_ORGANS`/`OSP_BRANCH` 白名单按需接线,尚未合并到各自的 main。

- **OSP scrublet 在 <100 细胞样本上崩溃**(`osp:tiny-sample-guard` 3d5b8ed):`n_prin_comps=30` 默认值超过细胞数直接
  `IndexError`;且哪怕不崩,scrublet 在这么少细胞上模拟的 doublet 分布也不可信(实测把 22 个细胞全部误判成 doublet)。
  改为细胞数 < `MIN_CELLS_FOR_SCRUBLET`(100)时跳过,不算 doublet。
- **OSP DecontX 在粗聚类分不出 2 类时崩溃**(`osp:tiny-sample-guard` 9a9b708):tabula-muris-facs 的极小 plate 样本
  常常同质,粗 Leiden 分不出 ≥2 群,DecontX 无法分离 ambient profile。改为捕获该异常,跳过 DecontX(报错信息本身就是这么建议的)。
- **OSP 聚类要求 ≥2 类才能算 DEG**(`osp:tiny-sample-guard` 3d5b8ed):单一同质小样本聚成 1 类是合法结果,不该强行报错。
  改为接受单 cluster,跳过 DEG/PAGA(反正没有第二类可比较)。
- **MSP per-batch HVG 在极小样本批次上崩溃**(`msp:hvg-small-batches` a588af6):ZMIP 下钻出的 lineage 子集里,
  某些贡献样本只有个位数到几十个细胞,`scanpy.pp.highly_variable_genes(..., batch_key=...)` 的 per-batch dispersion
  排序在这种批次上崩(`IndexError: index -1 is out of bounds for axis 0 with size 0`)。加 `MIN_HVG_BATCH_CELLS`(20)
  阈值,排除极小批次参与投票,不足两个合格批次时退化为全局 HVG。**影响范围比最初以为的大**:一开始只在
  mca3.0/Brain·Liver·Pancreas·Lung·Prostate 5 个器官上确认,后来陆续在 Bladder/Intestine/Kidney/Stomach/Testis/
  BoneMarrow 上复现,`HVG_ORGANS` 白名单最终扩到 11 个。
- **organize 把自己上一轮的输出当成"未声明的 h5ad"拒绝**(`eca-rsi:organize-skip-run-root` 52b50d2):`<organ>/rsi/`
  按约定就长在 ECA-PP 输入目录里,`organize` 复跑时 `os.walk` 会扫到自己的输出并报 `undeclared H5AD`,导致
  organize 阶段完全无法在原地续跑。加 `is_run_root()` 探测并剪掉这类子目录。
- **`sample_mapping.py` 缺少显式实验拆分支持**(`eca-rsi:organize-skip-run-root` 0ffaa0c):mca1.1 的库身份只在
  cell ID 前缀里(如 `AdultBrain_1.xxxxx`),没有对应 obs 列;tabula-muris-facs 的 `plate.barcode` 有 1–63% 缺失。
  加 `derive_from_cell_id`(正则从 cell ID 提取)和 `missing_as`(缺失值当伪 plate,opt-in)两个显式 `--sample-map` 选项。
- **bridge 没有把 HTTP 层超时/连接错误当瞬时错误重试**(`agent-harness-bridge` bb0e359,已合并进 main):
  `openai.APITimeoutError` 等直接杀死整个样本/轮次,而不是像 MCP 层错误那样重试。已合并主线。
- **bridge 没识别 Ark 的 item 数上限错误**(`agent-harness-bridge:item-limit-context-reset` b6eed01,未合并):
  Ark 的 Responses API 有"最多 1000 个 item"的硬限制(和已有的 token 数上限是不同的报错文案:
  `Maximum of 1000 items allowed in input.`),长会话(很多 cluster/lineage)容易撞到,之前直接判死,
  其实和已有的"context 太大"恢复路径是一回事,加一条模式匹配就行。**只接线给 2 个轮数浅的 organ**
  (tabula-muris-facs/Trachea、Brain_Myeloid),因为这个 fix 一接线就会让该 organ**所有已完成轮次**失效
  (见下方"架构发现")——轮数深的 mca2.0/Testis(3轮)、mca3.0/Liver(5轮)特意没接,先靠纯重投扛。
  **没有证据能确认它对深轮次 organ 是否真的会复发**——纯重投目前看起来多数情况下也能过(新进程=新会话,
  不一定会再撞到同一个 item 数)。
- **serve 前端一次 JS 语法错误(`const sb` 重复声明)拖垮整个导航脚本**(`eca-rsi:serve-ui-upgrade` f9bfdb8):
  没有任何控制台可见的报错线索,表现是"点数据集侧栏就消失、计数显示 0、排序拖拽都没反应"——因为整个 IIFE
  在解析阶段就失败,所有事件监听器都没绑上。补了一个 `node --check` 的语法回归测试。
- **serve 侧栏拖拽在鼠标进入 iframe 后失灵**(`eca-rsi:serve-ui-upgrade` a278c39):iframe 是独立文档,
  拖拽过程中鼠标一划进 iframe,父页面的 `window` 级 `mousemove`/`mouseup` 就完全收不到事件了。改为按下拖拽柄时
  临时把 iframe 设 `pointer-events:none`,松开再恢复。
- **serve 侧栏硬编码"每个数据集都是 Slurm 作业"的假设**(`eca-rsi:serve-ui-upgrade` 909aeb5):不是 bug,是设计
  上不该有的耦合,用户直接指出后整块删除(`_root_job_id`/`_slurm_states`/`_job_cell` 及其 squeue/sacct 轮询)。

## 还没修的已知问题

- **tabula-muris-facs 的"missing"伪 plate(缺 `plate.barcode`)在多个器官上只剩 1–3 个细胞活过 QC**,
  OSP 判定"细胞太少无法聚类"直接报错,而这个失败会挡住**整个 persample 步骤**——已知受影响:Bladder、Liver、
  Lung、Tongue。已确认的两个真实 bug(scrublet/DecontX 崩溃)修完之后,这类是纯粹的"数据本身就不够"。
  真要修需要给 `ecarsi/persample.py` + `ecarsi/ledger.py` 加一条"样本合法排除"路径——osp_worker.py 里其实已经有
  `qc_zero_survivors`/`qc_too_few_survivors` 的 `failure_kind` 分类,但下游从没读过、也没有让某个样本失败时
  只排除它、不拖垮整个 persample 的机制。这块直接牵涉 CLAUDE.md 明确要求"细胞数目必须严丝合缝"的账本正确性,
  风险较高,没有在没人拍板的情况下动。**建议**:比照 CLAUDE.md 已有的 `needs_review` 的 `sample_excluded` 类别,
  把这类失败做成"记录 + 排除该样本 + 继续跑其余样本"而不是"整个 organ 卡死",需要设计评审。
- **`openai.BadRequestError: ... items allowed in input` 对深轮次 organ 是否会复发未知**——`bridge-item-limit-
  context-reset` 分支的修复没有对 mca2.0/Testis、mca3.0/Liver 生效(代价太高没接线),只能等它们再次撞见同一个
  报错、且纯重投扛不过去时,才有真正的证据决定要不要付代价切分支。
- **persample 在没有人主动改分支的情况下"自己"变了身份,复现了两次**(mca2.0/Testis、之后 mca1.1/Kidney,
  两次都是"没在 BRIDGE_ORGANS/ECARSI_BRANCH/OSP_BRANCH 名单里的 organ,只是被加进了 HVG_ORGANS(只影响 msp),
  结果连 persample 阶段——理论上根本不调用 msp——都报 `input, configuration or runtime changed`")。
  Kidney 这次专门验证过一个假设:直接用 `ecarsi.run_state.runtime_identity()` 现算了一遍 ecarsi 包的
  `source_sha256`,和 persample manifest 里记录的旧值**完全一致**,说明至少不是"改了 ecarsi 源码"这么简单的原因,
  `BATCH_RUN_FINDINGS.md` 首次记录时的猜测(某个 worktree 改动的连带效应)大概率不对,或者不是唯一原因。
  真正原因仍未查明(没有继续深挖 osp/harness_bridge 的哈希、`config`、`explicit_mapping` 等其他分量是否变了)。
  两次都是当一次性事件处理掉(trash persample 重来),**如果第三次复现,值得专门花时间用上面这种"逐字段对比
  旧 manifest vs 现算 identity"的方法把真正变化的字段找出来**,而不是继续假设"当一次性事件"。
- **PyPI 上传 `ecarsi` 0.1.0 仍被新项目频率限制卡住**(非本轮新增,历史遗留,见 `eca-rsi-open-backlog` memory)。

## 架构/流程发现(不是这次要修的 bug,但值得未来设计时考虑)

- **`ecarsi.downstream.check_round()` 在每次 resume 时都会重新校验所有已完成轮次的 identity,不是只查当前轮**
  (`loop.py` 的 `for n in range(1, last_round+1)` 循环里,每个已决策的轮次都过一遍 `check_round`)。这意味着
  给一个 organ 切换任何 worktree 分支(哪怕只是为了修当前卡住的那一轮),都会让它**所有历史轮次**的 identity
  失效,必须从 round01 整个重来——本轮跑下来,这个代价在轮数浅的 organ 上可以接受,轮数深(3–5 轮)的 organ
  上代价很高(mca3.0/Liver 5 轮全部作废过一次)。**可能的改进方向**:允许显式标记"这一轮的身份不匹配是已知
  且可接受的",或者把 identity 校验做成"只校验将要复用的那部分产物",而不是全量历史。
- **`runtime_identity()`/`kernel_runtime()` 把包的**绝对路径**也算进哈希**(`str(folder.resolve())`),不只是
  文件内容。这意味着即使把一个 worktree checkout 到完全相同的 commit,只要路径不同,identity 也不同,没法用
  "pin 到旧 commit 的另一个 worktree"来蒙混过关恢复一个已经切换过分支的 organ(试过,不行)。**可能的改进方向**:
  identity 只哈希内容,不哈希路径;或者提供一个显式的"我知道路径变了,允许恢复"逃生舱。
- **`agent-harness-bridge` 的 blast radius 比 osp/msp 大得多**:它的源码哈希不管走不走 worktree 分支,都会被
  每一个 organ 的 persample **和** 每一轮的 identity 检查用到(因为它是 `runtime_identity()`/`kernel_runtime()`
  的固定模块列表成员)。给它切分支,哪怕只想影响 1 个 organ,那个 organ 的 persample 也会一起失效,不只是轮次
  ——这个代价比切 OSP_BRANCH/MSP_BRANCH 更隐蔽,一开始低估了。
- **监控脚本不能用 `sacct -S <时间>` 找"新失败"**——`-S` 是按作业**开始**时间过滤,不是结束时间,一个长跑了
  几小时后失败的作业会因为开始时间早于查询窗口而被漏掉。本轮吃过一次亏(一次性漏掉 22 个已失败但未察觉的
  organ)。正确做法是扫描每个未 release、不在队列里的 organ 的 `rsi/status.txt` 最后一行。
- **`tracker_rsi.py` 不带 batch 参数时静默"updated 0 rows"，不报错也不提示**——应该要么默认全部 5 个 batch,
  要么在没传参数时直接报错退出,而不是看起来正常执行、实际什么也没做。
- 目前"HVG_ORGANS/BRIDGE_ORGANS 白名单 + `gen_rsi.sh` 里手工枚举受影响 organ"的做法能跑,但本质是手工维护的
  临时状态,只要哪个 worktree 分支合并进对应仓库的 main、且当时**没有任何 organ 在跑**,这套白名单机制就可以
  整个删掉——建议全部 102 个器官跑完、确认没有遗留任务后,统一合并 5 个 worktree 分支并清理 `gen_rsi.sh`。
