import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["SEED_DEMO"] = "false"
os.environ["JWT_SECRET"] = "test-secret"

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src import database
from src.database import Base
from src.models import (
    ContainerCheck,
    DelayCase,
    GateEvent,
    LabelVersion,
    TrainingBatch,
    TrainingBatchItem,
    User,
    VesselCall,
)
from src.security import hash_password

engine = create_engine(
    "sqlite+pysqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSession = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@pytest.fixture(scope="session", autouse=True)
def wire_engine():
    database.engine = engine
    database.SessionLocal = TestSession
    database.redis_client = type("FakeRedis", (), {"rpush": lambda *a, **k: 1})()
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture()
def db():
    session = TestSession()
    yield session
    session.rollback()
    for table in reversed(Base.metadata.sorted_tables):
        session.execute(table.delete())
    session.commit()
    session.close()


@pytest.fixture()
def client(db):
    from src.main import app
    from src.security import get_db

    def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _token(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": f"{username}123"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture()
def users(db):
    rows = [
        User(username="operator", display_name="操作员", password_hash=hash_password("operator123"), role="operator"),
        User(username="supervisor", display_name="主管", password_hash=hash_password("supervisor123"), role="supervisor"),
        User(username="viewer", display_name="观察员", password_hash=hash_password("viewer123"), role="viewer"),
    ]
    db.add_all(rows)
    db.commit()
    return rows


def make_vessel(db, call_no="CALL-T-001", closed=False):
    now = datetime.now(timezone.utc)
    v = VesselCall(
        call_no=call_no,
        vessel_name="测试船",
        terminal="CCT",
        berth="B1",
        eta=now - timedelta(hours=4),
        etd=now + timedelta(hours=4),
        state="closed" if closed else "open",
        closed_at=now if closed else None,
    )
    db.add(v)
    db.flush()
    return v


def make_container(db, vessel, no="MSCU900001", customs="finished"):
    c = ContainerCheck(
        vessel_id=vessel.id,
        container_no=no,
        iso_type="22G1",
        cargo_class="dry",
        weight_t=10.0,
        customs_status=customs,
        seal_status="intact",
        check_state="verified" if customs == "finished" else "pending",
    )
    db.add(c)
    db.flush()
    return c


def make_gate(db, container, ref="G-T-1", dwell=12.0):
    g = GateEvent(
        container_id=container.id,
        event_ref=ref,
        gate_code="G1",
        event_type="in",
        event_time=datetime.now(timezone.utc),
        dwell_min=dwell,
        status="active",
        note="test",
    )
    db.add(g)
    db.flush()
    return g


def make_case(db, vessel, case_no="CASE-T-001"):
    c = DelayCase(vessel_id=vessel.id, case_no=case_no, reason="测试延误", status="draft")
    db.add(c)
    db.flush()
    return c


def auth(t):
    return {"Authorization": f"Bearer {t}"}


def run_job_sync(job_id, db):
    """模拟独立 worker 进程消费队列：用新会话处理作业，并令 API 侧会话过期。"""
    from src.worker import run_job

    run_job(job_id)
    db.expire_all()


# ---------- 首次成功分级特征固化 ----------

def test_feature_snapshot_frozen_at_first_success(db, client, users):
    vessel = make_vessel(db)
    container = make_container(db, vessel)
    make_gate(db, container)
    case = make_case(db, vessel)
    tok = _token(client, "operator")

    r = client.post(f"/api/cases/{case.id}/classify", headers=auth(tok))
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    run_job_sync(job_id, db)
    db.expire_all()
    case = db.get(DelayCase, case.id)
    assert case.status == "classified"
    assert case.feature_snapshot is not None
    first_snapshot = dict(case.feature_snapshot)
    assert case.snapshot_job_id == job_id
    # v1 模型标签自动登记
    v1 = db.execute(select(LabelVersion).where(LabelVersion.case_id == case.id)).scalar_one()
    assert v1.version_no == 1 and v1.source == "model" and v1.status == "active"
    assert v1.label == case.final_level

    # 再次跑分级并成功：快照与 v1 标签均不被覆盖/重复
    r = client.post(f"/api/cases/{case.id}/classify", headers=auth(tok))
    job_id_2 = r.json()["job_id"]
    run_job_sync(job_id_2, db)
    db.expire_all()
    case = db.get(DelayCase, case.id)
    assert case.feature_snapshot == first_snapshot
    assert case.snapshot_job_id == job_id
    versions = db.execute(select(LabelVersion).where(LabelVersion.case_id == case.id)).scalars().all()
    assert len(versions) == 1
    assert versions[0].id == v1.id


def test_blocked_classify_does_not_write_snapshot(db, client, users):
    vessel = make_vessel(db)
    container = make_container(db, vessel, customs="in_progress")
    make_gate(db, container)
    case = make_case(db, vessel)
    tok = _token(client, "operator")

    r = client.post(f"/api/cases/{case.id}/classify", headers=auth(tok))
    assert r.status_code == 409
    db.expire_all()
    case = db.get(DelayCase, case.id)
    assert case.feature_snapshot is None
    assert case.status == "blocked"
    assert db.execute(select(LabelVersion)).scalars().first() is None


# ---------- 靠泊结束与补标 ----------

def test_backfill_requires_closed_berthing_and_no_existing_label(db, client, users):
    vessel = make_vessel(db, closed=False)
    case = make_case(db, vessel)
    sup = _token(client, "supervisor")

    r = client.post(f"/api/training/cases/{case.id}/backfill", headers=auth(sup), json={"label": "major", "note": "实际延误"})
    assert r.status_code == 409

    r = client.post(f"/api/training/vessels/{vessel.id}/close-berthing", headers=auth(sup))
    assert r.status_code == 200 and r.json()["state"] == "closed"
    # 重复关闭幂等报错
    r2 = client.post(f"/api/training/vessels/{vessel.id}/close-berthing", headers=auth(sup))
    assert r2.status_code == 409

    r = client.post(f"/api/training/cases/{case.id}/backfill", headers=auth(sup), json={"label": "major", "note": "实际延误复盘"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version_no"] == 1 and body["source"] == "backfill" and body["label"] == "major"

    # 已有标签不能再补标
    r = client.post(f"/api/training/cases/{case.id}/backfill", headers=auth(sup), json={"label": "watch", "note": "x"})
    assert r.status_code == 409


def test_operator_cannot_backfill_or_close(db, client, users):
    vessel = make_vessel(db, closed=True)
    case = make_case(db, vessel)
    op = _token(client, "operator")
    assert client.post(f"/api/training/vessels/{vessel.id}/close-berthing", headers=auth(op)).status_code == 403
    assert client.post(
        f"/api/training/cases/{case.id}/backfill", headers=auth(op), json={"label": "major", "note": "无权限测试"}
    ).status_code == 403


# ---------- 标签更正 ----------

def test_correction_supersedes_old_version_but_keeps_old_batch_item(db, client, users):
    vessel = make_vessel(db, closed=True)
    container = make_container(db, vessel)
    make_gate(db, container)
    case = make_case(db, vessel)
    sup = _token(client, "supervisor")

    job = client.post(f"/api/cases/{case.id}/classify", headers=auth(_token(client, "operator"))).json()["job_id"]
    run_job_sync(job, db)

    # 封存第一批（含 v1）
    r = client.post("/api/training/batches/seal", headers=auth(sup), json={})
    assert r.status_code == 200
    batch1 = r.json()
    assert batch1["item_count"] == 1
    old_item = batch1["items"][0]
    assert old_item["label_version_id"]

    # 主管更正
    r = client.post(
        f"/api/training/cases/{case.id}/correct", headers=auth(sup), json={"label": "critical", "note": "复盘后上调等级"}
    )
    assert r.status_code == 200
    v2 = r.json()
    assert v2["version_no"] == 2 and v2["source"] == "correction" and v2["status"] == "active"
    db.expire_all()
    v1 = db.get(LabelVersion, old_item["label_version_id"])
    assert v1.status == "superseded"

    # 同标签重复更正在拒绝之列
    r_same = client.post(
        f"/api/training/cases/{case.id}/correct", headers=auth(sup), json={"label": "critical", "note": "再来一次"}
    )
    assert r_same.status_code == 409

    # 旧批仍按原样取用
    old = client.get(f"/api/training/batches/{batch1['id']}", headers=auth(sup)).json()
    assert old["items"][0]["label"] == old_item["label"]
    assert old["items"][0]["label_version_id"] == old_item["label_version_id"]

    # 下一批收录更正后的 v2，v1 不再出现
    r = client.post("/api/training/batches/seal", headers=auth(sup), json={})
    assert r.status_code == 200
    batch2 = r.json()
    assert batch2["item_count"] == 1
    assert batch2["items"][0]["label_version_id"] == v2["id"]
    assert batch2["items"][0]["label"] == "critical"


# ---------- 封存规则 ----------

def test_seal_includes_only_eligible_active_snapshot_labels(db, client, users):
    sup = _token(client, "supervisor")

    # 案件 A：已分级有模型标签
    va = make_vessel(db, call_no="CALL-A", closed=True)
    make_gate(db, make_container(db, va, no="MSCU-A1"), ref="G-A")
    ca = make_case(db, va, "CASE-A")
    run_job_sync(client.post(f"/api/cases/{ca.id}/classify", headers=auth(_token(client, "operator"))).json()["job_id"], db)

    # 案件 B：靠泊结束、无标签（待补标），不应入批
    vb = make_vessel(db, call_no="CALL-B", closed=True)
    cb = make_case(db, vb, "CASE-B")

    # 案件 C：有补标但无特征快照，不应入批
    vc = make_vessel(db, call_no="CALL-C", closed=True)
    cc = make_case(db, vc, "CASE-C")
    client.post(f"/api/training/cases/{cc.id}/backfill", headers=auth(sup), json={"label": "watch", "note": "无快照补标"})

    r = client.post("/api/training/batches/seal", headers=auth(sup), json={})
    assert r.status_code == 200
    detail = r.json()
    assert detail["status"] == "sealed"
    case_ids = {i["case_id"] for i in detail["items"]}
    assert case_ids == {ca.id}

    # 待补标工作台包含 B；C 有标签但缺快照，单列在 labeled_without_snapshot
    pending = client.get("/api/training/pending-backfill", headers=auth(sup)).json()
    pending_nos = {row["case_no"] for row in pending}
    assert "CASE-B" in pending_nos and "CASE-C" not in pending_nos
    wb = client.get("/api/training/workbench", headers=auth(sup)).json()
    no_snapshot = {row["case_no"]: row for row in wb["labeled_without_snapshot"]}
    assert "CASE-C" in no_snapshot and no_snapshot["CASE-C"]["active_label"] == "watch"

    # 再次封存：没有可收录版本 -> 409，不建空批
    r = client.post("/api/training/batches/seal", headers=auth(sup), json={})
    assert r.status_code == 409
    assert len(db.execute(select(TrainingBatch)).scalars().all()) == 1


def test_same_label_version_never_enters_two_batches(db, client, users):
    sup = _token(client, "supervisor")
    vessel = make_vessel(db, closed=True)
    make_gate(db, make_container(db, vessel, no="MSCU-X1"), ref="G-X")
    case = make_case(db, vessel, "CASE-X")
    run_job_sync(client.post(f"/api/cases/{case.id}/classify", headers=auth(_token(client, "operator"))).json()["job_id"], db)

    b1 = client.post("/api/training/batches/seal", headers=auth(sup), json={}).json()
    assert b1["item_count"] == 1
    # 直接再封存不会重复收录
    r = client.post("/api/training/batches/seal", headers=auth(sup), json={})
    assert r.status_code == 409
    assert len(db.execute(select(TrainingBatchItem)).scalars().all()) == 1


def test_failed_seal_can_retry_and_is_idempotent(db, client, users, monkeypatch):
    sup = _token(client, "supervisor")
    vessel = make_vessel(db, closed=True)
    make_gate(db, make_container(db, vessel, no="MSCU-R1"), ref="G-R")
    case = make_case(db, vessel, "CASE-R")
    run_job_sync(client.post(f"/api/cases/{case.id}/classify", headers=auth(_token(client, "operator"))).json()["job_id"], db)

    from src import training_service

    real_add = db.add
    calls = {"n": 0}

    def flaky_add(obj):
        if isinstance(obj, TrainingBatchItem):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated seal failure")
        return real_add(obj)

    monkeypatch.setattr(db, "add", flaky_add)
    with pytest.raises(RuntimeError, match="simulated seal failure"):
        client.post("/api/training/batches/seal", headers=auth(sup), json={})
    monkeypatch.undo()

    db.expire_all()
    failed = db.execute(select(TrainingBatch).where(TrainingBatch.status == "failed")).scalar_one()
    assert failed.item_count == 0
    # 失败未留下任何批次项
    assert db.execute(select(TrainingBatchItem)).scalars().all() == []

    # 用同一批次 id 重试 -> 成功且只入一次
    r = client.post("/api/training/batches/seal", headers=auth(sup), json={"batch_id": failed.id})
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["id"] == failed.id and detail["status"] == "sealed" and detail["item_count"] == 1

    # 已封存批次再重试 -> 409
    again = client.post("/api/training/batches/seal", headers=auth(sup), json={"batch_id": failed.id})
    assert again.status_code == 409
    assert len(db.execute(select(TrainingBatchItem)).scalars().all()) == 1


def test_concurrent_label_after_cutoff_waits_for_next_batch(db, client, users):
    """封存截止之后提交的补标不进本批，留到下批。"""
    sup = _token(client, "supervisor")
    vessel = make_vessel(db, closed=True)
    make_gate(db, make_container(db, vessel, no="MSCU-D1"), ref="G-D")
    case_a = make_case(db, vessel, "CASE-D1")
    case_b = make_case(db, vessel, "CASE-D2")
    run_job_sync(client.post(f"/api/cases/{case_a.id}/classify", headers=auth(_token(client, "operator"))).json()["job_id"], db)

    # 手工构造一个 sealing 批次：cutoff 落在 case_a 已标记、case_b 未补标之间
    from src.models import TrainingBatch

    cutoff = datetime.now(timezone.utc)
    batch = TrainingBatch(batch_no="TB-CUTOFF-01", status="sealing", cutoff_at=cutoff, created_by="supervisor")
    db.add(batch)
    db.commit()

    # cutoff 之后给 case_b 补标（附特征快照，使其具备入批资格，仅被 cutoff 挡在本批之外）
    case_b.feature_snapshot = {"container_count": 1, "avg_gate_dwell_min": 9.0}
    case_b.snapshot_at = datetime.now(timezone.utc)
    r = client.post(f"/api/training/cases/{case_b.id}/backfill", headers=auth(sup), json={"label": "watch", "note": "cutoff 后补标"})
    assert r.status_code == 200
    label_b = r.json()

    # 重试旧批次：沿用旧 cutoff，case_b 不进本批
    detail = client.post("/api/training/batches/seal", headers=auth(sup), json={"batch_id": batch.id}).json()
    assert {i["case_id"] for i in detail["items"]} == {case_a.id}

    # 新批次收录 case_b
    next_batch = client.post("/api/training/batches/seal", headers=auth(sup), json={}).json()
    assert next_batch["items"][0]["label_version_id"] == label_b["id"]


# ---------- 工作台 ----------

def test_workbench_and_batch_item_snapshot_is_copy(db, client, users):
    sup = _token(client, "supervisor")
    vessel = make_vessel(db, closed=True)
    make_gate(db, make_container(db, vessel, no="MSCU-W1"), ref="G-W")
    case = make_case(db, vessel, "CASE-W")
    run_job_sync(client.post(f"/api/cases/{case.id}/classify", headers=auth(_token(client, "operator"))).json()["job_id"], db)
    client.post("/api/training/batches/seal", headers=auth(sup), json={})

    wb = client.get("/api/training/workbench", headers=auth(sup)).json()
    assert wb["summary"]["sealed_batches"] == 1
    assert wb["summary"]["active_labeled_cases"] == 1
    assert wb["allowed_labels"] == ["normal", "watch", "major", "critical"]
    assert len(wb["batches"]) == 1

    # 批次项里的特征快照是封存时刻的独立拷贝：改案件快照不影响旧批
    batch = db.execute(select(TrainingBatch)).scalar_one()
    item = db.execute(select(TrainingBatchItem).where(TrainingBatchItem.batch_id == batch.id)).scalar_one()
    original = dict(item.feature_snapshot)
    case.feature_snapshot = {"tampered": True}
    db.commit()
    db.refresh(item)
    assert item.feature_snapshot == original

    # viewer 只读可用
    vt = _token(client, "viewer")
    assert client.get("/api/training/workbench", headers=auth(vt)).status_code == 200
    assert client.post("/api/training/batches/seal", headers=auth(vt), json={}).status_code == 403
