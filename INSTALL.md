# 安装与依赖(eca-rsi 主线)

整套系统 = 一个驱动包 + 三个内核包 + 一个共享库,外加 Claude 与几个外部工具。
所有 Python 包装进**同一个 venv**;内核以子进程 `python -m osp|msp|zmip` 被调用,
所以也可以指到别的解释器(`OSP_PYTHON` / `MSP_PYTHON` / `ZMIP_PYTHON`),默认就是当前解释器。

## 1. 组成与依赖关系

```
eca-rsi (ecarsi)  ── 驱动:organize / persample / loop / ledger / index / serve
   │  子进程调用
   ├── osp     每样本 QC · doublet(scanpy 内置 scrublet)· DecontX(内置)· leiden · 注释 agent
   ├── msp     跨样本 harmony 整合 · inspect / annotate agent · 报告        ── 依赖 standissect-lite, harmonypy, torch
   └── zmip    逐 lineage 下钻                                              ── 依赖 msp
agent 后端 ── deepseek(dsh) / openai(OpenAI Agents SDK + Ark) / claude(Claude Agent SDK)
```

| 包 | 仓库 | 版本 | 依赖(pyproject 声明) |
|---|---|---|---|
| ecarsi | `eca-rsi`(GitHub chansigit/eca-rsi,main) | 0.1.0 | claude-agent-sdk ≥0.2.139, openai-agents 0.22.0, mcp 1.x, anndata, scanpy, h5py, numpy, pandas, matplotlib |
| osp | GitHub chansigit/osp · **PyPI `osp-sc`**(`osp` 与已有 `OSP` 相似被拒,import 名仍是 `osp`) | 0.1.0 | scanpy, igraph, pandas, numpy, scipy, matplotlib, scikit-learn;`[agent]` claude-agent-sdk + openai-agents 0.22.0 + mcp 1.x |
| msp | GitHub chansigit/msp · **PyPI `msp-sc`**(`msp` 名已被占,import 名仍是 `msp`) | 0.2.0 | scanpy, anndata, igraph, **harmonypy==0.2.0**, torch, standissect-lite ≥0.2.0, pandas, numpy, scipy, scikit-learn, matplotlib, seaborn, adjustText;`[agent]` claude-agent-sdk + openai-agents 0.22.0 + mcp 1.x |
| zmip | GitHub chansigit/zmip · PyPI `zmip` | 0.1.0 | msp ≥0.2.0, claude-agent-sdk, scanpy, anndata, pandas, numpy, scipy, matplotlib |
| standissect-lite | GitHub chansigit/standissect-lite · PyPI `standissect-lite` | 0.2.0 | anndata, leidenalg, python-igraph, numpy, pandas, scikit-learn |

两个要点:

- **PyPI 发行名带 `-sc` 后缀:`osp-sc`、`msp-sc`**(`osp` 与已有 `OSP` 相似被拒,`msp` 被无关项目占用),装完照常 `import osp` / `import msp`、`python -m osp|msp`;
  zmip 的依赖写的就是 `msp-sc`。standissect-lite 0.2.0 也已在 PyPI(2026-09-02 起)。
- **harmonypy 钉在 0.2.0**(torch 版,msp 校验过 Z_corr 方向)。PyPI 已有 2.0.0,未测试,不要顺手升级。
  torch 装 CPU 版即可;有 GPU 时 harmony 自动用(`MSP_DEVICE` 可强制)。

## 2. 前置条件

| 项 | 说明 | 本机现状 |
|---|---|---|
| Python ≥3.10 | Sherlock:`ml python/3.12.1` | 3.12.1,venv 在 `/scratch/users/chensj16/venvs/dl2025/.venv` |
| Claude Code CLI + 登录 | `npm i -g @anthropic-ai/claude-code`,`claude login`(OAuth,Max 订阅;不设 `ANTHROPIC_API_KEY` 就不走 API 计费) | 2.1.257,凭据在 `~/.claude/.credentials.json` |
| claude-agent-sdk | Python 包,内部调用上面的 CLI | 0.2.139 |
| openai-agents | `HARNESS=openai` 的 Python agent loop;本项目固定版本 | 0.22.0 |
| Ark API key | `HARNESS=openai` / 豆包鉴权,通过 `ARK_API_KEY` 提供 | shell 环境变量 |
| eca-pp | **上游**:输入必须是 eca-pp 产物(`<样本>/standardize/standardized.h5ad` + `result.json`),organize 守门会拒绝裸 h5ad | 另一个项目 |
| ngrok(可选) | `ecarsi.serve --ngrok` 才需要;authtoken 自备,账号并发 endpoint 上限自负 | 3.37.6,`~/local/bin/ngrok` |
| Node ≥18(可选) | 只有画架构图的 archify skill 用 | 24.13.0(`ml nodejs`) |
| stanhue skill(可选) | `~/.claude/skills/stanhue`,有则 UMAP 配色按空间层次分配,没有回退到默认调色板 | 已装 |

## 3. 安装

**只想用**(内核全在 PyPI;ecarsi 本身尚未发 PyPI,从 GitHub 装):

```bash
pip install "osp-sc[agent]" "msp-sc[agent]" zmip "ecarsi @ git+https://github.com/chansigit/eca-rsi.git"
```

**开发**(全部 editable,改源码即生效):

```bash
ml python/3.12.1
export PIP_CACHE_DIR=$SCRATCH/.pip-cache          # 别把缓存写进 $HOME
python -m venv $SCRATCH/venvs/eca && source $SCRATCH/venvs/eca/bin/activate
pip install -U pip

cd $SCRATCH/projects
for r in standissect-lite osp msp zmip eca-rsi; do git clone https://github.com/chansigit/$r; done
pip install -e standissect-lite && pip install -e "osp[agent]" && pip install -e "msp[agent]" && pip install -e zmip && pip install -e eca-rsi
# msp 会顺带拉 harmonypy==0.2.0 和 torch;editable 的 osp / msp 在 pip 里叫 osp-sc / msp-sc
```

只想要 CPU torch(省几 GB):在装 msp 之前先
`pip install torch --index-url https://download.pytorch.org/whl/cpu`。

## 4. 验证

```bash
python - <<'EOF'
import importlib
for m in ["osp", "msp", "zmip", "standissect_lite", "claude_agent_sdk", "agents", "ecarsi"]:
    x = importlib.import_module(m); print(f"{m:18s} {x.__file__}")
EOF
python -m osp --help | grep -q report-context && echo osp-ok      # 旧副本没有这个参数
python -m msp --help >/dev/null && python -m zmip --help >/dev/null && echo msp-zmip-ok
python -m ecarsi.serve --help >/dev/null && echo ecarsi-ok
claude --version                                                     # SDK 靠它
```

`__file__` 必须指向源码树。曾经踩过的坑:venv 里留着一份非 editable 的 osp 旧副本,
源码改了不生效(缺 `qc_removed.csv`),`pip install -e` 会覆盖掉它。

## 5. 运行时环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `HARNESS` | agent 后端:`deepseek`(dsh + 豆包)、`openai`(OpenAI Agents SDK + Ark + 豆包)或 `claude` | `deepseek` |
| `OPENAI_AGENTS_API` | `HARNESS=openai` 使用 `responses` 或文本兼容路径 `chat_completions`;图像工具结果需要 Responses | `responses` |
| `OPENAI_AGENTS_MAX_NUDGES` | 模型正常结束但未 submit 时,保留同一历史继续提醒的次数 | `2` |
| `OPENAI_AGENTS_MAX_CONTEXT_RESETS` | Ark 拒绝过长图文上下文时,保留 host 任务/提交状态并开新会话继续的上限 | `2` |
| `OPENAI_AGENTS_SERVER_STATE` | Responses 用 `previous_response_id` 只发送每轮增量;设 `0` 则每轮重发完整本地历史 | `1` |
| `DOUBAO_BASE_URL` / `ARK_API_KEY` | Ark OpenAI-compatible endpoint / credential | 北京 `/api/v3` / 必填 |
| `MODEL` / `--model` | 所有 agent 调用的模型;CLI 优先;Pro 可显式选择为 `doubao-seed-2-1-pro-260628` | 随后端:`doubao-seed-2-1-turbo-260628` / `claude-sonnet-5` |
| `--allow-agent-change` | 恢复时显式允许与 manifest 不同的 harness/model;默认拒绝静默混跑 | 关闭 |
| `OSP_PYTHON` / `MSP_PYTHON` / `ZMIP_PYTHON` | 内核用的解释器 | 当前解释器 |
| `ZMIP_MIN_CELLS` | zmip 下钻的最小 lineage 细胞数 | 800 |
| `MSP_DEVICE` | harmony 设备(`cuda` / `cpu`) | 自动 |
| `AGENT_LIMIT_WAIT_MIN` / `AGENT_LIMIT_WAIT_MAX_H` | 撞到订阅额度时的等待间隔(分钟)/ 总预算(小时) | 10 / 12 |
| `PIP_CACHE_DIR`, `HF_HOME`, `XDG_CACHE_HOME` | 集群惯例,指到 `$SCRATCH` | — |

## 6. 一条龙

装好后有一个命令 `eca-rsi`(等价 `python -m ecarsi`;仓库里的 `run-eca-rsi.sh` 是只负责挑解释器的薄壳):

```bash
eca-rsi run <eca-pp 输出目录> <root> [--rounds N] [--serve 8899 [--ngrok --domain …]]   # organize → 每个 unit persample → loop → 落地页

# harness 与 model 正交选择;Pro 更贵,默认仍是已完成端到端验证的 Turbo
eca-rsi --harness openai --model doubao-seed-2-1-pro-260628 run <eca-pp 输出目录> <root>
./run-eca-rsi.sh <eca-pp 输出目录> <root>                                                # 同上,不用激活 venv

# 或者分步(每步都可续跑):
eca-rsi organize  <eca-pp 输出目录> <root>
eca-rsi persample <root>/units/<unit>
eca-rsi loop      <root>/units/<unit>            # 收敛即 release
eca-rsi serve scan-add <root 或 glob>...                       # 写进 registry 文件(~/.config/ecarsi/registry.json);remove/list/dump/reload 同理
eca-rsi serve [--port 8899] [--ngrok --domain …] [--auth u:p]   # 前台 server,http://127.0.0.1:8899/<名>/;registry 改了自动重读;--auth 整站密码,默认不设
```

运行产物一律放仓库外(`$SCRATCH/eca-runs/...`),本机是 Slurm 计算节点直接跑,不要 sbatch。
