"""对话导出域（规格 2.6 domains/export.py）。

Excel 导出核心：
- 不复用 domains/query.py 的 QueryExecutor（其语义是 R4 查询任务：每次
  实际调用建 attempt/result 审计对，11.2）——导出的翻页循环按
  agent/tools/query.py run_erp_query L166-221 的裸 ``ctx.client.call_tool(
  "query", ...)`` 循环：窗口 200、offset 递增、total 跟踪、空页 break；
  审计只在导出成功后记一条 EVENT_EXPORT 事件（单条事件，不建 attempt/
  result 对——导出非 ERP 写操作）；
- 行数上限 fail closed：total 超 settings.export_max_rows（50000）拒绝；
  offset 超 ERP 分页契约上限（MAX_QUERY_OFFSET=10000，domains/query.py）
  拒绝；页数 1000 兜底；
- 行富化（v2，R4.export.fields）：metal/powder 按 EXPORT_JOIN_FIELDS 关联
  （customer_name/supplier_name）与 EXPORT_COMPUTED_COLUMNS 计算
  （total_weight/total_price）；关联表查询失败 → ExportError（fail closed），
  计算失败该列留空不抛错；
- openpyxl write_only Workbook 落盘 {export_dir}/{file_id}.xlsx +
  {file_id}.meta.json（file_id=uuid4().hex 32 位；meta 含 columns 列序）；
  TTL 过期清理（cleanup_expired_exports，启动时 + 每次导出时调用，
  best effort）；
- 总计行按 EXPORT_SUBTOTAL_COLUMNS 只合计注册列（metal 总重量/总价格、
  powder 总重量、transactions 金额等）；删除
  「未注册实体（sum_cols=None）走全数值列兜底合计」路径——14 个可导出
  实体全部显式登记，None 视为空集；字符串→数值认定**仅限 sum_cols
  登记列**，非登记列只认 int/float/Decimal（电话/工号/银行账号/税号
  等纯数字串列不再被求和/套 #,##0.00）；
  ERP MCP 把 Decimal 序列化成**字符串**，_is_number
  旧实现只认 int/float → 金额列永不判为数值列、永远不出合计（既有
  transactions 金额总计缺失即此病）——现按「规范数字字符串」归一为
  Decimal（限 sum_cols 登记列），金额合计走 Decimal 语义（不用 float
  累积，避免 0.1+0.2 误差落到 Excel）；
  数据行多重量单位时，总计行按单位分桶各出一行（「总计（kg）」），不跨
  重量单位混加（见 _append_subtotal_rows）——判桶按**数据**（行内带
  weight_unit 键）而非输出列；行内无单位键（来源拿不到单位）保持
  「未注明单位」桶/单行总计现状；
- filename 参数：用户对话指定导出文件名时透传（可含中文；取数前校验，
  路径分隔符/非法字符/控制字符/超长 fail closed 拒绝，不浪费 ERP 翻页；
  不带 .xlsx 后缀自动补小写后缀）；缺省 {entity}_导出_{YYYYMMDD}.xlsx；
- 审计失败 → 删已落盘文件后抛错（10.4：审计失败不能假报成功，照
  routes_attachments.py L209-219 口径）。
"""

from __future__ import annotations

import glob
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from administrative_agent.config import settings
from administrative_agent.domains.export_fields import (
    EXPORT_COMPUTED_COLUMNS,
    EXPORT_FIELD_ALIASES,
    EXPORT_JOIN_FIELDS,
    EXPORT_SUBTOTAL_COLUMNS,
    header_label,
    validate_export_fields,
)
from administrative_agent.domains.query import MAX_QUERY_OFFSET

logger: structlog.stdlib.BoundLogger = structlog.get_logger("domains.export")

#: 翻页窗口（mcp-api.md §4：limit 最大 200）
EXPORT_PAGE_SIZE = 200
#: 翻页页数上限兜底（1000 页 × 200 = 20 万行，超过行数上限 5 万先触发；
#: 纯保险丝防 ERP 契约异常死循环）
MAX_EXPORT_PAGES = 1000
#: 数值格千分位两位小数格式（数据格与总计行同用；登记列数据格写作
#: Decimal 真数值——[2026-09-13 复核问题 1]，见 _append_row）
NUMERIC_NUMBER_FORMAT = "#,##0.00"
#: 表头行填充色（实心浅色底纹，仅表头；数据行/总计行不动）
HEADER_FILL_COLOR = "D9E1F2"
#: 表头行冻结位置（冻结首行，滚动时表头常驻）
HEADER_FREEZE_PANES = "A2"
#: 汇总行标签（末尾总计）
_TOTAL_LABEL = "总计"
#: 用户指定导出文件名最大长度（超出 fail closed）
MAX_EXPORT_FILENAME_LEN = 80


# ---------------------------------------------------------------------------
# 领域错误（fail closed：拒绝时给出中文原因）
# ---------------------------------------------------------------------------


class ExportError(ValueError):
    """导出领域错误基类。"""


class ExportLimitError(ExportError):
    """行数/翻页超限（fail closed：提示加过滤条件，不截断导出）。"""


class ExportNotConfiguredError(ExportError):
    """导出功能未启用（settings.export_dir 空串）。"""


# ---------------------------------------------------------------------------
# 领域结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExportResult:
    """一次导出结果（file_id=uuid4().hex 32 位，下载端点按 file_id 定位）。"""

    file_id: str
    filename: str  # 下载文件名（默认 {entity}_导出_{YYYYMMDD}.xlsx，可用户指定）
    path: str  # 绝对路径（{export_dir}/{file_id}.xlsx）
    row_count: int  # 实际写入行数
    total: int  # ERP 全量总数
    expires_at: datetime
    #: [2026-09-15] 实际导出的列（= 落盘 {file_id}.meta.json 的 columns 来源）。
    #: 带出来供**耐久审计**留痕——meta.json 随 TTL 被清理，审计里须能区分
    #: 「本次导出了哪些列」。**不进 sink 事件载荷**（那是前端契约面）。
    columns: list[str]


# ---------------------------------------------------------------------------
# 文件名校验（用户指定文件名 fail closed；缺省名在 export() 内构造）
# ---------------------------------------------------------------------------


def _validate_user_filename(filename: str) -> str:
    """用户指定导出文件名校验与规范化（fail closed）。

    - strip 后为空 / 含路径分隔符（/ 或 \\）/ 含非法字符（< > : " / \\ |
      ? *）或控制字符 / 超长（> MAX_EXPORT_FILENAME_LEN）→ ExportError；
    - 不带 .xlsx 后缀自动补小写后缀；返回规范化后的文件名。
    """
    name = filename.strip()
    if not name:
        raise ExportError("导出文件名不能为空")
    if os.path.basename(name) != name:
        raise ExportError("导出文件名不能包含路径分隔符")
    illegal = '<>:"/\\|?*'
    if any(c in illegal or c < " " for c in name):
        raise ExportError(
            "导出文件名含非法字符（< > : \" / \\ | ? * 或控制字符不允许）"
        )
    if len(name) > MAX_EXPORT_FILENAME_LEN:
        raise ExportError(f"导出文件名过长（最多 {MAX_EXPORT_FILENAME_LEN} 个字符）")
    if not name.endswith(".xlsx"):
        name = f"{name}.xlsx"
    return name


# ---------------------------------------------------------------------------
# Excel 导出器
# ---------------------------------------------------------------------------


class ExcelExporter:
    """Excel 导出器：翻页取数 → openpyxl 落盘 → 元数据 → 审计。

    审计经 ``record_export`` 单条事件（EVENT_EXPORT）；审计失败删除已落盘
    文件后抛错（10.4，不假报成功）。依赖（client/cache/audit）由调用方
    注入，None 依赖在构造/调用时 fail closed。
    """

    def __init__(
        self,
        *,
        client: Any,
        cache: Any,
        audit: Any,
        trace_id: str,
        actor_account: str,
        actor_role: str,
        export_dir: str | None = None,
        max_rows: int | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        self._client = client
        self._cache = cache
        self._audit = audit
        self._trace_id = trace_id
        self._actor_account = actor_account
        self._actor_role = actor_role
        self._export_dir = settings.export_dir if export_dir is None else export_dir
        self._max_rows = settings.export_max_rows if max_rows is None else max_rows
        self._ttl_seconds = (
            settings.export_file_ttl_seconds if ttl_seconds is None else ttl_seconds
        )

    def export(
        self,
        *,
        entity: str,
        fields: list[str] | None = None,
        filter: dict[str, Any] | None = None,
        order_by: str | list[str] | None = None,
        filename: str | None = None,
        sensitive_fields: list[str] | None = None,
    ) -> ExportResult:
        """执行导出（步骤：配置 → 校验 → 文件名校验 → 首轮取 total → 翻页 → 写盘 → 清理 → 审计）。

        ``sensitive_fields``：本次**显式点名导出**的敏感字段名列表（由
        ``run_export_data`` 复用其 ``explicit_sensitive`` 传入，避免两处口径
        不一致）；仅用于写进 `EVENT_EXPORT` 审计载荷，默认 None。实际导出列
        （``columns``）随 ``ExportResult`` 带出，同样只为审计留痕。
        """
        # 1. 导出功能启用检查（fail closed）
        export_dir = (self._export_dir or "").strip()
        if not export_dir:
            raise ExportNotConfiguredError(
                "导出功能未启用（导出目录未配置）"
            )
        if self._client is None or self._cache is None or self._audit is None:
            raise ExportError(
                "导出依赖未接线（client/cache/audit），不能执行（fail closed）"
            )
        # 2. 字段校验（实体白名单 + 元数据存在 + 未知字段 fail closed）
        request_fields = fields
        validated = validate_export_fields(entity, request_fields, self._cache)

        # 2.1 取数字段（对齐默认路径，绝不向 ERP 透传非原生列）：显式 fields
        # 可含 Agent 侧**本地富化列**——关联列（customer_name/supplier_name）
        # 与计算列（total_weight/total_price）由 _enrich_rows 在本地补齐，
        # ERP 不认这些字段名，原样透传会 UNKNOWN_FIELD 致整单失败。默认路径
        # （fields=None）不向 ERP 传 fields → ERP 返回整行、富化照常，故此处
        # 同口径：
        # - 显式字段**全部** ∈ 实体元数据字段 → 只把元数据内字段透传 ERP；
        # - 含任一非原生列（关联列/计算列等）→ **不传 fields**，让 ERP 返回
        #   整行（富化所需的 FK/来源字段随之可得），输出列仍严格 = 显式
        #   fields（_resolve_columns），关联/计算列值由 _enrich_rows 补齐。
        fetch_fields: list[str] | None = None
        if request_fields:
            meta = self._cache.get_entity(entity)
            native = set(getattr(meta, "fields", ()) or ())
            native_fields = [f for f in request_fields if f in native]
            if len(native_fields) == len(request_fields):
                fetch_fields = native_fields or None
            # else: 含非原生列 → fetch_fields 保持 None（整行取数，与默认
            # 路径一致；本地富化后按显式列输出）

        # 2.5 用户指定文件名校验（取数前 fail closed：非法文件名不浪费
        # ERP 翻页；filename=None 走默认名兜底）
        user_filename = (
            _validate_user_filename(filename) if filename is not None else None
        )

        # 3. 首轮查询：limit=200 offset=0 取 total；超行数上限 fail closed
        window = EXPORT_PAGE_SIZE
        rows: list[dict[str, Any]] = []
        total = 0
        pages = 0
        offset = 0
        while True:
            pages += 1
            call_args: dict[str, Any] = {"entity": entity, "limit": window, "offset": offset}
            if fetch_fields:
                call_args["fields"] = fetch_fields
            if filter:
                call_args["filter"] = filter
            if order_by:
                call_args["order_by"] = order_by
            result = self._client.call_tool("query", call_args)
            if not result.ok:
                err = getattr(result, "error", None)
                raise ExportError(
                    f"ERP 查询失败：{getattr(err, 'message', str(err)) if err else '未知错误'}"
                )
            data = result.data or {}
            items = data.get("items") or []
            total = data.get("total") or total or len(items)
            if pages == 1 and total > self._max_rows:
                raise ExportLimitError(
                    f"已超出导出上限 {self._max_rows} 行，请增加过滤条件"
                    "（如 in_date 范围）后重试"
                )
            rows.extend(dict(r) for r in items)
            # 4. 终止条件：空页 / 取全 / 翻页契约上限 / 页数兜底
            if not items or offset + len(items) >= total:
                break
            offset += window
            if offset > MAX_QUERY_OFFSET:
                raise ExportLimitError(
                    f"ERP 分页契约上限 offset={MAX_QUERY_OFFSET}，超过约 "
                    f"{MAX_QUERY_OFFSET + window} 行无法继续翻页，请增加过滤条件"
                )
            if pages >= MAX_EXPORT_PAGES:
                raise ExportLimitError(
                    f"导出翻页超过 {MAX_EXPORT_PAGES} 页仍未取完，请增加过滤条件后重试"
                )

        # 4.5 行富化（v2）：metal/powder 关联列（客户名称/供货商名称）+
        # 计算列（总重量/总价格）；其余实体 no-op。富化在取数完成后、
        # 列序解析前执行——默认列/显式列都走同一富化路径。
        rows = _enrich_rows(self._client, entity, rows)

        # 5. 写 Excel（write_only Workbook；中文表头；数值列原值 + 千分位）
        file_id = uuid.uuid4().hex  # 32 位，下载端点 _FILE_ID_PATTERN 匹配
        now = datetime.now(timezone.utc)
        filename = user_filename or f"{entity}_导出_{date.today().strftime('%Y%m%d')}.xlsx"
        path = os.path.join(export_dir, f"{file_id}.xlsx")
        # 目录可能未创建（.env 只配置路径不建目录），首次写入前自愈创建
        try:
            os.makedirs(export_dir, exist_ok=True)
        except OSError as exc:
            raise ExportError(f"导出目录不可写：{export_dir!r}（{exc}）") from exc
        columns = self._resolve_columns(request_fields, rows, validated)
        self._write_workbook(
            path,
            entity,
            columns,
            rows,
            sum_cols=EXPORT_SUBTOTAL_COLUMNS.get(entity),
        )

        # 6. 元数据落盘（{file_id}.meta.json）
        expires_at = now + timedelta(seconds=max(1, self._ttl_seconds))
        meta_path = os.path.join(export_dir, f"{file_id}.meta.json")
        meta: dict[str, Any] = {
            "file_id": file_id,
            "filename": filename,
            "entity": entity,
            "row_count": len(rows),
            "total": total,
            "filter": filter,
            "columns": columns,  # v2 新增（不破坏旧 meta 消费方兼容）
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        try:
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
        except OSError as exc:
            self._remove_best_effort(path)
            raise ExportError(f"导出元数据写盘失败：{exc}（fail closed）") from exc

        # 7. 顺带清理过期导出（best effort，失败不阻断本次结果）
        try:
            cleanup_expired_exports(export_dir, ttl_seconds=self._ttl_seconds)
        except Exception:
            logger.warning("export_cleanup_failed", exc_info=True)

        # 8. 审计（EVENT_EXPORT 单条事件；失败 → 删已落盘文件后抛错，10.4）
        try:
            self._audit.record_export(
                trace_id=self._trace_id,
                actor_account=self._actor_account,
                actor_role=self._actor_role,
                entity=entity,
                filter=filter,
                row_count=len(rows),
                file_id=file_id,
                columns=columns,
                sensitive_fields=sensitive_fields,
            )
        except Exception as exc:
            self._remove_best_effort(path)
            self._remove_best_effort(meta_path)
            logger.warning(
                "export_audit_failed", file_id=file_id, entity=entity, exc_info=True
            )
            raise

        logger.info(
            "export_done",
            file_id=file_id,
            entity=entity,
            row_count=len(rows),
            total=total,
            pages=pages,
        )
        return ExportResult(
            file_id=file_id,
            filename=filename,
            path=path,
            row_count=len(rows),
            total=total,
            expires_at=expires_at,
            columns=list(columns),
        )

    # ---- 内部 ----

    def _resolve_columns(
        self,
        fields: list[str] | None,
        rows: list[dict[str, Any]],
        validated: list[str],
    ) -> list[str]:
        """列序优先级 = 显式 fields → 校验字段（含默认列）→ 行 dict 键序兜底。

        v2：校验字段优先于行键序——fields 缺省时 validated 含默认导出列
        （customer_name/total_weight/total_price 等富化列），而行 dict 键序
        是 ERP 原始键（含 id 等系统字段），不能作列序来源。
        """
        if fields:
            return list(fields)
        if validated:
            return list(validated)
        if rows and isinstance(rows[0], dict):
            return list(rows[0])
        return []

    def _write_workbook(
        self,
        path: str,
        entity: str,
        columns: list[str],
        rows: list[dict[str, Any]],
        *,
        sum_cols: frozenset[str] | None = None,
    ) -> None:
        """write_only Workbook 落盘（失败删半成品后抛错，不留残缺文件）。

        末尾追加总计行（sum_cols 只合计登记列，None 视为空集不合计；多重量
        单位按桶分行，不跨单位混加，见 _append_subtotal_rows）；零行导出只
        留表头：空数据无汇总语义，不插总计行。

        数值格式/合计口径：字符串→数值认定仅限 sum_cols 登记列，其余列只
        认 int/float/Decimal（[2026-09-12 回归修复]，见 _numeric_columns）。
        """
        wb = Workbook(write_only=True)
        try:
            ws = wb.create_sheet(title="导出")
        except Exception:
            wb.close()
            raise
        # 冻结首行（write_only 模式下 create_sheet 后、首次 append 前设置；
        # 滚动时表头常驻）
        ws.freeze_panes = HEADER_FREEZE_PANES
        # 表头（中文名；export_fields.header_label 保留）——加粗/浅色底纹/
        # 居中/细边框，仅表头单元格套样式（_header_cell）
        header = [header_label(entity, col) for col in columns]
        ws.append([_header_cell(ws, label) for label in header])
        numeric_cols = _numeric_columns(columns, rows, sum_cols)
        for row in rows:
            _append_row(ws, columns, row, numeric_cols, sum_cols=sum_cols)
        if rows:
            _append_subtotal_rows(
                ws, columns, numeric_cols, _TOTAL_LABEL, rows, sum_cols=sum_cols
            )
        try:
            wb.save(path)
        except OSError as exc:
            self._remove_best_effort(path)
            raise ExportError(f"Excel 写盘失败：{exc}（fail closed）") from exc
        finally:
            wb.close()

    def _remove_best_effort(self, path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            logger.warning("export_file_cleanup_failed", path=path, exc_info=True)


# ---------------------------------------------------------------------------
# 行富化（v2，R4.export.fields）：关联列 + 计算列
# ---------------------------------------------------------------------------


def _to_int_id(value: Any) -> int | None:
    """FK/id 值转 int（字符串数字兼容；None/不可转 → None）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_non_empty(item: dict[str, Any], name_fields: tuple[str, ...]) -> str | None:
    """名称字段按优先序取第一个非空值（生产配置单字段时即取该字段；
    多字段按序回退的机制保留，配置只放生产真实字段）。"""
    for f in name_fields:
        value = item.get(f)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _fetch_name_map(
    client: Any,
    table: str,
    name_fields: tuple[str, ...],
    ids: list[int],
) -> dict[int, str]:
    """关联表名称映射：按 id 集合全量拉取（id+名称字段翻页），键为 int(id)。

    - 关联表主键恒为 ``id``（customers/suppliers 的 PK 字段；外键提取
      在调用方按行完成，本函数只负责表侧查询）；
    - 查询失败 → ExportError（fail closed：关联表不可用时宁可拒绝导出，
      不能让名称列缺数据落盘）；
    - 翻页契约与主查询同口径（窗口 200 / offset 上限 / 页数兜底）；
    - 只登记 id ∈ ids 且名称非空的行；未命中的 id 由调用方回退占位文本。
    """
    if not ids:
        return {}
    id_set = set(ids)
    fetch_fields = ["id", *name_fields]
    name_map: dict[int, str] = {}
    offset = 0
    pages = 0
    while True:
        pages += 1
        result = client.call_tool(
            "query",
            {
                "entity": table,
                "fields": fetch_fields,
                "limit": EXPORT_PAGE_SIZE,
                "offset": offset,
            },
        )
        if not result.ok:
            err = getattr(result, "error", None)
            raise ExportError(
                f"关联表 {table} 查询失败："
                f"{getattr(err, 'message', str(err)) if err else '未知错误'}"
            )
        data = result.data or {}
        items = data.get("items") or []
        for item in items:
            rid = _to_int_id(item.get("id"))
            if rid is None or rid not in id_set:
                continue
            name = _first_non_empty(item, name_fields)
            if name is not None:
                name_map[rid] = name
        total = data.get("total") or len(items)
        if not items or offset + len(items) >= total:
            break
        offset += EXPORT_PAGE_SIZE
        if offset > MAX_QUERY_OFFSET:
            raise ExportError(
                f"关联表 {table} 翻页超过契约上限 offset={MAX_QUERY_OFFSET}（fail closed）"
            )
        if pages >= MAX_EXPORT_PAGES:
            raise ExportError(
                f"关联表 {table} 翻页超过 {MAX_EXPORT_PAGES} 页仍未取完（fail closed）"
            )
    return name_map


def _computed_value(row: dict[str, Any], sources: tuple[str, ...]) -> float | None:
    """计算列值：来源字段按序相乘；任一缺失/不可转数值 → None（不抛错）。"""
    total = 1.0
    for src in sources:
        try:
            total *= float(row.get(src))
        except (TypeError, ValueError):
            return None
    return total


def _enrich_rows(
    client: Any,
    entity: str,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """行富化（v2）：metal/powder 关联列（客户名称/供货商名称）+ 计算列
    （总重量/总价格）；其余实体 no-op（原样返回）。

    - 关联：按 EXPORT_JOIN_FIELDS 建 {int(fk): 名称} 映射并回填；id 无映射
      → 该行名称回退「客户ID {id}」/「供货商ID {id}」（占位不阻断导出）；
    - 计算：EXPORT_COMPUTED_COLUMNS 按来源字段相乘；float 转换失败 → 该列
      None 不抛错；total_price 复用已算出的 total_weight（避免重复乘
      单件重量 × 数量——computed 字典保持插入序，total_weight 先算）。

    [2026-09-08 ERP 附加款] total_price 口径：ERP 行金额 = 数量×单件重量×
    单价 + surcharge_amount（mcp-api.md §6/§7 公式），本地复刻需并入行内
    附加款。不变式：total_price 只出现在**默认导出**（fields=None）或**显式
    请求含计算列**（如 total_price/total_weight——validate_export_fields
    放行本地计算列）两种路径；两者取数都走整行（默认不传 fields；显式含
    非原生列时不传 fields，见 export 步骤 2.1），行内必含 surcharge_amount。
    塑粉无 surcharge_amount 字段（该分支仅 metal_orders 注册），按 0 安全
    兜底。
    """
    joins = EXPORT_JOIN_FIELDS.get(entity) or {}
    computed = EXPORT_COMPUTED_COLUMNS.get(entity) or {}
    if not joins and not computed:
        return rows
    if joins:
        for join_col, (table, fk, name_fields) in joins.items():
            ids = sorted(
                {rid for rid in (_to_int_id(r.get(fk)) for r in rows) if rid is not None}
            )
            name_map = _fetch_name_map(client, table, name_fields, ids)
            fallback = "客户ID" if entity == "metal_orders" else "供货商ID"
            for row in rows:
                rid = _to_int_id(row.get(fk))
                if rid is not None and rid in name_map:
                    row[join_col] = name_map[rid]
                else:
                    row[join_col] = f"{fallback} {row.get(fk)}"
    if computed:
        for row in rows:
            for col, sources in computed.items():
                if col == "total_price" and "total_weight" in computed:
                    weight = row.get("total_weight")
                    if weight is None:
                        row[col] = None
                        continue
                    try:
                        # 基础金额 = 总重量 × 单价；并入附加款（缺失/空按 0——
                        # 整行取数必有该键，0.00/缺省均安全；数值异常整列留空）
                        base = float(weight) * float(row.get(sources[-1]))
                        surcharge_raw = row.get("surcharge_amount")
                        surcharge = 0.0 if surcharge_raw in (None, "") else float(surcharge_raw)
                    except (TypeError, ValueError):
                        row[col] = None
                        continue
                    row[col] = base + surcharge
                    continue
                row[col] = _computed_value(row, sources)
    return rows


# ---------------------------------------------------------------------------
# 行写入辅助（write_only 模式：WriteOnlyCell 才能设置 number_format）
# ---------------------------------------------------------------------------


#: 规范十进制串判定（str→Decimal 归一的唯一闸门，[2026-09-12 只读扩展]）：
#: 可选正负号 + 纯十进制（整数或小数），strip 首尾空白后匹配。
#: 刻意**不认**：空串、"-"、千分位 "1,234"、货币符号 "￥100"、中文、科学
#: 计数法 "1e3"、带单位 "12kg"、全角数字 "１２"——这些不是金额数据，误认
#: 会把错误数据加进合计。字符类显式写 [0-9]（不用 \d：\d 会匹配全角数字）。
#: ERP 侧 Decimal 经 str() 序列化（Numeric(12,2) → "123.45"），恒为本形态，
#: 不会出现科学计数法。
_NUMERIC_STR_RE: re.Pattern[str] = re.compile(
    r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)$"
)


def _to_decimal(value: Any) -> Decimal | None:
    """数值 → Decimal（无法安全判定为非数值、非规范数字串时返回 None）。

    - bool 明确排除（True/False 不是金额）；
    - Decimal 原样返回；int → Decimal(int)；
    - float → Decimal(str(value))（保持既有浮点语义，不引入额外误差）；
    - str → 仅当 strip 后匹配 _NUMERIC_STR_RE 才 Decimal(s)，否则 None；
    - 其余类型 → None。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        text = value.strip()
        if not _NUMERIC_STR_RE.match(text):
            return None
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    return None


def _is_number(value: Any) -> bool:
    """数值判定（纯闸门）：int/float/Decimal 原语义保留 + **规范数字字符串**。

    [2026-09-12 只读扩展] ERP MCP 把 Decimal 序列化成字符串（见
    hengfa-erp/backend/app/mcp/query.py _serialize_value）——旧实现只认
    int/float，金额列全被判非数值列，总计行永远不出金额合计。现把
    「规范数字字符串」（strip 后 ^[+-]?([0-9]+(\\.[0-9]*)?|\\.[0-9]+)$，
    ASCII 数字）一并认作数值；空串、"-"、"1,234"、"￥100"、中文、"1e3"、
    全角数字等一律**不**认。

    [2026-09-12 回归修复] 本函数只回答「这个值长不长得像数值」，**不再**
    单独决定列是否按数值列处理——字符串认定被限制在 **sum_cols 登记列**
    内（见 _numeric_columns / _append_row / _append_subtotal_row），否则
    「非空值全为纯数字串」的电话/工号/银行账号/税号列会被误判数值列、被
    求和并套 #,##0.00（交付给用户的是数字错误的 Excel）。非登记列一律走
    _is_legacy_number（只认 int/float/Decimal）。
    """
    return _to_decimal(value) is not None


def _is_legacy_number(value: Any) -> bool:
    """旧语义数值判定：仅 int/float/Decimal（bool 除外）——**不认字符串**。

    非 sum_cols 登记列的判列口径（本批前语义）：ERP 字符串化的业务码
    （电话/工号/银行账号/税号/单价等）不是数值，既不入合计、也不套数值格式。
    """
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float, Decimal))


def _numeric_columns(
    columns: list[str],
    rows: list[dict[str, Any]],
    sum_cols: frozenset[str] | None = None,
) -> list[bool]:
    """数值列判定：该列非空值全部判为数值（空值/缺失不判非数值）。

    [2026-09-12 回归修复] 字符串→数值的认定**仅限 sum_cols 登记列**：

    - 登记列（transactions.amount、metal.total_weight/total_price 等）→
      经 _is_number，放行 ERP 字符串化 Decimal；
    - 其余列（sum_cols 缺省/不含该列）→ 经 _is_legacy_number，只认
      int/float/Decimal——电话/工号/银行账号/税号等纯数字串列**不**被判
      数值列（既不求和也不套数值格式，恢复本批前语义）。
    """
    registered = sum_cols or frozenset()
    result: list[bool] = []
    for col in columns:
        non_empty = [r.get(col) for r in rows]
        non_empty = [v for v in non_empty if v is not None and v != ""]
        checker = _is_number if col in registered else _is_legacy_number
        result.append(bool(non_empty) and all(checker(v) for v in non_empty))
    return result


def _header_cell(ws: Any, value: str) -> WriteOnlyCell:
    """构造带样式的表头单元格（加粗 / 实心浅色底纹 / 水平垂直居中 / 四边细边框）。

    write_only 模式下只有 WriteOnlyCell 能承载样式；本函数仅用于表头行，
    数据行/总计行仍按 _append_row / _append_subtotal_row 原样写入。
    """
    cell = WriteOnlyCell(ws, value=value)
    cell.font = Font(bold=True)
    cell.fill = PatternFill("solid", fgColor=HEADER_FILL_COLOR)
    cell.alignment = Alignment(horizontal="center", vertical="center")
    thin = Side(style="thin")
    cell.border = Border(left=thin, right=thin, top=thin, bottom=thin)
    return cell


def _append_row(
    ws: Any,
    columns: list[str],
    row: dict[str, Any],
    numeric_cols: list[bool],
    *,
    sum_cols: frozenset[str] | None = None,
) -> None:
    """写数据行：数值列单元格套 NUMERIC_NUMBER_FORMAT，其余列原样写。

    数值形态判定与 _numeric_columns **同口径**（字符串仅 sum_cols 登记列
    放行，见 _is_number / _is_legacy_number）——避免非登记列（电话/工号/
    银行账号/税号等纯数字串）被判数值列并套 #,##0.00。

    [2026-09-13 复核问题 1] sum_cols 登记列以 **_to_decimal 归一后的
    Decimal** 落盘（None 回退原值）：ERP MCP 把 Numeric/Decimal 序列化成
    **字符串**下发，旧实现把该字符串原样写进单元格 → 落盘 data_type='s'
    （文本），Excel 显示「以文本形式存储的数字」、不能数值排序/求和/透视；
    而总计行（_append_subtotal_row）写 Decimal → data_type='n'。两者口径
    不一致，用户对明细金额列求和会得 0/算错。登记列统一写 Decimal 后，
    数据格与总计行同为真数值列。非登记列**不进**该归一（纯数字串如电话/
    工号/银行账号/税号/单价仍按字符串落盘，data_type='s'）。
    """
    registered = sum_cols or frozenset()
    cells = []
    for idx, col in enumerate(columns):
        value = row.get(col)
        checker = _is_number if col in registered else _is_legacy_number
        if numeric_cols[idx] and checker(value):
            # 登记列：str 数字串 → Decimal（真数值格）；None/不可归一 → 原值
            stored = value
            if col in registered:
                decimal_value = _to_decimal(value)
                if decimal_value is not None:
                    stored = decimal_value
            cell = WriteOnlyCell(ws, value=stored)
            cell.number_format = NUMERIC_NUMBER_FORMAT
            cells.append(cell)
        else:
            cells.append(value)
    ws.append(cells)


def _append_subtotal_row(
    ws: Any,
    columns: list[str],
    numeric_cols: list[bool],
    label: str,
    group: list[dict[str, Any]],
    *,
    sum_cols: frozenset[str] | None = None,
) -> None:
    """汇总行：首列标签（总计），**仅合计 sum_cols 登记列**，其余列留空。

    [2026-09-12 回归修复] 删除「sum_cols=None → 全数值列兜底合计」路径：
    None 现视为**空集**（不合计任何列）。此前未登记实体（customers/
    suppliers/employees）走兜底，把电话/工号/银行账号/税号等纯数字串列
    当数值列求和；现所有可导出实体（QUERYABLE_ENTITIES，2026-09-16 起 15 个）
    均显式登记
    sum_cols，None 不再代表任何合法实体——保留兜底只会下次再咬人。

    - sum_cols 非空（登记列，R4.export.fields 规则 6）→ 只合计列内且数值
      的列——metal 只合计 总重量/总价格，powder 只合计 总重量；比率（每
      公斤价格）/单价/计数（数量/色号）列不合计。
    """
    registered = sum_cols or frozenset()
    cells: list[Any] = [f"{label}"]
    for idx in range(1, len(columns)):
        col = columns[idx]
        do_sum = col in registered and numeric_cols[idx]
        if do_sum:
            # Decimal 累加（不用 float）：金额列已是字符串形态，float 累积会
            # 引入 0.1+0.2 类误差并落到 Excel；Decimal 保持精确语义。
            total = Decimal("0")
            for row in group:
                amount = _to_decimal(row.get(col))
                if amount is not None:
                    total += amount
            cell = WriteOnlyCell(ws, value=total)
            cell.number_format = NUMERIC_NUMBER_FORMAT
            cells.append(cell)
        else:
            cells.append("")
    ws.append(cells)


def _unit_key(value: Any) -> str:
    """重量单位归一化桶键（None/缺失 → ""，其余去首尾空白后原样）。"""
    if value is None:
        return ""
    return str(value).strip()


def _append_subtotal_rows(
    ws: Any,
    columns: list[str],
    numeric_cols: list[bool],
    label: str,
    rows: list[dict[str, Any]],
    *,
    sum_cols: frozenset[str] | None = None,
) -> None:
    """汇总行：单桶单行；多重量单位分桶各一行（[2026-09-10 P1-10]）。

    判桶按**数据**（行内 ``weight_unit`` 键，与输出列无关——需合计的
    重量列恒为本地计算列，含它即触发整行取数（见 ExcelExporter.export
    步骤 2.1），行内必带该键）：

    - 数据行出现多个不同单位值（None/空串归一为「未注明单位」桶）→
      **按单位分桶各出一行**（标签「总计（kg）」「总计（m²）」），每行只
      合计本桶行——不同物理量（kg 与 m²）绝不相加；未注明单位的行单独
      成桶（无法归入任何单位）；
    - 数据无 weight_unit 键、或全部行同一单位值 → 单行总计（原行为）。
    """
    buckets: list[str] = []
    for row in rows:
        key = _unit_key(row.get("weight_unit"))
        if key not in buckets:
            buckets.append(key)
    if len(buckets) <= 1:
        _append_subtotal_row(
            ws, columns, numeric_cols, label, rows, sum_cols=sum_cols
        )
        return
    for unit in buckets:
        group = [r for r in rows if _unit_key(r.get("weight_unit")) == unit]
        bucket_label = f"{label}（{unit}）" if unit else f"{label}（未注明单位）"
        _append_subtotal_row(
            ws, columns, numeric_cols, bucket_label, group, sum_cols=sum_cols
        )


# ---------------------------------------------------------------------------
# 过期清理（启动时 + 每次导出时调用；best effort）
# ---------------------------------------------------------------------------


def cleanup_expired_exports(
    export_dir: str, ttl_seconds: int | None = None
) -> list[str]:
    """清理过期导出文件，返回被删除的路径列表。

    - 过期的 *.meta.json（expires_at <= now）→ 删 meta + 对应 xlsx；
    - 孤儿 xlsx（无 meta 且 mtime 早于 now - ttl）→ 一并删；
    - 全程 best effort：单文件失败只 warning 不阻断其余清理。
    """
    if not export_dir:
        return []
    ttl = settings.export_file_ttl_seconds if ttl_seconds is None else ttl_seconds
    now = datetime.now(timezone.utc)
    removed: list[str] = []

    def _try_remove(path: str) -> None:
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            logger.warning("export_cleanup_remove_failed", path=path, exc_info=True)

    # 1. 过期 meta.json → meta + xlsx 一并删
    for meta_path in glob.glob(os.path.join(export_dir, "*.meta.json")):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            expires_raw = meta.get("expires_at") if isinstance(meta, dict) else None
            expires = (
                datetime.fromisoformat(str(expires_raw))
                if isinstance(expires_raw, str)
                else None
            )
            if expires is not None and expires <= now:
                file_id = meta.get("file_id") if isinstance(meta, dict) else None
                _try_remove(meta_path)
                if file_id:
                    _try_remove(os.path.join(export_dir, f"{file_id}.xlsx"))
        except Exception:
            logger.warning("export_meta_parse_failed", path=meta_path, exc_info=True)

    # 2. 孤儿 xlsx（无对应 meta 且 mtime 早于 now - ttl）
    cutoff = now - timedelta(seconds=max(1, ttl))
    for xlsx_path in glob.glob(os.path.join(export_dir, "*.xlsx")):
        base = os.path.basename(xlsx_path)[: -len(".xlsx")]
        if os.path.exists(os.path.join(export_dir, f"{base}.meta.json")):
            continue
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(xlsx_path), tz=timezone.utc)
            if mtime < cutoff:
                _try_remove(xlsx_path)
        except OSError:
            logger.warning("export_orphan_check_failed", path=xlsx_path, exc_info=True)
    return removed
