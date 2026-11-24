"""请求与响应模型。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

ShortText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
]


class BatchCreate(BaseModel):
    batch_no: ShortText

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"batch_no": "B2026-001"},
        }
    )


class BatchSummary(BaseModel):
    batch_no: str
    status: str
    created_at: datetime
    committed_at: datetime | None = None


class SlipUpsert(BaseModel):
    slip_no: ShortText
    student_no: ShortText
    school_id: ShortText
    effective_date: date

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "slip_no": "Z2026-0001",
                "student_no": "S0001",
                "school_id": "SCHOOL-A",
                "effective_date": "2026-09-01",
            }
        }
    )


class SlipRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    slip_no: str
    student_no: str
    school_id: str
    effective_date: date
    created_at: datetime
    updated_at: datetime


class SlipUpsertResponse(BaseModel):
    batch_no: str
    action: str
    slip: SlipRead


class Receipt(BaseModel):
    """批次回执。

    ``basis_record_count`` 是为复查额外留存的依据条数；它等于本批之前
    已生效单快照与本次登记单快照之和。
    """

    batch_no: str
    status: str
    committed_at: datetime
    slip_count: int
    affected_student_count: int
    involved_student_count: int
    basis_record_count: int
    basis_prior_record_count: int
    basis_batch_record_count: int


class BatchDetail(BatchSummary):
    slip_count: int
    slips: list[SlipRead]
    receipt: Receipt | None = None


class AdditionItem(BaseModel):
    """补报：截至统计日，该生归属应加入 ``school_id``。"""

    student_no: str
    school_id: str
    slip_no: str
    effective_date: date
    previous_school_id: str | None = None
    previous_slip_no: str | None = None


class WithdrawalItem(BaseModel):
    """撤回：截至统计日，该生已不应归属 ``school_id``。"""

    student_no: str
    school_id: str
    slip_no: str
    effective_date: date
    next_school_id: str | None = None
    next_slip_no: str | None = None


class CorrectionListResponse(BaseModel):
    batch_no: str
    school_id: str
    statistics_date: date
    additions: list[AdditionItem]
    withdrawals: list[WithdrawalItem]
    addition_count: int
    withdrawal_count: int
    candidate_student_count: int
    listed_student_count: int
    unmatched_student_count: int
    # 辅助记录（补报/撤回成立区间）本次实际读取条数。
    auxiliary_records_read: int
    # 复查依据（冻结单据快照）本次实际读取条数。
    basis_records_read: int
