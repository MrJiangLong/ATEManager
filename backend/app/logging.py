"""日志系统：控制台 + 滚动文件，配置 uvicorn 与应用 logger，避免重复输出。

在 app 导入时调用一次 configure_logging()。
uvicorn 的 configure_logging 在加载应用之前执行，因此本模块的处理器会生效。
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import settings

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s"
_LEVEL = settings.LOG_LEVEL.upper()


def _console_handler() -> logging.Handler:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
    return handler


def _file_handler():
    log_path = Path(settings.LOG_FILE)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
        return handler
    except OSError as exc:
        print(f"[warn] 日志文件初始化失败({exc})，仅输出到控制台")
        return None


def configure_logging() -> None:
    console = _console_handler()
    file = _file_handler()

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "app"):
        logger = logging.getLogger(logger_name)
        logger.setLevel(_LEVEL)
        logger.handlers.clear()
        logger.addHandler(console)
        if file:
            logger.addHandler(file)
        logger.propagate = False

    # 压制第三方噪音
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """获取应用命名空间下的 logger，如 get_logger('main')"""
    tag = name.replace("app.", "", 1)
    return logging.getLogger(f"app.{tag}")
