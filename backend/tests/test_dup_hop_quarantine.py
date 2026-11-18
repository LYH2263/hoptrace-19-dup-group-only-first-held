"""dup_hop_quarantine 回归测试：整组隔离 / 失败闭 / 同源报告 / 幂等重放。"""
import json
import os
import tempfile

import pytest

_tmp = tempfile.mkdtemp(prefix="hoptrace-test-")
os.environ["DATA_DIR"] = _tmp
os.environ["FIXTURES_DIR"] = os.path.join(os.path.dirname(__file__), "..", "..", "fixtures")

from fastapi.testclient import TestClient  # noqa: E402

from app import seed  # noqa: E402
from app.db import DB_PATH  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    if DB_PATH.exists():
        DB_PATH.unlink()
    seed.init_db()
    with TestClient(app) as c:
        yield c


def _run_ingest(client):
    r = client.post("/api/ingest/run")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"], body.get("error")
    return body["report"]


def test_dup_group_quarantined_wholesale(client):
    report = _run_ingest(client)
    # 夹具含两组重复键：H-7/H-8 批内撞键；H-9 撞正式线 H-2
    assert report["dup_group_count"] == 2
    assert report["quarantined_points"] == 4
    assert report["written_official"] == 2  # 仅 H-5、H-6 进正式线

    tl = client.get("/api/waybills/WB-1001/timeline").json()
    official_ids = [h["id"] for h in tl["hops"]]
    # 组内任一点（含被拉出的 H-2）不得出现在正式时间线
    for hid in ("H-2", "H-7", "H-8", "H-9"):
        assert hid not in official_ids
    assert official_ids == ["H-1", "H-3", "H-5"]


def test_quarantine_list_and_proof_same_source(client):
    _run_ingest(client)
    groups = client.get("/api/quarantine").json()
    assert len(groups) == 2
    for g in groups:
        proof = client.get(f"/api/dup-groups/{g['id']}/proof").json()
        assert proof["group"]["waybill_id"] == g["waybill_id"]
        assert proof["dup_key"]["event_ts"] == g["event_ts"]
        assert proof["dup_key"]["location_code"] == g["location_code"]
        assert proof["dup_key"]["event_code"] == g["event_code"]
        assert proof["disposition"]
        assert proof["official_leak"] == []
        assert len(proof["members"]) == g["member_count"] == 2
        # 成员列表与隔离列表 member_json 同源
        listed = {m["hop_id"] for m in json.loads(g["member_json"])}
        assert listed == {m["hop_id"] for m in proof["members"]}


def test_report_matches_derived_state(client):
    report = _run_ingest(client)
    overview = client.get("/api/overview").json()
    assert overview["quarantine"] == report["dup_group_count"]
    quarantine = client.get("/api/quarantine").json()
    assert sum(g["member_count"] for g in quarantine) == report["quarantined_points"]


def test_replay_is_idempotent(client):
    _run_ingest(client)
    before = client.get("/api/quarantine").json()
    report2 = _run_ingest(client)
    assert report2["written_official"] == 0
    assert report2["dup_group_count"] == 0
    assert report2["quarantined_points"] == 0
    assert report2["replayed"] == report2["input"]
    after = client.get("/api/quarantine").json()
    assert [g["id"] for g in after] == [g["id"] for g in before]


def test_proof_404(client):
    _run_ingest(client)
    assert client.get("/api/dup-groups/DG-000000000000/proof").status_code == 404
