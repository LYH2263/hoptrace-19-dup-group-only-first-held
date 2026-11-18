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


def test_same_key_triple_hit_quarantined_wholesale(client):
    """同键连打三次：整组 3 点全隔离，正式轴零泄漏，隔离全员=命中数。"""
    _run_ingest(client)
    from app.modules.dup_hop_quarantine import service as dup_svc

    triple = [
        {
            "id": f"H-2{i}",
            "waybill_id": "WB-1002",
            "waybill_title": "同城急送样件",
            "event_ts": "2026-11-18T08:00:00Z",
            "location_code": "SH-LOCAL",
            "event_code": "IN_TRANSIT",
            "seq": 10 + i,
        }
        for i in (1, 2, 3)
    ]
    res = dup_svc.run_batch(triple, batch_id="B-TRIPLE-01")
    assert res["ok"], res.get("error")
    rep = res["report"]
    assert rep["dup_group_count"] == 1
    assert rep["quarantined_points"] == 3  # 隔离全员 = 命中数
    assert rep["written_official"] == 0

    tl = client.get("/api/waybills/WB-1002/timeline").json()
    official_ids = [h["id"] for h in tl["hops"]]
    for hid in ("H-21", "H-22", "H-23"):
        assert hid not in official_ids

    gid = rep["groups"][0]["group_id"]
    tc = client.get(f"/api/dup-groups/{gid}/tear-check").json()
    assert tc["member_count"] == 3
    assert {m["hop_id"] for m in tc["members"]} == {"H-21", "H-22", "H-23"}
    assert tc["official_leak"] == []
    assert all(not e["on_official_axis"] for e in tc["official_exclusion"])
    assert tc["checks"]["axis_excluded"]
    # 正式线与投影保持组前态：本批对 WB-1002 无正式写入
    assert tc["proj_before"] == tc["proj_after"] == tc["proj_current"]


def test_tear_check_entity_after_ingest(client):
    """整组撕裂核证实体：组键、全员、正式轴排除证明、投影前后，与隔离列表同源。"""
    _run_ingest(client)
    overview = client.get("/api/overview").json()
    groups = client.get("/api/quarantine").json()
    assert len(groups) == 2
    assert overview["quarantine"] == 2
    assert overview["quarantine_points"] == sum(g["member_count"] for g in groups) == 4
    for g in groups:
        r = client.get(f"/api/dup-groups/{g['id']}/tear-check")
        assert r.status_code == 200
        tc = r.json()
        # 组键
        assert tc["dup_key"]["waybill_id"] == g["waybill_id"]
        assert tc["dup_key"]["event_ts"] == g["event_ts"]
        assert tc["dup_key"]["location_code"] == g["location_code"]
        assert tc["dup_key"]["event_code"] == g["event_code"]
        # 全员：实时名册、物化名册、隔离列表三方一致
        listed = {m["hop_id"] for m in json.loads(g["member_json"])}
        assert {m["hop_id"] for m in tc["members"]} == listed
        assert set(tc["recorded_member_ids"]) == listed
        assert tc["member_count"] == tc["recorded_member_count"] == g["member_count"] == 2
        # 正式轴排除证明
        assert tc["official_leak"] == []
        assert all(not e["on_official_axis"] for e in tc["official_exclusion"])
        # 投影前后
        assert tc["proj_before"] and tc["proj_after"] and tc["proj_current"]
        assert tc["checks"] == {
            "axis_excluded": True,
            "roster_consistent": True,
            "recorded_roster_intact": True,
        }


def test_tear_check_404(client):
    _run_ingest(client)
    assert client.get("/api/dup-groups/DG-000000000000/tear-check").status_code == 404


def test_batch_failure_rolls_back(client):
    """失败闭：批内任一点异常 → 整批回滚，正式线与投影保持批前状态。"""
    _run_ingest(client)
    before = client.get("/api/waybills/WB-1001/timeline").json()
    quarantine_before = client.get("/api/quarantine").json()
    from app.modules.dup_hop_quarantine import service as dup_svc

    payload = [
        {
            "id": "H-30",
            "waybill_id": "WB-1001",
            "event_ts": "2026-11-18T09:00:00Z",
            "location_code": "HZ-HUB",
            "event_code": "IN_TRANSIT",
            "seq": 8,
        },
        {"id": "H-31", "waybill_id": "WB-1001"},  # 缺 event_ts → 裁定异常
    ]
    res = dup_svc.run_batch(payload, batch_id="B-BAD-01")
    assert not res["ok"]
    after = client.get("/api/waybills/WB-1001/timeline").json()
    assert [h["id"] for h in after["hops"]] == [h["id"] for h in before["hops"]]
    assert after["waybill"]["status_proj"] == before["waybill"]["status_proj"]
    assert client.get("/api/quarantine").json() == quarantine_before
