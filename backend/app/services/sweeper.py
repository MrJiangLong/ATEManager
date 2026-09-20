"""孤儿锁回收：后台周期任务，无需等待他人抢锁即释放失联/超时锁。

- 失联（心跳断流 > LOCK_HEARTBEAT_GRACE_SEC）→ 立即解锁，会话 ABORTED，不计产品失败
- 硬超时（持锁 > stations.timeout_sec）→ 解锁并计一次失败
- 连续失联达到 LOST_LOCK_FAIL_THRESHOLD 次才计一次失败（机器问题不冤枉产品）

以 asyncio 任务形式常驻，零新增依赖；关闭时随 lifespan 一起退出。
"""

import asyncio
import contextlib
from typing import Optional

from ..config import settings
from ..database import SessionLocal
from ..logging import get_logger
from .gate import sweep_orphan_locks

logger = get_logger("sweeper")

_TASK: Optional[asyncio.Task] = None

def run_once() -> dict:
    """同步执行一次回收（测试与手动触发入口）。"""
    db = SessionLocal()
    try:
        stats = sweep_orphan_locks(db)
        if any(stats.values()):
            logger.info(
                "孤儿锁回收：失联 %s / 超时 %s / 计失败 %s",
                stats.get("lost", 0),
                stats.get("expired", 0),
                stats.get("failed", 0),
            )
        return stats
    except Exception as exc:
        db.rollback()
        logger.exception("孤儿锁回收失败：%s", exc)
        return {"lost": 0, "expired": 0, "failed": 0, "error": str(exc)}
    finally:
        db.close()

async def _loop() -> None:
    interval = max(5, settings.SWEEPER_INTERVAL_SEC)
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.get_event_loop().run_in_executor(None, run_once)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("孤儿锁回收异常：%s", exc)

def start() -> None:
    """在 FastAPI lifespan 内启动。"""
    global _TASK
    if _TASK is not None:
        return
    if not settings.SWEEPER_ENABLED:
        logger.info("孤儿锁回收任务已按配置关闭（SWEEPER_ENABLED=false）")
        return
    try:
        _TASK = asyncio.get_event_loop().create_task(_loop())
        logger.info("孤儿锁回收任务已启动（%ss/轮）", settings.SWEEPER_INTERVAL_SEC)
    except RuntimeError:
        logger.warning("无事件循环，孤儿锁回收任务未启动")

async def stop() -> None:
    global _TASK
    if _TASK is None:
        return
    _TASK.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _TASK
    _TASK = None

