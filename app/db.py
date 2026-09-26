"""服务端业务模块。"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import DATABASE_URL

# SQLite 默认忙等超时为 0，并发写入会立即报 database is locked；
# 给予合理的等待时间，让并发导入与身份版本提交得以串行完成。
_connect_args = (
    {"check_same_thread": False, "timeout": 30}
    if DATABASE_URL.startswith("sqlite")
    else {}
)

engine = create_engine(
    DATABASE_URL, future=True, pool_pre_ping=True, connect_args=_connect_args
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Iterator[Session]:
    """执行确定性的业务处理。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
