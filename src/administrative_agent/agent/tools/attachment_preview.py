"""只读附件预览工具（对账流程「先读再问」）。

背景（结构性死循环修复）：读附件内容的旧有唯一路径是 ``reconcile_analyze``，
而它的 ``entity`` / ``subject_name`` 都是必填——这两项只能从附件内容或
用户口中得到，模型却没有任何工具能先读附件内容（见 prompts/base.py 的
RECONCILE_WORKFLOW_RULE 旧①）。于是模型在附件面前是盲的，只能反问用户
「是哪个客户」。本工具给模型开一只眼：只凭 ``file_id`` 就能预览附件结构
（工作表清单/表头/前若干行/每列样例），让模型先看清「这是什么单、哪个
客户、哪个月」，再带正确参数去调 ``reconcile_analyze``。

关键设计：
- **只读**（is_write=False、prepare=None）：不建任务、不落审计——与
  reconcile_analyze 读附件本身不单独审计同口径；本线程内读用户自己上传
  的文件。
- **不经过 published 映射**：``parse_excel_bytes`` 要求先有映射模板；预览
  直接用 openpyxl 裸读工作表结构（与 files/excel_parser.py 同款
  ``load_workbook(BytesIO, read_only=True, data_only=True)``），不做列映射
  ——这是本工具能绕开死循环的关键。
- **fail closed 三检查**（不存在/已过期、跨线程）与 reconcile_analyze 同款：
  判据与异常类型逐条一致，仅提示语措辞略有差异（「需重新上传」vs
  「需重新上传对账文件」）；预览绝不能成为绕过线程隔离的后门。
- **有界输出**：表头 + 前 20 行、单元格截断 50 字符；**本模块每一个 return
  都经统一尺寸收口**（``_fit_summary``），成品 ``{"ok": …, **summary}`` 信封
  ≤ ``MAX_PREVIEW_CHARS``（含 ``truncated``/``note``，并预留 ``{"ok": …}``
  前缀），外部数据决定长度的字段（``sheet_names``/``file_id``/``error``）
  一律有界渲染——预览结果会进对话历史、写进 checkpoint，必须有界。
"""

from __future__ import annotations

import io
import json
import os
from datetime import date, datetime, timezone
from typing import Any

from openpyxl import load_workbook
from pydantic import BaseModel, Field

from administrative_agent.agent.tools.base import (
    ToolContext,
    ToolPreparationError,
    ToolResultSummary,
)
from administrative_agent.config import settings

#: 预览数据行上限（表头 + 前 N 行）。
MAX_PREVIEW_ROWS: int = 20
#: 单元格内容截断长度（字符）。
MAX_CELL_CHARS: int = 50
#: 总输出上限（字符）。**硬约束**：这里限制的是工具返回的**外层结果 dict**
#: （含 ``truncated``/``note`` 字段）经 ``json.dumps(..., ensure_ascii=False)``
#: 序列化后的长度——即 ``tools_node.summarize_tool_result`` 收到的那个字符串。
#: 必须严格小于 ``tools_node.TOOL_RESULT_HISTORY_CHARS(4096)`` 的非 JSON 兜底
#: 截断阈值，**并留 ≥100 字符余量**：否则摘要器会把结果腰斩成非法 JSON
#: （``json.loads`` 抛 ``Unterminated string``）。3900 相对 4096 留 196 字符。
MAX_PREVIEW_CHARS: int = 3900
#: 工作表扫描行数上限（read_only 流式读取；超大工作表只读前若干行即止，
#: 防止无界遍历；总行数另由 sheet.max_row 估计）。
MAX_SCAN_ROWS: int = 1000
#: 每列样例值数量上限。
MAX_COLUMN_SAMPLES: int = 3
#: ``file_id`` 最大长度（P2-2）：正常形如 ``att-<uuid16>``，64 足够。超长由
#: pydantic 直接拒绝（走 ``tools_node`` 参数校验安全网，``:178``），回显处再
#: 截断（双重保险：即使未来放宽校验也不会撑破尺寸收口）。
MAX_FILE_ID_CHARS: int = 64
#: 工作表清单一处回显的名称数量上限（P2-1：无界清单会撑破尺寸收口，
#: 前 N 个 + 总数已足以让模型选出正确表名重试）。
MAX_SHEET_NAMES_PREVIEW: int = 20
#: 单个工作表名回显截断长度。
MAX_SHEET_NAME_CHARS: int = 40
#: 文件名回显截断长度（上传文件名由用户控制，属外部数据决定长度，须有界）。
MAX_FILENAME_CHARS: int = 200
#: 错误文案回显截断长度（底层异常串可能极长，不能让 error 无界）。
MAX_ERROR_CHARS: int = 1000
#: 分级裁剪触发的通用截断提示。
_TRUNCATION_NOTE: str = "预览已截断（字符上限）：仅展示部分结构/样例行"
#: 宽表退化到「仅表头、无数据行」时的显式提示（P3-1：数据行里的客户/月份
#: 信息最有用，退化到只剩表头时须让模型知道可以换方式重试）。
_HEADER_ONLY_NOTE: str = (
    "预览已截断：数据行超出字符上限被裁掉，已仅保留表头；可指定 "
    "sheet_name 重试，或直接基于附件做对账分析读取数据行"
)
#: 表头与数据行**都没了**时的提示（P3-1：不能谎称「已仅保留表头」）。
_EMPTY_ONLY_NOTE: str = (
    "预览已截断：表头与数据行均超出字符上限被裁掉；可指定 sheet_name "
    "重试，或直接基于附件做对账分析读取数据"
)
#: 末级硬截断（整段预览塞进 preview_text）时的提示。
_FINAL_NOTE: str = "预览超出上限，已截断；如需完整内容请基于附件做对账分析"
#: 消费点信封 ``{"ok": <bool>, **summary}`` 相对裸 summary 多出的字符数
#: （``{"ok": true, `` 替换 ``{``，+12；``false`` 再 +1）。``_fit_preview``
#: 按 ``MAX_PREVIEW_CHARS - _ENVELOPE_RESERVE`` 收口 summary，保证**信封**
#: （而非仅 summary）≤ ``MAX_PREVIEW_CHARS``。
_ENVELOPE_RESERVE: int = 16


class AttachmentPreviewArgs(BaseModel):
    """attachment_preview 参数：只凭 file_id 预览附件内容（sheet_name 可选）。"""

    file_id: str = Field(
        ..., max_length=MAX_FILE_ID_CHARS,
        description="附件 id（上传返回的 attachment_id；用户消息里 "
        "[附件：文件名（类型，大小，id=…）] 标记中的 id）",
    )
    sheet_name: str | None = Field(
        None,
        description="工作表名（可选）：不传则预览活动工作表；传了但不存在会"
        "报错并附可用工作表清单",
    )


def attachment_preview(ctx: ToolContext, args: AttachmentPreviewArgs) -> ToolResultSummary:
    """只读预览附件结构（工作表/表头/样例行），用于对账前看清附件内容。

    fail closed：依赖未接线/附件不存在/已过期/跨线程 → ToolPreparationError
    （图内转错误 ToolMessage）；非 Excel 字节流/工作表不存在/读取异常 →
    ok=False + 中文 error（不抛裸异常）。
    """
    # ---- 前置校验（fail closed；与 reconcile_analyze 三检查逐字一致） ----
    if ctx.attachment_store is None:
        raise ToolPreparationError(
            "工具 attachment_preview 未接线附件仓储（harness 运行时缺 "
            "attachment_store），不能执行"
        )
    if ctx.context is None:
        raise ToolPreparationError("缺少会话上下文（context 未接线），不能执行")
    thread_id = ctx.context.thread_id

    record = ctx.attachment_store.get(args.file_id)
    if record is None or _expired(record):
        raise ToolPreparationError(
            f"附件 {args.file_id!r} 不存在或已过期（fail closed），需重新上传"
        )
    if record.thread_id != thread_id:
        raise ToolPreparationError(
            f"附件 {args.file_id!r} 属于其他会话线程，拒绝使用（跨线程附件隔离，fail closed）"
        )
    data = _read_attachment_bytes(record)  # 读取失败抛 ToolPreparationError

    # ---- 裸读工作表结构（不经过 published 映射） ----
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        # openpyxl 对非 Excel 字节流抛 InvalidFileException/BadZipFile 等：
        # 统一转中文 error（fail closed，不抛裸异常）
        return _finish(
            {
                "file_id": args.file_id,
                "error": f"附件解析失败：不是可读取的 Excel 文件（仅支持 .xlsx）"
                         f"：{exc}（fail closed）",
            },
            ok=False,
        )
    try:
        sheet_names = list(wb.sheetnames)
        if args.sheet_name is not None:
            if args.sheet_name not in wb.sheetnames:
                # P2-1：工作表清单可能有上百个工作表，必须**有界渲染**（前 N 个
                # + 总数），否则错误信封会被撑破、经 summarize_tool_result 腰斩
                # 成非法 JSON；保留前 N 个 + 计数足以让模型改对 sheet_name 重试。
                return _finish(
                    {
                        "file_id": args.file_id,
                        "error": f"工作表 {args.sheet_name!r} 不存在；"
                                 f"可用工作表：{_render_sheet_names(sheet_names)}",
                    },
                    ok=False,
                )
            sheet = wb[args.sheet_name]
            sheet_name = args.sheet_name
        else:
            sheet = wb.active
            sheet_name = sheet.title if sheet is not None else ""
        try:
            preview = _build_preview(
                sheet, sheet_name=sheet_name, sheet_names=sheet_names,
                file_id=args.file_id, filename=getattr(record, "filename", ""),
            )
        except Exception as exc:
            return _finish(
                {
                    "file_id": args.file_id,
                    "error": f"附件内容读取失败：{exc}（fail closed）",
                },
                ok=False,
            )
    finally:
        wb.close()
    return _finish(preview, ok=True)


# ===========================================================================
# 内部实现
# ===========================================================================


def _build_preview(
    sheet: Any,
    *,
    sheet_name: str,
    sheet_names: list[str],
    file_id: str,
    filename: str,
) -> dict[str, Any]:
    """从已打开工作表构造有界预览汇总（表头 + 前 20 行 + 列样例）。"""
    header: list[str] = []
    rows: list[list[str]] = []
    header_found = False
    # 扫描行数设上限（read_only 流式；避免超大稀疏表无界遍历）
    max_row = getattr(sheet, "max_row", None)
    scan_limit = max_row if isinstance(max_row, int) and 0 < max_row < MAX_SCAN_ROWS else MAX_SCAN_ROWS
    for row in sheet.iter_rows(min_row=1, max_row=scan_limit, values_only=True):
        cells = [_cell_text(c) for c in row]
        if not any(cells):
            continue  # 空行跳过（不计入数据行）
        if not header_found:
            header = cells  # 首个非空行视为表头（裸读无映射，best effort）
            header_found = True
            continue
        rows.append(cells)
        if len(rows) >= MAX_PREVIEW_ROWS:
            break

    total_estimate = max_row if isinstance(max_row, int) and max_row > 0 else None
    # 截断判定（P3-4）：① 估计总行数明显多于已读数据行；② **命中 20 行上限**
    # 也作为充分条件——read_only 下 ``max_row`` 可能为 None（无法估计总行数），
    # 此时即使已砍掉后续数据也必须如实标注，不能因估不出总数就漏报。
    rows_truncated = bool(
        (total_estimate is not None and total_estimate > len(rows) + 1)
        or len(rows) >= MAX_PREVIEW_ROWS
    )
    preview: dict[str, Any] = {
        # 回显字段有界渲染（外部数据决定长度）：filename 由用户上传名控制、
        # sheet_name 来自工作簿标题，均不得无界进入结果/checkpoint。
        "file_id": _clip_file_id(file_id),
        "filename": _clip_text(filename, MAX_FILENAME_CHARS),
        "sheet_name": _clip_text(sheet_name, MAX_SHEET_NAME_CHARS),
        "sheet_names": sheet_names,
        "header": header,
        "rows": rows,
        "total_rows_estimate": total_estimate,
        "column_samples": _column_samples(header, rows),
        "truncated": rows_truncated,
    }
    # 由 _fit_preview 统一写入 truncated/note 并保证外层 dict ≤ MAX_PREVIEW_CHARS
    # （含提示字段在内一起算尺寸，避免「裁剪后再追加 note」突破上限）。
    return _fit_preview(preview)


def _column_samples(header: list[str], rows: list[list[str]]) -> dict[str, list[str]]:
    """每列样例值（取该列前若干个非空且互异的值，最多 3 个）。

    表头可能重名（如两个「金额」）。重名时对第 2 个及以后加序号后缀
    （``金额``、``金额(2)``、``金额(3)``…），避免后列覆盖前列样例（P3-3）。
    """
    samples: dict[str, list[str]] = {}
    occurrences: dict[str, int] = {}
    for idx, raw_name in enumerate(header):
        name = raw_name or f"列{idx + 1}"
        occurrences[name] = occurrences.get(name, 0) + 1
        key = name if occurrences[name] == 1 else f"{name}({occurrences[name]})"
        values: list[str] = []
        for row in rows:
            value = row[idx] if idx < len(row) else ""
            if value and value not in values:
                values.append(value)
            if len(values) >= MAX_COLUMN_SAMPLES:
                break
        if values:
            samples[key] = values
    return samples


def _clip_text(value: Any, limit: int) -> str:
    """把任意值渲染为字符串并截断到 ``limit`` 字符（超出以 … 结尾）。"""
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _clip_file_id(file_id: Any) -> str:
    """回显用 file_id：截断到 ``MAX_FILE_ID_CHARS``（双重保险）。"""
    return _clip_text(file_id, MAX_FILE_ID_CHARS)


def _bounded_sheet_names(names: list[str]) -> list[str]:
    """工作表清单有界渲染：前 ``MAX_SHEET_NAMES_PREVIEW`` 个 + 每名截断。"""
    return [
        _clip_text(name, MAX_SHEET_NAME_CHARS)
        for name in names[:MAX_SHEET_NAMES_PREVIEW]
    ]


def _render_sheet_names(names: list[str]) -> str:
    """工作表清单文本（错误分支用）：前 N 个 + 「…共 N 个工作表」。"""
    shown = ", ".join(repr(n) for n in _bounded_sheet_names(names))
    text = f"[{shown}]"
    if len(names) > MAX_SHEET_NAMES_PREVIEW:
        text += f" …共 {len(names)} 个工作表"
    return text


def _envelope(summary: dict[str, Any], *, ok: bool) -> str:
    """真实消费点信封字符串（``tools_node.py:220`` 的 ``{"ok": ok, **summary}``）。"""
    return json.dumps({"ok": ok, **summary}, ensure_ascii=False)


def _fit_summary(summary: dict[str, Any], *, ok: bool) -> dict[str, Any]:
    """统一尺寸收口：本模块**所有** ``ToolResultSummary`` 出口的唯一出口。

    成功预览已由 ``_fit_preview`` 有界；错误分支的 ``sheet_names``/``file_id``/
    ``error`` 文案由外部数据决定长度，若不在此有界化，信封会被
    ``tools_node.summarize_tool_result`` 的非 JSON 兜底（4096）腰斩成非法
    JSON（P2-1/P2-2）。收口口径与真实消费点一致：``{"ok": ok, **summary}``
    序列化长度 ≤ ``MAX_PREVIEW_CHARS``。
    """
    out = dict(summary)
    # 已在上限内则原样返回（成功预览已由 _fit_preview 有界；不因收口而改动
    # 本就有界的字段，避免对正常结果做无谓裁剪）。
    if len(_envelope(out, ok=ok)) <= MAX_PREVIEW_CHARS:
        return out
    # 幂等有界化外部数据决定的字段（错误分支的主路径）。
    if isinstance(out.get("file_id"), str):
        out["file_id"] = _clip_file_id(out["file_id"])
    if isinstance(out.get("sheet_names"), list):
        total = len(out["sheet_names"])
        out["sheet_names"] = _bounded_sheet_names(out["sheet_names"])
        if total > len(out["sheet_names"]):
            out["sheet_names_total"] = total
    if isinstance(out.get("error"), str):
        out["error"] = _clip_text(out["error"], MAX_ERROR_CHARS)
    if len(_envelope(out, ok=ok)) <= MAX_PREVIEW_CHARS:
        return out
    # 终极兜底（理论上不可达：上一步已把外部字段收口）：整段内容塞进
    # preview_text 并按**最终信封尺寸**二分收缩，保证恒 ≤ 上限且合法 JSON。
    raw = json.dumps(out, ensure_ascii=False)
    head: dict[str, Any] = {}
    if "file_id" in out:
        head["file_id"] = _clip_file_id(out["file_id"])
    if "filename" in out:
        head["filename"] = _clip_text(out["filename"], MAX_FILENAME_CHARS)
    if "sheet_name" in out:
        head["sheet_name"] = _clip_text(out["sheet_name"], MAX_SHEET_NAME_CHARS)

    def build(text_len: int) -> dict[str, Any]:
        return {**head, "truncated": True, "preview_text": raw[:text_len],
                "note": _FINAL_NOTE}

    low, high = 0, len(raw)
    best = build(0)
    while low <= high:
        mid = (low + high) // 2
        probe = build(mid)
        if len(_envelope(probe, ok=ok)) <= MAX_PREVIEW_CHARS:
            best = probe
            low = mid + 1
        else:
            high = mid - 1
    return best


def _finish(summary: dict[str, Any], *, ok: bool) -> ToolResultSummary:
    """构造 ``ToolResultSummary``，summary 必经 ``_fit_summary`` 尺寸收口。"""
    return ToolResultSummary(ok=ok, summary=_fit_summary(summary, ok=ok))


def _fit_preview(preview: dict[str, Any]) -> dict[str, Any]:
    """把预览裁剪到**信封** ≤ MAX_PREVIEW_CHARS，返回最终结果 dict。

    **出口不变量**：``json.dumps({"ok": True, **结果}, ensure_ascii=False)``
    长度恒 ≤ ``MAX_PREVIEW_CHARS``（预算里预留了 ``_ENVELOPE_RESERVE`` 给
    ``{"ok": …}`` 信封前缀），且 ``truncated``/``note`` 等提示字段**在尺寸
    计算之内**（P2-1 修复：早先裁剪后才追加 note，会突破上限）。该不变量
    同时保证 ``tools_node.summarize_tool_result`` 的非 JSON 兜底（4096）不会
    腰斩结果、外层 ``json.loads`` 恒成功（P2-2 修复）。

    分级裁剪（尽量保留模型最需要的表头 + 行数据）：1) 去列样例（可被行数据
    覆盖）→ 2) 逐级减少行数（**优先保留行数据**，10/5/3/1 行）→ 2.5) 有界化
    工作表清单（可重取元信息，优先于表头/数据行裁剪，P3-2）→ 3) 表头退化为
    单串文本（仍优先留 1 行数据，必要时才退化为 0 行）→ 4) 整段预览塞进
    preview_text 并按最终尺寸二分收缩。
    """
    truncated = bool(preview.get("truncated"))
    cap = MAX_PREVIEW_CHARS - _ENVELOPE_RESERVE

    def size(obj: Any) -> int:
        return len(json.dumps(obj, ensure_ascii=False))

    def finalize(candidate: dict[str, Any], *, truncated_flag: bool) -> dict[str, Any]:
        """写入 truncated/note（纳入尺寸计算）；退化提示按实际保留内容选择。"""
        out = dict(candidate)
        out["truncated"] = truncated_flag
        if truncated_flag and not out.get("note"):
            rows_gone = bool(preview.get("rows")) and not out.get("rows")
            has_header = bool(out.get("header")) or bool(out.get("header_text"))
            if rows_gone and has_header:
                # P3-1：数据行被裁光但表头还在——明示可换方式重试。
                out["note"] = _HEADER_ONLY_NOTE
            elif rows_gone:
                # P3-1：表头与数据行都没了，不能谎称「已仅保留表头」。
                out["note"] = _EMPTY_ONLY_NOTE
            else:
                out["note"] = _TRUNCATION_NOTE
        return out

    # 0) 原样（truncated/note 计入尺寸）
    candidate = finalize(preview, truncated_flag=truncated)
    if size(candidate) <= cap:
        return candidate
    # 1) 去列样例
    lean = {k: v for k, v in preview.items() if k != "column_samples"}
    candidate = finalize(lean, truncated_flag=True)
    if size(candidate) <= cap:
        return candidate
    # 2) 逐级减少行数（10/5/3/1 行）。**不在此降到 0 行**——若连 1 行都放不下，
    #    说明表头本身过大，交给 3) 收缩表头文本后仍优先保留 1 行数据。
    rows = list(preview.get("rows") or [])
    for keep in (10, 5, 3, 1):
        candidate = finalize({**lean, "rows": rows[:keep]}, truncated_flag=True)
        if size(candidate) <= cap:
            return candidate
    # 2.5) P3-2：工作表清单是**可重取元信息**（模型传 sheet_name 即可重试取回，
    #      而客户/供货商名没了就没了）——先把无界 sheet_names 有界化，把预算还给
    #      **完整表头**与那一行数据；只有仍放不下才进入 3) 裁剪表头。
    all_names = list(preview.get("sheet_names") or [])
    bounded_names = _bounded_sheet_names(all_names)
    named = {**lean, "sheet_names": bounded_names}
    if len(all_names) > len(bounded_names):
        named["sheet_names_total"] = len(all_names)
    for keep in (10, 5, 3, 1):
        candidate = finalize({**named, "rows": rows[:keep]}, truncated_flag=True)
        if size(candidate) <= cap:
            return candidate
    # 3) 表头也过大：退化为单串表头文本。P3-1：**优先保留 1 行数据**（数据行
    #    里的客户/供货商/月份才是模型解死锁所需），为此可逐级收缩表头文本；
    #    只有 1 行数据在最小表头下都放不下时，才退化为「仅表头，零行数据」。
    header_text_full = " | ".join(preview.get("header") or [])
    degraded = {k: v for k, v in named.items() if k != "header"}
    for keep in (1, 0):
        for header_cap in (2000, 1000, 500, 200, 0):
            candidate = finalize(
                {
                    **degraded,
                    "header_text": header_text_full[:header_cap],
                    "rows": rows[:keep],
                },
                truncated_flag=True,
            )
            if size(candidate) <= cap:
                return candidate
    # 4) 最终兜底：整段预览序列化后塞入 preview_text。按**最终 dict 尺寸**
    #    二分收缩文本长度（而非先切原串再转义——后者二次转义会膨胀，P2-2），
    #    保证返回 dict 恒 ≤ 上限且为合法 JSON。
    raw = json.dumps(preview, ensure_ascii=False)

    def build(text_len: int) -> dict[str, Any]:
        return {
            "file_id": _clip_file_id(preview.get("file_id")),
            "filename": _clip_text(preview.get("filename"), MAX_FILENAME_CHARS),
            "sheet_name": _clip_text(preview.get("sheet_name"), MAX_SHEET_NAME_CHARS),
            "truncated": True,
            "preview_text": raw[:text_len],
            "note": _FINAL_NOTE,
        }

    low, high = 0, len(raw)
    best = build(0)
    while low <= high:
        mid = (low + high) // 2
        probe = build(mid)
        if size(probe) <= cap:
            best = probe
            low = mid + 1
        else:
            high = mid - 1
    return best


def _cell_text(cell: Any) -> str:
    """单元格 → 展示文本（None 为空串；日期/整数规范化；截断 50 字符）。"""
    if cell is None:
        return ""
    if isinstance(cell, datetime):
        return cell.strftime("%Y-%m-%d")
    if isinstance(cell, date):
        return cell.strftime("%Y-%m-%d")
    if isinstance(cell, float) and cell.is_integer():
        return str(int(cell))
    return str(cell)[:MAX_CELL_CHARS]


def _expired(record: Any) -> bool:
    """记录是否已过期（expires_at 非空且 <= now，与仓储清理口径一致）。

    与 reconciliation.py:_expired 同口径（SQLite 测试库回读 naive datetime
    时补时区后比较，避免 naive/aware 直接比较抛 TypeError）。本包内
    mapping_template/reconciliation 各自持有同名副本（既有风格），此处
    按同样口径实现，行为完全一致。
    """
    expires_at = getattr(record, "expires_at", None)
    if expires_at is None:
        return False
    now = datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= now


def _read_attachment_bytes(record: Any) -> bytes:
    """附件内容读取（storage_path 为 attachment_dir 下相对路径；失败 fail closed）。

    与 reconciliation.py:_read_attachment_bytes 同口径。
    """
    path = os.path.join(settings.attachment_dir, record.storage_path or "")
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as exc:
        raise ToolPreparationError(
            f"附件文件读取失败（{path}）：{exc}（fail closed）"
        ) from exc
