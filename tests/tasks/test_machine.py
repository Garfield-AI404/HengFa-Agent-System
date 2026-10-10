"""RV-09 状态机测试：8.1 八状态迁移表逐条覆盖。

通过标准（16.6 RV-09）：task 状态图和每条迁移条件；非法迁移被拒。
"""

from __future__ import annotations

import pytest

from administrative_agent.tasks.machine import (
    TERMINAL_STATES,
    TRANSITIONS,
    WAITING_STATES,
    TaskStatus,
    TaskTransitionError,
    assert_transition,
)

ALL_STATES = set(TaskStatus)


def test_eight_states_match_spec() -> None:
    """8.1 八状态与规格表格一致。"""
    assert ALL_STATES == {
        TaskStatus.PENDING,
        TaskStatus.RUNNING,
        TaskStatus.WAITING_USER_INPUT,
        TaskStatus.WAITING_CONFIRMATION,
        TaskStatus.WAITING_REVIEW,
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }
    assert len(ALL_STATES) == 8


def test_terminal_and_waiting_sets() -> None:
    assert TERMINAL_STATES == {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
    assert WAITING_STATES == {
        TaskStatus.WAITING_USER_INPUT,
        TaskStatus.WAITING_CONFIRMATION,
        TaskStatus.WAITING_REVIEW,
    }


@pytest.mark.parametrize(
    ("current", "target"),
    [(c, t) for c, targets in TRANSITIONS.items() for t in targets],
)
def test_every_legal_transition_accepts(current: TaskStatus, target: TaskStatus) -> None:
    """迁移表中每个合法迁移必须通过（状态图与代码一致）。"""
    assert_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [(c, t) for c in ALL_STATES for t in ALL_STATES if t not in TRANSITIONS[c]],
)
def test_every_illegal_transition_rejected(current: TaskStatus, target: TaskStatus) -> None:
    """迁移表之外的全部 8×8-合法组合一律拒绝（RV-09 通过标准：非法迁移被拒）。"""
    with pytest.raises(TaskTransitionError) as exc_info:
        assert_transition(current, target)
    assert exc_info.value.current == current.value
    assert exc_info.value.target == target.value


def test_terminal_states_are_absorbing() -> None:
    """终态（succeeded/failed/cancelled）不可再迁移到任何状态。"""
    for terminal in TERMINAL_STATES:
        for target in ALL_STATES:
            assert target not in TRANSITIONS[terminal]


def test_transition_accepts_raw_strings() -> None:
    """断言接受字符串形式的状态值（服务层入参兼容）。"""
    assert_transition("pending", "running")
    assert_transition(TaskStatus.RUNNING, "waiting_user_input")


def test_unknown_state_rejected() -> None:
    with pytest.raises(TaskTransitionError):
        assert_transition("pending", "no_such_state")


def test_every_transition_has_documented_condition() -> None:
    """每条迁移都有书面条件（RV-09 材料 §8 迁移表同源，防漏记）。"""
    from administrative_agent.tasks.machine import TRANSITION_CONDITIONS

    legal = {(c, t) for c, targets in TRANSITIONS.items() for t in targets}
    assert set(TRANSITION_CONDITIONS) == legal


def test_succeeded_cannot_be_reached_from_non_running() -> None:
    """succeeded 只能从 running 到达（且需验证路径，见 test_service.py）。"""
    for state in ALL_STATES - {TaskStatus.RUNNING}:
        assert TaskStatus.SUCCEEDED not in TRANSITIONS[state]
