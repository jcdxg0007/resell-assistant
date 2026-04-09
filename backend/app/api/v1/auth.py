from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import create_access_token, verify_password, get_password_hash
from app.models.system import User

router = APIRouter()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


@router.post("/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == form_data.username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    token = create_access_token(subject=str(user.id))
    return {"access_token": token, "token_type": "bearer", "user": {"id": str(user.id), "username": user.username, "display_name": user.display_name}}


@router.post("/init", summary="初始化管理员账号（仅首次）")
async def init_admin(username: str, password: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User))
    if result.first():
        raise HTTPException(status_code=400, detail="管理员已存在")
    user = User(username=username, hashed_password=get_password_hash(password), is_admin=True, display_name="管理员")
    db.add(user)
    await db.commit()
    return {"message": "管理员创建成功"}
