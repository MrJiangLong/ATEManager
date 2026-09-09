"""业务异常与统一错误响应包。

上位机以 **HTTP 状态码** 作为第一分流依据（需求 2 的 403 阻断 / 409 复测拦截 / 400 工艺不符），
响应体中的 `exit_code` 保留给既有产线脚本做数值分支。

    GET  /api/v1/...      200
    POST check-in         200 放行
                          400 工位不在该流程 / 用例ID清单不匹配、漏测拦截
                          403 跳站拦截 / 固件不符 / 已锁定 / 已报废 / 锁超时
                          409 复测拦截 / 锁冲突
    POST check-out        201 落库成功（即 ACK）
                          400 漏测拦截
                          403 锁失效
"""

from typing import Any, Optional

# 退出码（与 HTTP 状态并存，供上位机数值分支）
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_GATE_BLOCKED = 10
EXIT_LOCK_CONFLICT = 11
EXIT_MISSING_MANDATORY = 13
EXIT_LOCK_EXPIRED = 14
EXIT_CASE_ID_MISMATCH = 15
EXIT_PRODUCT_LOCKED = 16


class AppError(Exception):
    """业务异常：由全局处理器转换为统一响应包。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        exit_code: int = EXIT_GATE_BLOCKED,
        data: Optional[Any] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.data = data


def bad_request(code: str, message: str, **kw) -> AppError:
    return AppError(400, code, message, **kw)


def forbidden(code: str, message: str, **kw) -> AppError:
    return AppError(403, code, message, **kw)


def conflict_error(code: str, message: str, **kw) -> AppError:
    return AppError(409, code, message, **kw)


def not_found(code: str = "not_found", message: str = "Resource not found") -> AppError:
    return AppError(404, code, message, exit_code=EXIT_GATE_BLOCKED)


def get_or_404(db, model, key, label: str):
    """取实体，不存在抛 404。"""
    row = db.get(model, key)
    if row is None:
        raise not_found(f"{label}_not_found", f"{label}_not_found: {key}")
    return row
