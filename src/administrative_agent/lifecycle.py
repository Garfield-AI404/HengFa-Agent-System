"""15.3-7 优雅停机协调器（D25/RV-38、RV-39）。

规格 15.3 步骤 7：关闭时先停止接收新写任务，等待当前调用落审计或超时，
再优雅停机。本模块提供进程级协调原语：

- ``begin_shutdown``：置位停机标志。新写任务创建入口
  （web/routes_chat.py 提交消息）检查 ``accepting_new_writes()``，
  停机后拒绝并返回 503（稳定契约 SERVICE_UNAVAILABLE，2.7.3）；
- ``in_flight_write`` 上下文：MCP 写工具调用（mcp/client.py call_tool
  对 _WRITE_TOOLS 的工具）包裹——进行中的 ERP 写调用计数；调用返回后
  其结果由上层执行器落审计（D12+），停机等待该计数归零即"等待当前
  调用落审计"；
- ``drain_writes``：lifespan shutdown 阶段等待计数归零或超时
  （SHUTDOWN_DRAIN_TIMEOUT，默认 30 秒）。超时则记录并强制继续停机
  （不无限挂起；运维观察超时事件后可人工核查未收尾调用）；
- ``reset``：进程启动时复位（main.py lifespan startup 调用）——停机
  状态与计数不跨进程存活，TestClient/重启均为干净状态。

线程安全：API 与执行路径可能并发（多 worker 线程），计数与条件等待
持同一把锁（threading.Condition）。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

#: 是否已进入停机状态（拒绝新写任务）。
_shutting_down = False

#: 进行中的 ERP 写调用计数（in_flight_write 进出配对）。
_inflight = 0

#: 计数与停机标志共用锁；归零时 notify 等待 drain 的线程。
_lock = threading.Lock()
_cond = threading.Condition(_lock)


def reset() -> None:
    """进程启动复位：清空停机标志与计数（main.py lifespan startup 调用）。"""
    global _shutting_down, _inflight
    with _cond:
        _shutting_down = False
        _inflight = 0
        _cond.notify_all()


def begin_shutdown() -> None:
    """进入停机：置位标志，此后新写任务被拒绝（入口处检查）。"""
    global _shutting_down
    with _cond:
        _shutting_down = True


def accepting_new_writes() -> bool:
    """是否仍接受新写任务（停机后返回 False，写入口据此拒绝）。"""
    with _cond:
        return not _shutting_down


def inflight_count() -> int:
    """进行中的写调用数（可观测性/日志用，不用于同步）。"""
    with _cond:
        return _inflight


@contextmanager
def in_flight_write() -> Iterator[None]:
    """包裹一次进行中的 ERP 写调用（mcp/client.py call_tool 写工具）。

    进入时计数 +1；退出（调用返回，其结果已由上层落审计）时 -1，
    归零则唤醒等待 drain 的停机路径。
    """
    global _inflight
    with _cond:
        _inflight += 1
    try:
        yield
    finally:
        with _cond:
            _inflight -= 1
            if _inflight == 0:
                _cond.notify_all()


def drain_writes(timeout: float) -> bool:
    """等待进行中写调用归零或超时（15.3-7"等待当前调用落审计或超时"）。

    :param timeout: 超时秒数（SHUTDOWN_DRAIN_TIMEOUT）。
    :return: True=已收尾（无进行中调用）；False=超时（强制继续停机）。
    """
    with _cond:
        if _inflight == 0:
            return True
        _cond.wait(timeout)
        return _inflight == 0
