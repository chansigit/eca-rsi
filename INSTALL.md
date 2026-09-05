# 安装与运行（ecarsi 主线）

本文对应 2026-09-04 核对的配套源码。流程和结果说明见 [README.md](README.md)。
ECA-PP 先独立运行；ECA-RSI 环境包含驱动包、OSP/MSP/ZMIP 三个内核和共享库。
建议装入同一个独立环境，内核通过 `python -m osp|msp|zmip` 子进程运行。

## 1. 包与兼容关系

下表是配套源码的版本声明，不代表包索引上同版本的文件包含全部当前修改，
也不是完整工作流的环境锁定文件。

| 发行名 / import 名 | 源码版本 | 关键依赖与职责 |
| --- | --- | --- |
| `ecarsi` / `ecarsi` | 0.1.0 | 驱动；依赖 `agent-harness-bridge[all]==0.1.0`、anndata、scanpy、h5py、numpy、pandas、matplotlib |
| `osp-sc` / `osp` | 0.1.1 | 每样本 QC、Scrublet、内置 DecontX、聚类和注释建议；`[agent]` 安装 bridge 的全部后端依赖 |
| `msp-sc` / `msp` | 0.2.0 | 跨样本整合与审查；依赖 `harmonypy==0.2.0`、torch、`standissect-lite>=0.2.0`；`[agent]` 安装后端依赖 |
| `zmip` / `zmip` | 0.2.0 | lineage 内重算与细化；依赖 `msp-sc>=0.2.0,<0.3` 和 `agent-harness-bridge[all]==0.1.0`，另有运行时 API 兼容检查 |
| `agent-harness-bridge` / `harness_bridge` | 0.1.0 | core 无依赖；extras 为 `openai`、`claude`、`deepseek`、`all` |
| `standissect-lite` / `standissect_lite` | 0.2.0 | MSP 使用的群体内部小片段检测库 |

安装名是 `osp-sc` 和 `msp-sc`，import 和模块入口仍为 `osp` 和 `msp`。
`ecarsi` 默认依赖不包含三套内核；其 `[kernels]` extra 声明了内核依赖，
但仅用这些版本下限不能锁定配套源码。MSP 当前固定 `harmonypy==0.2.0`，
不要在安装时自行替换；torch 可以使用 CPU 构建。

## 2. 前置条件

- Python ≥3.10；旧 Linux 的原生库兼容性需要结合 h5py/HDF5 等依赖检查。
- 默认后端 `HARNESS=openai` 使用 OpenAI Agents SDK，通过 Ark 调用豆包。
  在环境中设置 `ARK_API_KEY`；bridge 的 OpenAI extra 固定 `openai-agents==0.22.0`。
- 选择 `HARNESS=claude` 时才需要可用的 Claude Code 登录及 Claude Agent SDK。
  选择 `HARNESS=deepseek` 时需要可执行的 dsh；按
  [bridge 文档](https://github.com/chansigit/agent-harness-bridge) 配置 `DSH_BIN`。
  dsh runtime 不随 bridge 的 Python extra 自动安装。
- 输入必须包含 ECA-PP 的 `standardize/standardized.h5ad` 与同目录 `result.json`。
  `identify_columns/result.json` 可选。目录约束见 [README](README.md#prepare-the-input)。
- `ngrok` 仅在远程隧道模式需要；Node 不是分析工作流的必需依赖。

旧集群上若 dsh 报 glibc 不兼容，应使用集群提供的兼容运行环境或已验证的
源码构建。Sherlock 可按本地环境检查 `polyfill-glibc/0.1` 模块是否适用。

## 3. 从配套源码安装

当前先使用同级源码 checkout 安装共享库和内核。下面的命令适用于这些仓库
均已放在同一父目录的情况；替换路径，并选择需要保留的提交后再安装。
这是开发环境的安装步骤，本次文档修订没有重新执行完整环境安装。

```bash
python -m venv /path/to/venvs/eca
source /path/to/venvs/eca/bin/activate
python -m pip install -U pip

cd /path/to/source-checkouts
python -m pip install \
  -e './agent-harness-bridge[all]' \
  -e ./standissect-lite \
  -e './osp[agent]' \
  -e './msp[agent]' \
  -e ./zmip \
  -e ./eca-rsi
```

源码仓库位于 GitHub 的 `chansigit` 账号下：
[eca-rsi](https://github.com/chansigit/eca-rsi)、
[osp](https://github.com/chansigit/osp)、
[msp](https://github.com/chansigit/msp)、
[zmip](https://github.com/chansigit/zmip)、
[agent-harness-bridge](https://github.com/chansigit/agent-harness-bridge)、
[standissect-lite](https://github.com/chansigit/standissect-lite)。

只需要 CPU torch 时，可在安装上面的包之前执行：

```bash
python -m pip install torch --index-url https://download.pytorch.org/whl/cpu
```

ZMIP 的配套源码提供独立 wheel 安装验证脚本，检查安装后的 API、依赖和测试。
其 2026-09-04 验证记录说明，当时使用的索引未能解析要求的 harness 发行版；
因此不能把 `pip install zmip` 当作该验证环境的复现方式。具体步骤及原生库要求见
[ZMIP 安装说明](https://github.com/chansigit/zmip/blob/main/docs/runtime.md#install)
和[验证记录](https://github.com/chansigit/zmip/blob/main/VALIDATION.md)。
这些内核验证也不等于整套 ECA-RSI 在更新后的组合上已完成端到端验证。

## 4. 检查实际运行环境

在准备运行分析的环境内执行：

```bash
python -m pip check
python - <<'PY'
import importlib
from importlib.metadata import version
for dist, module in [
    ('ecarsi', 'ecarsi'), ('osp-sc', 'osp'), ('msp-sc', 'msp'),
    ('zmip', 'zmip'), ('agent-harness-bridge', 'harness_bridge'),
    ('standissect-lite', 'standissect_lite'),
]:
    loaded = importlib.import_module(module)
    print(dist, version(dist), loaded.__file__)
PY
python -m ecarsi --help
python -m ecarsi run --help
python -m osp --help
python -m msp --help
python -m zmip --help
```

editable 安装的 `__file__` 应指向预期源码目录；wheel 安装应指向对应环境的
安装目录。检查路径是为了防止误用旧副本，不能统一要求所有安装都指向源码树。
检查通过说明依赖和入口可用；数据处理与模型提交仍需实际运行验证。

## 5. 运行配置

| 配置 | 作用 / 默认值 |
| --- | --- |
| `HARNESS` / `--harness` | `openai`（默认）、`deepseek` 或 `claude` |
| `MODEL` / `--model` | CLI 优先；OpenAI/dsh 默认 `doubao-seed-2-1-turbo-260628`，Claude 默认 `claude-sonnet-5` |
| `ARK_API_KEY` / `DOUBAO_BASE_URL` | Ark 密钥 / endpoint；默认使用北京区域 `/api/v3` |
| `OPENAI_AGENTS_API` | 默认 `responses`；`chat_completions` 是文本兼容路径，图像工具结果使用 Responses |
| `OPENAI_AGENTS_MAX_NUDGES` | 未 submit 时在同一历史中继续提醒，默认 2 次 |
| `OPENAI_AGENTS_MAX_CONTEXT_RESETS` | 超长上下文恢复次数上限，默认 2 次 |
| `OPENAI_AGENTS_SERVER_STATE` | 默认 1，Responses 使用 `previous_response_id` 增量续接 |
| `--allow-agent-change` | 显式允许被检查的 manifest 与当前 harness/model 不同；不重算已完成步骤 |
| `OSP_PYTHON` / `MSP_PYTHON` | 内核解释器，默认当前解释器 |
| `ZMIP_PYTHON` | 默认依次取 `MSP_PYTHON`、当前解释器 |
| `ECA_RSI_PYTHON` | `run-eca-rsi.sh` 使用的解释器；未设时先尝试脚本内的本机 venv，再回退到 `python` |
| `PERSAMPLE_PARALLEL` / `PERSAMPLE_MEM_PER_CELL_MB` | 每样本并发上限 / 内存估算；默认按可用 CPU 和内存调度 |
| `ZMIP_PARALLEL` | ZMIP lineage 并发上限；设为 1 可顺序运行 |
| `ZMIP_MIN_CELLS` | 下钻最小 lineage 细胞数，默认 800 |
| `MSP_DEVICE` | Harmony 设备选择，默认自动，可设 `cpu` 或 `cuda` |
| `AGENT_WALL_MIN` | 单次 agent 调用时间预算，默认 180 分钟 |
| `AGENT_LIMIT_WAIT_MIN` / `AGENT_LIMIT_WAIT_MAX_H` | 额度等待间隔（分钟）/ 总预算（小时），默认 10 / 12 |

后端细节由共享 bridge 管理。更换解释器时，也要在对应环境安装兼容的内核和
bridge。并非内核的全部 CLI 参数都能经由 `eca-rsi run` 传入，以驱动的帮助为准。

## 6. 运行、续跑和浏览

```bash
# 已设置 ARK_API_KEY，以下路径替换为实际目录。
eca-rsi run /path/to/eca-pp-output /path/to/eca-runs/study

# 分步运行；organize 用于首次组织输入。
eca-rsi organize /path/to/eca-pp-output /path/to/eca-runs/study
eca-rsi persample /path/to/eca-runs/study/units/UNIT
eca-rsi loop /path/to/eca-runs/study/units/UNIT --no-prune

# 登记并启动本地浏览服务；scan-add 更新 registry 文件。
eca-rsi serve scan-add /path/to/eca-runs/study
eca-rsi serve --port 8899
```

`UNIT` 取自 `organize/manifest.json`。中断后使用原来的 `eca-rsi run` 命令，
复用已组织的目录及已完成输出；不要把重新执行 `organize` 当作通用续跑方法。
`--rounds N` 是总轮数，覆盖自动收敛规则；`--force-reopen` 越过既有 release
继续新轮。发布默认清理中间 H5AD，要保留它们则在 `run` / `loop` 加 `--no-prune`。
停止条件、清理范围和续跑检查边界见 [README](README.md#resume-and-storage)。

默认服务地址为 `http://127.0.0.1:8899/`。registry 默认位于
`~/.config/ecarsi/registry.json`，可通过 `XDG_CONFIG_HOME` 或 `--registry` 指定。
已有 ngrok 配置时，可以使用：

```bash
eca-rsi serve --port 8899 --ngrok --domain YOUR_DOMAIN --auth USER:PASS
```

`eca-rsi run ... --serve 8899` 在处理结束后启动前台服务；查看进行中的结果可
另开终端执行 `serve`。服务默认仅绑定本机，`--auth` 可为页面增加 HTTP Basic Auth。

运行结果和输入均放仓库外，且输出不能嵌入 ECA-PP 输入目录。集群上先取得适当的
CPU/内存资源；已在计算 allocation 内时可直接运行。缓存目录按集群约定配置，
不要把特定会话的节点、CLI 版本或登录状态当作安装前提。
