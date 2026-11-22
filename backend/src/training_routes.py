"""训练样本工作台路由：靠泊关闭、补标/更正、训练批封存与查询。"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import training_service as service
from .models import DelayCase, VesselCall
from .security import current_user, get_db, require_roles

router = APIRouter(prefix="/api/training", tags=["训练样本"])


class BackfillIn(BaseModel):
    label: str
    note: str = Field(default="", max_length=500)


class CorrectIn(BaseModel):
    label: str
    note: str = Field(min_length=2, max_length=500)


def _label_version_dict(v) -> dict:
    return {
        "id": v.id,
        "version_no": v.version_no,
        "label": v.label,
        "source": v.source,
        "source_label": service.LABEL_SOURCE_LABELS.get(v.source, v.source),
        "status": v.status,
        "supersedes_id": v.supersedes_id,
        "created_by": v.created_by,
        "note": v.note,
        "created_at": v.created_at.isoformat(),
    }


def _get_case(db: Session, case_id: int) -> DelayCase:
    case = db.get(DelayCase, case_id)
    if case is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="延误案件不存在")
    return case


@router.get("/workbench")
def workbench(db: Session = Depends(get_db), user=Depends(current_user)):
    return service.workbench(db)


@router.get("/pending-backfill")
def pending_backfill(db: Session = Depends(get_db), user=Depends(current_user)):
    return service.pending_backfill_cases(db)


@router.get("/labeled-without-snapshot")
def labeled_without_snapshot(db: Session = Depends(get_db), user=Depends(current_user)):
    return service.labeled_without_snapshot_cases(db)


@router.get("/batches")
def batches(db: Session = Depends(get_db), user=Depends(current_user)):
    return service.list_batches(db)


@router.get("/batches/{batch_id}")
def batch_detail(batch_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    return service.batch_detail(db, batch_id)


@router.get("/cases/{case_id}/labels")
def case_labels(case_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    case = _get_case(db, case_id)
    return {
        "case_id": case.id,
        "case_no": case.case_no,
        "feature_snapshot": case.feature_snapshot,
        "snapshot_job_id": case.snapshot_job_id,
        "snapshot_at": case.snapshot_at.isoformat() if case.snapshot_at else None,
        "berthing_closed": case.vessel.closed_at is not None,
        "versions": [_label_version_dict(v) for v in service.label_history(db, case.id)],
    }


@router.post("/vessels/{vessel_id}/close-berthing")
def close_berthing(
    vessel_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_roles("supervisor")),
):
    vessel = db.get(VesselCall, vessel_id)
    if vessel is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="船次不存在")
    service.close_berthing(db, vessel, user)
    return {"id": vessel.id, "call_no": vessel.call_no, "state": vessel.state, "closed_at": vessel.closed_at.isoformat()}


@router.post("/cases/{case_id}/backfill")
def backfill(
    case_id: int,
    payload: BackfillIn,
    db: Session = Depends(get_db),
    user=Depends(require_roles("supervisor")),
):
    case = _get_case(db, case_id)
    version = service.backfill_label(db, case, payload.label, payload.note, user)
    return _label_version_dict(version)


@router.post("/cases/{case_id}/correct")
def correct(
    case_id: int,
    payload: CorrectIn,
    db: Session = Depends(get_db),
    user=Depends(require_roles("supervisor")),
):
    case = _get_case(db, case_id)
    version = service.correct_label(db, case, payload.label, payload.note, user)
    return _label_version_dict(version)


@router.post("/batches/seal")
def seal_batch(
    payload: dict | None = None,
    db: Session = Depends(get_db),
    user=Depends(require_roles("supervisor")),
):
    batch_id = None
    if payload and isinstance(payload, dict):
        batch_id = payload.get("batch_id")
    batch = service.seal_batch(db, user, int(batch_id) if batch_id is not None else None)
    return service.batch_detail(db, batch.id)
