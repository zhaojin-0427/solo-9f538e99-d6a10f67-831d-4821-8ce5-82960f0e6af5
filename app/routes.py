"""HTTP 路由。"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, Response, status

from . import service
from .db import Database
from .deps import get_database
from .schemas import (
    AdditionItem,
    BatchCreate,
    BatchDetail,
    BatchSummary,
    CorrectionListResponse,
    Receipt,
    SlipRead,
    SlipUpsert,
    SlipUpsertResponse,
    WithdrawalItem,
)

router = APIRouter(prefix="/api", tags=["学籍更正清单"])


def _batch_summary(batch) -> BatchSummary:
    return BatchSummary(
        batch_no=batch.batch_no,
        status=batch.status,
        created_at=batch.created_at,
        committed_at=batch.committed_at,
    )


def _receipt(batch) -> Receipt | None:
    if batch.status != "committed" or batch.committed_at is None:
        return None
    return Receipt(
        batch_no=batch.batch_no,
        status=batch.status,
        committed_at=batch.committed_at,
        slip_count=batch.slip_count,
        affected_student_count=batch.affected_student_count,
        involved_student_count=batch.changed_student_count,
        basis_record_count=batch.basis_record_count,
        basis_prior_record_count=batch.basis_prior_record_count,
        basis_batch_record_count=batch.basis_batch_record_count,
    )


# ---------------------------------------------------------------------------
# 批次
# ---------------------------------------------------------------------------


@router.post(
    "/batches",
    response_model=BatchSummary,
    status_code=status.HTTP_201_CREATED,
    summary="新建草稿批次",
)
def create_batch(payload: BatchCreate, database: Database = Depends(get_database)):
    batch = service.create_batch(database, payload.batch_no)
    return _batch_summary(batch)


@router.get("/batches", response_model=list[BatchSummary], summary="列出批次")
def list_batches(
    status_filter: str | None = Query(default=None, alias="status"),
    database: Database = Depends(get_database),
):
    return [_batch_summary(b) for b in service.list_batches(database, status_filter)]


@router.get(
    "/batches/{batch_no}",
    response_model=BatchDetail,
    summary="批次详情（含明细与回执）",
)
def get_batch(batch_no: str, database: Database = Depends(get_database)):
    batch = service.get_batch(database, batch_no)
    slips = service.list_slips(database, batch_no)
    return BatchDetail(
        batch_no=batch.batch_no,
        status=batch.status,
        created_at=batch.created_at,
        committed_at=batch.committed_at,
        slip_count=len(slips),
        slips=[SlipRead.model_validate(slip) for slip in slips],
        receipt=_receipt(batch),
    )


# ---------------------------------------------------------------------------
# 转学单（草稿批次内登记 / 更正）
# ---------------------------------------------------------------------------


@router.post(
    "/batches/{batch_no}/slips",
    response_model=SlipUpsertResponse,
    summary="登记或更正转学单（同号学生固定，日期与学校可改）",
    description=(
        "按转学单号 upsert：单号不存在则登记，已存在则更正生效日与转入校。"
        "同一单号的学生号不可更改。仅草稿批次可写。"
    ),
)
def upsert_slip(
    batch_no: str,
    payload: SlipUpsert,
    database: Database = Depends(get_database),
):
    action, slip = service.upsert_slip(
        database,
        batch_no,
        slip_no=payload.slip_no,
        student_no=payload.student_no,
        school_id=payload.school_id,
        effective_date=payload.effective_date,
    )
    return SlipUpsertResponse(
        batch_no=batch_no, action=action, slip=SlipRead.model_validate(slip)
    )


@router.get(
    "/batches/{batch_no}/slips",
    response_model=list[SlipRead],
    summary="列出批次内的转学单",
)
def list_slips(batch_no: str, database: Database = Depends(get_database)):
    return [
        SlipRead.model_validate(slip)
        for slip in service.list_slips(database, batch_no)
    ]


@router.delete(
    "/batches/{batch_no}/slips/{slip_no}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="从草稿批次中移除转学单",
)
def delete_slip(
    batch_no: str, slip_no: str, database: Database = Depends(get_database)
):
    service.delete_slip(database, batch_no, slip_no)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# 提交
# ---------------------------------------------------------------------------


@router.post(
    "/batches/{batch_no}/commit",
    response_model=Receipt,
    summary="整批生效并生成回执",
)
def commit_batch(batch_no: str, database: Database = Depends(get_database)):
    batch = service.commit_batch(database, batch_no)
    receipt = _receipt(batch)
    assert receipt is not None
    return receipt


# ---------------------------------------------------------------------------
# 清单
# ---------------------------------------------------------------------------


@router.get(
    "/batches/{batch_no}/corrections",
    response_model=CorrectionListResponse,
    summary="按批号、学校、统计日查询补报与撤回名单",
    description=(
        "仍按批号、学校、统计日查询，名单单号、日期与各项人数沿用原口径。"
        "查询数据按生效日边界保存（不按日展开），通过索引条件直接定位统计日"
        "命中的学生，不会扫完该校候选再筛日期。读取上限：辅助记录基础最多 64 条，"
        "每命中一名学生再放宽 4 条；复查依据每名命中学生最多读取 4 条"
        "（实际为本批/历史各至多 1 条）。响应分别以 auxiliary_records_read 与 "
        "basis_records_read 报出这两类记录的实际读取条数。"
    ),
)
def get_corrections(
    batch_no: str,
    school_id: str = Query(..., min_length=1, max_length=64, description="学校代码"),
    statistics_date: date = Query(..., description="统计日（含当日）"),
    database: Database = Depends(get_database),
):
    result = service.correction_list(
        database, batch_no, school_id, statistics_date
    )
    return CorrectionListResponse(
        batch_no=result.batch_no,
        school_id=result.school_id,
        statistics_date=result.statistics_date,
        additions=[AdditionItem(**item) for item in result.additions],
        withdrawals=[WithdrawalItem(**item) for item in result.withdrawals],
        addition_count=len(result.additions),
        withdrawal_count=len(result.withdrawals),
        candidate_student_count=result.candidate_student_count,
        listed_student_count=result.listed_student_count,
        unmatched_student_count=result.unmatched_student_count,
        auxiliary_records_read=result.auxiliary_records_read,
        basis_records_read=result.basis_records_read,
    )


# ---------------------------------------------------------------------------
# 运维
# ---------------------------------------------------------------------------


@router.get("/health", summary="健康检查")
def health(database: Database = Depends(get_database)):
    return {"status": "ok"}


@router.get("/stats", summary="数据量统计")
def stats(database: Database = Depends(get_database)):
    return service.statistics(database)
