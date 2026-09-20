"""Web 管理端鉴权入口（/api/auth）。"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..errors import EXIT_GATE_BLOCKED, AppError
from ..security import create_access_token, current_user, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=schemas.TokenResponse, summary="登录")
def login(payload: schemas.LoginRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.username == payload.username).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise AppError(401, "invalid_credentials", "Username or password incorrect", exit_code=EXIT_GATE_BLOCKED)
    if not user.is_active:
        raise AppError(403, "user_disabled", "Account is disabled, contact administrator")
    return {"access_token": create_access_token(user), "token_type": "bearer", "user": user}


@router.get("/me", response_model=schemas.UserOut, summary="当前登录用户")
def me(user: models.User = Depends(current_user)):
    return user


@router.post("/change-password", response_model=schemas.MessageOut, summary="修改密码")
def change_password(
    payload: schemas.PasswordChangeRequest,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    if not verify_password(payload.current_password, user.password_hash):
        raise AppError(400, "current_password_incorrect", "Current password is incorrect", exit_code=EXIT_GATE_BLOCKED)
    if payload.current_password == payload.new_password:
        raise AppError(400, "new_password_same_as_current", "New password must differ", exit_code=EXIT_GATE_BLOCKED)
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    return schemas.MessageOut(code="password_changed")
