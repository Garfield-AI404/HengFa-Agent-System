"""会话记忆存档存储（2026-08-24 定稿：压缩前原始会话全量落库，memory_lookup 查询源）。

- append：按 (thread_id, segment_no) 唯一追加；messages 为 [{role, content}] 文本形态
  （压缩节点从 LangChain 消息对象转换；不还原为消息对象——查询只回文本）；
- get_segment / search：memory_lookup 工具查询源。

扩展工具查询存档（tool_query_archive）：
- append_tool_query：只读工具调用写入（结果写对话历史前），按
  (thread_id, query_id) 唯一追加，items 为原始全量明细（不裁剪）；
  [2026-10-06 §3.8] 正常返回（成功 / 0 行 / ok=False）+ 执行抛异常 +
  参数校验失败三条路径各落一行——arguments（本次入参）/ok（成败）两列
  可空、供取证回放（不进模型上下文）；未知工具 / NotImplementedError /
  写工具 / archive 未接线不落（见 tools_node._archive_read_tool）；
- get_tool_query：memory_lookup 按 query_id 查回 (items, total, entity)
  ——total 为存档总行数（写档 count 列），供分页取齐全量；entity 供
  memory_lookup 按实体白名单裁剪明细；
- delete_thread 联动覆盖两表（routes_chat.delete_thread 删除会话时一并
  清理查询存档，11.4 仅会话内可查、随会话删除）。
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from administrative_agent.persistence.models import ChatArchive, ToolQueryArchive


class ArchiveStore:
    """会话记忆存档存储（memory_lookup 工具查询源）。

    依赖注入 session_factory（与仓储层同模式：请求级 app.state 优先，
    默认 SessionLocal）；查询/写入异常上抛——压缩节点捕获后降级
    （fail closed），工具侧转错误结果。
    """

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def append(self, *, thread_id: str, segment_no: int, messages: list[dict]) -> None:
        """追加存档段（唯一键冲突 → 幂等跳过：不覆盖已有段）。"""
        session = self._session_factory()
        try:
            existing = (
                session.query(ChatArchive)
                .filter_by(thread_id=thread_id, segment_no=segment_no)
                .first()
            )
            if existing is not None:
                return  # 幂等：已存在不覆盖
            session.add(
                ChatArchive(thread_id=thread_id, segment_no=segment_no, messages=messages)
            )
            session.commit()
        except IntegrityError:
            session.rollback()  # 并发重复追加：唯一键冲突 → 幂等跳过（不覆盖）
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_segment(self, thread_id: str, segment_no: int) -> list[dict] | None:
        """按段号取回（不存在 → None）。"""
        session = self._session_factory()
        try:
            row = (
                session.query(ChatArchive)
                .filter_by(thread_id=thread_id, segment_no=segment_no)
                .first()
            )
            return list(row.messages) if row is not None else None
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def append_tool_query(
        self,
        *,
        thread_id: str,
        query_id: str,
        entity: str,
        count: int,
        items: list,
        arguments: dict | None = None,
        ok: bool | None = None,
    ) -> None:
        """追加工具查询存档（唯一键冲突 → 幂等跳过：不覆盖已有记录）。

        [2026-08-28 两级数据访问 2a] items 为原始全量明细（不裁剪，含
        噪音字段）；写入失败上抛——tools 节点捕获后降级（无存档，上下文
        走摘要路径），不阻断对话。

        [2026-10-06 §3.8 读工具调用参数落审计] ``arguments`` 为本次调用
        模型传的入参（JSON 形态）、``ok`` 为本次成败——二者可空（缺省
        None，历史/旧调用方形态不变）；仅供取证回放，不进模型上下文。
        """
        session = self._session_factory()
        try:
            existing = (
                session.query(ToolQueryArchive)
                .filter_by(thread_id=thread_id, query_id=query_id)
                .first()
            )
            if existing is not None:
                return  # 幂等：已存在不覆盖
            session.add(
                ToolQueryArchive(
                    thread_id=thread_id,
                    query_id=query_id,
                    entity=entity,
                    count=count,
                    items=items,
                    arguments=arguments,
                    ok=ok,
                )
            )
            session.commit()
        except IntegrityError:
            session.rollback()  # 并发重复追加：唯一键冲突 → 幂等跳过（不覆盖）
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_tool_query(self, thread_id: str, query_id: str) -> tuple[list, int, str] | None:
        """按 query_id 取回 (items, total, entity)（不存在 → None）。

        [2026-08-28 D1 修复] total = 写档时的 count 列（该次查询返回的
        原始行数，全量口径，不随 memory_lookup 分页窗口变化）；count 列
        缺省/为空（历史异常数据）时保守回退 len(items)。items 为原始全量
        明细（含噪音字段，不裁剪——裁剪由 memory_lookup 按 entity 白名单
        进行）。entity = 写档时的实体名（crop_items 白名单裁剪依据；
        [2026-09-09] 查询类工具以外可为 catalog/erp_calc 等信封标记——
        不在裁剪白名单，条目原样返回）。

        [2026-10-06 §3.8] 语义变化：0 行/失败调用现在也会落行——对这类
        query_id，本方法由「返回 None」变为「返回 ([], 0, entity)」。
        **模型侧不可达**：query_id（可回查标识）仅对「成功 + 非空 items +
        存档标识」的调用挂给模型（tools_node），故 0 行/失败的 query_id
        不会进入模型上下文，此变化对 memory_lookup 工具与既有行为无实际
        影响（仅改变「手工以未挂载的 query_id 调用」时的返回值）。
        """
        session = self._session_factory()
        try:
            row = (
                session.query(ToolQueryArchive)
                .filter_by(thread_id=thread_id, query_id=query_id)
                .first()
            )
            if row is None:
                return None
            items = list(row.items)
            total = row.count if row.count else len(items)
            return items, total, row.entity
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def delete_thread(self, thread_id: str) -> None:
        """删除该 thread 全部存档（会话压缩段 + 工具查询存档；幂等不抛错）。

        [2026-08-28 两级数据访问 2a] 删除会话（DELETE /chat/threads）时
        联动清理查询存档（11.4 随会话删除）——只删会话自己的存档，不触碰
        审计/复核/映射等 Agent 表（11.1 审计不可变）。
        """
        session = self._session_factory()
        try:
            session.query(ChatArchive).filter_by(thread_id=thread_id).delete()
            session.query(ToolQueryArchive).filter_by(thread_id=thread_id).delete()
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
