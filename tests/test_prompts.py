"""委托时机的判据、质量闸与配额提示。

这一层不做任何计算，但它决定了一次云端调用值不值——模型会不会在最该问的
时候不问、在不该问的时候乱问，全靠这里的措辞。
"""

from __future__ import annotations

import json

import pytest

from steward.context import count_tokens
from steward.prompts import (
    CONSULT_TOOL,
    DEFAULT_SYSTEM_TEXT,
    DEFAULT_TOOL_DESCRIPTION,
    build_consult_tool,
    parse_brief,
    precheck,
    render_quota_note,
)

GOOD_DESIGN = "goal: 选出缓存分层方案\ngiven: 单机 QPS 2k，读多写少\ntried: none\nblocked: 不确定 L2 用什么"
GOOD_DIAG = (
    "goal: 定位 refresh 返回旧 token 的原因\n"
    "given: 只在并发下有\ntried: 单测跑三遍，第三遍必现；日志见 /tmp/r.log"
)


# ---------------------------------------------------------------- 工具描述


def test_tool_description_drops_unjudgeable_clause():
    """“你本来就知道答案只是需要打字”这种判据模型无法自判，只会造成摇摆。"""
    assert "只是需要打字" not in DEFAULT_TOOL_DESCRIPTION.replace("你只是嫌打字麻烦", "")
    assert "本来就是" not in DEFAULT_TOOL_DESCRIPTION


def test_tool_description_states_expert_positioning():
    """定位必须写清：给的是角度，不是替它写代码。"""
    assert "外部专家" in DEFAULT_TOOL_DESCRIPTION
    assert "没想过的角度" in DEFAULT_SYSTEM_TEXT
    assert "不愿意写的代码" in DEFAULT_SYSTEM_TEXT


def test_tool_description_marks_irreversible_as_mandatory():
    """不可逆操作必须是无条件的，不能靠模型自己权衡。"""
    assert "不可逆" in DEFAULT_TOOL_DESCRIPTION
    assert "必问" in DEFAULT_TOOL_DESCRIPTION
    # 不可逆那一句和"哪怕有把握"必须在同一段里，否则容易被读成建议
    block = [b for b in DEFAULT_TOOL_DESCRIPTION.split("\n\n") if "不可逆" in b]
    assert block and "哪怕有把握" in block[0]


def test_refs_param_hidden_when_disabled():
    assert "refs" not in build_consult_tool()["function"]["parameters"]["properties"]
    assert "refs" in build_consult_tool(refs_enabled=True)["function"]["parameters"]["properties"]


# ---------------------------------------------------------------- brief 解析


def test_parse_brief_reads_five_lines():
    f = parse_brief(GOOD_DESIGN)
    assert f["goal"].startswith("选出")
    assert f["given"].startswith("单机")
    assert f["blocked"].startswith("不确定")


def test_parse_brief_maps_chinese_keys():
    f = parse_brief("目标: 修 bug\n已试: 改了两行\n约束: 不能动 schema")
    assert f["goal"] == "修 bug"
    assert f["tried"] == "改了两行"
    assert f["constraints"] == "不能动 schema"


def test_parse_brief_keeps_unrecognised_lines_in_given():
    """认不出的行不能丢，它可能正是关键事实。"""
    f = parse_brief("goal: 修 bug\n顺手发现上游 502 日志里有 timeout")
    assert "timeout" in f["given"]


def test_parse_brief_accepts_freeform():
    f = parse_brief("线上偶发 502，重启后恢复，怀疑连接池")
    assert f.get("goal", "") == ""  # 没有 key，但内容在 given 里
    assert "502" in f["given"]


# ---------------------------------------------------------------- 质量闸


def test_good_design_passes():
    assert precheck("design", "缓存怎么分层", GOOD_DESIGN) == ""


def test_good_diagnosis_passes():
    assert precheck("diagnosis", "为什么第三遍才复现", GOOD_DIAG) == ""


def test_empty_ask_blocked():
    assert "ask" in precheck("design", "   ", GOOD_DESIGN)


def test_overlong_ask_blocked():
    issue = precheck("design", "问" * 500, GOOD_DESIGN)
    assert "过长" in issue


def test_missing_goal_blocked_when_keyed():
    issue = precheck("design", "缓存怎么分层", "given: 单机 QPS 2k\ntried: none")
    assert "goal" in issue


def test_freeform_brief_allowed_if_substantial():
    """没按五行格式写不算错，散文也能干活；只有太薄才拦。"""
    prose = (
        "现在订单表 4 亿行要分片，读多写少，高峰期 3k QPS，不能停机迁移，"
        "也回不了滚，想听听有没有比先双写更稳的顺序。"
    )
    assert precheck("design", "分片顺序怎么排", prose) == ""


def test_thin_brief_blocked():
    assert "太短" in precheck("design", "缓存怎么分层", "brief 1")


def test_diagnosis_without_tried_blocked():
    issue = precheck("diagnosis", "为什么偶发 502", "goal: 修 502\ngiven: 线上偶发")
    assert "tried" in issue


def test_review_without_tried_blocked():
    issue = precheck("review", "这个迁移脚本能跑吗", "goal: 迁数据\ngiven: 2000 万行")
    assert "tried" in issue


def test_diagnosis_with_explicit_none_tried_blocked():
    issue = precheck("diagnosis", "为什么偶发 502", "goal: 修 502\ntried: none")
    assert "tried" in issue


@pytest.mark.parametrize("word", ["none", "无", "N/A", "-", "null", "暂无"])
def test_various_empty_markers_detected(word):
    issue = precheck("diagnosis", "为什么偶发 502", f"goal: 修 502\ntried: {word}")
    assert "tried" in issue


def test_design_does_not_require_tried():
    """设计题不需要先试错，硬要 tried 会把正常调用挡在门外。"""
    assert precheck("design", "缓存怎么分层", "goal: 分层\ngiven: QPS 2k\ntried: none") == ""


def test_block_message_tells_model_it_cost_nothing():
    issue = precheck("diagnosis", "为什么偶发 502", "goal: 修 502")
    assert "未消耗额度" in issue
    assert "补齐" in issue


# ---------------------------------------------------------------- 配额提示


def test_quota_note_when_plenty():
    note = render_quota_note(50, 4)
    assert "还剩 50 次" in note
    # 宽裕时不该附带"攒着问"的建议——那是每轮都付的 token
    assert "攒成" not in note


def test_quota_note_when_tight_advises_merging():
    note = render_quota_note(3, 4)
    assert "还剩 3 次" in note
    assert "攒成" in note


def test_quota_note_does_not_reference_ambiguous_context():
    """「按上面四条判断」在 system 里找不到"上面"——指代必须自洽。"""
    for remaining in (None, 0, 3, 50):
        assert "上面" not in render_quota_note(remaining, 2)


def test_quota_note_when_exhausted_says_dont_try():
    note = render_quota_note(0, 4)
    assert "已用尽" in note
    assert "不要尝试" in note


def test_quota_note_unlimited_daily():
    note = render_quota_note(None, 3)
    assert "今日不限量" in note
    assert "4" not in note.split("最多")[1][:4]  # 别把 per_turn 写错位


def test_quota_note_empty_when_tool_disabled():
    assert render_quota_note(10, 0) == ""


# ---------------------------------------------------------------- 注入预算
#
# 这套网关是在智能体自己的提示词之上又包了一层，每一分注入都是净增加。
# 所以这里给一个硬上限：提示词可以改，但不能悄悄长回去。


def _inject_cost(refs: bool = False) -> dict[str, int]:
    tool = build_consult_tool(refs_enabled=refs)
    return {
        "system": count_tokens(DEFAULT_SYSTEM_TEXT),
        "quota": count_tokens(render_quota_note(5, 2)),
        "tool": count_tokens(json.dumps(tool, ensure_ascii=False)),
    }


def test_injection_stays_within_budget():
    cost = _inject_cost()
    total = sum(cost.values())
    print(f"\n[注入成本] system={cost['system']} 配额={cost['quota']} 工具={cost['tool']} 合计={total}")
    assert total <= 560, f"每轮固定注入 {total} tokens，超出预算：{cost}"


def test_description_stays_lean():
    """工具描述是最大的一块，单独设上限，逼着它保持紧凑。"""
    n = count_tokens(DEFAULT_TOOL_DESCRIPTION)
    print(f"[工具描述] {len(DEFAULT_TOOL_DESCRIPTION)} 字 / {n} tok")
    assert n <= 240, f"工具描述 {n} tokens，超出预算"


def test_refs_budget_is_known():
    """refs 打开后工具定义会变长，这个增量要心里有数。"""
    plain = _inject_cost()["tool"]
    with_refs = _inject_cost(refs=True)["tool"]
    print(f"[refs 增量] {with_refs - plain} tokens")
    assert with_refs - plain <= 400


# ---------------------------------------------------------------- 不重复


def test_description_does_not_repeat_brief_schema():
    """五行格式只在 brief 参数里说一遍，描述里不再复述。"""
    assert "goal: 一句话目标" not in DEFAULT_TOOL_DESCRIPTION
    brief_desc = CONSULT_TOOL["function"]["parameters"]["properties"]["brief"]["description"]
    assert "goal: 一句话目标" in brief_desc


def test_description_does_not_repeat_ask_rule():
    """「一次只问一个」只在 ask 参数里说，描述里不再复述。"""
    assert "一次只问一个" not in DEFAULT_TOOL_DESCRIPTION
    ask_desc = CONSULT_TOOL["function"]["parameters"]["properties"]["ask"]["description"]
    assert "多个问题拆成多次" in ask_desc


def test_system_text_does_not_repeat_tool_role():
    """system 不该复述工具描述里已经写过的定位。"""
    assert "只出方案" not in DEFAULT_SYSTEM_TEXT
    assert "不执行任何操作" not in DEFAULT_SYSTEM_TEXT
    assert "consult_cloud" not in DEFAULT_SYSTEM_TEXT


def test_no_meta_commentary_about_own_structure():
    """解释"这几条判据怎么排序"的元话语是纯开销，模型看得懂判据本身。"""
    assert "四条里第" not in DEFAULT_TOOL_DESCRIPTION
    assert "是提醒" not in DEFAULT_TOOL_DESCRIPTION


# ---------------------------------------------------------------- 无歧义


def test_review_tried_is_defined_in_its_own_terms():
    """review 是"还没执行先评审"，要求"报错原文"说不通。

    参数说明必须给 review 一个它能满足的取材口径，否则模型要么编一个
    tried，要么被网关退回。
    """
    brief_desc = CONSULT_TOOL["function"]["parameters"]["properties"]["brief"]["description"]
    tried_line = [l for l in brief_desc.splitlines() if l.startswith("tried:")][0]
    assert "报错原文" in tried_line
    assert "dry-run" in tried_line or "检查结果" in tried_line


def test_precheck_message_covers_both_kinds():
    issue = precheck("review", "迁移脚本能跑吗", "goal: 迁数据\ntried: none")
    assert "已验证" in issue or "已试过" in issue
