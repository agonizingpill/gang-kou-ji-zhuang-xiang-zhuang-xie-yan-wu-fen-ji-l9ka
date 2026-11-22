from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .database import Base

def now_utc():
    return datetime.now(timezone.utc)

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(40), unique=True)
    display_name: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(20))

class VesselCall(Base):
    __tablename__ = "vessel_calls"
    id: Mapped[int] = mapped_column(primary_key=True)
    call_no: Mapped[str] = mapped_column(String(40), unique=True)
    vessel_name: Mapped[str] = mapped_column(String(100))
    terminal: Mapped[str] = mapped_column(String(60))
    berth: Mapped[str] = mapped_column(String(20))
    eta: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    etd: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_status: Mapped[str] = mapped_column(String(20), default="planned")
    state: Mapped[str] = mapped_column(String(20), default="open")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    containers: Mapped[list["ContainerCheck"]] = relationship(back_populates="vessel", cascade="all, delete-orphan")
    cases: Mapped[list["DelayCase"]] = relationship(back_populates="vessel", cascade="all, delete-orphan")

class ContainerCheck(Base):
    __tablename__ = "container_checks"
    id: Mapped[int] = mapped_column(primary_key=True)
    vessel_id: Mapped[int] = mapped_column(ForeignKey("vessel_calls.id"), index=True)
    container_no: Mapped[str] = mapped_column(String(20), unique=True)
    iso_type: Mapped[str] = mapped_column(String(10))
    cargo_class: Mapped[str] = mapped_column(String(30))
    weight_t: Mapped[float] = mapped_column(Float)
    customs_status: Mapped[str] = mapped_column(String(20), default="pending")
    seal_status: Mapped[str] = mapped_column(String(20), default="intact")
    check_state: Mapped[str] = mapped_column(String(20), default="pending")
    vessel: Mapped[VesselCall] = relationship(back_populates="containers")
    gates: Mapped[list["GateEvent"]] = relationship(back_populates="container", cascade="all, delete-orphan")

class GateEvent(Base):
    __tablename__ = "gate_events"
    __table_args__ = (UniqueConstraint("container_id", "event_ref", name="uq_gate_event_ref"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    container_id: Mapped[int] = mapped_column(ForeignKey("container_checks.id"), index=True)
    event_ref: Mapped[str] = mapped_column(String(40))
    gate_code: Mapped[str] = mapped_column(String(20))
    event_type: Mapped[str] = mapped_column(String(24))
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    dwell_min: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(20), default="active")
    note: Mapped[str] = mapped_column(String(240), default="")
    container: Mapped[ContainerCheck] = relationship(back_populates="gates")

class DelayCase(Base):
    __tablename__ = "delay_cases"
    id: Mapped[int] = mapped_column(primary_key=True)
    vessel_id: Mapped[int] = mapped_column(ForeignKey("vessel_calls.id"), index=True)
    case_no: Mapped[str] = mapped_column(String(40), unique=True)
    requested_level: Mapped[str] = mapped_column(String(20), default="pending")
    final_level: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    reason: Mapped[str] = mapped_column(Text, default="")
    feature_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    snapshot_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    model_output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    exception_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)
    vessel: Mapped[VesselCall] = relationship(back_populates="cases")
    actions: Mapped[list["DispatchAction"]] = relationship(back_populates="case", cascade="all, delete-orphan")
    label_versions: Mapped[list["LabelVersion"]] = relationship(back_populates="case", cascade="all, delete-orphan")

class DispatchAction(Base):
    __tablename__ = "dispatch_actions"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("delay_cases.id"), index=True)
    action_type: Mapped[str] = mapped_column(String(32))
    assignee: Mapped[str] = mapped_column(String(80))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    instruction: Mapped[str] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    case: Mapped[DelayCase] = relationship(back_populates="actions")

class AnalysisJob(Base):
    __tablename__ = "analysis_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("delay_cases.id"), index=True)
    job_type: Mapped[str] = mapped_column(String(32), default="delay_triage")
    status: Mapped[str] = mapped_column(String(20), default="queued")
    input_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(40))
    role: Mapped[str] = mapped_column(String(20))
    action: Mapped[str] = mapped_column(String(80))
    entity: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

class LabelVersion(Base):
    """Append-only label versions for one delay case.

    v1 is created by the worker at the first successful classification
    (source=model). After berthing closes, the supervisor adds the actual
    delay label via backfill (source=backfill) or corrects an existing
    label (source=correction), which supersedes the prior active version.
    """

    __tablename__ = "label_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("delay_cases.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(20), default="model")
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("label_versions.id"), nullable=True)
    created_by: Mapped[str] = mapped_column(String(40))
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    case: Mapped[DelayCase] = relationship(back_populates="label_versions")
    __table_args__ = (
        UniqueConstraint("case_id", "version_no", name="uq_label_version_no"),
    )

class TrainingBatch(Base):
    __tablename__ = "training_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_no: Mapped[str] = mapped_column(String(40), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="sealing", index=True)
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    items: Mapped[list["TrainingBatchItem"]] = relationship(back_populates="batch", cascade="all, delete-orphan")

class TrainingBatchItem(Base):
    __tablename__ = "training_batch_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("training_batches.id"), index=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("delay_cases.id"), index=True)
    label_version_id: Mapped[int] = mapped_column(ForeignKey("label_versions.id"))
    label: Mapped[str] = mapped_column(String(20))
    feature_snapshot: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    batch: Mapped[TrainingBatch] = relationship(back_populates="items")
    # 同一版标签不重复入批（跨批次全局唯一）
    __table_args__ = (
        UniqueConstraint("label_version_id", name="uq_batch_item_label_version"),
        UniqueConstraint("batch_id", "case_id", name="uq_batch_item_case"),
    )
