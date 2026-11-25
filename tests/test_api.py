import threading

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def register(client, **over):
    body = {
        "kind": "dark",
        "detector": "CCD-A",
        "exposure_seconds": 60,
        "batch_id": "B1",
        "size_bytes": 1000,
    }
    body.update(over)
    r = client.post("/api/calibration-files", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def image(i="img1", **over):
    body = {"image_id": i, "detector": "CCD-A", "exposure_seconds": 60, "filter": "r"}
    body.update(over)
    return body


def two_pair_setup(client):
    """B1 expensive (2x1000), B2 cheap (2x10). Returns (ids B1, ids B2)."""
    d1 = register(client, kind="dark", batch_id="B1", size_bytes=1000)
    f1 = register(client, kind="flat", filter="r", exposure_seconds=None, batch_id="B1", size_bytes=1000)
    d2 = register(client, kind="dark", batch_id="B2", size_bytes=10)
    f2 = register(client, kind="flat", filter="r", exposure_seconds=None, batch_id="B2", size_bytes=10)
    return (d1["id"], f1["id"]), (d2["id"], f2["id"])


# ---------------------------------------------------------------------------

def test_health_and_root(client):
    assert client.get("/health").status_code == 200
    assert client.get("/healthz").status_code == 200
    assert client.get("/").status_code == 200


def test_register_get_list_deactivate(client):
    f = register(client)
    assert f["id"] == 1 and f["is_active"] is True
    got = client.get(f"/api/calibration-files/{f['id']}").json()
    assert got["batch_id"] == "B1" and got["size_bytes"] == 1000

    assert len(client.get("/api/calibration-files").json()) == 1
    assert len(client.get("/api/calibration-files?active=true").json()) == 1

    r = client.post(f"/api/calibration-files/{f['id']}/deactivate", json={"reason": "drift"})
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert r.json()["deactivate_reason"] == "drift"
    assert client.get("/api/calibration-files?active=true").json() == []
    logs = client.get("/api/deactivations").json()
    assert any(e["action"] == "deactivated" for e in logs)

    # soft delete alias behaves the same
    f2 = register(client, batch_id="B2")
    assert client.delete(f"/api/calibration-files/{f2['id']}").json()["deactivated"] is True


def test_chinese_field_names_and_kind(client):
    body = {"类型": "暗场", "探测器": "X", "曝光时长": 45, "标定批次": "CB", "下载字节数": 777}
    r = client.post("/api/calibration-files", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["kind"] == "dark" and data["exposure_seconds"] == 45 and data["size_bytes"] == 777


def test_client_supplied_string_and_numeric_ids(client):
    r = client.post("/api/calibration-files", json={
        "id": "DARK-A", "kind": "dark", "detector": "D", "exposure_seconds": 10,
        "batch_id": "B", "size_bytes": 5})
    assert r.status_code == 200 and r.json()["id"] == "DARK-A"
    assert client.get("/api/calibration-files/DARK-A").status_code == 200

    r2 = client.post("/api/calibration-files", json={
        "file_no": 42, "kind": "flat", "detector": "D", "filter": "r",
        "batch_id": "B", "size_bytes": 7})
    assert r2.json()["id"] == 42
    assert client.get("/api/calibration-files/42").json()["pk"] == 42

    # candidates may reference either identity form
    o = client.post("/api/orders", json={
        "images": [image(detector="D", exposure_seconds=10)],
        "candidate_ids": ["DARK-A", 42]})
    assert o.status_code == 200, o.text
    body = o.json()
    assert body["publishable"] is True
    assert body["total_bytes"] == 12


def test_candidate_cap_is_16(client):
    r = client.post("/api/orders", json={"images": [image()], "candidate_file_ids": list(range(1, 18))})
    assert r.status_code == 422
    assert r.json()["max_candidates"] == 16


def test_unknown_candidate_rejected(client):
    r = client.post("/api/orders", json={"images": [image()], "candidate_file_ids": [999]})
    assert r.status_code == 422
    assert r.json()["unknown_candidate_ids"] == [999]


def test_order_minimum_and_reuse(client):
    b1, b2 = two_pair_setup(client)
    o = client.post("/api/orders", json={
        "images": [image("i1"), image("i2")],
        "candidate_file_ids": [*b1, *b2],
    })
    d = o.json()
    assert d["status"] == "ready" and d["publishable"] is True
    assert d["total_bytes"] == 20
    assert {f["id"] for f in d["files"]} == set(b2)
    assert all(p["dark_file_id"] == b2[0] and p["flat_file_id"] == b2[1] for p in d["pairings"])


def test_publish_recomputes_after_deactivation_then_freezes(client):
    b1, b2 = two_pair_setup(client)
    d = client.post("/api/orders", json={
        "images": [image("i1"), image("i2")], "candidate_file_ids": [*b1, *b2],
    }).json()
    oid = d["id"]
    assert d["total_bytes"] == 20

    # Deactivate one of the selected cheap files BEFORE publishing.
    r = client.post(f"/api/calibration-files/{b2[0]}/deactivate", json={"reason": "bad column"})
    assert r.status_code == 200

    p = client.post(f"/api/orders/{oid}/publish").json()
    assert p["published"] is True and p["status"] == "published"
    assert p["recomputed"] is True
    assert b2[0] in p["deactivated_selected_file_ids"]
    # Only full B1 pair remains (B2 now lacks its dark).
    assert p["total_bytes"] == 2000
    issued = p["manifest"]
    assert issued["total_bytes"] == 2000

    # Repeated publish is idempotent and returns the same issued manifest.
    p2 = client.post(f"/api/orders/{oid}/publish").json()
    assert p2.get("already_published") is True
    assert p2["manifest"] == issued

    # Deactivating issued files afterwards must not alter the frozen manifest.
    client.post(f"/api/calibration-files/{b1[0]}/deactivate")
    got = client.get(f"/api/orders/{oid}").json()
    assert got["total_bytes"] == 2000
    assert got["manifest"] == issued
    assert got["published"] is True

    # Query by order number works too.
    by_no = client.get(f"/api/orders/{p['order_no']}").json()
    assert by_no["id"] == oid and by_no["total_bytes"] == 2000


def test_atomic_publish_query_param(client):
    b1, b2 = two_pair_setup(client)
    r = client.post("/api/orders?publish=true", json={
        "images": [image()], "candidate_file_ids": [*b1, *b2]})
    assert r.status_code == 200
    assert r.json()["published"] is True
    assert r.json()["total_bytes"] == 20


def test_missing_pairs_list_images_and_not_published(client):
    b1, _ = two_pair_setup(client)
    o = client.post("/api/orders", json={
        "images": [
            image("good"),
            image("wrong-exp", exposure_seconds=999),
            image("wrong-det", detector="CCD-Z"),
        ],
        "candidate_file_ids": list(b1),
    })
    d = o.json()
    assert d["status"] == "unmatched" and d["published"] is False
    assert d["missing_image_ids"] == ["wrong-exp", "wrong-det"]
    assert "wrong-exp" in d["missing_images"]

    # Publishing keeps it unpublished and repeats the missing list.
    p = client.post(f"/api/orders/{d['id']}/publish")
    assert p.status_code == 200
    pd = p.json()
    assert pd["published"] is False and pd["publish_blocked"] is True
    assert pd["missing_image_ids"] == ["wrong-exp", "wrong-det"]

    g = client.get(f"/api/orders/{d['id']}").json()
    assert g["published"] is False
    assert g["manifest"]["published"] is False
    assert g["manifest"]["missing_image_ids"] == ["wrong-exp", "wrong-det"]
    assert g["manifest"]["total_bytes"] is None

    # strict mode turns the domain failure into a 422 but keeps the order.
    s = client.post(f"/api/orders/{d['id']}/publish?strict=true")
    assert s.status_code == 422
    assert s.json()["missing_images"] == ["wrong-exp", "wrong-det"]


def test_recompute_picks_up_reactivation(client):
    b1, b2 = two_pair_setup(client)
    d = client.post("/api/orders", json={
        "images": [image()], "candidate_file_ids": [*b1, *b2]}).json()
    # wipe out B2 dark and B1 dark -> nothing
    client.post(f"/api/calibration-files/{b2[0]}/deactivate")
    client.post(f"/api/calibration-files/{b1[0]}/deactivate")
    p = client.post(f"/api/orders/{d['id']}/publish").json()
    assert p["published"] is False
    assert p["missing_image_ids"] == ["img1"]

    # reactivate expensive B1 dark -> B1 pair works
    client.post(f"/api/calibration-files/{b1[0]}/activate")
    p2 = client.post(f"/api/orders/{d['id']}/publish").json()
    assert p2["published"] is True and p2["total_bytes"] == 2000


def test_get_unpublished_order_refreshes_after_deactivation(client):
    b1, b2 = two_pair_setup(client)
    d = client.post("/api/orders", json={
        "images": [image()], "candidate_file_ids": [*b1, *b2]}).json()
    assert d["total_bytes"] == 20

    client.post(f"/api/calibration-files/{b2[0]}/deactivate")
    g = client.get(f"/api/orders/{d['id']}").json()
    # Unpublished draft view is recomputed with active candidates.
    assert g["draft_stale"] is True
    assert g["total_bytes"] == 2000
    assert all(f["id"] != b2[0] for f in g["files"])
    # Nothing published yet.
    assert g["published"] is False
    # Publish freezes exactly that plan and reports recomputation.
    p = client.post(f"/api/orders/{d['id']}/publish").json()
    assert p["published"] is True and p["total_bytes"] == 2000


def test_path_aliases(client):
    f = client.post("/calibrations", json={
        "type": "dark", "camera": "C", "exptime": 1, "batch": "B", "size": 3}).json()
    assert f["id"] == 1
    o = client.post("/orders", json={
        "images": [{"name": "x", "camera": "C", "exptime": 1, "filter": None}],
        "candidate_ids": [f["id"]]})
    # only a dark, no flat -> unmatched but endpoint exists
    assert o.status_code == 200 and o.json()["published"] is False
    assert client.get("/calibrations").status_code == 200


def test_deactivation_aliases(client):
    f = register(client)
    # collection-level body route
    r = client.post("/api/deactivations", json={"file_id": f["id"], "reason": "r1"})
    assert r.status_code == 200 and r.json()["is_active"] is False
    client.post(f"/api/calibration-files/{f['id']}/activate")
    # status route with string state
    r = client.post(f"/api/calibration-files/{f['id']}/status", json={"status": "retired"})
    assert r.status_code == 200 and r.json()["is_active"] is False
    client.post(f"/api/calibration-files/{f['id']}/activate")
    # bare POST to item with boolean body
    r = client.post(f"/api/calibration-files/{f['id']}", json={"active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False
    # static /deactivate must not be swallowed by /{file_id}
    f2 = register(client, batch_id="B9")
    r = client.post("/api/calibration-files/deactivate", json={"id": f2["id"]})
    assert r.status_code == 200 and r.json()["id"] == f2["id"] and r.json()["is_active"] is False


def test_bulk_registration(client):
    r = client.post("/api/calibration-files", json=[
        {"kind": "dark", "detector": "D", "exposure_seconds": 1, "batch_id": "B", "size_bytes": 1},
        {"kind": "flat", "detector": "D", "filter": "r", "batch_id": "B", "size_bytes": 2},
    ])
    assert r.status_code == 200 and r.json()["count"] == 2


def test_concurrent_orders_and_deactivations_no_500(tmp_path):
    app = create_app(str(tmp_path / "conc.db"))
    with TestClient(app) as c:
        ids = []
        for i in range(12):
            kind = "dark" if i % 2 == 0 else "flat"
            body = {"kind": kind, "detector": "D", "batch_id": f"G{i//2}", "size_bytes": 10 + i}
            if kind == "dark":
                body["exposure_seconds"] = 60
            else:
                body["filter"] = "r"
            ids.append(c.post("/api/calibration-files", json=body).json()["id"])

        errors = []

        def worker(k):
            try:
                o = c.post("/api/orders", json={
                    "images": [image(f"img{k}", detector="D")], "candidate_file_ids": ids}).json()
                assert o["publishable"] is True
                if k % 3 == 0:
                    pub = c.post(f"/api/orders/{o['id']}/publish").json()
                    assert pub["published"] is True
                else:
                    c.post(f"/api/calibration-files/{ids[k]}/deactivate")
                    pub = c.post(f"/api/orders/{o['id']}/publish").json()
                    # six independent batches; killing one file leaves >=5 pairs
                    assert pub["published"] is True
                g = c.get(f"/api/orders/{o['id']}")
                assert g.status_code == 200
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
