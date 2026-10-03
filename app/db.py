"""数据库引擎、会话与建库初始化。"""

from __future__ import annotations

import logging
import time

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

logger = logging.getLogger("downtime.db")


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db(retries: int = 30, delay: float = 1.0) -> None:
    """建库建表：创建全部表结构，以及"同设备班次不重叠"的排他约束。

    带重试以等待 Compose 中数据库就绪。
    """
    from . import models  # noqa: F401  确保模型已注册到 metadata

    last_err: Exception | None = None
    for _ in range(retries):
        try:
            with engine.begin() as conn:
                Base.metadata.create_all(conn)
            break
        except OperationalError as exc:  # 数据库尚未就绪
            last_err = exc
            time.sleep(delay)
    else:
        raise RuntimeError(f"cannot initialize database: {last_err}")
    _create_exclusion_constraint()
    _migrate_change_feed()
    _migrate_adjudications()


def _migrate_adjudications() -> None:
    """为现场人工裁定补齐既有库结构（幂等，可重复执行）。

    manual_adjudications 由 create_all 直接建出；既有库只需把 shift_results.trigger
    加宽以容纳 adjudication / adjudication_revoke 触发器。
    """
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE shift_results ALTER COLUMN trigger TYPE VARCHAR(32)")
        )


def _migrate_change_feed() -> None:
    """为既有库补齐变更同步结构（幂等，可重复执行）。

    - shift_results 增加全局递增序号列 seq，并按原自增 id 顺序回填既有修订
      （历史数据无法还原精确提交顺序，id 顺序是最接近的可用次序）；
    - 单例计数器 change_counter 初始化为当前最大序号，之后由写事务内
      行锁递增分配（见 services._allocate_seq）。
    新建库由 create_all 直接建出完整结构，以下语句均为空操作。
    """
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE shift_results ADD COLUMN IF NOT EXISTS seq INTEGER")
        )
        conn.execute(
            text(
                """
                DO $$
                DECLARE
                    base_seq int;
                BEGIN
                    SELECT COALESCE(MAX(seq), 0) INTO base_seq FROM shift_results;
                    UPDATE shift_results r
                    SET seq = base_seq + n.rn
                    FROM (
                        SELECT id, row_number() OVER (ORDER BY id) AS rn
                        FROM shift_results
                        WHERE seq IS NULL
                    ) n
                    WHERE r.id = n.id;
                END
                $$;
                """
            )
        )
        conn.execute(text("ALTER TABLE shift_results ALTER COLUMN seq SET NOT NULL"))
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_shift_results_seq "
                "ON shift_results (seq)"
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO change_counter (id, last_seq)
                SELECT 1, COALESCE(MAX(seq), 0) FROM shift_results
                ON CONFLICT (id) DO NOTHING
                """
            )
        )


def _create_exclusion_constraint() -> None:
    """为 shifts 增加排他约束（半开区间 [start, end)，相邻不算重叠）。

    依赖 btree_gist 扩展（官方 postgres 镜像自带）；个别精简发行版缺失时
    降级为仅应用层校验并输出告警。
    """
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
            conn.execute(
                text(
                    """
                    DO $$
                    BEGIN
                        ALTER TABLE shifts
                        ADD CONSTRAINT shifts_no_overlap_excl
                        EXCLUDE USING gist (
                            device_id WITH =,
                            tstzrange(start_ts, end_ts, '[)') WITH &&
                        );
                    EXCEPTION WHEN duplicate_object OR duplicate_table THEN
                        NULL;
                    END
                    $$;
                    """
                )
            )
    except SQLAlchemyError as exc:
        logger.warning(
            "btree_gist 不可用，未创建班次排他约束（应用层重叠校验仍生效）：%s",
            exc,
        )
