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

- **HVG IndexError 名单继续增长(2026-09-06 17:2x)**:mca2.0/Testis round 2 的 Sertoli lineage 撞上同一个 scanpy 逐批次 HVG `IndexError: index -1 ... size 0`(确定性 bug),按规矩直接加进 `HVG_ORGANS`(现在 15 个:mca3.0 ×13、mca1.1/Kidney、mca2.0/Testis),rounds/ 挪走重来(job 42291621)。mca3.0/Heart round 1 zoomin 的 zmip plan agent 因 Doubao `response.incomplete reason=length` 耗尽 2 次输出长度恢复预算而失败,属模型侧非确定性问题,纯重投(42291600)。
- **"样本 QC 后 0 存活"从 bug 变成正式语义(2026-09-06 18:2x,worktree `eca-rsi-empty-samples`,分支 `empty-samples`,
  基于 organize-skip-run-root,3938fdb)**:facs v2 的 Fat 板 MAA000873(55 个孔,TM 作者一个都没标注,OSP QC 55/55 淘汰)
  让 persample 整体失败,和 v1 Tongue 的 missing 桶同一类。之前判断"要动契约、三层 ledger 语义"是对的,但**恰好 0 存活**
  这一子情况其实很干净:每个细胞都已经带着原因躺在该样本的 `qc_removed.csv` 里,守恒天然成立,不需要新的 ledger 状态。
  实现:`osp_contract.is_empty()`(run_state `failure_kind=qc_zero_survivors` + identity 匹配 + qc_summary n_cells==
  n_low_quality + qc_removed 覆盖 input_cells 且每条有原因)和 `is_finished = is_done or is_empty`;persample 的 drive/
  pending/missing 用 is_finished,manifest 记 `empty_samples`;crosssample 在纳入 agent 之前把空样本剔掉(不进
  sample_decisions.csv,否则 ledger 的"决策覆盖进入样本"校验会炸),round manifest 记 `empty_samples`;ledger 对空样本只
  取 removed 行;review 以 `sample_excluded`/step=persample 列出;index 显示 "empty" pill。OSP 契约的 ≥3 细胞门槛不动,
  1–2 个存活(`qc_too_few_survivors`)仍然是失败——那才是真需要设计评审的情况。测试 `tests/test_empty_samples.py`,
  前半程+下游套件 115 passed(`test_harness_sync` 的 2 个失败是 worktree 没有 sibling checkout 的环境问题,基线同样失败)。
  接入:`gen_rsi.sh` 新增 `ECARSI_BRANCH_EMPTY` + `EMPTY_ORGANS`(默认 tabula-muris-facs/Fat),Fat 的 rsi-v2/WORK 挪走
  重投(42295223)。其它 facs v2 organ 仍在 organize-skip-run-root 上(已封存 persample,不能切)。
  **追加(19:1x)**:42295223 的 persample 通过了(28 个实验 0 失败,MAA000873 记为 empty),但 loop 入口
  `crosssample.load_persample` 还在用 `is_done` 做"每个样本都完成注释"的硬前提,空样本被判 incomplete → exit 1。
  第一版只改了 persample/纳入/ledger/review/index,漏了这一道门,而 `test_empty_samples` 没覆盖 load_persample。
  修复 4726739:`is_done`→`is_finished` + 回归测试(空样本通过前提;identity 不匹配仍判 incomplete)。改了 worktree 源码
  = Fat 的 organize/persample 身份全变,rsi-v2/WORK 再挪一次(`*.stale-loopgate-20260906-1913`)重投 42298364,
  代价是 28 块板的 persample 重跑(~45 min)。教训:新语义要沿着数据流把每个"complete"判定点都过一遍。
- **mca2.0/Testis 的 persample 也是 4dff5cc commit-mismatch**(逐字段比对:ecarsi commit 75c820f→4dff5cc,其余全同),
  挪走 persample 重投(42294800)——它是最早那次"神秘"事故的 organ,现在看那次大概率也是同一机制。

## 还没修的已知问题
- **Ark `BadRequestError: Error when parsing request`(图片后)——已查清,不是 payload 限制(2026-09-06 20:4x,worktree `bridge-image-request`,分支 `image-request`,9d6a3e0)**:
  3 次失败(Trachea MSP annotate 5 张、Liver ZMIP round 4 8 张、Testis MSP inspect 9 张 + CSV)全在 07:37–08:32 一小时内,同一小时还有 6 次 Ark 500;
  错误体是非 JSON 的裸 400(Ark 真正的校验错误都是带 code/request id 的 JSON);把三次的原始 payload 原样重放(含 previous_response_id 链)全部成功,
  9.5 MB/18 图、19 MB/36 图也成功;失败体 0.66/0.99/4.75 MB,远低于文档限制(单图 base64 ≤10 MB、body ≤64 MB)。全批 27,939 次 PNG Read、
  ~2,800 次 ≥5 张连读,只失败 3 次(0.1%)= 网关级瞬时拒绝。修复:`TRANSIENT_PATTERN` 加 `error when parsing request|internalservererror|
  internalserviceerror`,走既有的整次重试(5 次、20–80 s 退避);测试 88 通过。没做缩图/限图(无证据,且会改变模型看到的东西)。
  分析与重放脚本:`$SCRATCH/eca-rsi-runs/_arkimage/`。

- **tabula-muris-facs 的"missing"伪 plate——根因已查清,已用配置修复(facs v2),不需要改内核**。
  表象:`__missing__` 伪样本在 Lung 只剩 2 个细胞活过 QC,撞上 OSP 契约的 `clustered.h5ad ≥3 细胞` 硬门槛,
  整个 persample 失败(Tongue 同类,另有 DecontX 单 cluster 崩溃已在 OSP_BRANCH 修掉)。
  根因(2026-09-06 逐 organ 核对):这些细胞不是"不知道来自哪块板",而是 **Tabula Muris 作者自己 QC 淘汰、
  没进 annotation 表的孔**——原始 `<organ>.h5ad`(count 矩阵含全部孔,left-join 只含过 QC 细胞的注释表)里它们的
  `plate.barcode`/`tissue`/`mouse.id` 是空串,eca-pp standardize 把空串改写成字面量 `"missing"`;数据完全符合
  TM 原文阈值(正常细胞里 <500 基因的是 0 个;missing 细胞中位数 234 基因 / 371 counts vs 正常 2388 / 55 万)。
  **板号其实就在 cell ID 里**(`M15.MAA000526.3_9_M.1.1` = 孔.板.小鼠.x.x):正则 `^[A-P]\d{1,2}\.([A-Za-z0-9]+)\.`
  在全部 20 个 facs organ 上 100% 匹配、与已标注的 `plate.barcode` 100% 一致,7783 个 missing 细胞全部落回
  已存在的真实板(仅 Fat 有一整块 55 细胞的板完全没被标注)。9 月 6 日的 `missing_as: "missing"` 那个 opt-in
  是选错了——`_validate_sample_column` 本来就拒绝含 NA 的样本列("silent-garbage trap"),`missing_as` 把各板
  废孔攒成一个假样本,再靠纳入 agent 认出它是垃圾(15 个 organ 整体排除;Brain_Non-Myeloid 没排除,37 个无
  元数据细胞进了 release;Lung 直接崩)。
  修复:每个 facs organ 新增 `rsi-sample-map-v2.json`(`derive_from_cell_id` 取板号 + rationale),`gen_rsi.sh`/
  `submit.sh` 加 `RSI_SUFFIX`(输出到 `<organ>/rsi-v2`、`eca-rsi-v2.sbatch`、job `ecarsi-<batch>-v2-<organ>`、
  WORK `<batch>-v2/<organ>`)、`SAMPLE_MAP`、`PERSAMPLE_FLAGS`(facs v2 用 `--no-decontx`:DecontX 假设液滴
  ambient RNA,Smart-seq2 板上没有这回事,agent 曾拿"ambient 39.7%"当排除理由)。v1 结果原样保留供对比。
  Lung 干跑验证:8 块真实板、1923 细胞全部落账、无 missing 样本、`decontx=False` 进 config。
  **第一次 v2 提交(42285649–42285698)26 分钟后被 Bladder 打回**:板 B002771(121 细胞,中位 74 万 counts /
  4621 基因,质量很好)被 scrublet 以 0.0068 的自动阈值标了 113 个"双细胞",QC 后只剩 3 个,再撞上 umap-learn
  spectral 初始化在 N=3 时的 `k >= N` TypeError。回查 v1 的 220 块 facs 真实板:doublet 比例中位数 1.5%(合理),
  但 10 块板 >20%、2 块板 >50%(Brain_Non-Myeloid MAA000932 70.7%、MAA000923 55.6%),共 1920/40086(4.8%)
  细胞被当双细胞删掉——scrublet 在 100–300 细胞的板上阈值检测经常失效,而 FACS 单细胞分选的真实双细胞率只有
  1–3%。**两处处理**:(1) facs v2 的 persample 改为 `--no-decontx --no-scrublet`(OSP/MSP 对缺 `doublet_score`
  列的情况全部有 `if col in obs` 守卫,已逐处核对;MSP 层仍有 cluster 级 doublet 检查);(2) 新开 osp worktree
  `osp-tiny-sample-guard-2`(分支 `tiny-sample-guard-2`,commit e1a23e6):`cluster_and_deg` 在 n_obs≤3 时用
  `init_pos="random"` 做 UMAP,加了 3 细胞回归测试(37 tests pass)。不能改旧 `osp-tiny-sample-guard`,因为
  v1 的 Aorta/Brain_Myeloid 还在引用它;`gen_rsi.sh` 新增 `OSP_BRANCH_V2`,`RSI_SUFFIX` 非空时 facs 走它。
  第一批 v2 全部 scancel,`rsi-v2/`(只有 Bladder/Fat 落到了 Oak)和 scratch WORK 用 mv 挪走,
  2026-09-06 16:5x 重新提交 20 个(42289027–42289183,8 lane)。
  **没做、留待评审的两条**:(1) `MSP_BATCH_COL=mouse.id`——板严格 ⊂ 小鼠×分选门(20 organ 无例外),按小鼠
  校正更合理,但孤儿孔的 `mouse.id` 仍是字面量 `"missing"`,`crosssample.py` 的"每个实验内单值"校验会把它当成
  第二个值而拒绝;要么上游把孤儿孔的元数据从 cell ID 回填,要么 ecarsi 加"从 cell ID 派生 obs 列"的功能。
  v2 暂时仍按板做 Harmony(Heart 30 板实测各腔室标签混合良好,无过校正迹象;Lung/Brain_Non-Myeloid/Fat/Marrow/
  Thymus 是 marker 门分选、板≡细胞类型,跑完要专门看有没有过校正)。(2) 把 `subtissue`(分选门)作为设计
  上下文喂给 inspect/annotate——`layout.report_context` 只是报告标题,没有现成通道,要改 msp 的 prompt 接口;
  现在 agent 把"某 cluster 全来自一个样本"当 batch 嫌疑,其实是分选门使然。
  通用教训:**样本列来自 left-join 一张已 QC 过的注释表**是任何数据集都可能踩的坑(drop 用 `channel`、零缺失,
  没这个问题);规则是永远别用 `missing_as` 把 NA 细胞攒成一桶——ID 里有 library 就 `derive_from_cell_id`,
  没有就带记录地拒绝。OSP 的"QC 后 <3 细胞"硬门槛本身不需要改。
- **`openai.BadRequestError: ... items allowed in input` 对深轮次 organ 是否会复发未知**——`bridge-item-limit-
  context-reset` 分支的修复没有对 mca2.0/Testis、mca3.0/Liver 生效(代价太高没接线),只能等它们再次撞见同一个
  报错、且纯重投扛不过去时,才有真正的证据决定要不要付代价切分支。
- **根因已查清(第三次复现,mca3.0/Heart,才真正定位):`runtime_identity()` 给每个包记录的 `commit`
  字段(`git rev-parse HEAD`)是**整个仓库**的当前 HEAD,不是只反映实际被哈希的 `ecarsi/` 包子目录内容。
  当晚我往 primary eca-rsi checkout(main 分支)提交了这份 `BATCH_RUN_FINDINGS.md` 本身(537361c、4dff5cc)——
  文件在 `ecarsi/` 包目录之外,`source_sha256` 因此完全不变(已验证过),但**只要提交一次,整个仓库的
  `git rev-parse HEAD` 就变了**,而这个 `commit` 字符串和 `source_sha256` 一起被塞进同一个 `runtime` 字典里,
  `persample.py`/`downstream.py` 拿它做 `==` 全量比较——`source_sha256` 没变但 `commit` 变了,一样判定为
  "runtime changed",所有**没有走 worktree 分支、直接用 editable-installed 主 checkout 的 organ**在提交后的
  下一次 resume 全部会中招。逐字段比对旧 manifest 和现算 identity 才真正抓到:`config`/`input_identity`/
  `metadata_identity`/`osp` 和 `harness_bridge` 的哈希全部一致,唯独 `ecarsi.commit` 不同。
  之前第一次(mca2.0/Testis)的原因大概率也是这个——只是那次发生在我第一次提交 `BATCH_RUN_FINDINGS.md` 之前,
  说明**至少还有另一次未知的"改了什么导致 HEAD 变化"的事件**(不一定是我做的;例如别的会话/进程往同一个
  primary checkout 提交过),没有继续深挖,留待下次复现时用这里记录的"逐字段对比"方法验证。
  **教训**:即使一份改动完全在包目录之外、`source_sha256` 保证不变,只要提交进 primary checkout,就会通过
  `commit` 字段传染给所有跑在这个 checkout 上(没走 worktree 分支)的 organ——这是比之前认识到的"改 worktree
  只影响接了那个分支的 organ"更大的 blast radius,等同于批量跑期间**不能往任何一个正在被引用的 primary
  checkout 提交任何东西**,哪怕只是纯文档。因此**本节内容故意不提交**(截至写这段话,一直保持 working-tree
  的未提交状态),等整个批量跑彻底跑完、没有任何 organ 还在引用这份 checkout 时再统一提交。
- **PyPI 上传 `ecarsi` 0.1.0 仍被新项目频率限制卡住**(非本轮新增,历史遗留,见 `eca-rsi-open-backlog` memory)。

## 架构/流程发现(不是这次要修的 bug,但值得未来设计时考虑)
- **mca3.0/Kidney 不收敛(2026-09-06 深夜观察)**:round 2–7 每轮删除 1.6 / 3.4 / 5.2 / 2.3 / 3.3 / 3.0%,在 3% 附近震荡,
  会一路跑到 `--cap 10` 强制 release。删的几乎全是 ZMIP 各 lineage 里 20–260 细胞的小簇,理由 doublet / ambient(round 5 zoomin 793、
  round 6 1095、round 7 1004 个;crosssample 每轮只删 74–126 个)。模式是"每次重聚类都暴露出一批新的小簇被判 doublet",
  而不是同一批细胞反复被删。**后记(01:00)**:round 8 只删 285 个(0.82%),正常收敛 release,没到上限;8 轮 × 1.5–1.9 h = 13.5 h。值得考虑:(1) 停机判据加"删除类别集中在 zoomin 小簇且总量稳定"的收敛信号;(2) ZMIP 的 doublet
  判定对 <50 细胞的簇要求更强证据(或交给 needs_review 而不是删);(3) 对比 round 7 与 round 2 的标签稳定性来判断后几轮有没有实际收益。

- **整个批次的 numpy 没有链接任何优化 BLAS(2026-09-06 17:5x 排查 scrublet 慢时发现)**:`dl2025/.venv` 里的
  numpy 2.3.4 是 uv **从源码编译**的(dist-info 里有 `uv_build.json`,`Tag: cp312-cp312-linux_x86_64`,
  `np.show_config()` 显示 `blas: name: auto / lapack: name: lapack`),原因是 Sherlock 的 glibc 是 2.17,而 numpy ≥2.3
  只发 manylinux_2_28 wheel,uv 找不到二进制就退回源码编译,编译时没找到 BLAS,于是 `X @ Y` 走 numpy 自带的朴素循环:
  实测 4000³ dgemm **0.8 Gflop/s**,同一进程里 scipy 自带的 OpenBLAS 是 137 Gflop/s(170 倍);numpy 2.2.6 的
  manylinux2014 wheel(自带 scipy-openblas)在同一节点上 172 Gflop/s,dsyrk 245 Gflop/s,scipy/sklearn/scanpy/numba/
  umap/pynndescent/harmonypy 全部能正常导入。**受影响的一切**:sklearn PCA(arpack 的 matvec)、scrublet 的 PCA、
  Harmony、scanpy PCA/neighbors、UMAP、DE 里的矩阵乘——OSP/MSP/ZMIP 所有 BLAS 密集阶段都在慢 10–100 倍地跑。
  **处理**:批量跑期间不动 venv(numpy 不在 identity 里,但换 BLAS 会破坏跨轮次的逐位可复现性,facs v2 也在跑);
  批量跑完后 `uv pip install "numpy==2.2.6" --only-binary=:all:`(numba 0.65 / scipy 1.16 / scanpy 1.11 都兼容 2.2),
  并在 INSTALL.md 里写明"glibc 2.17 的机器必须装有二进制 wheel 的 numpy(≤2.2.x),装完用 `np.show_config()`
  确认 `blas: found: true`"。venv 里其它 uv 源码编译的包(h5py、contourpy、scikit-misc、louvain、annoy…)不走 BLAS,
  无碍。通用教训:**新环境装完先跑一个 dgemm 基准**(4000³ 应在 1 s 内),别等到 profile 时才发现。
- **scrublet/DecontX 加速调研(worktree `osp-fast-qc`,分支 `fast-qc`,基于 timed-logs)**:cProfile 显示 19k 细胞样本
  scrublet 169 s 里 131 s 是 pynndescent 近似 kNN(其中 55 s 是每个 OSP 子进程重新 numba JIT 编译 pynndescent,
  `NUMBA_CACHE_DIR` 无效,因为 pynndescent 几乎没有 `cache=True`),改 `use_approx_neighbors=False`(sklearn 精确
  kNN)降到 29 s,其中 18.6 s 是 sklearn PCA arpack——在残废 numpy 上;DecontX 86 s 里 77 s 是 `decontx_em` 的
  Python 逐细胞循环 + 每轮 `_as_csc` 重复拷贝 7 s,换成 numba 内核(291226a,8 线程 prange 分块累加,确定性)后
  **81 s → 6 s**,结果差 <1e-14。结论:不需要 C++/Rust——热点要么是纯 Python 循环(numba 即可),要么是 BLAS
  没接上;上 Rust 只会多一套 maturin/wheel 工具链和 identity 复杂度。修好 BLAS(numpy 2.2.6 wheel)+ fast-qc 之后同一 19k 细胞样本:scrublet 精确 kNN **11.5 s**(原 110–169 s;
  pynndescent 路径即使 BLAS 修好仍 131 s,瓶颈是 numba 而不是 BLAS)、DecontX EM **5.2 s**(原 66–86 s)、DecontX 粗聚类
  8.6 s、cluster_and_deg 8.4k 存活细胞 29.6 s(原 49 s)、Harmony 66k 13.2 s(不变——harmonypy 2 内部是 C++,不走
  numpy BLAS)。一个 19k 样本的 OSP 计算部分约从 230 s 降到 55 s。
- **日志里没有任何耗时信息**(2026-09-06 用户问"哪些本地工具慢"时暴露):bridge 的 `configure_logging` 默认格式是
  `%(message)s`,所有 kernel 的 `== 阶段` 行、`agent: tool(...)` 行都没有时间戳,Harmony / scrublet / DecontX /
  standissect-lite / 每个 agent 工具的耗时一个都拿不到,只能靠实测。实测(Stomach,mca3.0):19k 细胞样本 scrublet 110 s、
  DecontX 66 s、OSP 聚类+DEG 49 s(2.5k 样本 13.5 s);66k 细胞 Harmony(harmonypy 2.0.0,15 批次)13 s;MSP 工具里
  pooled-reference `check_deg` 现算 wilcoxon 3–13 s(annotate 阶段若 inspect 有 drop,"vs rest" 也走现算,25k 细胞 25 s),
  `subcluster` 0.4–3.6 s,其余工具 ≤0.25 s;全批次 198k 次工具调用中 live check_deg 6468 次(97% 是 pooled)。
  已修(等批量跑完再合并):bridge worktree `bridge-timed-logs`(分支 `timed-logs`,基于 item-limit,253a95e + 6ffbd1b):
  每行日志带 `MM-DD HH:MM:SS`;`run_agent` 给每个应用工具包一层计时,≥1 s 的调用打 `took` 行,每次 run 结束打
  `time: wall N s, tools N s in K call(s) — tool 总秒 ×次数` 汇总;`python -m harness_bridge.logtimes <log>` 从时间戳
  反推各阶段/工具耗时。OSP worktree `osp-timed-logs`(分支 `timed-logs`,基于 tiny-sample-guard-2,deb3daa):27 处
  `print` 改 `log.info`,CLI 入口 `ensure_logging("osp")`,这样 OSP 阶段行也带时间戳。msp/zmip 本来就走 bridge logging,
  不用改。合并顺序:bridge item-limit → timed-logs;osp tiny-sample-guard → -2 → timed-logs。
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

## 后记 2026-09-07:facs v1 下线,v2 转正

- 用户拍板 facs 20 个器官一律以 v2(`--no-decontx --no-scrublet`、QC 空白孔归回真实 plate)为准,不再跑第三遍。
  Oak 上每个器官的 v1 `rsi/` 连同 Fat 的 `rsi-v2.stale-*` 移到 `_eca-rsi-jobs/_trash-facs-v1-20260907/<organ>/`(19 GB),
  `rsi-v2/` 改名为 `rsi/`;scratch 同样 `tabula-muris-facs` → `_trash-20260907-facs-v1`、`tabula-muris-facs-v2` → `tabula-muris-facs`。
  运行目录内没有任何文件引用自身绝对路径(grep 过),改名后 serve / index 正常。`eca-rsi-v2.sbatch`、`rsi-v2-slurm-*.log`、
  `rsi-sample-map-v2.json` 原名保留作为记录,但 sbatch 里的 WORK/OAK_OUT 路径已失效,不能直接重投。
- serve registry 去掉 18 个 v1 条目,20 个 v2 条目改名 `tabula-muris-facs-<organ>`、指向 `rsi/`(122 → 104 项)。
  Google Sheet 20 行改指 v2 release;`tracker_rsi.py` 的 notes 现在会替换旧的 `eca-rsi: … rounds; final cells=…` 段而不是叠加。
- 遗留:Aorta "Platelet"(27 细胞,20 个是 QC 空白孔)和 Kidney "Renal interstitial matrix fibroblast"(37 细胞,27 个空白孔)
  两个伪簇留在 v2 release 里,已在 needs_review;integration 分支的 `exclude_cells` 策略会在下次运行前就把空白孔丢掉,
  这类簇不会再出现。`gen_rsi.sh` 的 facs lane 仍是 v1 配置(sample-map、decontx/scrublet),合并 integration 时一并改。

## 批量跑结束 2026-09-07 02:14

- 102/102 器官 release(mca1.1 30、mca2.0 11、mca3.0 29、tabula-muris-drop 12(含 2 个 0.1.0 前的 legacy release)、
  tabula-muris-facs 20,后者以 v2 为准)。最后一个是 mca3.0/Uterus:7 轮,02:14 以 0.43% 收敛,71,204 → 34,643 细胞。
  100 个有 summary.json 的 release 全部按细胞数规则自然收敛,没有触顶 cap 或强制 release;轮数分布 2 轮 47、3 轮 14、
  4 轮 14、5 轮 11、6 轮 10、7 轮 3、8 轮 1(mca3.0/Kidney)。
- 收尾:tracker 五个 batch 已同步;registry 104 条;`mirror_light.sh` 已停(02:31);serve 跑 integration 代码
  (db90762,access log 带真实来源 IP 和 UA)。

## 后记 2026-09-07 03:xx:integration 合并、作业脚本收编

- 五个仓库的 `integration` 分支已合并进各自 main(eca-rsi 018df28 为 merge commit,其余 fast-forward);
  版本号 ecarsi 0.2.0 / osp 0.1.3 / msp 0.3.4 / zmip 0.3.4 / bridge 0.2.4,只打 GitHub tag,未上传 PyPI。
  合并后唯一的测试失配是 msp `tests/test_log.py`:bridge 0.2.4 给每行日志加了时间戳,msp 的"裸消息格式"断言已改成剥掉时间戳再比。
- `_eca-rsi-jobs/gen_rsi.sh` 重写:默认解释器改为 Apptainer 包装器 `venvs/eca-ct/python`;去掉全部 worktree 分支变量和
  HVG_ORGANS / BRIDGE_ORGANS / EMPTY_ORGANS 白名单(已合并进 main);去掉作业内 5 分钟 rsync 循环和退出时的
  `--delete` 全量 rsync,改为 `eca-rsi run … --mirror "$OAK_OUT"`(ecarsi 自己按步同步轻量文件、release 时全量);
  作业只负责把 `status.txt` 拷到 Oak,失败时留一份可续跑的全量副本。facs 默认 `--no-decontx --no-scrublet`。
  `RSI_SUFFIX`(v2 机制)从 gen_rsi.sh / submit.sh 删除。旧脚本留 `gen_rsi.sh.bak-*`。
- facs 20 个器官的 `rsi-sample-map.json` 重写成"v2 分板 + 细胞策略"示例:`derive_from_cell_id` 同 v2;
  `exclude_cells` 一条 `blank: [mouse.id, subtissue, cell_ontology_class] → upstream_qc_blank`;
  `batch_key: mouse.id` 只写给"板 = 鼠 × FACS 门"的器官(subtissue ≥2 个门且每只鼠 ≥2 块板,且 mouse.id 在每块板内恒定;
  12 个:Brain_Myeloid、Brain_Non-Myeloid、Fat、Heart、Large_Intestine、Liver、Lung、Marrow、Pancreas、Skin、Thymus 以及
  Heart 的 subtissue 实为心腔而非门,按同一规则也归 mouse),其余 8 个保持按板校正(板 = 鼠)。v1 的 map 随 v1 运行进了 trash。
  这些 map 尚未真实跑过(没有第三遍计划),只是配置示例。

## 后记 2026-09-07 03:xx–12:xx:容器环境验证 + 新四批(tabula-muris-senis-drop/facs、tome-mouse、brickman-mouse)

容器验证用 calico-aging(kidney 7.7k / lung 20.8k / spleen 26.8k,eca-pp 完整、非 MCA/TM)。三个器官最终在容器里完整
release(persample、MSP、ZMIP、release、prune、mirror 全程),但前两次尝试各暴露一个环境问题:

- **镜像里没有 `git`**:ecarsi 0.2.0 的 `source_provenance()` 在 persample 崩(`[Errno 2] ... 'git'`)。ecarsi 0.2.1 把
  provenance 的 commit 查询改成可选(OSError → null);身份本来就不用它。教训:容器里任何 shell-out(git、node)都必须可选。
- **容器装到了 pandas 3.0.5**:Copy-on-Write 常开,`Series.values` 返回只读数组,osp `_apply_proposal` 的 `mask &= ...`
  在 cells 级 QC 动作上抛 "output array is read-only"。四个包的测试套件在 pandas 3 下**全部通过**,说明测试覆盖不到 CoW
  和默认 str dtype 这类行为变化。修法两头:osp 0.1.4(`.to_numpy(copy=True)` + 在 pandas 2 下打开 `mode.copy_on_write`
  的回归测试)和容器 `build.sh` 固定 `pandas<3`(就地降到 2.3.3,与 dl2025 一致,所有已发布的器官都在这个版本上跑的)。
  容器与 dl2025 其余差异(scanpy 1.12.4 / igraph 1.0 / leidenalg 0.12 / scipy 1.18 / sklearn 1.9 / numba 0.67)没再出问题。
- **效率**:容器只加速纯计算步(scrublet 精确 kNN 9 s 对 29 s),而一轮 60 分钟里计算只占一成多(calico spleen 第 1 轮:
  integrate 5.5 min,inspect agent 9.6 min,annotate agent 17 min,ZMIP ~25 min),同规模器官的轮次时长与 dl2025 批次同区间。
  收益是可复现、不再依赖 numpy BLAS 补丁,不是墙钟。Ark 429 过载今晚 17 次 / 1547 次 agent 调用(1.1%),全部一次重试成功,
  与上一批(21 / 1582)持平。

新四批 60 个器官(LANES=60,05:1x 提交)第一小时的失败按根因分四类,加两个部署失误:

1. **规模(tome MOCA 阶段)**:E10.5–E13.5(26.5–45.5 万细胞,sci-RNA-seq3 整期一个样本,TOME 导出没有 embryo 列)在
   OSP QC 被 OOM 杀(exit -9,158–240G)。E9.5(11.1 万)峰值 57 GiB,所以需求超线性。`gen_rsi.sh` 把 ≥20 万细胞路由到
   `-p bigmem`(mem = 20 + 1.05 GB/千细胞:E11.5 497G/31 核;1 天墙钟,超时重 sbatch 续跑)。normal 的 `--qos=long`
   每人只给 32 核 / 4 个作业,用不上。E8.5b(15.4 万,139G)在 normal 活了下来。
2. **合并版 atlas 的 cell ID 两种格式(senis-drop)**:作者把 Tabula Muris 3 月龄数据(`10X_P4_4_<bc>-1`,channel 在前缀)
   和 18/21/24 月龄数据(`<bc>-1-<channel>-i-j`,channel 是拼接后缀)拼在一起,没留 channel 列。后缀正则漏掉 11 个器官的
   老格式细胞(Bladder 2498/8945),persample 拒绝。改成单捕获组的合并正则
   `^(?:[ACGT]+-1-)?((?:\d+-\d+-\d+)|10X_P\d+_\d+)(?:_[ACGT]+-1)?$`(16 个器官 100%)。已过 persample 的 5 个器官 map 不动
   (改 map 会使 persample 身份失效)。
3. **msp 潜伏 bug:obs 里有 `cell` 列**:`integrate/outliers.py` 用 `pd.DataFrame(index=ad.obs_names)` 建表再
   `df.index.name = "cell"`,pandas 的 Index 对象是共享的,`ad.obs.index` 一起被改名;TMS 自带作者的 `cell` 列(老格式全名,
   与 index 不同),anndata 两个版本都拒写 `integrated.h5ad`("index.name ('cell') is also used by a column")。此前 130 多个
   器官没有一个带 `cell` 列。senis-facs 之所以先 release 了 18 个,是因为 `--no-scrublet --no-decontx` 下没有
   `doublet_score / decontX_contamination`,outlier 函数在改名前就返回了;drop 有这两个指标才触发。修法:`ad.obs_names.copy()`
   + 回归测试(去掉修正即失败),放在 worktree `$SCRATCH/worktrees/msp-obs-index-name`(分支 obs-index-name,d7cae35,
   容器里全套测试通过),**只**通过 senis 两批 sbatch 的 `export PYTHONPATH=<worktree>` 生效。原因:`downstream.verify()`
   在阶段结束时重算 `kernel_runtime`,改主 checkout 的 msp 会让当时正在 crosssample/zoomin 里的 calico/tome 作业以
   "runtime changed during computation" 失败;身份只比内容,以后主线合并同一内容后 senis 已封存的阶段仍然有效。
4. **OSP 最小样本(senis-facs Thymus)**:板 B001256 只有 2 个孔,两个都过 QC,`cluster` 要求 ≥3 抛 ValueError;ecarsi 把它归为
   `qc_too_few_survivors` 却只把 `qc_zero_survivors` 当空样本 → persample 硬失败。临时用 map 的 `exclude_cells`
   (`where: {cell: [两个孔]}`,reason `plate_below_osp_minimum`,进 needs_review)。**backlog**:OSP 在 <3 存活时把存活细胞
   追加进 `qc_removed.csv`(reason `too_few_survivors`),ecarsi 把 `qc_too_few_survivors` 也当空样本;需要 osp+ecarsi 同改,
   等没有作业处于 persample 时再做(persample 身份含 osp 源码)。
5. **部署失误 A:apptainer 1.5 剥掉 PYTHONPATH**。senis 重提后 drop 器官仍从 `projects/msp` 导入(traceback 路径),
   worktree 修复没生效。我此前的验证是假阳性:在 worktree 目录里跑 `python -c`,cwd 在 sys.path 最前。
   `APPTAINERENV_PYTHONPATH` 能透传,包装器 `venvs/eca-ct/python` 现在显式转发;从 `/tmp` 重验:父进程、子进程、
   `ecarsi.downstream runtime msp` 探针都取 worktree。顺带发现 sbatch 会继承提交 shell 里 module 加的 PYTHONPATH
   (py-cupy、x11),以前被 apptainer 剥掉反而安全,模板现在 `unset PYTHONPATH` 再按需 export。
6. **部署失误 B:整批重提没跳过已 release 的器官**(直接循环 sbatch 而不是 submit.sh),10 个多余作业已取消;
   released 单元被重跑也无害(loop 见到 release 直接退出,scratch 已清)。
7. 会话在用户睡后被挂起了约 5 小时(06:2x 的取消/重提实际 11:1x 才执行),期间 senis 作业各自撞死在 3.,没有其他损失。

辅助脚本(`_eca-rsi-jobs/`):`scan_status.sh <batch>...`(每器官一行:RELEASED / R jobid / end= exit=N + 最后一行 progress)、
`register_serve.sh <batch>...`(幂等 scan-add,命名 `<batch>-<organ>`)。`tracker_rsi.py` 改按 `eca_pp_output_dir` 匹配行
(h5ad 文件名 ≠ 器官目录名:`tms-drop-Bladder.h5ad`、`seurat_object_E3.5.h5ad`)。
