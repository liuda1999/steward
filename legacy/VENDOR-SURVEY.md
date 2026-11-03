# 选型复查：本地模型 API 代理（2026-11）

> 上一轮用词偏差：搜的是 gateway / router / proxy / 服务端工具循环，命中的全是基础设施类项目。
> 换词后（cascade routing、cheap-first、Mixture-of-Models、local model gateway）才挖到 2026 真正热的那一波。
> 本文档替代 `MCP-SETUP.md` 的选型章节，其余文件保留。

---

## 一、结论

**首选 `musistudio/claude-code-router`（CCR）。**

理由只有一条，但它足够硬：**在全部候选里，只有 CCR 同时具备"网关侧成本路由"和"模型侧工具自决"两条链路**，而这正好是你需求的两半。

- 你要"省钱的默认行为" → 走它的路由规则（简单请求不出本地）
- 你要"本地模型自己判断该不该请外援" → 走它的 ToolHub / Fusion（注入工具 + 注入强制指令）

其余热门项目都只有前者。

---

## 二、梯队（按贴合度，不按 star）

| 项目 | Stars | 最近提交 | 协议 | 定位 | 关键机制 | 贴合 |
|---|---|---|---|---|---|---|
| **claude-code-router** `musistudio/claude-code-router` | **37.9k** | 2026-10 | MIT | 本地模型网关 + 控制平面 | 路由规则 / 请求改写 / **ToolHub 元工具** / **Fusion 组合模型** / wrapper+gateway 插件 | ★★★★★ |
| **cascadeflow** `lemony-ai/cascadeflow` | ~4k | 2026-08 | MIT | 级联运行时 + 兼容网关 | drafter→verifier 投机级联 / 完整工具循环 / 5 档复杂度 | ★★★★ |
| **semantic-router** `vllm-project/semantic-router` | 6.4k | **2026-11-02** | Apache-2.0 | Mixture-of-Models 决策层 | Signal（难度/风险/意图/偏好）→ Recipe（cascade / 多模型 / 挂 prompts+tools） | ★★★★ |
| **Manifest** `mnfst/llm-gateway` | 7.6k | 2026-10-29 | — | OpenAI 兼容网关 | `/auto` 端点 / 300+ 模型 / 本地 Ollama | ★★★ |
| **bifrost** `maximhq/bifrost` | 9.3k | 活跃 | — | 高性能网关 | 23+ provider / failover / 语义缓存 / 预算 | ★★ |
| **Portkey Gateway** | 13.3k | 活跃 | — | 生产级网关 | 1600+ 模型 / guardrails / 缓存 | ★★ |
| **Maestro** | v0.1 | 新 | MIT | Fugu 开源平替 | cheap-first → verify → escalate | ★★（太早） |

**热点源头**：`Sakana Fugu`（闭源）——"One model orchestrates the world's best LLMs"，Trinity（Thinker / Worker / Verifier）+ Conductor 训练式编排。2026 这波 cheap-first 热潮由它带起，开源侧全是它的平替尝试。

**已排除**：
- `mindsdb/mindshub` 39.8k —— 是 MindsDB 改名后的 Agent 工作台，star 是历史积累，不是路由项目
- `OpenClaw` fallbacks —— 是**故障转移**（本地挂了才切云端），不是**难度路由**，语义不同

---

## 三、关键分野：谁来决策

所有候选可以按"决策发生在哪"分成两类。这个分野决定你的规则文件写在哪。

### A 类：网关代决策

网关看请求 → 打分 / 分类 / 匹配规则 → 直接改投另一个模型。模型全程不知道自己在被路由。

- 代表：semantic-router、cascadeflow、Manifest、bifrost、Maestro
- 优点：便宜（分类在本地跑）、可控、可审计、可复现
- 缺点：判断依据是**请求文本**，不是**模型的实际把握**。模型"其实会做"的活可能被误判上云，反之亦然
- 适合：请求量大、模式稳定、要审计的场景

### B 类：模型自决

网关往工具列表里塞一个"外援工具"，模型自己决定调不调。

- 代表：**CCR（ToolHub / Fusion）**、werkzeugkoppler（actions）、Plano（prompt_targets）
- 优点：判断依据是模型对任务的实际理解，符合你的原意
- 缺点：依赖模型判断质量；每次都要带工具定义，吃 context；模型可能该调不调
- 适合：任务异质、边界说不清、量不大的场景 —— **就是你的场景**

**CCR 是唯一 A+B 都有的。** 这也是它值 37.9k star 的原因：它不是纯路由器，是控制平面。

---

## 四、CCR 的关键证据

不是看 README 猜的，是看实现：

**1. 它真的往 system prompt 注入强制指令。**
`packages/core/src/gateway/claude-code-router-plugin.ts` 里有 `injectClaudeCodeToolHubInstructions()`，注入内容是：

> 凡是涉及外部服务、已安装 MCP 能力、业务 API、订单、优惠券、商店、账户、可用工具的请求，**必须先调用解析工具，即使对话里没提到 ToolHub 也要调**；仅当请求明显是本地代码 / 文件 / shell 工作或简单会话时才允许跳过；拿到 resolve 结果后应调用 invoke 执行，而不是告诉用户"没有这种能力"。

集成测试 `toolhub-mcp-runtime.test.mjs` 还断言了这段描述的强制性关键词。

**这就是"内置提示词"机制，已经在生产跑通。** 你要做的只是把"涉及外部服务"换成"高难度任务"，把 `invoke` 的目标换成你的云端 API。

**2. ToolHub 是元工具模式，不是硬编码工具列表。**
- `tool_hub.resolve`：按当前任务检索可用工具目录
- `tool_hub.invoke`：调用被选中的工具
- 后端支持 stdio / streamable-http / sse 三种 MCP 传输

好处：外援工具不必常驻 context，只在需要时加载。对你的 256K 上下文是加分项。

**3. Fusion 可以造"虚拟模型"。**
基础模型 + 自定义 MCP 工具 / 视觉 / 联网搜索 → 组合成一个新的可选模型。也就是说你可以造一个 `local-with-cloud-consult` 模型，选它就让本地模型带着外援工具跑。

**4. 接入面够宽。**
`127.0.0.1:3456` 一个端点，OpenAI Chat/Responses + Anthropic Messages + Gemini 三协议，官方列了 Claude Code / Codex / Grok CLI / Kimi CLI / Kilo Code / OpenCode / Pi / ZCode / WorkBuddy 及"compatible API clients"。Windows 有 exe。

**5. 有成本观测。**
每条请求记录 provider / model / 延迟 / token / 成本估算，路由决策可回查。这是"省钱"能不能验证的前提。

---

## 五、为什么不选另外两个更像的

**werkzeugkoppler** —— 机制最纯（`actions` = 模型触发、网关执行 shell、stdout 回灌），但它只测过 Linux、8 个月没提交、`/v1/chat/completions` 之外全不支持。在 Windows 上跑通是赌博。**保留为备选**，如果 CCR 的插件机制不够用，可以抄它的 action 设计。

**Plano / archgw** —— 生产级、原生支持 Ollama，但 `prompt_targets` 这个特性 2026-06 起弃用。等于买一个正在拆的房子。**保留为参考**，它的 prompt_target 设计值得抄。

**CascadeGateway** —— 4 star、单人。目标对（本地兜 85-95%），但决策在网关规则。**放弃**。

---

## 六、落地路径（三步）

1. **装 CCR，先只做 A 类**：本地 Ollama 当 default，云端当 `think` / 长上下文档。零代码，先验证端点和宿主接得上。
2. **写一个 MCP server，只暴露一个工具**：`consult_cloud(question, context, constraints)`，内部调你的云 API，返回结构化交付物（沿用 `HANDOFF-PROTOCOL.md` 的四段格式）。
3. **把它挂进 Fusion 或 ToolHub，改注入的提示词**：把"涉及外部服务必须先 resolve"改成你 `ESCALATION-RULES.md` 里的白名单判据。CCR 支持 wrapper plugin，改注入文本不用 fork。

第 2 步是唯一要写代码的地方，约 100 行 Python。

---

## 七、保留的旧候选

| 项目 | 保留理由 |
|---|---|
| werkzeugkoppler | `actions` 的"模型触发 + stdout 回灌"是最干净的参考实现 |
| Plano / archgw | `prompt_targets` 的设计思路值得抄，但别当底座 |
| cli-bridge-mcp | 如果最后不走 CCR，MCP 路线仍成立；lane 抽象和配额护栏设计可直接复用 |

---

## 八、待确认

1. 宿主智能体具体是哪个？（决定 CCR 的 Agent Profile 怎么配；如果不是列表里那 10 个，走 "compatible API clients" 也能接）
2. 云 API 是哪家端点？（决定 MCP server 里填什么）
3. 要不要我直接写第 2 步那个 MCP server？
