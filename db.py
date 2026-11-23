import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
url = os.getenv("DATABASE_URL", "sqlite:///./data.db")
if url.startswith("postgres://"): url = url.replace("postgres://", "postgresql+psycopg2://", 1)
elif url.startswith("postgresql://"): url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
_pg = not url.startswith("sqlite")
engine = create_engine(url, pool_pre_ping=True, connect_args={"check_same_thread": False} if not _pg else {},
                       **(dict(pool_size=int(os.getenv("DB_POOL", "10")), max_overflow=int(os.getenv("DB_OVERFLOW", "20")), pool_timeout=15, pool_recycle=280) if _pg else {}))
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
class Base(DeclarativeBase): pass
def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()
