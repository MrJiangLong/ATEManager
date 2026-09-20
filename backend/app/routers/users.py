"""用户管理（/api/admin/users，仅 admin）。

三角色模型：viewer 只读 / operator 产线操作 / admin 配置+用户管理。
守卫核心：系统中必须永远存在至少一个"启用中的 admin"——所有会触碰
这一不变量的操作（自删、自停、自降权、停用/降级最后一个 admin）一律 409。
"""

from typing import List, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..errors import AppError, conflict_error, get_or_404
from ..security import ALL_ROLES, ROLE_ADMIN, hash_password, require_admin

router = APIRouter(prefix="/api/admin/users", tags=["admin-用户"])


def _active_admin_count(db: Session, exclude_user_id: Optional[int] = None) -> int:
    q = db.query(models.User).filter(models.User.role == ROLE_ADMIN, models.User.is_active.is_(True))
    if exclude_user_id is not None:
        q = q.filter(models.User.id != exclude_user_id)
    return q.count()


def _assert_not_last_admin(
    db: Session,
    target: models.User,
    *,
    removing: bool,
    new_role: Optional[str] = None,
    deactivating: bool = False,
) -> None:
    """目标操作会消灭"最后一个有效 admin"时拒绝。

    有效 admin = role=admin 且 is_active。删除 / 停用 / 降权任一都可能
    消灭最后一个有效 admin，三者都要过这道闸。
    """
    if not (target.role == ROLE_ADMIN and target.is_active):
        return  # 目标本来就不是有效 admin，不影响不变量
    would_lose_admin = removing or deactivating or (new_role is not None and new_role != ROLE_ADMIN)
    if would_lose_admin and _active_admin_count(db, exclude_user_id=target.id) == 0:
        raise conflict_error(
            "last_admin",
            "cannot remove/disable/demote the last active admin",
        )


@router.get("", response_model=List[schemas.UserOut], summary="用户清单(仅 admin)")
def list_users(db: Session = Depends(get_db), user: models.User = Depends(require_admin)):
    return db.query(models.User).order_by(models.User.id).all()


@router.post("", response_model=schemas.UserOut, status_code=201, summary="新建用户(仅 admin)")
def create_user(payload: schemas.UserCreateIn, db: Session = Depends(get_db), user: models.User = Depends(require_admin)):
    if payload.role not in ALL_ROLES:
        raise AppError(400, "invalid_role", f"role must be one of {', '.join(ALL_ROLES)}")
    exists = db.query(models.User).filter(models.User.username == payload.username).first()
    if exists is not None:
        raise conflict_error("username_exists", f"username '{payload.username}' already exists")
    row = models.User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        role=payload.role,
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.put("/{user_id}", response_model=schemas.UserOut, summary="更新用户(角色/启停/改名/重置密码，仅 admin)")
def update_user(
    user_id: int,
    payload: schemas.UserUpdateIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_admin),
):
    target = get_or_404(db, models.User, user_id, "user")
    if payload.role is not None and payload.role not in ALL_ROLES:
        raise AppError(400, "invalid_role", f"role must be one of {', '.join(ALL_ROLES)}")
    if payload.is_active is False or payload.role is not None:
        _assert_not_last_admin(db, target, removing=False, new_role=payload.role, deactivating=payload.is_active is False)
    if payload.full_name is not None:
        target.full_name = payload.full_name
    if payload.role is not None:
        target.role = payload.role
    if payload.is_active is not None:
        target.is_active = payload.is_active
    if payload.password:
        target.password_hash = hash_password(payload.password)
    db.commit()
    db.refresh(target)
    return target


@router.delete("/{user_id}", status_code=204, summary="删除用户(仅 admin)")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_admin),
):
    if user_id == user.id:
        raise conflict_error("cannot_delete_self", "cannot delete your own account")
    target = get_or_404(db, models.User, user_id, "user")
    _assert_not_last_admin(db, target, removing=True)
    db.delete(target)
    db.commit()
