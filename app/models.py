"""ORM 模型。

核心表：
- batches                批次（草稿 / 已生效）
- slips                  批次内登记的转学单版本（可对同一单号做更正）
- effective_slips        当前生效的学籍依据（由已生效批次维护）
- basis_records          批次生效时留存的复查依据快照（不可变）
- basis_student_schools  复查依据中“归属可能发生变化的学校”索引，供清单查询精确取数
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UtcDateTime(TypeDecorator):
    """始终以 UTC 读写的 datetime。

    SQLite 不保存时区，直接存取会让“提交响应”带 ``Z``、而重启后读回变成
    无时区值。这里在写入时统一转为 UTC，读回时补回 UTC 时区，保证同一记录
    在提交响应、详情接口与重启之后表示一致。
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Counter(Base):
    """跨批次的单调计数器（如提交序号），在写事务内递增以获得写锁。"""

    __tablename__ = "counters"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_no: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), nullable=False, default=utcnow
    )
    committed_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    # 提交顺序，用于判断同一单号多个版本中哪一个生效。
    commit_seq: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    # 回执与复查信息，在生效时一次性写入，之后不再变化。
    slip_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    basis_record_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    basis_prior_record_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    basis_batch_record_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    affected_student_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    changed_student_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    slips: Mapped[list[Slip]] = relationship(
        back_populates="batch",
        cascade="all, delete-orphan",
        order_by="Slip.id",
    )


class Slip(Base):
    """批次内的一条转学单版本。"""

    __tablename__ = "slips"
    __table_args__ = (
        UniqueConstraint("batch_id", "slip_no", name="uq_slips_batch_slip_no"),
        Index("ix_slips_student_no", "student_no"),
        Index("ix_slips_slip_no", "slip_no"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("batches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    slip_no: Mapped[str] = mapped_column(String(64), nullable=False)
    student_no: Mapped[str] = mapped_column(String(64), nullable=False)
    school_id: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), nullable=False, default=utcnow, onupdate=utcnow
    )

    batch: Mapped[Batch] = relationship(back_populates="slips")


class EffectiveSlip(Base):
    """已生效的学籍归属依据；每个单号只保留最新已生效版本。"""

    __tablename__ = "effective_slips"
    __table_args__ = (
        # “同一学生同日最多一单”的数据库级兜底约束。
        UniqueConstraint(
            "student_no", "effective_date", name="uq_effective_student_date"
        ),
        Index("ix_effective_student", "student_no"),
    )

    slip_no: Mapped[str] = mapped_column(String(64), primary_key=True)
    student_no: Mapped[str] = mapped_column(String(64), nullable=False)
    school_id: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    source_batch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_batch_no: Mapped[str] = mapped_column(String(64), nullable=False)
    source_commit_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), nullable=False, default=utcnow, onupdate=utcnow
    )


class BasisRecord(Base):
    """批次生效时冻结的复查依据。

    每个受影响学生保存：本批之前已生效的单据快照 + 本批单据快照。
    查询清单只读这些快照，不再回放历史批次。
    """

    __tablename__ = "basis_records"
    __table_args__ = (
        UniqueConstraint(
            "batch_id",
            "is_batch_slip",
            "slip_no",
            name="uq_basis_batch_flag_slip",
        ),
        Index("ix_basis_batch_student", "batch_id", "student_no"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("batches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    student_no: Mapped[str] = mapped_column(String(64), nullable=False)
    slip_no: Mapped[str] = mapped_column(String(64), nullable=False)
    school_id: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    origin_batch_no: Mapped[str] = mapped_column(String(64), nullable=False)
    # True 表示该快照来自本批；False 表示本批之前的已生效版本。
    is_batch_slip: Mapped[bool] = mapped_column(Boolean, nullable=False)


class BasisStudentSchool(Base):
    """本批中“归属变化可能涉及某校”的学生索引。

    只在学生的归属确实会随统计日发生变化、且变化前后的学校包含该校时写入；
    清单查询据此只读取相关学生的依据。
    """

    __tablename__ = "basis_student_schools"
    __table_args__ = (
        UniqueConstraint(
            "batch_id", "student_no", "school_id", name="uq_basis_student_school"
        ),
        Index("ix_basis_student_school_query", "batch_id", "school_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("batches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    student_no: Mapped[str] = mapped_column(String(64), nullable=False)
    school_id: Mapped[str] = mapped_column(String(64), nullable=False)
