# 本地智能体的"云端外援"接线方案

## 你要的东西到底是什么

不是网关，不是基础设施，是一个**挂上去就多出一批工具的插件**。形态 = MCP 服务器。

- 你的本地智能体 = 宿主（host），仍然是大脑和执行者
- 外援 = MCP 服务器进程，给它开几个工具：复杂任务找云端要方案、疑难杂症找云端分析
- 平时零调用 = 零花钱，只有本地真兜不住时才产生云端费用

## 推荐：cli-bridge-mcp

`JoaoBerne/cli-bridge-mcp` — Apache 2.0，最后提交 2026-10-13（活跃），Python 3.12+ / uv。

选它的四个理由：

1. **官方就有 local-first 方案**。仓库里 `examples/local-first-host.md` 写的就是你的场景：本地模型当大脑，cli-bridge 让它"打电话给朋友"。不是我硬凑的。
2. **省钱护栏是内建的**。有 `PROFILE=saver|balanced|max`、按 lane 的 `DAILY_LIMIT`、`COST=free|limited|paid`、`set_lane_cost` 记账，还有 `ask_cascade` 走"便宜→强"自动回退。
3. **lane 抽象正好对上你的分工**。Ollama 是内置 lane（免费），LM Studio / llama.cpp / MLX 通过 lane JSON 接，任意 OpenAI 兼容云 API 也能接（就是你"已有的云 API"）。
4. **多轮回退有现成机制**。每次咨询返回 thread id，跨 lane 复用同一个 thread 就能接着聊，`/compact` 之后也不丢 —— 这就是你要的"执行中遇到问题再回云端"。

### 工具怎么对上你的需求

| 你的需求 | 用哪个 |
|---|---|
| 复杂任务让云端设计 | `workflow(preset="refine_plan")` 或 `ask_mycloud(task="设计……")` |
| 疑难杂症让云端分析 | `ask_best(task=…, mode="deep")` 让 router 挑强 lane |
| 能省则省，本地优先 | `ask_cascade` 便宜→强，本地失败才升级 |
| 拿不准要不要上云 | `ask_all` 并行跑所有免费 lane，看 disagreement score |
| 本地执行重复劳动 | 宿主自己干，压根不经过外援 |
| 云端评审 / 兜底判断 | `review_diff(focus="security")`、`debate(vote="borda")` |
| 严控开销 | `PROFILE=saver`、`CLI_BRIDGE_MYCLOUD_DAILY_LIMIT` |

## 三步接线

### 1. 自检

```bash
uvx --from cli-bridge-mcp cli-bridge doctor
uvx --from cli-bridge-mcp cli-bridge doctor --deep
```

`doctor --deep` 会实际探测每个 lane，告诉你哪些能用。建议每周跑一次。

### 2. 配 lane

编辑同目录的 `cli-bridge-lanes.json`，把三处占位符改掉：

- 本地 lane 的 `default_model`（LM Studio 里的模型 ID）；用 Ollama 的话整段删掉，Ollama 是内置的
- 云 lane 的 `default_model`（你的云端强模型 ID）
- 云 lane 的 `base-url`（出现两次，都改）和 `CLOUD_API_KEY`

### 2.5 装"使用规则"（别跳过）

工具装好只解决"能不能找外援"，**"该不该找"是另一回事**，靠 `EXTERNAL-AI-RULES.md`。把它按宿主改成对应文件名（`CLAUDE.md` / `AGENTS.md` / `.clinerules`）放进项目或全局。

没有这份规则，30B 模型会凭即兴发挥决定要不要花钱 —— 判断松了预算照烧，紧了外援白装。

### 3. 注册到宿主

把 `mcp-config.json` 里 `mcpServers.cli-bridge` 整段拷进宿主的 MCP 配置，重启会话。

Claude Code 一行搞定：`claude mcp add cli-bridge -- uvx cli-bridge-mcp`

## 针对你的配置：30B MoE Q4 + 256K

这个本地脑比我最初的假设强一大截，方案跟着收紧三处：

**1. 白名单要窄。** 30B MoE 的 Q4（Qwen3-30B-A3B 这一系，工具调用稳）本来就够格当宿主，绝大多数"复杂任务"它自己扛得住。云端只留四个口子：**架构设计 / 两轮以上没收敛的疑难根因 / 不可逆操作评审 / 打破僵局的第二意见**。具体清单见 `ESCALATION-RULES.md`。

**2. 256K 上下文是省钱武器，不是摆设。** 常规做法是"边跑边问"，那最烧钱。你的正确打法是**本地先攒够上下文 → 一次性打包 → 一次问透 → 拿回方案本地跑完**。一趟买卖替代三轮往返，配额消耗直接砍到三分之一。

**3. 配额硬锁。** 见下方"配额怎么卡死"。

## 配额怎么卡死

三层闸，逐层收紧：

| 层 | 机制 | 配在哪 |
|---|---|---|
| 场景闸 | 白名单，不在名单内禁止上云 | `ESCALATION-RULES.md` → 贴进宿主的 CLAUDE.md / AGENTS.md / .cursorrules |
| 显式闸 | `PROFILE=saver`：付费 lane **不参与 fan-out**，必须被显式点名才动 | `mcp-config.json` |
| 次数闸 | `DAILY_LIMIT=5` + `MIN_INTERVAL_S=60`：每天最多 5 次，一分钟最多 1 次 | `mcp-config.json` |

关键在**场景闸**：MCP 服务器只会数数，不会判断"这活该不该上云"。语义层面的拦截必须写在宿主的规则文件里，靠本地模型自觉遵守。所以 `ESCALATION-RULES.md` 不是可选项，是这套方案的承重墙。

先按 5 次/天跑一周，看实际够不够再调 —— 宁紧勿松。

## 剩下的坑

**Windows 上先验证 uvx 能起进程。** 建议先跑通第 1 步的 `doctor`，别直接进 IDE 配。GUI 宿主启动时 PATH 很窄，`uvx` 找不到就白发。真不行就 `uv tool install cli-bridge-mcp` 后用绝对路径。

**MoE 量化模型的工具调用要单独验。** 30B MoE Q4 一般没问题，但接好之后先让本地模型调一次 `ask_myclocal` 或 `list_models` 确认它能稳定吐出 tool call，再谈上云。

## 30B Q4 MoE 的专项调参

30B Q4 MoE 当宿主是很合适的规格 —— active 参数少所以快，知识面是 30B 所以 tool calling 判断力够。但有几个点必须调：

**上下文长度拉到 32K 以上，能上 64K 更好。** MCP 的工具定义本身要吃掉几千 token（cli-bridge 有 15 个固定工具，每多一个 lane 再多一个 `ask_<lane>`）。上下文太短会出现"工具列表把对话挤没了"。在 LM Studio / Ollama 的模型加载设置里改，不是在这儿改。

**用 Instruct 版，别用 Thinking 版。** 如果拉的是 `-Thinking` / 带 reasoning 的版本，在运行时把 reasoning 关掉。thinking 会显著拖慢每一次工具调用决策，而且推理内容会占上下文。当宿主需要的是快而稳的判断，不是深度思考 —— 深度思考是外援的活。

**量化档位注意。** Q4 对 MoE 的 expert 权重比较敏感：
- 稳妥：`Q4_K_M` 及以上
- 谨慎：`Q4_K_S`、`IQ4_XS` 这类低比特档在长上下文 tool calling 上可能退化
- 如果跑起来发现工具调用偶尔格式错乱，先怀疑量化档位，再怀疑模型

**温度压到 0.2 以下。** 宿主在决定是否调工具时需要稳定，不需要创造性。创造的部分交给云端。

**lane 不用手动裁剪。** 内置 lane 在二进制缺失时会自动隐藏，所以没装的 CLI 不会白白占工具位。只有当你确实装了多个云端 CLI、又不想让宿主乱花钱时，才显式关掉：

```bash
CLI_BRIDGE_GEMINI_ENABLED=false
CLI_BRIDGE_GPT_ENABLED=false
CLI_BRIDGE_CLAUDE_ENABLED=false
```

## 备选

**PAL MCP（原 zen-mcp-server）** `BeehiveInnovations/pal-mcp-server`
工具语义更贴你的原话 —— `planner` 就是"把复杂项目拆成可执行计划"，`thinkdeep` 就是"扩展推理 / 找边界情况"，`debug` 是根因分析。Apache 2.0。
但最后提交停在 2025-12-15，已经十个月没动了；而且它天生是给 Claude Code 当宿主设计的（帮你把别的模型塞进来），跟你的"本地是主人"方向是反的。想要 planner/thinkdeep 那套语义可以装它，但别当主力。

**multi-llm-mcp** `arjunkr303/multi-llm-mcp`
极简，就一个 `ask_llm` 工具，指定 provider + model 问一句。想要"最轻的外援"选这个，代价是没有 cascade / 预算护栏 / 会话线程。

**model-council-mcp**
把同一个问题丢给一堆模型（本地 + 云）再汇总，适合重大决策要多方意见。跟省钱方向相反，按需。

## 文件清单

| 文件 | 作用 |
|---|---|
| `MCP-SETUP.md` | 本文件：选型、接线步骤、配额策略、坑 |
| `ESCALATION-RULES.md` | 场景白名单。**承重墙**，必须贴进宿主的规则文件 |
| `cli-bridge-lanes.json` | 本地 + 云端两条 lane 的定义 |
| `mcp-config.json` | MCP 注册配置 + 省钱护栏 |

## 还差的信息

要我把配置彻底写实，告诉我两件事：

1. 你的本地智能体具体是哪个（Claude Code / Codex CLI / opencode / Cline / Continue / Roo·Kilo / Aider）—— 决定 `ESCALATION-RULES.md` 贴进哪个文件、MCP 配置写哪儿
2. 你的云 API 是哪家的 OpenAI 兼容端点 —— 决定 base-url 和模型 ID 填什么

另外确认一下：本地模型若是 Qwen3-30B-A3B-Instruct-2507 这一系（262K 上下文），那和"30B MoE + 256K"完全对得上，工具调用稳定性有保证。
