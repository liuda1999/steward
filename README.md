# steward

> 本地模型是主力，云端是外援。steward 管这中间的一切：什么时候值得花钱、窗口装不装得下、档案怎么不把磁盘写满。

```
智能体 ──OpenAI 兼容──▶ 127.0.0.1:27418/v1 ──┬── 本地模型（默认）
                                            └── 云端模型（需模型主动申请）
```

| | |
|---|---|
| 版本 | **v1.0.0**（[发布说明](docs/releases/v1.0.0.md)）· MIT |
| 形态 | 单进程 FastAPI 网关，无状态 |
| 默认端口 | 27418 |
| 对外协议 | `/v1/chat/completions` + `/v1/responses`（双向互转） |
| 依赖 | fastapi / uvicorn / httpx / PyYAML，无数据库 |
| 规模 | 5,824 行 Python（含注释），14 个模块 |
| 测试 | 345 个：可靠性 20 · 对照审计 23 · 端口/槽位/版本 31 · 其余为各层单测与端到端 |
| 每轮注入 | 531 tokens（system + 配额 + 工具定义） |
| 上游 | 任何 OpenAI 兼容端点：llama.cpp / vLLM / LM Studio / Ollama / 云 API |

**名字取自「管家」。** 它不替你决定做什么，只管三件事：账本（花不花得起）、窗口（装不装得下）、档案（存不存得住）。这三件事恰好是本地模型跑长任务时最容易崩的地方 —— 也是原来的名字 `cloudbridge` 说不出来的部分：桥只负责连通，不负责节制。

---

## 一、它解决什么问题

把本地模型当日常主力、云端当外援，这个想法本身不难。难的是三件事：

**1. 谁来决定「该不该花钱」。**

网关代决策（按请求文本打分、分类、改投）便宜可控，但判断依据是**请求文本**，不是**模型的实际把握** —— 它觉得难的可能模型一秒就做完了，它觉得简单的可能模型根本不会。

所以这里选了另一条路：往工具列表里塞一个 `consult_cloud`，让模型自己申请，但**护栏放在网关里**，不放在提示词里。模型可以判断"这件事我拿不准"，但"能不能花这次钱"由网关数着。

**2. 本地窗口远小于云端，而且长度不固定。**

云端普遍 1M+，本地可能 64K / 128K / 256K，还受 `--parallel` 影响（`-c 131072 --parallel 4` 实际每槽只有 32768）。

更麻烦的是：llama.cpp 开 `--context-shift` 会**从位置 0 开始丢**，system prompt 和工具定义最先阵亡；Ollama 从头截断只写服务端日志；两者的 HTTP 状态码都是 **200**。也就是说，超窗不报错，只是模型忽然变笨。

**3. 云端输出很长，回灌一次吃掉本地窗口的 12%。**

一次 16K 字符的交付物 ≈ 11K tokens，作为 `role=tool` 永久驻留。同一轮问两次，窗口就没了。

这三件事，加上「跑三个月磁盘会不会被写满」，构成了这个项目的全部内容。

---

## 二、三张图

### 2.1 部署拓扑

![部署拓扑](docs/topology.svg)

读图要点：

- 智能体只看得到一个端点、一把 `st-` 开头的 key，不知道后面有几个模型。
- 走哪条路由由网关内部决定：**默认本地**，只有模型主动调用 `consult_cloud` 才出网。
- 云端拿到的只有委托单 + `refs` 展开的文件，**看不到完整对话历史**。
- 三处本地状态：`artifacts/`（交付物全文）、`state/`（配额与估算系数）、refs 白名单内的文件。

### 2.2 模块结构

![模块结构](docs/structure.svg)

三层，全部在同一进程里：

| 层 | 模块 | 职责 |
|---|---|---|
| 决策 | `guard.py` / `prompts.py` / `keys.py` / `refs.py` | 该不该放行、委托单够不够格、这把 key 是谁、这个文件能不能发 |
| 上下文 | `context.py` / `spine.py` / `artifacts.py` | 量窗口、裁剪、留骨架、落盘回收 |
| 传输 | `upstream.py` / `cloud.py` / `responses.py` | 两个上游客户端 + 协议互转 |

不拆 MCP 的理由见 `ARCHITECTURE.md`：工具循环必须在网关侧，因为只有网关同时知道「客户端发了多少」和「本地模型真能装多少」。这个信息差是整套方案的前提，MCP 拿不到。

### 2.3 请求流程

![请求流程](docs/flow.svg)

读图要点：

- **实线**是正常路径，**虚线**是不花钱的路径（裁剪、退回、被闸拦下）。
- 裁剪发生在**循环内部**，每一轮发请求前都判一次 —— 交付物和工具结果是在循环中途长出来的，只在入口判一次，第四轮就会被顶爆。
- 云端调用只是循环里的一个可选步骤。模型不调用，这一轮就结束了。

> 三张图由 `scripts/gen_diagrams.py` 生成。改了模块或流程就重跑，不会出现「图比代码旧」。生成器自带三条自检：文字塞不进框、元素出画布、箭头穿过方框。

---

## 三、三个关键设计

### 3.1 委托时机：按可判的事实，不按"复杂"

最初的判据是「只有任务确实复杂、且本地尝试后仍拿不准时才用」。问题在于 **"复杂"是主观词**，模型无法据此决策，最后变成凭感觉调用。禁止项里还有一条「你本来就知道答案只是需要打字」—— 模型不可能对自己做出这种判断。

改成四个它真能回答的问题：

| 判据 | 含义 |
|---|---|
| 前提是否足够下判断？ | 不够就先去查，别拿半截信息去问 |
| 能否当场写出带失败判据的步骤？ | 能，就自己写 |
| 失败的代价是否不可逆？ | 数据迁移 / 批量删除 / 线上变更 —— **必须问**，哪怕有把握 |
| 是否已经实际试过并失败？ | 没试过先试，带着真实报错去问 |

再加两道机制：

- **质量闸**：`ask` 为空或超 400 字、用了五行格式但缺 `goal`、`diagnosis`/`review` 的 `tried` 是 `none`、纯散文又不足 40 字 —— 退回让模型补，**不计数、不花钱**。同一请求最多退两次，防止原地打转。
- **配额可见**：每轮把「今天还剩几次」写进 system。只在被拒时才说，那一轮已经浪费了。

判据的尺度刻意放宽：格式不规范但内容够的**放行**。云端读散文也能干活，多退一轮纯属浪费本地时间。

**注入成本按 token 卡上限。** 这套网关包在智能体自己的提示词外面，每一分注入都是净增加，所以每轮固定注入控制在 **531 tokens**：

| 组成 | 开销 |
|---|---|
| system 定位（一句心态校准） | 21 tok |
| 配额提示（每轮变） | 12 tok |
| 工具定义（description 222 + 参数 schema） | 498 tok |

`tests/test_prompts.py` 里有硬上限断言。精简前是 804 —— 砍掉的都是重复（system 与工具描述各讲一遍定位、五行格式在两个地方各写一遍）和解释自己结构的元话语，判据一条没少。

### 3.2 上下文治理：本地窗口才是硬瓶颈

四件事，按发生顺序：

**探真实窗口。** `/props` → `/v1/models` → `/api/v0/models` → 配置 → 保守值，四条路依次尝试，并且读 `total_slots`（`--parallel` 会切分 `n_ctx`）。

**精确计数。** 字符启发式在代码和 JSON 上会低估 25%–40%（`"1234567890"` 就是 10 个 token）。所以优先用上游 `/tokenize` 拿精确值，估算只做兜底，并用每次返回的 `usage.prompt_tokens` 做 EWMA 自校准。

**分级裁剪。** 按代价从低到高，前四级不调模型（第 3 级会落盘，但不产生推理）：

| 级 | 动什么 | 可恢复 |
|---|---|---|
| 1 | 历史思维链 | 是（当轮已用完） |
| 2 | 图片换占位 | 否 |
| 3 | 旧工具输出 → 头尾 + 错误行，全文落盘 | 是（读文件） |
| 4 | 整组丢最早的 `tool_call` + 结果 | 是（落盘） |
| 5 | 硬截断 | 否 |
| 6 | 结构修复（删孤儿 tool、摘悬空 `tool_calls`） | — |

第 6 级是补上去的：硬截断按位置删、不认配对，可能删掉带 `tool_calls` 的 assistant 却留下它的 tool 结果。llama.cpp 能忍，vLLM / 严格 Qwen 模板直接 **400**。

同样补上的还有「首条 user 必须保住」—— 那是任务目标本身。之前只保护「最后一条 user」，硬截断会把目标整句删掉，模型只能从后面某段测试日志反推自己要干什么。这条缺陷实测复现过，现在有回归用例盯着。

**任务骨架。** 裁到 10% 之后，模型手里只剩一句「前面有 226 条被压缩」。它知道有缺口，但不知道缺口里是什么，于是要么重试已经否决的方案，要么用「根据我们之前的讨论」凭空编。所以裁掉大量历史时，网关会把那片区域交给本地模型提炼成六行：

```
goal: 把这套服务的配额从按次改成按 token 计量，并补测试。
constraints: key 格式不能变
facts: prefill 实测 1305 tok/s；单槽 n_ctx=131072
rejected: Redis 计数（运维不批）
current: 已改完 guard.py，测试未跑
next: 补 test_guard.py 的计量用例
```

最值钱的是 `rejected` 和 `facts`：前者丢了会重试已知失败的路径，后者丢了会重新量一遍。这两行纯机械摘要产不出来，得判断哪句是结论、哪句是过程 —— 所以这里真的要调一次模型，代价实测 7–13 秒，一个请求最多一次。

### 3.3 交付物存档与回收

超过 1500 字的交付物**先落盘，再只回灌约 500 tokens 的摘要**（id + 绝对路径 + 标题目录 + 开头 800 字）。约 500 tokens 换掉 3 万 tokens。

关键不在摘要，在**路径落在智能体自己的文件系统里** —— 它有读文件工具，要细节时自己去翻。

长期使用的三个约束：

- **统一抬头**：每个文件开头是 `kind / turn / created / chars`，人翻得懂、脚本扫得动。
- **路径只由日期 + 类型 + 内容 hash 组成**：轮次只进抬头。否则同一份内容在多轮各存一份。
- **批量 + 回收**：一次裁剪压掉上百条工具输出，逐条落盘会写出 113 个文件 —— 合并成一个、指针带行锚点后是 **2 个**，体积不变。回收按**目录名里的日期**判断（不是 mtime，否则天天追加的旧目录永远删不掉）。

---

## 四、快速开始

```bash
git clone https://github.com/liuda1999/steward.git
cd steward

# 1. 装依赖（Python 3.11+）
pip install -r requirements.txt

# 2. 初始化：先配本地模型，再配云端，最后签发一把项目 key
python -m steward init

# 3. 启动
python -m steward serve
```

没有本地模型也能先跑起来看个究竟——`init` 里把两个 `base_url` 都填任意 OpenAI 兼容端点即可，`python -m steward test` 会告诉你通不通。

启动后：

```bash
python -m steward test          # 测两个槽位的连通性
python -m steward ctx           # 看本地模型的真实窗口与输入预算
```

客户端这边只需要两行：

```python
base_url = "http://127.0.0.1:27418/v1"
api_key  = "st-..."                 # init 时打印过一次，之后只存 hash
```

**key 只在签发时显示一次。** 丢了就重新签发、吊销旧的 —— 网关只存 hash，找不回来。

改造后第一次连客户端，建议先跑一遍 `python -m steward ctx`：它会把真实窗口、输入预算、计数方式全打出来。很多"模型忽然变笨"的问题，从这三个数字就能看出来。

### 4.1 端口

默认 **27418**。选它有三条理由，改这个值之前先看一眼：

| 规矩 | 为什么 |
|---|---|
| 不用 80 / 443 / 8080 / 8000 / 3000 | 这些端口本机几乎总有别的服务占着，撞上了报错只有一句 `address in use` |
| 不用 12345 / 56789 这类连续数字 | 撞车时看端口号根本分不清是谁的 |
| 落在 1024–49151 之间 | 低于 1024 要 root；高于 49151 是 Windows 的临时端口段，会被本机随机连接抢走 |

配错了不会拦住你，但会提醒：

```
[配置] 注意  server.port=8080 是常见端口，本机上很容易被别的服务先占；建议换成 27418 一类不冲突的值
```

真撞上了也不会只丢一句 `shutdown complete`：

```
[端口] 127.0.0.1:27418 已被占用（PID 13028，tasklist /FI "PID eq 13028" 可查进程名）。
[端口] 停掉它，或用 --port 换一个（默认 27418，也可以改 config.yaml 的 server.port）。
```

临时换端口不用改配置：`python -m steward serve --port 27xxx`。

### 4.2 两个槽位，可以只填一个

`base` 和 `advanced` 是两个平等的模型槽位，不按"本地/云端"分。

| 配置 | 结果 |
|---|---|
| 两个都填 | 完整能力。日常走 base，难题由模型申请后走 advanced |
| 只填 base | **合法**。纯本地模式：不注入 `consult_cloud`，每轮少发约 500 tokens 的工具定义 |
| 只填 advanced | 起不来。没有基础模型就没有任何模型能接活 |
| 填了一半（有地址没模型名） | 按空槽位处理，并在启动时提醒 |

空槽位是被**特意设计**成合法的，因为"我只有一台本地模型"是最常见的情况。此时网关不会塞给模型一个永远会拒绝它的工具——那 500 tokens 是每轮都要重复付的。

```bash
python -m steward serve --check -c config.yaml
# [配置] 注意  进阶槽位空着（缺 advanced.base_url）：consult_cloud 不注入…每轮也省下约 500 tokens
# [配置] OK  基础=http://127.0.0.1:11434/v1 model=qwen3 protocol=chat
# [配置]     进阶=空槽位（不注入 consult_cloud）
```

`GET /health` 里也有这个状态：`advanced.configured: false`。

---

## 五、目录结构

```
.
├── steward/                   网关本体（14 个模块）
│   ├── config.py              配置解析 + validate
│   ├── keys.py                项目 key（只存 hash）
│   ├── prompts.py             工具描述 · 委托质量闸 · 骨架/交付物模板
│   ├── guard.py               四道闸 + 去重 + 配额持久化
│   ├── upstream.py            基础模型客户端（404 自愈、窗口探测）
│   ├── cloud.py               进阶模型客户端（交付物外置）
│   ├── responses.py           Responses ↔ Chat 互转
│   ├── context.py             窗口探测 / 精确计数 / 分级裁剪
│   ├── spine.py               任务骨架
│   ├── artifacts.py           落盘 · 批量 · 回收
│   ├── refs.py                本地文件引用的白名单与审计
│   ├── server.py              FastAPI 路由、鉴权、工具循环
│   ├── __main__.py            serve / init / test / key / ctx / refs / artifacts
│   └── README.md              实现细节与排错手册
├── docs/                        三张图，由 scripts/gen_diagrams.py 生成
├── scripts/                     联调、真机验证、图生成、推送
│   └── publish.py               推 GitHub：git push 优先，走不通退回 Git Data API
├── tests/                       345 个用例
│   └── fixtures/                审计用的 363 条历史样本
├── legacy/                      两代前身的存档，不推荐使用
├── ARCHITECTURE.md              架构决策与被否掉的方案
├── CONTEXT-DESIGN.md            上下文治理的设计论证
├── HANDOFF-PROTOCOL.md          委托单与交付物格式的原始定义
│
├── state/                       运行时状态（不进仓库）
└── .steward/artifacts/          交付物与工具输出存档（不进仓库，可用 CLI 回收）
```

---

## 六、常用命令

| 命令 | 用途 |
|---|---|
| `python -m steward serve` | 启动网关 |
| `python -m steward init` | 交互式配置两个槽位并签发首把 key |
| `python -m steward test` | 测两个端点连通性 |
| `python -m steward ctx` | 看真实窗口 / 预算 / 计数方式 |
| `python -m steward key new --label <名字>` | 签发新 key |
| `python -m steward key list` / `revoke` | 列出 / 吊销 |
| `python -m steward refs <路径…>` | 查某个路径会不会被 refs 放行 |
| `python -m steward artifacts` | 看存档占用，`--clean` 回收 |
| `python scripts/publish.py --doctor` | 诊断推送环境（证书 / 连通性 / 令牌 / 预演） |
| `python scripts/publish.py -m "说明"` | 提交并推送，不挑环境 |
| `python -m steward --version` | 看版本号 |
| `python scripts/publish.py --release vX.Y.Z --notes-file …` | 打 tag 并发布 |

### 6.1 推送为什么需要脚本

`git push` 在有代理的环境里经常过不去，而且报错会把人引向错误方向：

| 报错 | 真实原因 |
|---|---|
| `CRYPT_E_NO_REVOCATION_CHECK` | 代理做了 TLS 中间人，git 查不到吊销列表 |
| `unable to get local issuer certificate` | 同一件事，openssl 后端的说法 |
| `CONNECT tunnel failed 502` | 代理只放行 api.github.com，不放行 github.com:443 |
| `Authentication failed` | 用的是 `Authorization: bearer`，那只对 API 有效，git 要 URL 里的 `x-access-token:` |

`scripts/publish.py` 把这些都封在里面：运行时现找一份**真能连通**的 CA（不写死任何路径，也不改 `.git/config`），git 走不通就自动退回 Git Data API——两条路的产物等价，后者还能用树哈希对账。

```bash
STEWARD_GH_TOKEN=xxx python scripts/publish.py --doctor   # 先看环境
STEWARD_GH_TOKEN=xxx python scripts/publish.py -m "说明"  # 提交并推
```

令牌只从环境变量或 `--token` 读，不落盘。
| `python -m steward artifacts` | 看存档占用，`--clean` 按策略回收 |

---

## 七、配置速查

完整注释版见 `steward/config.yaml`。

```yaml
base:          # 本地，日常主力。任何 OpenAI 兼容端点
  base_url: http://127.0.0.1:8080/v1
  model: /models/your-model.gguf
  thinking: "off"           # 推理型模型关掉，否则工具调用迟迟不吐

advanced:      # 云端，只在 consult_cloud 时用
  base_url: https://your-provider.example.com/v1
  model: your-advanced-model
  max_tokens: 4096

guard:
  max_calls_per_turn: 2     # 单请求上限
  daily_limit: 5            # 每日总量（最终防线）
  min_interval_s: 30
  dedupe: true

context:
  ctx_limit: 0              # 0 = 自动探测
  target_ratio: 0.65        # 一次裁到预算的多少
  min_clear_tokens: 8000    # 一次至少清这么多，否则不值得让 KV 缓存失效
  spine: true               # 裁剪后生成六行骨架

artifacts:
  dir: .steward/artifacts
  max_age_days: 30
  max_total_mb: 512

refs:          # 让云端读本地文件。默认关闭 —— 这是唯一主动外发的通道
  enabled: false
  roots: []                # 开了却不配白名单，直接拒绝启动
```

**refs 是唯一会把本地内容发出去的通道。** 默认关闭、根目录白名单、25 条黑名单、单文件 64 KB / 单次 12 万字符上限、每次展开一行审计日志。默认关是有意的：这个功能的正确用法需要你确认过 `roots` 写对了。

---

## 八、测试

```bash
python -m pytest tests -q                    # 345 个
python -m pytest tests/test_audit.py -s -q   # 只看对照表
python scripts/ctx_live.py --fit 130000 --spine   # 真机验一次裁剪 + 骨架
```

三类值得单独说：

**`test_audit.py`（23 个）不是"再跑一遍单元测试"**，而是把修复前的代码原样搬进测试当对照组，同一份数据跑两遍。三组对照 + 三条元验证（把旧实现喂给判定，必须判它不合格），确保审计不是"写了但测不出"。

**`test_reliability.py`（20 个）** 专门打失败路径：10 个并发抢 3 个额度恰好只过 3 个、畸形 token、上游抛异常、`usage` 字段给了个 list、客户端自带同名工具。三处修复都做过回归注入验证 —— 把修复改回旧行为，测试必须变红。

**`test_prompts.py`** 把注入成本写成硬断言：每轮固定注入 ≤560 tokens、工具描述 ≤240，以及"五行格式只说一遍""system 不复述工具"这类不重复规则。

真机实测（27B / `n_ctx=131072` / 单槽 / prefill 1305 tok/s）：

```
363 条消息 / 159,239 tokens
  → 裁剪到 68,926，一次 prefill，HTTP 200，62.1s
  → 上游实收 61,126（预算 106,803）
```

---

## 九、文档索引

**当前有效**

| 文档 | 内容 |
|---|---|
| `steward/README.md` | 实现细节：每个模块怎么工作、每个参数什么含义、排错对照表 |
| `ARCHITECTURE.md` | 为什么这样设计：决策过程与被否掉的方案 |
| `CONTEXT-DESIGN.md` | 窗口问题的完整分析与 L0–L5 设计论证，含 P0–P3 落地记录 |
| `HANDOFF-PROTOCOL.md` | BRIEF 委托单与四段式交付物的原始定义，`prompts.py` 是它的简化实现 |

**`legacy/`：两代前身的存档**

这个项目有过两个前身，都不是现在这个东西：

1. **第一代是接第三方 MCP**（`cli-bridge-mcp`），工具叫 `ask_mycloud` / `ask_cascade`，配额靠 `DAILY_LIMIT` 环境变量。问题是护栏只能数次数，判断不了"这活该不该上云"，而且工具定义常驻挤占上下文。见 `legacy/MCP-SETUP.md`、`legacy/EXTERNAL-AI-RULES.md`。
2. **第二代自建网关**，叫 `cloudbridge`。名字只说了"连着云端"，但实际做的是预算、窗口、存档三件管家活。功能长到已经和"桥"没关系，于是改名为 `steward`。

`legacy/ESCALATION-RULES.md` 的 L0/L1/L2 分层判据仍有参考价值（已吸收进 `prompts.py` 的调用条件）；`legacy/VENDOR-SURVEY.md` 是选型调研存档，结论已不用，但「网关代决策 vs 模型自决」的分野分析仍成立。

这些文件保留是为了说明**为什么是现在这个样子**，不代表推荐用法。

---

## 十、已知取舍

**内部非流式。** 首字延迟 = 整轮耗时。换来的是不会出现「答案吐一半、突然冒出工具调用、重来一遍」。

**只做单轮对话内的循环。** 网关无状态，不记跨请求上下文；历史由客户端维护。所以窗口治理是按单次请求算的 —— 客户端如果把历史撑爆了，网关只能在收进来的那一刻裁。

**骨架是摘要，不是备份。** 丢掉的报错原文、代码片段不会回来，但那些多半已经落盘。真正的风险是模型**编造**缺失部分，所以骨架末尾固定写一句「缺细节时用工具重新确认」。

**裁剪要付前缀重算的代价。** 改历史中段 = llama.cpp 前缀缓存失效。所以是「少裁但裁到位」，用 `min_clear_tokens` 保证每次裁剪都值回票价。如果你的 prefill 远快于 1305 tok/s，可以把 `target_ratio` 调到 0.8 附近，裁得更细也更勤。

**静默截断只能事后发现。** 上游 200、内容看着正常，只有 `prompt_tokens` 明显小于估算才露馅。检测到之后网关会永久收缩预算，但那一轮已经丢了信息。

**存档不加密。** 落盘的是完整交付物和工具原始输出，可能含源码片段。目录权限跟着网关进程的 umask 走。

**Windows 上 `chmod 0600` 是空操作**（`keys.json` 走默认 ACL），但文件里只有 hash，拿到也还原不出 key。**`state/usage.json` 假定单进程**，两个实例指向同一文件会互相覆盖计数。
