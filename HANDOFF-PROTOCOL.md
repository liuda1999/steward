# 云端交接协议

> **适用范围：仅 L2 正式委托。** L1 云端速判是一句话问答，不走这套模板。
> 分层见 `legacy/ESCALATION-RULES.md`。

## 边界

云端 = 承包专家。只出谋划策，不参与本地事务。
禁：读写文件 / 执行命令 / 改代码 / 假设本地环境 / 给模糊建议。
本地 = 唯一执行者，持有全部上下文，对结果负责。

---

## 上行：BRIEF 委托单

```yaml
type: design | diagnosis | review | decision
goal: 一句话，要达成的终态
given: 已确认事实，只列影响判断的
tried: 已尝试路径 + 各自结果（没试过就写 none）
blocked: 卡点，具体现象
constraints: 硬约束，不可改 / 不可用 / 边界
ask: 唯一核心问题
want: plan | verdict | review | decision
```

### 纪律

- 自包含。云端无记忆，不写"如上所述""接着刚才"。
- 不贴整文件。只贴必要片段 + 路径 + 行号。
- `ask` 只能有一个。多问题拆成多次委托，或本地自己先解掉一部分。
- 不复述代码逻辑、不解释术语、不客套。
- 上限 150 行。超了说明本地没想清楚，先自己收敛。

---

## 下行：交付物

### type=design → `## PLAN`

```yaml
## PLAN
goal: 复述目标，一行
steps:
  - n: 1
    do: 动作
    in: 输入
    out: 产出
    fail_if: 走不通的判据
risks:
  - 风险 → 缓解
open: 需本地确认的，最多 3 条；无则 none
```

### type=diagnosis → `## VERDICT`

```yaml
## VERDICT
cause: 根因，一行
confidence: high | mid | low
evidence: 支撑证据
kill: 一句话证伪方法
fix: 最小修复动作
```

### type=review → `## REVIEW`

```yaml
## REVIEW
verdict: go | no-go | go-with-fixes
blockers:
  - 严重级 | 问题 | @位置 | 改法
notes: 次要建议，最多 5 条
```

### type=decision → `## DECISION`

```yaml
## DECISION
pick: 选中的选项
why: 决定性理由，一行
tradeoff: 放弃了什么
revisit_if: 什么条件下推翻
```

### 纪律

- 字段名固定，不改、不增。
- 每条建议必带 `fail_if` / `kill`，否则无效。
- 禁"可能""也许""建议考虑"。要么给可执行动作，要么写进 `open` 反问。
- 不输出分析过程、不复述委托单、不加客套。

---

## 追问

- 只在同 thread 内，只发增量（新的报错 / 新证据 / 哪条判据不成立）。
- 不重述背景。
- 同一问题最多 2 轮。超了降级：本地方案 + 标记人工介入。

---

## 落地

交付物到本地后：拆解成步骤 → 本地执行 → 按 `fail_if` 判据逐条验证 → 不通过才回云端。
云端方案是设计稿，不是指令。执行权始终在本地。
