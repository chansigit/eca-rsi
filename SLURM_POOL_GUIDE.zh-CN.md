# Sherlock warm pool：从申请节点到结束计算

适用于 ECA-RSI 0.3.0 / MSP 0.5.2，以及本账号已经配置好的 Sherlock Python/Apptainer 环境。其他账号先按 [INSTALL.md](INSTALL.md) 安装环境，再替换下文的账号路径。

你负责申请和归还 Slurm 资源。pool scheduler 管理已经加入的 worker，把任务分给资源够用、机时足够的空闲 worker。driver 是运行 `eca-rsi run` 的主程序，负责推进分析、调用模型、核对结果和保存进度。

```text
你手动申请 Slurm 作业
  ├─ 控制节点：scheduler + driver（给它们预留自己的 CPU / 内存）
  ├─ CPU 作业：worker ─┐
  ├─ CPU 作业：worker ─┼─ 启动后自动向 scheduler 注册
  └─ GPU 作业：worker ─┘

driver → 提交重计算 → scheduler 分配 worker → driver 收回结果继续运行
```

同一个 pool 可以服务多个 driver。driver 结束不代表 pool 结束，worker 空闲也不会自动归还节点。每个 worker 当前同时执行一个重任务，任务可以使用这个 worker 分配到的多核资源。

## A. 直接使用已经运行的 pool

Periscope 左侧 **Overview** 下方有 **Warm pool** 按钮，点击后在首页右侧内容区查看实时节点负载和任务队列，沿用 Periscope 配色并带有轻微辉光。未启动或无法连接 pool 时按钮显示为灰色且不可点击；不提供独立监控页面或网址。独立启动 Periscope 时，用 `eca-rsi serve --pool-scheduler /path/to/scheduler.json` 指定连接，或提前设置 `ECA_POOL_SCHEDULER`。详情见 [网页监控说明](PERISCOPE_POOL.md)。

如果只是开始分析，当前无需重新申请 worker 或启动 scheduler。在你已有的 **driver 计算节点终端**运行：

```bash
source /scratch/users/chensj16/dask-pool/env.sh

# env.sh 默认是 pool；若希望小任务及资源允许时的本地计算，改成 auto。
export OSP_COMPUTE_ENDPOINT=auto
export MSP_COMPUTE_ENDPOINT=auto

/scratch/users/chensj16/dask-pool/pool status

# 替换为实际 ECA-PP 输入目录和本次分析输出目录。
/scratch/users/chensj16/venvs/eca-ct/python -P -m ecarsi run \
  /path/to/eca-pp-output /scratch/users/chensj16/eca-runs/my-study
```

`source` 只设置当前终端的环境；随后启动的 driver 才会继承。它不会启动计算，也不会申请资源。模型后端和密钥仍沿用你已有的 [模型环境配置](INSTALL.md#5-运行配置)；pool 的连接配置不代替模型配置。

2026-09-12 的部署记录：

| 作业 | 节点 | worker CPU | Slurm 内存 | worker 内存预算 | GPU |
| --- | --- | ---: | ---: | ---: | --- |
| 43173336 | sh03-13n22 | 64 | 256 GiB | 224 GiB | — |
| 43173344 | sh03-06n50 | 17 | 128 GiB | 112 GiB | — |
| 42933929 | sh03-08n39 | 9 | 64 GiB | 56 GiB | — |
| 43173346 | sh03-15n03 | 8 | 64 GiB | 56 GiB | RTX 3090，24 GiB |

这是部署快照，节点会到期，以 `pool status` 和 `squeue` 为准。`cpu-session` 没有加入 worker。当前 scheduler 位于 `sh03-06n50`，其所属作业结束会使整个 pool 失联；下面的新建流程把 scheduler 放在单独的控制作业里。

## B. 从零建立一个新 pool

下面是一套可逐步执行的例子：一个控制作业、一个 CPU worker、一个可选 GPU worker。CPU / 内存 / 机时按你的数据量调整。已有作业可以直接从第 3 步开始，不要在同一份 CPU / GPU 上重复启动 worker。

### 1. 在登录节点准备公共配置

每个新 pool 用一个新的目录。下文所有终端都使用同一份 `config.sh`。

```bash
mkdir -p /scratch/users/chensj16/dask-pool/my-pool-01
cat > /scratch/users/chensj16/dask-pool/my-pool-01/config.sh <<'EOF'
export POOL_DIR=/scratch/users/chensj16/dask-pool/my-pool-01
export POOL_CODE=/scratch/users/chensj16/eca-runs/slurm-pool-release-20260912/site
export CPU_PY=/scratch/users/chensj16/venvs/eca-ct/python
export GPU_PY=/scratch/users/chensj16/venvs/eca-ct-gpu/python
export GPU_OVERLAY=/scratch/users/chensj16/dask-pool/slurm-20260912/gpu-runtime
export PYTHONPATH="$POOL_CODE"
export PYTHONSAFEPATH=1
export PYTHONDONTWRITEBYTECODE=1
export LC_ALL=C LANG=C
export ECA_POOL_SCHEDULER="$POOL_DIR/scheduler.json"
export ECA_POOL_DATA_ROOT=/scratch
EOF
```

这里复用已经验证的发布包和 GPU 兼容包目录，不需要重新安装。保留这些目录，不要边运行边修改包源码。driver 和 worker 的 Python、科学计算包和相关源码需要匹配；换版本时统一重启。

### 2. 手动申请控制作业和 worker 作业

建议为每个长期交互作业保留一个终端；可以先在登录节点开 `tmux`，再运行 `salloc`。例如 `tmux new -s rsi-control`，申请后用 `Ctrl-b d` 暂离，用 `tmux attach -t rsi-control` 返回。保留申请作业的 shell；`nohup` 不能让进程活过 Slurm 作业终止。

**终端 1：控制作业，用于 scheduler 和 driver。**

```bash
salloc -p normal -N 1 -n 1 -c 4 --mem=24G -t 1-00:00:00 -J rsi-control
```

driver 仍会读取数据、组织输出并执行部分本地工作，24 GiB 只是示例；根据分析规模给它足够内存。控制作业最好覆盖整次分析的时间。

**终端 2：CPU worker 作业。**

```bash
salloc -p normal -N 1 -n 1 -c 9 --mem=64G -t 1-00:00:00 -J pool-cpu
```

**终端 3：可选 GPU worker 作业。**

```bash
# 先查看当前型号和节点配置，再决定需要什么卡。
sinfo -p gpu -N -o '%N %f %G'
salloc -p gpu -N 1 -n 1 -c 8 --mem=64G --gres=gpu:1 \
  -C 'GPU_SKU:RTX_3090' -t 1-00:00:00 -J pool-gpu
```

GPU 示例对应已经做过 CuPy 验证的型号，不代表完整 RAPIDS 流程已经验收。需要别的卡时替换 `-C`：型号和代际是精确标签，`GPU_MEM:24GB` 不是“至少 24 GB”；L40S 的代际标签是 `GPU_GEN:LOV`。以当时的 `sinfo`、分区权限和软件需求为准。

以上是由你执行的手动申请，不是 warm pool 自动申请。在 Sherlock，`salloc` 获批后通常进入计算节点 shell；每个终端记录自己的作业和主机：

```bash
hostname
echo "$SLURM_JOB_ID"
scontrol show job "$SLURM_JOB_ID" -o
```

核对 `ReqTRES`、`AllocTRES`、`NodeList`、`EndTime`。申请 CPU 和实配 CPU 有时不同：本次实查 `normal` 分区 `MaxMemPerCPU=8000` MiB，64 GiB 内存需要至少 9 CPU，128 GiB 至少 17 CPU。worker 最终以自己实际获准的 CPU affinity 和内存限制为准。GPU 型号标签可参考 [Sherlock 节点特征文档](https://www.sherlock.stanford.edu/docs/advanced-topics/node-features/)。

申请、交互终端和连接计算节点的规则见 [Sherlock 作业文档](https://www.sherlock.stanford.edu/docs/user-guide/running-jobs/)。

### 3. 在控制节点启动 scheduler

回到 **终端 1 获批后的计算节点 shell**：

```bash
source /scratch/users/chensj16/dask-pool/my-pool-01/config.sh
cd "$POOL_DIR"
hostname > scheduler.host
echo "$SLURM_JOB_ID" > scheduler.jobid
nohup "$CPU_PY" -P -m ecarsi.pool scheduler \
  --scheduler-file "$ECA_POOL_SCHEDULER" \
  > scheduler.log 2>&1 < /dev/null &
echo $! > scheduler.pid
```

随后检查：

```bash
cat "$ECA_POOL_SCHEDULER"
tail -n 20 "$POOL_DIR/scheduler.log"
"$CPU_PY" -P -m ecarsi.pool status
```

刚启动可能需要几秒钟生成文件；此时没有 worker 是正常的。scheduler 文件保存连接信息，driver 和 worker 都通过它找到同一个 pool。新 scheduler 必须使用尚不存在的文件路径；重建 pool 时换一个新目录。

### 4. 在获批节点启动 worker，自动注册入池

**终端 2：CPU worker。**

```bash
source /scratch/users/chensj16/dask-pool/my-pool-01/config.sh
cd "$POOL_DIR"
POOL_WORKER_DIR="$POOL_DIR/cpu-$SLURM_JOB_ID"
mkdir -p "$POOL_WORKER_DIR"
hostname > "$POOL_WORKER_DIR/host"
echo "$SLURM_JOB_ID" > "$POOL_WORKER_DIR/jobid"
nohup python3 -m ecarsi.pool.slurm \
  --scheduler "$ECA_POOL_SCHEDULER" \
  --directory "$POOL_WORKER_DIR" --python "$CPU_PY" \
  --memory-gb 56 --root /scratch --root /oak --root /home \
  > "$POOL_WORKER_DIR/launcher.log" 2>&1 < /dev/null &
echo $! > "$POOL_WORKER_DIR/launcher.pid"
```

`python3` 是宿主机解释器，读取 Slurm 和 cgroup；`--python` 才是实际计算使用的容器解释器。默认使用这个进程获准的全部 CPU；可以加 `--cpus N` 限制。worker 内存预算不能超过 Slurm/cgroup 上限的 90%，64 GiB 配 56 GiB 为系统和辅助进程留出了空间。

**终端 3：GPU worker。**

```bash
source /scratch/users/chensj16/dask-pool/my-pool-01/config.sh
cd "$POOL_DIR"
export PYTHONPATH="$GPU_OVERLAY:$POOL_CODE"
POOL_WORKER_DIR="$POOL_DIR/gpu-$SLURM_JOB_ID"
mkdir -p "$POOL_WORKER_DIR"
hostname > "$POOL_WORKER_DIR/host"
echo "$SLURM_JOB_ID" > "$POOL_WORKER_DIR/jobid"
nohup srun --jobid="$SLURM_JOB_ID" --overlap --exact \
  -N 1 -n 1 -c 8 --gres=gpu:1 \
  python3 -m ecarsi.pool.slurm \
  --scheduler "$ECA_POOL_SCHEDULER" \
  --directory "$POOL_WORKER_DIR" --python "$GPU_PY" \
  --memory-gb 56 --gpu --root /scratch --root /oak --root /home \
  > "$POOL_WORKER_DIR/launcher.log" 2>&1 < /dev/null &
echo $! > "$POOL_WORKER_DIR/launcher.pid"
```

这里 `srun --jobid=...` 在**已获批作业内**开 GPU step，不申请新作业。`--overlap` 允许与交互 shell 的 step 共用已分配资源，不意味着可以重复启动两个占用同一块 GPU 的 worker。`-c 8` 与示例申请对应，改了申请就一并调整。step 行为见 [Slurm srun 文档](https://slurm.schedmd.com/srun.html)。

启动器会探查资源并自动注册；不需要另外执行“告诉 keeper”命令。GPU 由 Slurm 分配的设备标识和 CUDA 可见设备匹配得到，不手动把物理卡号硬写成 `CUDA_VISIBLE_DEVICES=0`。

回到控制终端验证：

```bash
"$CPU_PY" -P -m ecarsi.pool status
"$CPU_PY" -P -m ecarsi.pool status --json > "$POOL_DIR/resources.json"
```

应看到 worker 行及 `idle` 状态。约等待 10–30 秒仍未出现时，看对应 `launcher.log`。只有资源申请成功而没有 worker 行，尚不能算入池完成。

提交一个很轻的远端调用，确认实际可用：

```bash
"$CPU_PY" -P - <<'PY'
import socket
from ecarsi.pool.client import PoolEndpoint
with PoolEndpoint(mode="pool") as pool:
    host = pool.submit(socket.gethostname,
                       needs={"cpus": 1, "memory": 512 * 2**20,
                              "seconds": 10}).result(timeout=60)
    print("任务执行节点:", host)
PY
```

返回的主机应是已注册 worker 所在节点。这里强制 `pool`，避免小任务被 `auto` 留在 driver 本地。

### 5. 在控制节点启动 driver

**终端 1**，先确认两个 worker 已注册：

```bash
source /scratch/users/chensj16/dask-pool/my-pool-01/config.sh
export OSP_COMPUTE_ENDPOINT=auto
export MSP_COMPUTE_ENDPOINT=auto

# 沿用已有模型后端/密钥配置；输入须为 ECA-PP 产品目录。
# 替换这两个路径。输出放在输入树和源码目录之外。
ECA_INPUT=/path/to/eca-pp-output
ECA_RUN=/scratch/users/chensj16/eca-runs/my-study
mkdir -p "$ECA_RUN"

# 第一遍可先跑到逐样本 OSP 完成，再检查结果。
"$CPU_PY" -P -m ecarsi run "$ECA_INPUT" "$ECA_RUN" \
  --stop-after persample > "$ECA_RUN/driver-front.log" 2>&1

# 接着继续整合和迭代；复用已验证的前序结果。
"$CPU_PY" -P -m ecarsi run "$ECA_INPUT" "$ECA_RUN" \
  > "$ECA_RUN/driver.log" 2>&1
```

这些命令在前台运行，保留控制终端的 tmux 会话即可。不需要先做前序检查时，直接执行第二条完整运行命令。中断后使用同一输入、输出和计算配置重跑以恢复；换输入、源码或分析设置时，使用新的输出目录。

| 模式 | 行为 |
| --- | --- |
| `local` | 走原有本地计算，不需要 pool。 |
| `pool` | 接入点的计算始终提交 pool；没有适配 worker 时等待或超时。 |
| `auto` | 估计很小且本地资源足够时本地算；其他任务优先找适配的空闲 worker，pool 忙或无适配空位且本地资源足够时也可本地算。 |

`auto` 的估算是粗略启发式，不预测完整排队时间，也不会迁移已提交的任务。**已配置的 scheduler 失联会报错**，不能把 `auto` 当成任意故障下的静默本地回退。

pool 分配的是计算位置。OSP 的 QC、聚类和 DE 是一个远端计算段，模型注释仍在 driver；MSP 的适配计算调用可以进 pool，ZMIP 使用的相应 MSP 调用也沿用此模式。并非整个 RSI 进程都移到 worker。GPU worker 可以接 CPU 任务，加入 GPU 也不会自动把科学算法改成 GPU 实现；`MSP_COMPUTE_GPU=1` 另需匹配的 RAPIDS 环境，本指南默认不启用。

多开 driver 时，各自在计算作业内加载同一份配置，使用各自的输出目录。pool 队列按到达顺序寻找第一个资源适配的空闲 worker；CPU、内存、GPU、软件版本、共享路径和剩余机时都会影响分配。

### 6. 运行中看负载、队列和资源

新 pool 的控制终端：

```bash
watch -n 5 "$CPU_PY" -P -m ecarsi.pool status
squeue -u "$USER" -o '%.12i %.16j %.14N %.8T %.12L'
tail -n 50 "$ECA_RUN/driver.log"
```

当前已部署 pool 的快捷命令则是：

```bash
watch -n 5 /scratch/users/chensj16/dask-pool/pool status
```

| 字段 | 读法 |
| --- | --- |
| `STATE` | `idle` 空闲；`granted` 已分配；`running` 执行中；`draining` 不接新任务；`stale` 心跳/资源信息过期；`expiring` 接近机时结束。 |
| `CPU` / `CPU%` | worker 获准的 CPU 数 / 按该 CPU 数归一化的 worker CPU 使用率。 |
| `RSS/WORKER GiB` | worker 进程驻留内存 / worker 内存预算。 |
| `SLURM GiB` | Slurm 分配的内存，与 worker 预算区分。 |
| `GPU` / `VRAM` | 已注册 GPU 数，以及 GPU 利用率和显存使用。GPU 信息约每 30 秒更新。 |
| `LEFT` / `Queued` | 剩余机时 / 等待分配的任务数；队列条目附等待原因。 |

CPU/RSS 是 worker 进程指标，不是整台机器所有用户的总负载。GPU 指标反映已分配设备。JSON 另含 `requested_tres`、`allocated_tres`、CPU ID、GPU UUID 和 worker 地址，可用于核对 Slurm 申请。

已知任务较重时可以在启动 driver **之前**设置粗略需求，例如：

```bash
export ECA_POOL_TASK_CPUS=4
export ECA_POOL_TASK_MEMORY_GB=16
export ECA_POOL_TASK_SECONDS=900
export ECA_POOL_QUEUE_TIMEOUT=3600
```

这些是接入点的默认估计，调用方显式提供的需求优先；不是增加 Slurm 分配。单个任务必须能放进一个 worker，不会把两台机器的内存拼起来。任务估计时长加 60 秒余量必须放进剩余机时。路径传递要求各节点都能访问相同绝对路径；数组也可以直接传输。

### 7. 动态加入、退出和最后归还节点

**增加 worker：** 你申请新作业后，在新节点执行第 4 步，使用同一份配置和新的 worker 目录。scheduler 与 driver 不需要重启。

**只移除一个 worker：** 先取得它的地址，再 drain：

```bash
"$CPU_PY" -P -m ecarsi.pool status --json | "$CPU_PY" -P -c \
  'import json,sys; [print(w["host"], w["job_id"], w["address"], w["state"], w["task"]) for w in json.load(sys.stdin)["workers"]]'

# 替换为上一步查到的完整地址。
"$CPU_PY" -P -m ecarsi.pool drain tcp://worker-ip:port
```

drain 不打断当前任务。等待该 worker 的 JSON `task` 变成 `null`，确认相关 driver 已收回结果后，在**启动该 worker 的节点**检查记录的进程并停止：

```bash
# POOL_WORKER_DIR 指向第 4 步记录的那个 worker 目录。
POOL_WORKER_PID=$(cat "$POOL_WORKER_DIR/launcher.pid")
ps -p "$POOL_WORKER_PID" -o pid,args
# 确认是该 worker 的启动器后：
kill -TERM "$POOL_WORKER_PID"
```

CPU 例子记录的是宿主机启动器 PID；GPU 例子记录的是该 `srun` step 的客户端 PID。停止它们不等于归还 allocation；你可以保留作业，稍后重启 worker。

**全部结束：** 先让所有 driver 完成，或按 [暂停与恢复说明](README.md#resume-and-storage)暂停并等待退出；确认 pool 无执行中任务和排队任务，再在控制节点运行：

```bash
"$CPU_PY" -P - <<'PY'
from ecarsi.pool.client import connect
from ecarsi.pool.scheduler import dispatch
with connect() as client:
    state = client.run_on_scheduler(dispatch, "status")
    assert not any(t["state"] in {"queued", "granted", "running"}
                   for t in state["tasks"].values()), "pool still busy"
    assert not any(client.processing().values()), "Dask still busy"
    client.shutdown()
PY
```

这会关闭该 pool 的 scheduler 和 worker 计算进程。随后由你逐一归还**确认不用的** Slurm 作业，例如 `scancel <作业号>`，或退出持有 allocation 的 `salloc` shell。不要使用 `scancel -u "$USER"` 清场，否则会连其他用途的作业一起取消。保留 pool 目录中的日志和分析输出。

如果只是结束 driver，保留 pool 即可。scheduler 自己的机时到期或进程退出会中断整个 pool；队列只在内存中，没有持久化重放。重建后让 worker 重新注册，driver 通过已有检查点恢复。

## 常见问题

| 现象 | 先检查什么 |
| --- | --- |
| Slurm 作业仍是 `PENDING` | 资源尚未获批，用 `squeue` 查看原因；pool 无法替你加速 Slurm 排队。 |
| worker 没出现 | 看 `launcher.log`；确认在实际 Slurm job cgroup 内，单独 `export SLURM_JOB_ID=...` 不够。 |
| CPU affinity 超出 Slurm grant | 在明确的 `srun --jobid=... --overlap --exact -N1 -n1 -c N` step 内启动，N 不超过该作业分配；GPU 加 `--gres=gpu:1`。 |
| CPU 锁冲突 | 已有启动器占用同一批 CPU；先核对已有 pool，不要盲目删除锁文件。 |
| 内存预算被拒绝 | 预算须在该进程 Slurm/cgroup 上限的 90% 以内；GPU 显存与主机 RAM 是两回事。 |
| `runtime` / 版本不匹配 | driver 与 worker 加载同一发布代码和兼容科学包；重启加载了旧代码的进程。 |
| 一直排队 | 看 status 的原因：单节点资源、路径、软件版本或剩余机时是否符合需求。 |
| GPU 不可见 / 设备号不匹配 | 确认在 GPU step 启动且带 `--gpu`，检查 `nvidia-smi` 与 Slurm GPU 分配。 |
| scheduler 连不上 | 看控制作业是否到期、scheduler 日志、配置路径；旧 scheduler 文件存在不代表服务还活着。 |
| worker 丢失或计算失败 | driver 应收到失败；保留日志和 OSP `.pool-attempts/`，恢复可用 worker 后从 driver 检查点重跑。 |

实现接口见 [SLURM_POOL.md](SLURM_POOL.md)，测试范围见 [SLURM_POOL_VALIDATION.md](SLURM_POOL_VALIDATION.md)。
