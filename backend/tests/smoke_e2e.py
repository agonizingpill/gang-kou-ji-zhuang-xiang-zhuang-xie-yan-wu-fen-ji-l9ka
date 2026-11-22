"""端到端冒烟：SQLite + 真实 seed + TestClient，覆盖工作台全链路。

手动运行：python tests/smoke_e2e.py（不作为 pytest 用例收集）
"""
import os, sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skip(reason="手动端到端冒烟脚本，用 python tests/smoke_e2e.py 运行")

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["SEED_DEMO"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src import database
from src.database import Base

engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestSession = sessionmaker(bind=engine, expire_on_commit=False)
database.engine = engine
database.SessionLocal = TestSession
database.redis_client = type("FakeRedis", (), {"rpush": lambda *a, **k: 1})()
Base.metadata.create_all(engine)

from scripts.seed_demo import seed
seed()

from src.main import app
from src.security import get_db
def _override():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()
app.dependency_overrides[get_db] = _override

c = TestClient(app)
def login(u):
    r = c.post("/api/auth/login", json={"username": u, "password": f"{u}123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}

sup = login("supervisor")
op = login("operator")
view = login("viewer")

# 1. seed 后的工作台
wb = c.get("/api/training/workbench", headers=sup).json()
print("workbench summary:", wb["summary"])
print("batches:", [(b["batch_no"], b["status"]) for b in wb["batches"]])
print("pending:", [p["case_no"] for p in wb["pending_backfill"]])
assert any(p["case_no"] == "CASE-NORTH-004" for p in wb["pending_backfill"])

# 2. 案件标签版本（seed 的更正链路）
hai = next(x for x in c.get("/api/cases", headers=sup).json() if x["case_no"] == "CASE-HAI-001")
print("HAI versions:", [(v["version_no"], v["label"], v["source"], v["status"]) for v in hai["label_versions"]])
assert hai["feature_snapshot"] is not None and len(hai["label_versions"]) == 2
assert hai["active_label"]["label"] == "critical"

# 3. 分级一个可分级案件（TRI 先补海关，再分级）
containers = c.get("/api/containers", headers=op).json()
tri_box = next(x for x in containers if x["container_no"] == "MSCU200001")
r = c.post(f"/api/containers/{tri_box['id']}/customs", headers=op, json={"customs_status": "finished"})
assert r.status_code == 200
tri_case = next(x for x in c.get("/api/cases", headers=op).json() if x["case_no"] == "CASE-TRI-002")
r = c.post(f"/api/cases/{tri_case['id']}/classify", headers=op)
assert r.status_code == 200, r.text
job_id = r.json()["job_id"]
from src.worker import run_job
run_job(job_id)
detail = c.get(f"/api/training/cases/{tri_case['id']}/labels", headers=sup).json()
print("TRI snapshot keys:", sorted(detail["feature_snapshot"].keys()))
assert len(detail["feature_snapshot"]) == 14
assert detail["versions"][0]["source"] == "model"

# 4. 关闭 CALL-TRI 靠泊后给待补标案件补标
vessels = c.get("/api/vessels", headers=sup).json()
tri_vessel = next(v for v in vessels if v["call_no"] == "CALL-TRI-20261121")
r = c.post(f"/api/training/vessels/{tri_vessel['id']}/close-berthing", headers=sup)
assert r.status_code == 200, r.text

north_case = next(x for x in c.get("/api/cases", headers=sup).json() if x["case_no"] == "CASE-NORTH-004")
r = c.post(f"/api/training/cases/{north_case['id']}/backfill", headers=sup, json={"label": "watch", "note": "靠泊复盘实际延误较轻"})
assert r.status_code == 200, r.text
# 权限：operator 不能补标
r = c.post(f"/api/training/cases/{north_case['id']}/backfill", headers=op, json={"label": "watch", "note": "越权尝试"})
assert r.status_code == 403

# 5. 封存第一批
b1 = c.post("/api/training/batches/seal", headers=sup, json={}).json()
print("batch1:", b1["batch_no"], b1["item_count"])
nos = sorted(i["case_no"] for i in b1["items"])
print("batch1 cases:", nos)
assert b1["status"] == "sealed" and b1["item_count"] >= 2

# 6. 更正 HAI 标签后再封存：旧批不动，新批收新版本
r = c.post(f"/api/training/cases/{hai['id']}/correct", headers=sup, json={"label": "major", "note": "二次复盘回调为重大"})
assert r.status_code == 200
b2 = c.post("/api/training/batches/seal", headers=sup, json={}).json()
print("batch2:", b2["batch_no"], b2["item_count"], [(i["case_no"], i["label"]) for i in b2["items"]])
old = c.get(f"/api/training/batches/{b1['id']}", headers=sup).json()
hai_old = next(i for i in old["items"] if i["case_id"] == hai["id"])
assert hai_old["label"] == "critical"  # 旧批原样
hai_new = next(i for i in b2["items"] if i["case_id"] == hai["id"])
assert hai_new["label"] == "major"     # 下批收更正版

# 7. 无候选时空封存被拒
r = c.post("/api/training/batches/seal", headers=sup, json={})
assert r.status_code == 409

# 8. viewer 只读
assert c.get("/api/training/workbench", headers=view).status_code == 200
assert c.post("/api/training/batches/seal", headers=view, json={}).status_code == 403

# 9. 健康检查与审计
assert c.get("/health").json()["status"] == "ok"
print("\nSMOKE OK")
