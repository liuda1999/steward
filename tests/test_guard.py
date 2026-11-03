# SPDX-License-Identifier: Apache-2.0
import asyncio

from steward.config import GuardCfg
from steward.guard import Guard


def make_guard(tmp_path, **overrides) -> Guard:
    params = dict(
        state_file=str(tmp_path / "usage.json"),
        max_calls_per_turn=2,
        max_tool_rounds=4,
        min_interval_s=0,
        daily_limit=10,
        dedupe=True,
        similarity=0.92,
    )
    params.update(overrides)
    return Guard(GuardCfg(**params))


def run(coro):
    return asyncio.run(coro)


def test_turn_limit(tmp_path):
    guard = make_guard(tmp_path, max_calls_per_turn=2)
    turn = guard.new_turn()

    async def scenario():
        return [
            await guard.reserve(turn, "design", f"问题{i}", f"brief 内容 {i}") for i in range(3)
        ]

    results = run(scenario())
    assert results[0].allowed and results[1].allowed
    assert not results[2].allowed
    assert results[2].code == "turn_limit"
    assert turn.calls == 2


def test_new_turn_resets_counter(tmp_path):
    guard = make_guard(tmp_path, max_calls_per_turn=1)

    async def scenario():
        t1 = guard.new_turn()
        a = await guard.reserve(t1, "design", "问题A", "briefA")
        t2 = guard.new_turn()
        b = await guard.reserve(t2, "design", "问题B", "briefB")
        return a, b

    a, b = run(scenario())
    assert a.allowed and b.allowed


def test_daily_limit(tmp_path):
    guard = make_guard(tmp_path, max_calls_per_turn=10, daily_limit=2)

    async def scenario():
        out = []
        for i in range(3):
            turn = guard.new_turn()
            out.append(await guard.reserve(turn, "design", f"问题{i}", f"brief{i}"))
        return out

    results = run(scenario())
    assert results[0].allowed and results[1].allowed
    assert not results[2].allowed
    assert results[2].code == "daily_limit"


def test_duplicate_brief(tmp_path):
    guard = make_guard(tmp_path, max_calls_per_turn=10, min_interval_s=0)
    turn = guard.new_turn()

    async def scenario():
        first = await guard.reserve(turn, "design", "如何设计缓存层", "goal: 提升读取性能")
        same = await guard.reserve(turn, "design", "如何设计缓存层", "goal: 提升读取性能")
        repunctuated = await guard.reserve(turn, "design", "如何设计缓存层！", "goal: 提升读取性能。")
        different = await guard.reserve(turn, "design", "如何做数据迁移回滚", "goal: 保证可回退")
        return first, same, repunctuated, different

    first, same, repunctuated, different = run(scenario())
    assert first.allowed
    assert not same.allowed and same.code == "duplicate"
    assert not repunctuated.allowed and repunctuated.code == "duplicate"
    assert different.allowed


def test_cooldown(tmp_path):
    guard = make_guard(tmp_path, max_calls_per_turn=10, min_interval_s=60)

    async def scenario():
        a = await guard.reserve(guard.new_turn(), "design", "问题一", "brief 一")
        b = await guard.reserve(guard.new_turn(), "design", "问题二", "brief 二")
        return a, b

    a, b = run(scenario())
    assert a.allowed
    assert not b.allowed and b.code == "cooldown"


def test_persistence_across_instances(tmp_path):
    path = tmp_path / "usage.json"

    async def consume():
        guard = Guard(GuardCfg(state_file=str(path), max_calls_per_turn=10, min_interval_s=0, daily_limit=5))
        await guard.reserve(guard.new_turn(), "design", "问题", "brief")

    run(consume())

    async def check():
        guard = Guard(GuardCfg(state_file=str(path), max_calls_per_turn=10, min_interval_s=0, daily_limit=5))
        return await guard.snapshot()

    snap = run(check())
    assert snap["used_today"] == 1
    assert snap["remaining_today"] == 4


def test_refund(tmp_path):
    guard = make_guard(tmp_path)

    async def scenario():
        turn = guard.new_turn()
        await guard.reserve(turn, "design", "问题", "brief")
        await guard.refund(turn)
        return await guard.snapshot()

    snap = run(scenario())
    assert snap["used_today"] == 0


def test_zero_means_disabled(tmp_path):
    guard = make_guard(tmp_path, max_calls_per_turn=0)

    async def scenario():
        return await guard.reserve(guard.new_turn(), "design", "问题", "brief")

    decision = run(scenario())
    assert not decision.allowed
    assert decision.code == "disabled"
