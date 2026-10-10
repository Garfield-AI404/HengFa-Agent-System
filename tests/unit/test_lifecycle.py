"""D25/RV-38 优雅停机协调器单元测试（lifecycle.py；规格 15.3 步骤 7）。

语义：关闭时先停止接收新写任务（accepting_new_writes 翻转）→ 等待
进行中写调用落审计（drain_writes 等待计数归零）或超时 → 再停机。
"""

from __future__ import annotations

import threading
import time

import pytest

from administrative_agent import lifecycle


@pytest.fixture(autouse=True)
def _reset_lifecycle():
    """每个用例前后复位模块级状态（进程启动复位语义，main.py lifespan）。"""
    lifecycle.reset()
    yield
    lifecycle.reset()


def _hold_inflight(seconds: float) -> None:
    with lifecycle.in_flight_write():
        time.sleep(seconds)


def test_initial_state_accepting_and_idle() -> None:
    assert lifecycle.accepting_new_writes() is True
    assert lifecycle.inflight_count() == 0
    assert lifecycle.drain_writes(0) is True


def test_begin_shutdown_rejects_new_writes() -> None:
    lifecycle.begin_shutdown()
    assert lifecycle.accepting_new_writes() is False
    # 停机标志不影响进行中计数（已受理的任务继续完成收尾）
    assert lifecycle.inflight_count() == 0


def test_in_flight_write_context_counts() -> None:
    with lifecycle.in_flight_write():
        assert lifecycle.inflight_count() == 1
        with lifecycle.in_flight_write():
            assert lifecycle.inflight_count() == 2
        assert lifecycle.inflight_count() == 1
    assert lifecycle.inflight_count() == 0


def test_in_flight_write_context_decrements_on_exception() -> None:
    with pytest.raises(RuntimeError):
        with lifecycle.in_flight_write():
            raise RuntimeError("boom")
    assert lifecycle.inflight_count() == 0


def test_drain_waits_until_inflight_zero() -> None:
    thread = threading.Thread(target=_hold_inflight, args=(0.3,))
    thread.start()
    time.sleep(0.05)  # 确保计数已进入
    started = time.monotonic()
    drained = lifecycle.drain_writes(5.0)
    elapsed = time.monotonic() - started
    thread.join()
    assert drained is True
    assert elapsed >= 0.25  # 等待了进行中调用（而非立即返回）
    assert lifecycle.inflight_count() == 0


def test_drain_times_out_but_continues_shutdown() -> None:
    thread = threading.Thread(target=_hold_inflight, args=(1.0,))
    thread.start()
    time.sleep(0.05)
    started = time.monotonic()
    drained = lifecycle.drain_writes(0.2)  # 超时 < 调用耗时
    elapsed = time.monotonic() - started
    thread.join()
    # 超时语义：返回 False（未收尾），但不无限挂起（运维可观察后处置）
    assert drained is False
    assert 0.15 <= elapsed < 0.9


def test_drain_immediate_when_idle() -> None:
    assert lifecycle.drain_writes(0) is True


def test_reset_clears_shutdown_and_inflight() -> None:
    lifecycle.begin_shutdown()
    with lifecycle.in_flight_write():
        pass  # 正常进出，计数归零
    lifecycle.reset()  # 进程启动复位（main.py lifespan startup）
    assert lifecycle.accepting_new_writes() is True
    assert lifecycle.inflight_count() == 0
