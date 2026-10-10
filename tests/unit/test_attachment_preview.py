"""[2026-09-11 attachment_preview] 只读附件预览工具测试。

覆盖：
- 正常预览（表头 + 前 20 行、单元格截断 50、行数上限即截断标注）；
- fail-closed 三检查：附件不存在 / 已过期 / **跨线程**（最重要——预览不得
  成为绕过线程隔离的后门）；
- 解析失败（非 Excel 字节流，不抛裸异常）、空表、多工作表 / 指定工作表；
- 输出上限（超长单元格 + 超多行 → 裁剪后 ≤ 4000 字符并注明「已截断」）；
- 隔轮驱逐标记（tools_node 对预览成功结果打 detail 标记）。

D5：假仓储 + 真实 openpyxl 生成的工作簿字节（不接真实 ERP）。
"""

from __future__ import annotations

import dataclasses
import importlib
import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from openpyxl import Workbook

from administrative_agent.agent.configuration import RuntimeBag
from administrative_agent.agent.nodes.tools_node import (
    TOOL_RESULT_HISTORY_CHARS,
    summarize_tool_result,
    tools_node,
)
from administrative_agent.agent.state import AgentContext
from administrative_agent.agent.tools import TOOL_REGISTRY

# 模块对象（供 monkeypatch 模块级常量，如 _TRUNCATION_NOTE）。
_ap_module = importlib.import_module(
    "administrative_agent.agent.tools.attachment_preview"
)
from administrative_agent.agent.tools.attachment_preview import (
    MAX_CELL_CHARS,
    MAX_FILE_ID_CHARS,
    MAX_FILENAME_CHARS,
    MAX_PREVIEW_CHARS,
    MAX_PREVIEW_ROWS,
    MAX_SHEET_NAMES_PREVIEW,
    AttachmentPreviewArgs,
    _EMPTY_ONLY_NOTE,
    _HEADER_ONLY_NOTE,
    _fit_preview,
    attachment_preview,
)
from administrative_agent.agent.tools.base import ToolContext, ToolPreparationError
from administrative_agent.config import settings
from administrative_agent.domains.attachments import AttachmentRecord


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


class FakeAttachmentStore:
    """假附件仓储（AttachmentStore.get 同形最小实现）。"""

    def __init__(self, record: AttachmentRecord | None) -> None:
        self._record = record

    def get(self, attachment_id: str) -> AttachmentRecord | None:
        return self._record


def _wb_bytes(wb: Workbook) -> bytes:
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _make_record(
    tmp_path,
    monkeypatch,
    data: bytes,
    *,
    thread_id: str = "thread-1",
    expired: bool = False,
) -> AttachmentRecord:
    """把字节写到临时 attachment_dir 并返回 AttachmentRecord。"""
    monkeypatch.setattr(settings, "attachment_dir", str(tmp_path))
    name = "对账单.xlsx"
    (tmp_path / name).write_bytes(data)
    now = datetime.now(timezone.utc)
    expires = now - timedelta(hours=1) if expired else now + timedelta(hours=1)
    return AttachmentRecord(
        attachment_id="att-1",
        thread_id=thread_id,
        filename=name,
        content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        storage_path=name,
        expires_at=expires,
    )


def _ctx(store: FakeAttachmentStore, *, thread_id: str = "thread-1") -> ToolContext:
    return ToolContext(
        context=AgentContext(
            user_id="manager01",
            user_role="manager",
            scope=frozenset({"erp:query"}),
            thread_id=thread_id,
        ),
        attachment_store=store,
    )


def _simple_workbook(*, data_rows: int = 3, extra_cols: int = 0) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "对账表"
    ws.append(["客户", "型号", "数量"] + [f"列{i}" for i in range(extra_cols)])
    for i in range(data_rows):
        ws.append([f"客户{i}", f"M-{i:03d}", i + 1] + ["x"] * extra_cols)
    return _wb_bytes(wb)


# ---------------------------------------------------------------------------
# 正常预览
# ---------------------------------------------------------------------------


def test_preview_header_rows_and_row_cap(tmp_path, monkeypatch) -> None:
    """正常预览：表头 + 前 20 行（超出 20 行的部分被截断并标注）。"""
    data = _simple_workbook(data_rows=30)
    record = _make_record(tmp_path, monkeypatch, data)
    result = attachment_preview(_ctx(FakeAttachmentStore(record)),
                                TOOL_REGISTRY["attachment_preview"].args_schema(
                                    file_id="att-1"))
    assert result.ok is True
    summary = result.summary
    assert summary["header"] == ["客户", "型号", "数量"]
    assert len(summary["rows"]) == MAX_PREVIEW_ROWS
    assert summary["rows"][0] == ["客户0", "M-000", "1"]
    assert summary["sheet_name"] == "对账表"
    assert summary["sheet_names"] == ["对账表"]
    assert summary["truncated"] is True  # 30 行 > 20 行上限
    assert "已截断" in summary["note"]
    # 每列样例值
    assert "客户" in summary["column_samples"]


def test_preview_cell_truncated_to_50_chars(tmp_path, monkeypatch) -> None:
    """单元格内容截断 50 字符（超长单元格）。"""
    wb = Workbook()
    ws = wb.active
    ws.append(["备注"])
    ws.append(["A" * 200])
    record = _make_record(tmp_path, monkeypatch, _wb_bytes(wb))
    result = attachment_preview(
        _ctx(FakeAttachmentStore(record)),
        TOOL_REGISTRY["attachment_preview"].args_schema(file_id="att-1"),
    )
    assert result.ok is True
    cell = result.summary["rows"][0][0]
    assert len(cell) == MAX_CELL_CHARS == 50


def test_preview_no_truncation_when_within_limits(tmp_path, monkeypatch) -> None:
    """行数在 20 以内时不标注截断。"""
    data = _simple_workbook(data_rows=3)
    record = _make_record(tmp_path, monkeypatch, data)
    result = attachment_preview(
        _ctx(FakeAttachmentStore(record)),
        TOOL_REGISTRY["attachment_preview"].args_schema(file_id="att-1"),
    )
    assert result.ok is True
    assert result.summary["truncated"] is False
    assert "note" not in result.summary


# ---------------------------------------------------------------------------
# fail-closed 三检查
# ---------------------------------------------------------------------------


def test_preview_missing_attachment_fail_closed(tmp_path, monkeypatch) -> None:
    """附件不存在 → ToolPreparationError（fail closed）。"""
    monkeypatch.setattr(settings, "attachment_dir", str(tmp_path))
    with pytest.raises(ToolPreparationError) as exc:
        attachment_preview(
            _ctx(FakeAttachmentStore(None)),
            TOOL_REGISTRY["attachment_preview"].args_schema(file_id="att-missing"),
        )
    assert "不存在或已过期" in str(exc.value)


def test_preview_expired_attachment_fail_closed(tmp_path, monkeypatch) -> None:
    """附件已过期 → ToolPreparationError（fail closed）。"""
    record = _make_record(
        tmp_path, monkeypatch, _simple_workbook(), expired=True
    )
    with pytest.raises(ToolPreparationError) as exc:
        attachment_preview(
            _ctx(FakeAttachmentStore(record)),
            TOOL_REGISTRY["attachment_preview"].args_schema(file_id="att-1"),
        )
    assert "不存在或已过期" in str(exc.value)


def test_preview_cross_thread_fail_closed(tmp_path, monkeypatch) -> None:
    """跨线程附件 → ToolPreparationError（线程隔离，最重要的一条）。

    预览绝不能成为绕过线程隔离的后门：附件属于其他 thread_id 时即使文件
    存在也必须拒绝。
    """
    record = _make_record(
        tmp_path, monkeypatch, _simple_workbook(), thread_id="thread-other"
    )
    with pytest.raises(ToolPreparationError) as exc:
        attachment_preview(
            _ctx(FakeAttachmentStore(record), thread_id="thread-1"),
            TOOL_REGISTRY["attachment_preview"].args_schema(file_id="att-1"),
        )
    assert "其他会话线程" in str(exc.value)
    assert "fail closed" in str(exc.value)


# ---------------------------------------------------------------------------
# 解析失败 / 空表 / 多工作表
# ---------------------------------------------------------------------------


def test_preview_non_excel_bytes_fail_closed(tmp_path, monkeypatch) -> None:
    """非 Excel 字节流 → ok=False + 中文 error（不抛裸异常）。"""
    record = _make_record(tmp_path, monkeypatch, b"this is not an excel file")
    result = attachment_preview(
        _ctx(FakeAttachmentStore(record)),
        TOOL_REGISTRY["attachment_preview"].args_schema(file_id="att-1"),
    )
    assert result.ok is False
    assert "解析失败" in result.summary["error"]


def test_preview_empty_sheet(tmp_path, monkeypatch) -> None:
    """空工作表：ok=True，表头与数据行为空（不报错）。"""
    wb = Workbook()  # 默认空 Sheet
    record = _make_record(tmp_path, monkeypatch, _wb_bytes(wb))
    result = attachment_preview(
        _ctx(FakeAttachmentStore(record)),
        TOOL_REGISTRY["attachment_preview"].args_schema(file_id="att-1"),
    )
    assert result.ok is True
    assert result.summary["header"] == []
    assert result.summary["rows"] == []


def test_preview_multi_sheet_and_select(tmp_path, monkeypatch) -> None:
    """多工作表：默认活动表；指定 sheet_name 选对应表；不存在则报错附清单。"""
    wb = Workbook()
    first = wb.active
    first.title = "一月"
    first.append(["客户", "金额"])
    first.append(["甲", 100])
    second = wb.create_sheet("二月")
    second.append(["供货商", "色号"])
    second.append(["乙", "9010"])
    record = _make_record(tmp_path, monkeypatch, _wb_bytes(wb))
    store = FakeAttachmentStore(record)
    schema = TOOL_REGISTRY["attachment_preview"].args_schema

    default = attachment_preview(_ctx(store), schema(file_id="att-1"))
    assert default.ok is True
    assert default.summary["sheet_names"] == ["一月", "二月"]
    assert default.summary["sheet_name"] == "一月"
    assert default.summary["header"] == ["客户", "金额"]

    picked = attachment_preview(
        _ctx(store), schema(file_id="att-1", sheet_name="二月")
    )
    assert picked.ok is True
    assert picked.summary["sheet_name"] == "二月"
    assert picked.summary["header"] == ["供货商", "色号"]

    missing = attachment_preview(
        _ctx(store), schema(file_id="att-1", sheet_name="三月")
    )
    assert missing.ok is False
    assert "不存在" in missing.summary["error"]
    assert "一月" in missing.summary["error"]


# ---------------------------------------------------------------------------
# 输出上限
# ---------------------------------------------------------------------------


def test_preview_output_capped_at_4000_chars(tmp_path, monkeypatch) -> None:
    """超长单元格 + 超多列/行 → 输出裁剪到 ≤ 4000 字符并注明「已截断」。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "大表"
    ws.append([f"表头{i:03d}" + "H" * 40 for i in range(40)])
    for r in range(20):
        ws.append([f"R{r}C{c}" + "V" * 40 for c in range(40)])
    record = _make_record(tmp_path, monkeypatch, _wb_bytes(wb))
    result = attachment_preview(
        _ctx(FakeAttachmentStore(record)),
        TOOL_REGISTRY["attachment_preview"].args_schema(file_id="att-1"),
    )
    assert result.ok is True
    serialized = json.dumps(result.summary, ensure_ascii=False)
    assert len(serialized) <= MAX_PREVIEW_CHARS
    assert result.summary["truncated"] is True
    assert "已截断" in result.summary["note"]


def test_preview_huge_header_never_exceeds_cap(tmp_path, monkeypatch) -> None:
    """表头本身超长（无数据行）：退化路径仍保证 ≤ 4000 字符。"""
    wb = Workbook()
    ws = wb.active
    ws.append([f"很长的表头{i:03d}" + "X" * 60 for i in range(80)])
    record = _make_record(tmp_path, monkeypatch, _wb_bytes(wb))
    result = attachment_preview(
        _ctx(FakeAttachmentStore(record)),
        TOOL_REGISTRY["attachment_preview"].args_schema(file_id="att-1"),
    )
    assert result.ok is True
    assert len(json.dumps(result.summary, ensure_ascii=False)) <= MAX_PREVIEW_CHARS


def _envelope(result) -> str:
    """真实消费点信封（tools_node.py:220 的 ``{"ok": ok, **summary}``）。"""
    return json.dumps({"ok": result.ok, **result.summary}, ensure_ascii=False)


def test_preview_note_included_in_char_cap(tmp_path, monkeypatch) -> None:
    """P2-1：truncated/note 一起计入尺寸，信封（含 ok）不突破上限。

    断言口径与真实消费点对齐（``{"ok": True, **summary}``），而非仅 summary。
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "宽表"
    ws.append([f"H{i:02d}" + "H" * 19 for i in range(34)])  # 34 列 × 22 字符
    for r in range(20):
        ws.append([f"R{r:02d}C{c:02d}" + "V" * 16 for c in range(34)])
    record = _make_record(tmp_path, monkeypatch, _wb_bytes(wb))
    result = attachment_preview(
        _ctx(FakeAttachmentStore(record)),
        TOOL_REGISTRY["attachment_preview"].args_schema(file_id="att-1"),
    )
    assert result.ok is True
    assert result.summary["truncated"] is True
    assert "note" in result.summary  # 触发截断必须有提示
    envelope = _envelope(result)
    assert len(envelope) <= MAX_PREVIEW_CHARS
    # 硬约束：相对非 JSON 兜底阈值留 ≥100 字符余量
    assert len(envelope) <= TOOL_RESULT_HISTORY_CHARS - 100


def test_preview_note_length_counted_in_size(tmp_path, monkeypatch) -> None:
    """P3-4：note 的长度必须**参与**尺寸判定（否则旧行为下会撑破上限）。

    变异验证：把 note 换成长文本（500 字符）——若实现是「尺寸判定之后再追加
    note」（旧行为），选中档位的内容会逼近上限、加上长 note 后信封必超限；
    当前实现在 finalize 内把 note 计入尺寸，故仍恒 ≤ 上限。实测注入旧行为
    （``size()`` 忽略 note 且 ``_finish`` 不做二次收口）后本用例变红 4222>3900，
    修复版为 2450。
    """
    monkeypatch.setattr(_ap_module, "_TRUNCATION_NOTE", "N" * 500)
    wb = Workbook()
    ws = wb.active
    ws.title = "宽表"
    ws.append([f"H{i:02d}" + "H" * 19 for i in range(34)])
    for r in range(20):
        ws.append([f"R{r:02d}C{c:02d}" + "V" * 16 for c in range(34)])
    record = _make_record(tmp_path, monkeypatch, _wb_bytes(wb))
    result = attachment_preview(
        _ctx(FakeAttachmentStore(record)),
        TOOL_REGISTRY["attachment_preview"].args_schema(file_id="att-1"),
    )
    assert result.ok is True
    assert len(_envelope(result)) <= MAX_PREVIEW_CHARS


def test_preview_envelope_within_cap_at_consumption_point(
    tmp_path, monkeypatch
) -> None:
    """P3-5：与真实消费点同口径断言「信封 ≤ 4096 - 余量」，并过 summarize。

    生产传的是 ``json.dumps({"ok": ok, **summary})``（tools_node.py:220），
    比裸 summary 多约 12 字符；本用例按该口径断言，并确认
    ``summarize_tool_result`` 不腰斩成非法 JSON。
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "宽表"
    ws.append([f"H{i:02d}" + "H" * 19 for i in range(34)])
    for r in range(20):
        ws.append([f"R{r:02d}C{c:02d}" + "V" * 16 for c in range(34)])
    record = _make_record(tmp_path, monkeypatch, _wb_bytes(wb))
    result = attachment_preview(
        _ctx(FakeAttachmentStore(record)),
        TOOL_REGISTRY["attachment_preview"].args_schema(file_id="att-1"),
    )
    envelope = _envelope(result)
    assert len(envelope) <= TOOL_RESULT_HISTORY_CHARS - 100
    assert json.loads(summarize_tool_result(envelope))["truncated"] is True


def test_preview_many_sheets_never_breaks_json(tmp_path, monkeypatch) -> None:
    """P2-1/P3-2：工作表清单有界化（前 N + 计数），信封合法且不超上限。

    220 个 31 字符工作表名：修复后 sheet_names 只回显前 ``MAX_SHEET_NAMES_PREVIEW``
    个并附 ``sheet_names_total``，避免无界清单独占预算。
    """
    wb = Workbook()
    wb.active.title = "S000" + "N" * 27  # 31 字符，保证唯一
    for i in range(1, 220):
        wb.create_sheet(f"S{i:03d}" + "N" * 27)
    record = _make_record(tmp_path, monkeypatch, _wb_bytes(wb))
    result = attachment_preview(
        _ctx(FakeAttachmentStore(record)),
        TOOL_REGISTRY["attachment_preview"].args_schema(file_id="att-1"),
    )
    assert result.ok is True
    envelope = _envelope(result)
    assert len(envelope) <= MAX_PREVIEW_CHARS
    assert len(envelope) <= TOOL_RESULT_HISTORY_CHARS
    assert len(result.summary["sheet_names"]) <= MAX_SHEET_NAMES_PREVIEW
    assert result.summary["sheet_names_total"] == 220
    # 消费点：summarize_tool_result 不得腰斩成非法 JSON
    summarized = summarize_tool_result(envelope)
    parsed = json.loads(summarized)
    assert parsed["truncated"] is True


def test_preview_missing_sheet_error_envelope_bounded(tmp_path, monkeypatch) -> None:
    """P2-1：工作表不存在分支的清单有界（前 N + 总数），错误信封 ≤ 上限。"""
    wb = Workbook()
    wb.active.title = "S000" + "N" * 27
    for i in range(1, 220):
        wb.create_sheet(f"S{i:03d}" + "N" * 27)
    record = _make_record(tmp_path, monkeypatch, _wb_bytes(wb))
    result = attachment_preview(
        _ctx(FakeAttachmentStore(record)),
        TOOL_REGISTRY["attachment_preview"].args_schema(
            file_id="att-1", sheet_name="nope"
        ),
    )
    assert result.ok is False
    envelope = json.dumps({"ok": result.ok, **result.summary}, ensure_ascii=False)
    assert len(envelope) <= MAX_PREVIEW_CHARS
    assert "共 220 个工作表" in result.summary["error"]  # 保留可重试性（总数）
    # 回显的工作表名不超过前 N 个
    assert result.summary["error"].count("N" * 27) <= MAX_SHEET_NAMES_PREVIEW
    parsed = json.loads(summarize_tool_result(envelope))
    assert parsed["ok"] is False


def test_preview_file_id_echo_bounded(tmp_path, monkeypatch) -> None:
    """P2-2：超长 file_id 被 pydantic 拒绝；回显处再截断（双重保险）。"""
    with pytest.raises(Exception):  # pydantic ValidationError（max_length）
        TOOL_REGISTRY["attachment_preview"].args_schema(file_id="x" * 6000)
    # 绕过校验直连（模拟未来放宽校验/内部调用）时回显仍有界
    record = _make_record(tmp_path, monkeypatch, _simple_workbook())
    args = AttachmentPreviewArgs.model_construct(file_id="x" * 6000)
    result = attachment_preview(_ctx(FakeAttachmentStore(record)), args)
    assert result.ok is True
    assert len(result.summary["file_id"]) <= MAX_FILE_ID_CHARS + 1
    assert len(_envelope(result)) <= MAX_PREVIEW_CHARS


def test_preview_filename_echo_bounded(tmp_path, monkeypatch) -> None:
    """外部数据决定长度的 filename（用户上传名）回显有界，不撑破信封。"""
    record = dataclasses.replace(
        _make_record(tmp_path, monkeypatch, _simple_workbook()),
        filename="F" * 5000,  # 上传名可任意长
    )
    result = attachment_preview(
        _ctx(FakeAttachmentStore(record)),
        TOOL_REGISTRY["attachment_preview"].args_schema(file_id="att-1"),
    )
    assert result.ok is True
    assert len(result.summary["filename"]) <= MAX_FILENAME_CHARS + 1
    assert len(_envelope(result)) <= MAX_PREVIEW_CHARS


def test_preview_header_and_rows_gone_uses_distinct_note() -> None:
    """P3-1：表头与数据行都没了时，note 不得谎称「已仅保留表头」。"""
    preview = {
        "file_id": "att-1",
        "filename": "F" * 2600,  # 不可裁剪字段，迫使表头文本收缩到 0
        "sheet_name": "T000",
        "sheet_names": [f"T{i:03d}" + "X" * 36 for i in range(20)],
        "header": ["H" * 50 for _ in range(100)],
        "rows": [["V" * 50 for _ in range(100)]],
        "total_rows_estimate": 2,
        "column_samples": {},
        "truncated": True,
    }
    out = _fit_preview(preview)
    assert out.get("header") is None
    assert out["header_text"] == ""
    assert out["rows"] == []
    assert out["note"] == _EMPTY_ONLY_NOTE
    assert out["note"] != _HEADER_ONLY_NOTE


def test_preview_many_sheets_do_not_evict_header(tmp_path, monkeypatch) -> None:
    """P3-2：宽表 + 大量长名工作表时，先裁剪可重取的 sheet_names，保住表头。

    修复前无界 sheet_names（约 3500-3700 字符）把表头挤到 header_cap=0；修复后
    清单有界化，表头文本得以保留（本用例断言 header_text 非空）。
    """
    wb = Workbook()
    wb.active.title = "T000" + "X" * 27
    for i in range(1, 107):
        wb.create_sheet(f"T{i:03d}" + "X" * 27)
    ws = wb.active
    ws.append([f"客户{i:03d}" + "H" * 47 for i in range(100)])
    ws.append([f"客户名{i:03d}" + "V" * 47 for i in range(100)])
    record = _make_record(tmp_path, monkeypatch, _wb_bytes(wb))
    result = attachment_preview(
        _ctx(FakeAttachmentStore(record)),
        TOOL_REGISTRY["attachment_preview"].args_schema(file_id="att-1"),
    )
    assert result.ok is True
    # 表头（结构化 header 或退化 header_text）未被无界清单挤掉
    assert bool(result.summary.get("header")) or bool(
        result.summary.get("header_text")
    )
    assert len(result.summary["sheet_names"]) <= MAX_SHEET_NAMES_PREVIEW
    assert result.summary["sheet_names_total"] == 107
    assert result.summary["note"] == _HEADER_ONLY_NOTE
    assert len(_envelope(result)) <= MAX_PREVIEW_CHARS


def test_preview_duplicate_headers_do_not_overwrite_samples(
    tmp_path, monkeypatch
) -> None:
    """P3-3：表头重名（两个「金额」）时样例不互相覆盖，第 2 个加序号后缀。"""
    wb = Workbook()
    ws = wb.active
    ws.append(["客户", "金额", "金额"])
    ws.append(["甲", "100", "200"])
    record = _make_record(tmp_path, monkeypatch, _wb_bytes(wb))
    result = attachment_preview(
        _ctx(FakeAttachmentStore(record)),
        TOOL_REGISTRY["attachment_preview"].args_schema(file_id="att-1"),
    )
    assert result.ok is True
    samples = result.summary["column_samples"]
    assert samples["金额"] == ["100"]
    assert samples["金额(2)"] == ["200"]


def test_preview_truncated_when_row_cap_hit_without_max_row(
    tmp_path, monkeypatch
) -> None:
    """P3-4：即使估不出总行数（read_only 下 max_row 为 None），命中 20 行
    上限也须标注截断。"""
    wb = Workbook()
    ws = wb.active
    ws.append(["客户"])
    for i in range(25):
        ws.append([f"客户{i}"])
    record = _make_record(tmp_path, monkeypatch, _wb_bytes(wb))
    result = attachment_preview(
        _ctx(FakeAttachmentStore(record)),
        TOOL_REGISTRY["attachment_preview"].args_schema(file_id="att-1"),
    )
    assert result.ok is True
    assert len(result.summary["rows"]) == MAX_PREVIEW_ROWS
    assert result.summary["truncated"] is True


# ---------------------------------------------------------------------------
# 隔轮驱逐标记（design 第四节：预览结果参与隔轮驱逐）
# ---------------------------------------------------------------------------


def test_tools_node_marks_preview_as_detail(tmp_path, monkeypatch) -> None:
    """tools_node 对预览成功结果打 detail 标记（隔轮驱逐），但不带 query_id
    ——预览不进 tool_query_archive（可由模型按 file_id 重取）。"""
    record = _make_record(tmp_path, monkeypatch, _simple_workbook())
    runtime = RuntimeBag(
        tools=TOOL_REGISTRY,
        attachment_store=FakeAttachmentStore(record),
        context=AgentContext(
            user_id="manager01",
            user_role="manager",
            scope=frozenset({"erp:query"}),
            thread_id="thread-1",
        ),
    )
    state = {
        "messages": [],
        "pending_action": {
            "tool_name": "attachment_preview",
            "tool_arguments": {"file_id": "att-1"},
        },
    }
    out = tools_node(state, runtime)
    assert out["current_step"] == "tool_done"
    message = out["messages"][0]
    assert message.additional_kwargs == {"detail": True}  # 无 query_id
    assert json.loads(message.content)["ok"] is True
