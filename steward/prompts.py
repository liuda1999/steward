# SPDX-License-Identifier: Apache-2.0
"""注入文本与工具定义。

设计取舍：规则写进工具 description，不写进 system prompt。
system 只留一句指向，避免每轮都为一个长段规则付 token。
"""

from __future__ import annotations

CONSULT_NAME = "consult_cloud"

DEFAULT_SYSTEM_TEXT = (
    "把它当外部专家：给你的是没想过的角度，不是你不愿意写的代码。"
)

DEFAULT_TOOL_DESCRIPTION = """\
请一位外部专家出方案。它看不到你的仓库，只拿到你写的 brief。

要不要问，看这四个问题：
1. 前提够不够下判断？不够先去查 —— 你少说一句，它就多猜一句。
2. 能不能当场写出带失败判据的执行步骤？能就自己写。
3. 做错的代价是否不可逆（数据迁移、批量删除、线上变更、对外承诺）？
   是就必问，哪怕有把握。
4. 是否已实际试过并失败？没试过先试，带真实报错去问。

不要问：常规重构、写测试、补文档、重复改动；报错已定位只差动手；
查文档、翻译、格式转换。

tried 必须有料：diagnosis 带报错原文，review 带已验证的结果（dry-run、检查项）。
写 none 会被网关退回。密钥、口令、内网地址、客户数据不要写进 brief。
拿回来是四段式（结论/步骤/风险/待补充），在本地拆解执行，不要照抄。\
"""

# 质量闸：这些情况不值得花一次云端额度，退回让模型补
PRECHECK_NOTICE = """\
[网关退回] 这次委托单不完整，没有发出（未消耗额度）。缺：{missing}

补齐后重新调用 consult_cloud。别把这段提示当成新信息写进 brief。\
"""

# 给本地模型看的自检清单，只在要生成骨架时用
SPINE_PROMPT = """\
你在压缩一段很长的对话历史。下面是任务目标、已有的骨架、以及本次被丢弃的内容。
只输出六行，每行一个字段，不要别的字：

goal: 当前任务要达成什么（照抄上面给的目标原话，不要改写；没给就写 none）
constraints: 不可违反的约束，分号隔开（技术栈、不可动的部分、硬性要求）
facts: 已经确认的事实和结论，分号隔开（含实测数字、版本、路径）
rejected: 已经否决的方案及原因，分号隔开
current: 现在做到哪一步了（看被丢弃内容的**末尾**，不要看开头）
next: 下一步该做什么（一句话）

规则：
- 只写材料里真实出现过的信息，不要推测、不要补充。
- rejected 最重要：已经试过并失败的方案必须留下原因，否则会重试一遍。
- facts 只写结论和数字，不要写「读了哪些文件」这类过程描述。
- 每条尽量短，整段不超过 800 tokens。
- 某行确实没有内容就写 none。\
"""

DEFAULT_FORCE_FINISH_TEXT = (
    "进阶模型咨询轮次已用尽。不要再调用任何工具，直接基于现有信息给出最终答复；"
    "如果有信息缺口，明确写出缺什么、需要人工确认什么。"
)

CONSULT_SYSTEM_PROMPT = """\
你是外部技术顾问，只输出交付文档。

硬性要求：
- 不复述委托单，不写寒暄，不输出思考过程。
- 只给方案，不要输出完整代码文件。
- 每条执行步骤必须带失败判据，否则视为无效建议。
- 拿不到的信息写进「待本地补充」，不要猜。
"""

BRIEF_TEMPLATE = """\
## 委托类型
{kind}

## 委托单
{brief}

## 核心问题
{ask}

## 输出格式（严格四段，缺段无效）

## 1. 结论
不超过 3 句，直接给答案或选型。

## 2. 执行步骤
1. 动作：
   输入：
   输出：
   失败判据：
2. ...

## 3. 风险与不确定项
- 什么可能不成立 + 触发条件

## 4. 待本地补充
- 需要本地查证的信息
"""

DENIAL_TEMPLATES = {
    "turn_limit": (
        "[网关拦截] 本次请求内进阶模型调用已达上限（{used}/{limit}）。不要重试。"
        "基于已经拿到的交付物继续完成；信息不足就在答复里写明缺口。"
    ),
    "daily_limit": (
        "[网关拦截] 今日进阶模型额度已用尽（{used}/{limit}）。不要重试，也不要换工具绕行。"
        "用本地能力完成，必要时在答复里说明需要人工决策。"
    ),
    "cooldown": (
        "[网关拦截] 距上次进阶模型调用不足 {gap} 秒。不要重试。"
        "先继续本地工作，确有新问题时再考虑咨询。"
    ),
    "duplicate": (
        "[网关拦截] 这个问题刚刚已经问过，交付物就在上文。不要重复提问。"
        "要么基于已有答复继续执行，要么补充真正的新信息后重新组织问题。"
    ),
    "disabled": "[网关拦截] 进阶模型当前不可用。用本地能力完成。",
    "error": (
        "[网关失败] 进阶模型调用未成功（{reason}）。不要重试。"
        "用本地能力继续，必要时在答复里说明这一节未获得外部输入。"
    ),
}

CONSULT_TOOL = {
    "type": "function",
    "function": {
        "name": CONSULT_NAME,
        "description": DEFAULT_TOOL_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["design", "diagnosis", "review", "decision"],
                    "description": "design=方案设计, diagnosis=疑难根因, review=不可逆操作评审, decision=选型取舍",
                },
                "ask": {
                    "type": "string",
                    "description": "唯一的那个核心问题，一句话。多个问题拆成多次委托。",
                },
                "brief": {
                    "type": "string",
                    "description": (
                        "五行，每行一段，没有内容的写 none：\n"
                        "goal: 一句话目标\n"
                        "given: 已确认的事实\n"
                        "tried: 已试过/已验证什么，结果如何（diagnosis 要报错原文，review 要 dry-run 或检查结果）\n"
                        "blocked: 现在卡在哪\n"
                        "constraints: 技术栈版本、不可动的部分、规模限制"
                    ),
                },
            },
            "required": ["kind", "ask", "brief"],
        },
    },
}


REFS_PARAM = {
    "type": "array",
    "description": (
        "要进阶模型直接读的本地文件。用它代替把代码抄进 brief —— 进阶模型窗口很大，"
        "给它真实源码比你的转述准得多，而且不占你自己的上下文。\n"
        "每个元素三选一：\n"
        '  {"path": "src/auth.py"}                 读整个文件\n'
        '  {"path": "src/auth.py", "range": "80-220"}  只读这几行（含两端）\n'
        '  {"path": "src/auth.py", "grep": "def login"} 只读匹配行及上下几行\n'
        '  {"glob": "src/**/*.py", "max_files": 8}      批量读，慎用\n'
        "只引你确认存在、且确实和问题相关的文件。引不到会告诉你原因。"
    ),
    "items": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作目录或绝对的路径"},
            "glob": {"type": "string", "description": "通配符，和 path 二选一"},
            "range": {"type": "string", "description": '行区间，如 "80-220"'},
            "grep": {"type": "string", "description": "只取匹配行附近的内容"},
            "max_files": {"type": "integer", "description": "glob 时最多读几个文件"},
        },
    },
}


def build_consult_tool(description: str | None = None, refs_enabled: bool = False) -> dict:
    params = {
        **CONSULT_TOOL["function"]["parameters"],
        "properties": dict(CONSULT_TOOL["function"]["parameters"]["properties"]),
    }
    if refs_enabled:
        # 没开就别说有这个参数：广告一个必然被拒的能力，等于浪费一次工具调用
        params["properties"]["refs"] = REFS_PARAM
    tool = {
        "type": "function",
        "function": {**CONSULT_TOOL["function"], "parameters": params},
    }
    if description:
        tool["function"]["description"] = description
    return tool


def render_brief(kind: str, ask: str, brief: str, max_chars: int) -> str:
    brief = (brief or "").strip()
    if len(brief) > max_chars:
        brief = brief[:max_chars] + "\n[委托单已截断]"
    return BRIEF_TEMPLATE.format(kind=kind, ask=ask.strip(), brief=brief)


# ---------------------------------------------------------------- 质量闸

_BRIEF_KEYS = ("goal", "given", "tried", "blocked", "constraints")
_CN_KEYS = {"目标": "goal", "事实": "given", "已试": "tried", "卡点": "blocked", "约束": "constraints"}
# 这两类必须有真实尝试记录，否则等于让专家凭空诊断
_NEED_TRIED = {"diagnosis", "review"}
_EMPTY_VALUES = {"", "none", "无", "n/a", "na", "-", "null", "没有", "暂无"}


def _parse(brief: str) -> tuple[dict[str, str], set[str]]:
    """拆五行 brief。第二个返回值是**原文里真正出现过的键**。

    这个区分很关键：自由文本会被归到 given，但那是兜底，不是模型写了
    `given:`。拿它去判断“用户是否用了键格式”会把散文误判成结构化。
    """
    out: dict[str, str] = {}
    seen: set[str] = set()
    extra: list[str] = []
    for line in (brief or "").splitlines():
        raw = line.strip()
        if not raw:
            continue
        head, sep, tail = raw.partition(":")
        key = head.strip().lower()
        if sep and key in _BRIEF_KEYS:
            out[key] = tail.strip()
            seen.add(key)
        elif sep and key in _CN_KEYS:
            mapped = _CN_KEYS[key]
            out[mapped] = tail.strip()
            seen.add(mapped)
        else:
            extra.append(raw)
    if extra:
        out["given"] = (out.get("given", "") + "；" + " ".join(extra)).strip("；")
    return out, seen


def parse_brief(brief: str) -> dict[str, str]:
    """把 brief 拆成字典。认不出的行归到 given，不丢弃内容。"""
    return _parse(brief)[0]


def _is_empty(value: str | None) -> bool:
    return (value or "").strip().lower() in _EMPTY_VALUES


def precheck(kind: str, ask: str, brief: str, max_ask_chars: int = 400, min_brief_chars: int = 40) -> str:
    """返回空串表示放行，否则返回退回原因。

    为什么值得单独做闸：一份烂 brief 换来一份错方案，比不调用更糟——
    模型会把错方案当真执行，而且这次额度已经花了。

    尺度：只在"确实没法回答"时拦。格式不规范但内容够的放行，
    因为云端读散文也能干活，多退一轮纯属浪费本地时间。
    """
    text = (brief or "").strip()
    fields, seen = _parse(text)
    missing: list[str] = []

    core = (ask or "").strip()
    if not core:
        missing.append("ask（核心问题）")
    elif len(core) > max_ask_chars:
        missing.append(f"ask 过长（{len(core)} 字，上限 {max_ask_chars}），拆成一次只问一个")

    if seen:
        if _is_empty(fields.get("goal")):
            missing.append("goal（一句话目标）")
    elif len(text) < min_brief_chars:
        # 没按格式写可以，但要写够。9 个字的委托单换不回有用的方案。
        missing.append(f"brief 太短（{len(text)} 字），至少说清目标、已确认的事实、卡在哪")

    if kind in _NEED_TRIED and _is_empty(fields.get("tried")):
        missing.append(
            "tried（已试过/已验证什么）。"
            f"{kind} 类必须带上实际执行结果，否则专家只能猜"
        )

    return PRECHECK_NOTICE.format(missing="；".join(missing)) if missing else ""


def render_quota_note(remaining: int | None, per_turn: int) -> str:
    """把剩余额度写进 system，让模型自己掂量，而不是等被拦才知道。

    只报数，不复述判据 —— 判据在工具描述里，这里再说一遍是重复，
    而且「按上面四条判断」这种指代在 system 里根本找不到"上面"。
    """
    if per_turn <= 0:
        return ""
    if remaining is None:
        return f"[配额] 本轮最多 {per_turn} 次，今日不限量。"
    if remaining <= 0:
        return "[配额] 今日额度已用尽，consult_cloud 会直接拒绝。不要尝试，也不要换参数绕行。"
    note = f"[配额] 本轮最多 {per_turn} 次，今日还剩 {remaining} 次。"
    if remaining <= per_turn:
        # 只在紧的时候给"合并提问"的建议，宽裕时不必每轮重复
        return note + "剩得少，攒成一个更值钱的问。"
    return note


def render_denial(code: str, **kwargs) -> str:
    template = DENIAL_TEMPLATES.get(code, DENIAL_TEMPLATES["disabled"])
    return template.format(**kwargs)
