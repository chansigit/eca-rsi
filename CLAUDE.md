# eca-rsi — 循环改进单细胞数据质量与标注的全自动 pipeline

**主线(2026-09-02 起)是 `ecarsi/` 包**:确定的计算内核(osp / msp / zmip)+ Agent SDK 窄决策 +
自驱动循环,见下文"主线:ecarsi 包"一节。入口:

```bash
eca-rsi run <eca-pp 输出目录> <root> [--rounds N] [--serve 8899]   # = organize → 每 unit persample → loop → 落地页;./run-eca-rsi.sh 是薄壳
eca-rsi organize|persample|loop|serve ...                          # 分步,等价 python -m ecarsi.<step>
```

`run.sh` + `steps/*.md` 是上一代"六步 prompt 循环"(agent 自己写分析代码),完整封存在
分支 **`primitive`**(原 main;树里的 run.sh / steps 仍在,但不再维护)。下面"上一代"一节是它的记录,
其中的教训(特征空间每轮重算、双重计数、apply 必须执行、checkpoint 不原地覆写、release 数字锚、删除预算)
在 ecarsi 里已从 prompt 降格为代码。

## 上一代:run.sh 六步循环(分支 primitive;2026-08-25 推倒重做后;总共 ~300 行)

一条命令处理一个装着 h5ad 的文件夹:

```bash
./run.sh <h5ad文件夹> <工作目录> [最大轮数]   # 默认 sonnet-5;MODEL/MODEL_<步骤> 可覆盖
```

每轮六步:explore(探查规划)→ compute(重算特征空间)→ annotate(注释)
→ qc(质控判决)→ apply(执行)→ stop(判 continue/release)。收敛或到达
轮数上限即 release,绝不中途停下等人;存疑事项以 flag 形式进最终报告的
"needs review" 一节。

- `run.sh` — 循环本体。每步 = 一次全新的 `claude -p`(全工具、
  `--dangerously-skip-permissions`、`--max-turns 200`),cwd 在工作目录。
  步骤完成的唯一契约:写出 `rounds/roundNN/<step>.md` 报告;缺文件重试一次
  再失败才停。**续跑天然支持**:重跑同一命令,已完成步骤自动跳过。
- `steps/*.md` — 六份任务书,每步现读(改动即刻生效于下一步)。分析代码由
  agent 自己写、自己跑,连同输出存进本轮目录 —— 代码即审计痕迹。
- `docs -> ../eca-cycle/docs` — 方法文档(CONSTITUTION / RULES_annotation /
  RULES_data_cleaning),相对 symlink 引用不复制;任务书让 agent 按需读
  具体条款,不全文注入。
- `attic-v01/` — 上一版(skills + bin/eca-check + schema)的封存,勿用。
- 上游兼容:输入旁若有 eca-pp/ecasteps 产物(`standardized.h5ad` +
  `result.json`、`batch.tsv`),explore 任务书会让 agent 读取并采信
  (物种/counts/批次列/先验标签),探查力气花在上游管不了的跨文件关系上。
  纯 prompt 指引,无硬编码;没有上游产物照常跑。
- 停机判决走 `rounds/roundNN/decision.txt`(仅一个小写单词 continue 或
  release)—— 机器读机器文件,散文归 stop.md,不做文本捞词。
- **重开**:`--force-reopen` 可越过启动前已存在的 release 判决继续开新轮
  (本次运行新产生的 release 不受影响),事件记入 progress.log,再次收敛
  时原地更新 release/ 并在 summary 注明取代关系。
- 全局重嵌入豁免默认**关闭**(`EXEMPT_PCT=0`:细胞数一变就必须重算);设为
  正数 N 则"上轮删除 <N% 可跳过全局重算"。教训:Liu 数据集豁免连用三轮,
  round 1 分区(含 1 细胞残渣 cluster)原样进了 release。
- `--one-round` 调试模式:只跑一个新轮(resume 跳过的旧轮不算)即停,
  不触发强制 release;事件记 progress.log(oneround)。
- progress.log 每轮记 stats 事件(removed / label_l1_changed /
  label_l2_changed,由 apply 写 stats.txt、runner 中继);apply 另出
  `umap_removed.png`(本轮删除红/保留浅灰,零删除也出全灰图)。

## 核心设计哲学(与上一版的根本区别)

上一版把 compute/apply 写成固定脚本、决策格式定 schema 加 lint,结果六个
silent bug 全部长在"规格与实现的接缝"上(记录了但没人执行、写了但从未实现)。
本版反转:**能力全部交给每步的 agent(它有工具),固定的只有循环骨架和任务
书**。教训不丢,但从代码降格为任务书里的硬句子:

- 特征空间每轮在当前细胞上重算;阈值(群体统计量)不许缓存,doublet 只在
  完整每样本池上算一次(那次缓存是规则要求的)。
- barcode 重叠 + 表达一致 = 同一批细胞,合并即双重计数,必须排除其一。
- apply 必须执行每条决策;执行不了明列"not executed",不许自行变通;
  声称写了的文件必须核实存在("那个失败真实发生过")。
- checkpoint 绝不原地覆写:写 `checkpoint.tmp.h5ad` 再 rename(唯一状态,
  半截写坏无法恢复)。
- release 数字锚:本轮删除 < 当前细胞 1% 才许收敛(7.6% 曾被判"almost
  none",教训);round 1 永不 release;最后一轮强制 release-with-flags。
- 删除预算(单轮 ~10%/累计 ~30%)越线不停机,转保守:边缘删除降级为 flag。

## 现状(2026-08-25)

- 两个数据集真实跑通:18_Clayton_2025(1766 细胞,Fable,3 轮收敛;数据
  后因上游丢样本弃用)与 **Fu 2022 半月板(35k 细胞,eca-pp 完整产物,
  全 Sonnet,3 轮 67 分钟自主收敛)**,后者交付在
  `$OAK/.../05_Fuetal/rsi/`,关键结论与手工分析一致且多出深度混杂检验。
- Fu 跑后复盘已修:annotate 引用须持久化(round 2 幽灵拆分,系统一轮自愈)、
  全局重算的 <1% 豁免成文(但全局四图每轮必出)、flag 必附了结检验
  (无检验的 flag 直进 needs-review 不占悬案)、standissect-lite 每轮 qc
  必调(R11)、逐轮列命名 roundNN_* 升格为正式约定(agent 自发发明)。
- **未修 backlog**:步骤无墙钟超时、无并发锁、run.sh 运行中被编辑有 bash
  增量解析隐患(包 main() 可解)、启动时不验证输入、限额等待逻辑未经实战
  (措辞变体覆盖未知)、强制末轮 release 与 exhausted 路径从未走过、
  checkpoint 逐轮列膨胀(35k 细胞已 791MB,大数据集需归档策略)、
  progress.log retry 事件措辞不准、"每个大谱系 release 前至少一次专属重嵌入"是否入收敛判据待用户拍板。

## 环境

- 本机即 Slurm 计算节点,直接跑,勿 sbatch;`claude -p` headless 已实测可用。
- Python: `/scratch/users/chensj16/venvs/dl2025/.venv/bin/python`
  (scanpy/harmonypy/scrublet/anndata 齐)。
- **本目录是开发目录:运行产物一律放仓库外**(workdir 指到如
  `$SCRATCH/eca-runs/<数据集名>`),输入数据也不进本仓库。

## 主线:ecarsi 包(2026-09-02 由 agent-sdk 分支升格为 main)

确定的计算进包(osp / msp / zmip,各自独立仓库,同级目录),agent 只做窄决策
且被 host 校验;`ecarsi/` 是 wrapper + 驱动。

```
python -m ecarsi.organize    <输入目录> <root>       # eca-pp 守门 + 分析单元规划(agent)+ 细胞守恒审计
python -m ecarsi.persample   <unit>                 # 样本列识别(agent)+ host 子进程池并行跑 osp(每样本 subset.h5ad;QC 只此一次,doublet 只在完整样本池算)
python -m ecarsi.loop        <unit> [--rounds N] [--cap 10] [--force-reopen]
   round 1: ecarsi.crosssample(样本纳入 agent → msp integrate/inspect/annotate)→ ecarsi.zoomin(zmip)
   round N: 上轮 zoomin/annotated_zmip.h5ad,先验列改名 r(N-1)_* → msp --from-h5ad → zmip
python -m ecarsi.ledger      <unit> [round dirs]    # 逐细胞台账 cell_ledger.csv + Sankey(每步删除流进红色 sink)
python -m ecarsi.index       <root|unit>            # 从磁盘推导落地页(每步结束也自动写)
python -m ecarsi.serve       start [dir] [--port 8899] [--ngrok [--domain D] [--auth u:p]] [--attach]   # 常驻多数据集导航 daemon(tmux 里)
                             bind <dir> [--name N] | unbind <name> | list | dump [p] | reload [p]      # 运行时增删数据集,内存态;dump/reload 手动快照
                             attach | stop | status                                                    # attach 进 tmux 看日志,prefix+d 退出;stop 连 ngrok 一起收
eca-rsi <step> ... / eca-rsi run ...                # console 入口(ecarsi/__main__.py);run.sh 是 primitive 分支的旧入口
```

**目录结构只在 `ecarsi/layout.py` 一处定义**,各步不得自拼路径(2026-09-02 统一):

```
<root>/                     organize 的 out_root = 一个数据集一次运行;serve 投这一层
  index.html  organize/manifest.json
  units/<unit>/
    index.html  progress.log  input/  persample/<sample>/
    rounds/roundNN/{manifest.json, input.h5ad(N≥2), crosssample/, zoomin/, ledger/, stats.txt, decision.txt}
    release/{final.h5ad, summary.md, needs_review.md, needs_review.json, cell_ledger.csv, sankey_coarse.png}
```

- 落地页(`ecarsi.index`)**纯从磁盘推导**(manifest / 契约文件 / stats / decision / progress.log),
  跑到一半也能渲染(round 进行中显示到哪一步、persample 完成数);serve 每次请求根页/unit 页都现算,
  各步结束再写一份静态页留档。内核(osp/msp/zmip)只写各自的 `report.html`,永不写 index.html。
- release 时 `ecarsi.umapdata` 从 final.h5ad 抽 `release/umap.json`(坐标 16 位量化 + 标签索引;超过 `--max-points`=10 万时分层抽样,
  <300 细胞的小簇全保留,图例计数仍是全量);unit 落地页用原生 JS canvas 画 coarse / fine 两个同步面板
  (像素缓冲直写 + 基图缓存 + 网格找最近点 + 拖拽缩放时 LOD),不依赖外部库。
- needs_review(`ecarsi.review`)按**类别**分节而非按轮:convergence → removed(低于 high 的真删,不可逆)
  → sample_excluded → reassigned(跨轮重复的标 recurs)→ inspect_flag → lineage_skipped → low_confidence;
  每条带 round/step/scope/cluster/细胞数/report 链接;同一记录渲染 md / json / html。
- persample manifest 记录的 `dir` 是绝对路径,但所有读取方一律用 `layout.sample_dir()` 按 basename
  在本 unit 的 persample/ 下定位,目录搬家不坏。

- **停机只看细胞数**,标签变动不作判据(agent 措辞有随机性):给了 `--rounds N` 就只看轮数,跑满 N 轮;
  没给则 (1) 本轮删除比 < 1% 或删除数 < 100,或 (2) 连续三轮删除比 < 2% 即 release;round 1 永不 release;
  `--cap`(默认 10)是安全上限,触顶强制 release 并标记。`--force-reopen` 越过已有 release 继续开轮。
- **绝不中途等人**:各步的疑点(低 confidence、zmip `budget_exceeded`、inspect flag、样本排除、reassign)
  只在 `release/needs_review.md` 一次汇总;`release/{final.h5ad, summary.md, cell_ledger.csv, sankey_coarse.png}`。
- **每次删除都逐细胞记账**:osp `qc_removed.csv`、msp `annotation_removed.csv`、zmip `zmip_removed.csv`;
  ledger 把它们对齐成一张表,数目必须严丝合缝。
- 真删只发生在 msp annotate 和 zmip;integrate/inspect 只提议。`integrated.h5ad` 永不改,
  幸存者在 `annotated.h5ad` / `annotated_zmip.h5ad`。
- zmip:lineage 由 UMAP 连通性决定(agent 必须看图;一个岛一个 lineage,状态并入所在岛),
  ≥800 细胞才下钻;每 lineage 单 agent,可 recluster、remove、reassign;删除超 10% 触发一次复核(软预算)。
- **单样本 / 单批次**(persample 判不出样本列 → 整文件一个样本 `all`,或纳入 agent 只留 1 个):走同一条链,
  差别只有三处——纳入 agent 不开(直接纳入)、msp 跳过 harmony(`X_pca_harmony = X_pca`,uns 记 skipped)、
  inspect / annotate 被告知样本组成不作证据;osp 的 drop 照旧只作证据、在 annotate 一次真删;zmip 不变。
- 环境:`MODEL`(默认 claude-sonnet-5)、`HARNESS`(claude|deepseek)、`MSP_PYTHON` / `ZMIP_PYTHON`、`ZMIP_MIN_CELLS`;
  `AGENT_WALL_MIN`(每次 agent 调用的墙钟预算,默认 180 分钟,两个后端都强制,超时重开一次);
  并发池:`PERSAMPLE_PARALLEL` / `PERSAMPLE_MEM_PER_CELL_MB`(persample)、`ZMIP_PARALLEL` / `ZMIP_MEM_PER_CELL_MB`(zoomin),
  默认从 affinity CPU + cgroup 内存自动定(`ecarsi.resources`,与 msp.resources 同一份拷贝)。
- persample(2026-09-03 起)不再开 agent 驱动:host 读一次 organized.h5ad 写出每样本 `subset.h5ad`,
  子进程池并行跑 `python -m osp`(大样本先跑),失败重试一次后记 `persample/failures.md` 继续;12 样本 Fu2022 约 5 分钟。
- zmip plan 有 host 连通性校验:`lineage_islands.csv`(UMAP 2D kNN 连通分量)——把分开的岛并成一个 lineage 直接打回;
  同一岛拆成多个 lineage 打回一次,agent 可带 `confirm_shared_islands: true` 重交,记入 plan 的 `host_warnings` 与 needs_review。
- 测试数据:`$SCRATCH/eca-runs/_organize_test/fu2022/fu2022-meniscus` 是旧结构的真实跑(不迁移);
  `$SCRATCH/eca-runs/_layout_test/fu2022` 是它的 symlink 复刻(新结构,验证 index/serve 用),
  `_layout_test/running` 是"round 3 跑到一半"的假象。直播:`eca-rsi serve start <root> --domain csj.ngrok.pizza`(一个 daemon、一条隧道,`/<name>/` 路径路由;再 `bind` 别的 root 不用重启),
  用户自己的 ngrok 隧道 8899 → csj.ngrok.pizza(勿动;ngrok 账号并发 endpoint 有上限,`--ngrok` 会直接报它的错)。
