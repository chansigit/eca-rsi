# eca-rsi — 循环改进单细胞数据质量与标注的全自动 pipeline

一条命令处理一个装着 h5ad 的文件夹:

```bash
./run.sh <h5ad文件夹> <工作目录> [最大轮数]   # 默认 sonnet-5;MODEL/MODEL_<步骤> 可覆盖
```

每轮六步:explore(探查规划)→ compute(重算特征空间)→ annotate(注释)
→ qc(质控判决)→ apply(执行)→ stop(判 continue/release)。收敛或到达
轮数上限即 release,绝不中途停下等人;存疑事项以 flag 形式进最终报告的
"needs review" 一节。

## 架构(2026-08-25 推倒重做后;总共 ~300 行)

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
- **步骤 agent 会读 Sherlock 组织级 CLAUDE.md 而自行 sbatch**(2026-08-26 Jia/Gan
  实测):临时解法是往每个 workdir 放一份 CLAUDE.md 声明"已在 Slurm 分配内,
  直接跑";长期应由 run.sh 在 mkdir workdir 时自动写入(或写进 context header),
  待无运行中实例时改 run.sh。
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
