"""evolution/log_handler.py — MySQL 日志 Handler

将 evolution.* 命名空间下 WARNING 及以上级别的日志异步批量写入 MySQL，
供面板回溯排查。使用内存缓冲 + emit 时写库，避免每条日志都开 session。
"""
import logging
import threading
from datetime import datetime, timezone

from evolution.db import get_session
from evolution.models import EvolutionPipelineLog


class MySQLLogHandler(logging.Handler):
    """将日志记录写入 evolution_pipeline_logs 表。"""

    def __init__(self, level=logging.WARNING):
        super().__init__(level)

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            session = get_session()
            try:
                entry = EvolutionPipelineLog(
                    logger_name=record.name,
                    level=record.levelname,
                    message=msg,
                    session_id=getattr(record, "session_id", None),
                )
                session.add(entry)
                session.commit()
            except Exception:
                session.rollback()
            finally:
                session.close()
        except Exception:
            # 日志 handler 绝不能抛异常，静默丢弃
            pass


def install_evolution_log_handler():
    """为自进化相关 logger 安装 MySQL handler。

    覆盖 evolution.* 命名空间，以及 main_ws 的 ws_agent_service logger
    —— game_over / 局后管道 / minimal archive 的关键日志都在后者下，
    否则面板回溯排查时这些失败全程无声。
    """
    handler = MySQLLogHandler(level=logging.WARNING)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    ))

    for name in ("evolution", "ws_agent_service"):
        target = logging.getLogger(name)
        if not any(isinstance(h, MySQLLogHandler) for h in target.handlers):
            target.addHandler(handler)
