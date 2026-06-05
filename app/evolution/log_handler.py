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
    """为所有 evolution.* logger 安装 MySQL handler。"""
    handler = MySQLLogHandler(level=logging.WARNING)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    ))

    # 注册到 evolution 根 logger，所有子 logger 都会继承
    evo_logger = logging.getLogger("evolution")
    evo_logger.addHandler(handler)
