"""训练样本领域服务：补标、更正、交班封存与工作台查询。

规则要点：
- 标签按版本追加（label_versions），更正只把旧版置为 superseded，旧批次项保持原样。
- 封存以批次创建时的 cutoff_at 为界：当前有效、有特征快照且未入过批的标签版本才可收录；
  cutoff 之后提交的补标/更正留到下批。
- 封存与重试在同一事务内完成；uq_batch_item_label_version 保证同一版标签不重复入批。
"""

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from .models import (
    AuditLog,
    DelayCase,
    LabelVersion,
    TrainingBatch,
    TrainingBatchItem,
    VesselCall,
)

DELAY_LABELS = ("normal", "watch", "major", "critical")
LABEL_SOURCE_LABELS = {"model": "模型首判", "backfill": "靠泊补标", "correction": "主管更正"}


def _now():
    return datetime.now(timezone.utc)


def _audit(db, user, action, entity, entity_id, detail=""):
    db.add(
        AuditLog(
            actor=user.username,
            role=user.role,
            action=action,
            entity=entity,
            entity_id=entity_id,
            detail=detail,
        )
    )


def active_label(db: Session, case_id: int) -> LabelVersion | None:
    return db.execute(
        select(LabelVersion)
        .where(LabelVersion.case_id == case_id, LabelVersion.status == "active")
        .order_by(LabelVersion.version_no.desc())
    ).scalars().first()


def label_history(db: Session, case_id: int) -> list[LabelVersion]:
    return db.execute(
        select(LabelVersion)
        .where(LabelVersion.case_id == case_id)
        .order_by(LabelVersion.version_no.desc())
    ).scalars().all()


def close_berthing(db: Session, vessel: VesselCall, user) -> VesselCall:
    """靠泊结束：封存船次业务输入，此后主管才能按实际延误补标。"""
    if vessel.closed_at is not None:
        raise HTTPException(status_code=409, detail=f"船次 {vessel.call_no} 靠泊已结束，不能重复关闭")
    vessel.state = "closed"
    vessel.closed_at = _now()
    _audit(db, user, "close_berthing", "vessel_call", vessel.id, vessel.call_no)
    db.commit()
    db.refresh(vessel)
    return vessel


def _next_version_no(db: Session, case_id: int) -> int:
    current = db.scalar(
        select(func.coalesce(func.max(LabelVersion.version_no), 0)).where(
            LabelVersion.case_id == case_id
        )
    )
    return int(current) + 1


def _validate_label(label: str):
    if label not in DELAY_LABELS:
        raise HTTPException(status_code=422, detail=f"标签不合法，允许：{' / '.join(DELAY_LABELS)}")


def backfill_label(db: Session, case: DelayCase, label: str, note: str, user) -> LabelVersion:
    """靠泊结束后主管按实际延误补标（案件此前没有任何标签版本）。"""
    if case.vessel.closed_at is None:
        raise HTTPException(status_code=409, detail="船次靠泊尚未结束，不能补标")
    _validate_label(label)
    existing = db.scalar(
        select(func.count(LabelVersion.id)).where(LabelVersion.case_id == case.id)
    )
    if existing:
        raise HTTPException(status_code=409, detail="案件已有标签，更正请使用标签更正接口")
    version = LabelVersion(
        case_id=case.id,
        version_no=_next_version_no(db, case.id),
        label=label,
        source="backfill",
        status="active",
        created_by=user.username,
        note=note,
    )
    db.add(version)
    db.flush()
    _audit(db, user, "backfill_label", "label_version", version.id, f"{case.case_no} -> {label}")
    db.commit()
    db.refresh(version)
    return version


def correct_label(db: Session, case: DelayCase, label: str, note: str, user) -> LabelVersion:
    """更正标签：旧版本置 superseded，新版本成为当前有效版本。

    已封存批次里的旧版本项不可变；新版本在下次封存时收录。
    """
    _validate_label(label)
    current = active_label(db, case.id)
    if current is None:
        raise HTTPException(status_code=409, detail="案件尚无标签，不能更正，请先补标")
    if current.label == label:
        raise HTTPException(status_code=409, detail="新标签与当前标签相同，无需更正")
    current.status = "superseded"
    version = LabelVersion(
        case_id=case.id,
        version_no=_next_version_no(db, case.id),
        label=label,
        source="correction",
        status="active",
        supersedes_id=current.id,
        created_by=user.username,
        note=note,
    )
    db.add(version)
    db.flush()
    _audit(
        db,
        user,
        "correct_label",
        "label_version",
        version.id,
        f"{case.case_no}: {current.label} v{current.version_no} -> {label} v{version.version_no}",
    )
    db.commit()
    db.refresh(version)
    return version


def register_model_label(db: Session, case: DelayCase, label: str, snapshot: dict, job_id: int) -> None:
    """worker 首次分级成功时调用：固化首次成功输入并登记 v1 模型标签。

    只在特征快照尚不存在时写入；后续重试分级既不覆盖快照，也不重复建标签。
    """
    if case.feature_snapshot is not None:
        return
    case.feature_snapshot = snapshot
    case.snapshot_job_id = job_id
    case.snapshot_at = _now()
    if not db.scalar(select(LabelVersion.id).where(LabelVersion.case_id == case.id).limit(1)):
        db.add(
            LabelVersion(
                case_id=case.id,
                version_no=1,
                label=label,
                source="model",
                status="active",
                created_by="worker",
                note="首次成功分级自动登记",
            )
        )


def _next_batch_no(db: Session) -> str:
    prefix = f"TB-{_now().strftime('%Y%m%d')}-"
    today_count = db.scalar(
        select(func.count(TrainingBatch.id)).where(TrainingBatch.batch_no.like(prefix + "%"))
    )
    return f"{prefix}{int(today_count) + 1:02d}"


def _eligible_versions_stmt(cutoff: datetime):
    already_batched = select(TrainingBatchItem.id).where(
        TrainingBatchItem.label_version_id == LabelVersion.id
    )
    return (
        select(LabelVersion)
        .join(DelayCase, DelayCase.id == LabelVersion.case_id)
        .where(
            LabelVersion.status == "active",
            LabelVersion.created_at <= cutoff,
            DelayCase.feature_snapshot.is_not(None),
            ~exists(already_batched),
        )
        .order_by(LabelVersion.id)
    )


def seal_batch(db: Session, user, batch_id: int | None = None) -> TrainingBatch:
    """封存训练批。

    分两阶段：先把批次行（sealing + cutoff）独立提交，保证封存失败后批次仍在、
    可按同一 id 重试；再在另一事务里按 cutoff 快照收录样本。重试沿用原 cutoff，
    保证是同一次封存的重试，而不是偷偷换界。
    """
    if batch_id is not None:
        batch = db.get(TrainingBatch, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="训练批次不存在")
        if batch.status == "sealed":
            raise HTTPException(status_code=409, detail=f"批次 {batch.batch_no} 已封存，不能重复封存")
        batch.status = "sealing"
        batch.failure_reason = None
    else:
        cutoff = _now()
        candidate_count = db.scalar(
            select(func.count()).select_from(_eligible_versions_stmt(cutoff).subquery())
        )
        if not candidate_count:
            raise HTTPException(status_code=409, detail="没有可收录的标签版本，暂不创建空批次")
        batch = TrainingBatch(
            batch_no=_next_batch_no(db),
            status="sealing",
            cutoff_at=cutoff,
            item_count=0,
            created_by=user.username,
        )
        db.add(batch)
        db.flush()
        _audit(db, user, "seal_training_batch", "training_batch", batch.id, batch.batch_no)
        # 阶段一：批次行先落库，失败后仍可按该 id 重试
        db.commit()

    try:
        versions = db.execute(_eligible_versions_stmt(batch.cutoff_at)).scalars().all()
        for version in versions:
            case = db.get(DelayCase, version.case_id)
            db.add(
                TrainingBatchItem(
                    batch_id=batch.id,
                    case_id=case.id,
                    label_version_id=version.id,
                    label=version.label,
                    # 封存时拷贝特征快照，旧批日后按原样取用
                    feature_snapshot=dict(case.feature_snapshot),
                )
            )
        batch.item_count = len(versions)
        batch.status = "sealed"
        batch.sealed_at = _now()
        batch.failure_reason = None
        _audit(
            db,
            user,
            "seal_training_batch_done",
            "training_batch",
            batch.id,
            f"{batch.batch_no} 收录 {len(versions)} 条",
        )
        # 阶段二：样本收录与封存完成原子提交；uq_batch_item_label_version 兜底防重复
        db.commit()
    except Exception as exc:  # 封存失败：样本事务回滚，批次留 failed 等待重试
        db.rollback()
        failed = db.get(TrainingBatch, batch.id)
        if failed is not None and failed.status != "sealed":
            failed.status = "failed"
            failed.failure_reason = str(exc)[:500]
            db.commit()
        raise
    db.refresh(batch)
    return batch


def list_batches(db: Session) -> list[dict]:
    batches = db.execute(select(TrainingBatch).order_by(TrainingBatch.id.desc())).scalars().all()
    result = []
    for batch in batches:
        result.append(
            {
                "id": batch.id,
                "batch_no": batch.batch_no,
                "status": batch.status,
                "cutoff_at": batch.cutoff_at.isoformat(),
                "sealed_at": batch.sealed_at.isoformat() if batch.sealed_at else None,
                "item_count": batch.item_count,
                "failure_reason": batch.failure_reason,
                "created_by": batch.created_by,
            }
        )
    return result


def batch_detail(db: Session, batch_id: int) -> dict:
    batch = db.get(TrainingBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="训练批次不存在")
    items = db.execute(
        select(TrainingBatchItem).where(TrainingBatchItem.batch_id == batch.id).order_by(TrainingBatchItem.id)
    ).scalars().all()
    return {
        "id": batch.id,
        "batch_no": batch.batch_no,
        "status": batch.status,
        "cutoff_at": batch.cutoff_at.isoformat(),
        "sealed_at": batch.sealed_at.isoformat() if batch.sealed_at else None,
        "item_count": batch.item_count,
        "failure_reason": batch.failure_reason,
        "created_by": batch.created_by,
        "items": [
            {
                "id": item.id,
                "case_id": item.case_id,
                "case_no": db.get(DelayCase, item.case_id).case_no,
                "label_version_id": item.label_version_id,
                "label": item.label,
                "feature_snapshot": item.feature_snapshot,
            }
            for item in items
        ],
    }


def pending_backfill_cases(db: Session) -> list[dict]:
    """待补标案件：靠泊已结束但没有当前有效标签版本的案件。"""
    has_active = select(LabelVersion.id).where(
        LabelVersion.case_id == DelayCase.id, LabelVersion.status == "active"
    )
    cases = db.execute(
        select(DelayCase)
        .join(VesselCall, VesselCall.id == DelayCase.vessel_id)
        .where(VesselCall.closed_at.is_not(None), ~exists(has_active))
        .order_by(DelayCase.id)
    ).scalars().all()
    return [
        {
            "id": case.id,
            "case_no": case.case_no,
            "vessel": case.vessel.call_no,
            "vessel_name": case.vessel.vessel_name,
            "status": case.status,
            "has_feature_snapshot": case.feature_snapshot is not None,
            "closed_at": case.vessel.closed_at.isoformat(),
            "reason": case.reason,
        }
        for case in cases
    ]


def labeled_without_snapshot_cases(db: Session) -> list[dict]:
    """已产生当前有效标签、但缺少首次成功分级特征快照、无法入批的案件。"""
    cases = db.execute(
        select(DelayCase)
        .join(LabelVersion, LabelVersion.case_id == DelayCase.id)
        .where(LabelVersion.status == "active", DelayCase.feature_snapshot.is_(None))
        .order_by(DelayCase.id)
    ).scalars().unique().all()
    return [
        {
            "id": case.id,
            "case_no": case.case_no,
            "vessel": case.vessel.call_no,
            "vessel_name": case.vessel.vessel_name,
            "reason": case.reason,
            "active_label": active_label(db, case.id).label,
        }
        for case in cases
    ]


def workbench(db: Session) -> dict:
    total_cases = db.scalar(select(func.count(DelayCase.id)))
    labeled = db.scalar(
        select(func.count(func.distinct(LabelVersion.case_id))).where(
            LabelVersion.status == "active"
        )
    )
    batched = db.scalar(select(func.count(func.distinct(TrainingBatchItem.case_id))))
    return {
        "summary": {
            "total_cases": int(total_cases or 0),
            "active_labeled_cases": int(labeled or 0),
            "cases_in_sealed_batches": int(batched or 0),
            "sealed_batches": db.scalar(
                select(func.count(TrainingBatch.id)).where(TrainingBatch.status == "sealed")
            ),
        },
        "batches": list_batches(db),
        "pending_backfill": pending_backfill_cases(db),
        "labeled_without_snapshot": labeled_without_snapshot_cases(db),
        "allowed_labels": list(DELAY_LABELS),
    }
