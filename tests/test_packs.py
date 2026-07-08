"""Pack CRUD + manifest validation."""

from conftest import BUILTIN_PACK_IDS, make_manifest


def test_setup_seeds_builtin_packs(client):
    r = client.get("/api/plugins/tutorials/packs")
    assert r.status_code == 200
    ids = {p["id"] for p in r.json()["packs"]}
    assert set(BUILTIN_PACK_IDS) <= ids


def test_create_and_get_pack(client):
    r = client.post(
        "/api/plugins/tutorials/packs",
        json={"id": "my-pack_1", "title": "My Pack", "author": "me"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["schema"] == 1
    assert body["id"] == "my-pack_1"
    assert body["lessons"] == []

    r = client.get("/api/plugins/tutorials/packs/my-pack_1")
    assert r.status_code == 200
    assert r.json()["title"] == "My Pack"


def test_create_duplicate_pack_conflicts(client, pack):
    r = client.post(
        "/api/plugins/tutorials/packs",
        json={"id": pack, "title": "Again"},
    )
    assert r.status_code == 409


def test_create_pack_invalid_id(client):
    for bad in ("../evil", "UPPER", "", "a b", "-leading"):
        r = client.post(
            "/api/plugins/tutorials/packs",
            json={"id": bad, "title": "Bad"},
        )
        assert r.status_code == 400, bad


def test_get_missing_pack_404(client):
    assert client.get("/api/plugins/tutorials/packs/nope").status_code == 404


def test_list_packs_includes_created(client, pack):
    r = client.get("/api/plugins/tutorials/packs")
    summaries = {p["id"]: p for p in r.json()["packs"]}
    assert pack in summaries
    s = summaries[pack]
    assert s["title"] == "Test Pack"
    assert s["lesson_count"] == 0
    assert s["cover_url"] is None


def test_update_pack_roundtrip(client, pack):
    manifest = make_manifest(pack, lessons=[
        {"id": "lesson-1", "title": "L1", "pass": {"accuracy": 0.6}},
    ])
    r = client.put(f"/api/plugins/tutorials/packs/{pack}", json=manifest)
    assert r.status_code == 200

    r = client.get(f"/api/plugins/tutorials/packs/{pack}")
    assert [l["id"] for l in r.json()["lessons"]] == ["lesson-1"]


def test_update_missing_pack_404(client):
    r = client.put(
        "/api/plugins/tutorials/packs/ghost",
        json=make_manifest("ghost"),
    )
    assert r.status_code == 404


def test_update_rejects_wrong_schema(client, pack):
    manifest = make_manifest(pack)
    manifest["schema"] = 2
    assert client.put(f"/api/plugins/tutorials/packs/{pack}", json=manifest).status_code == 400


def test_update_rejects_id_mismatch(client, pack):
    manifest = make_manifest("other-id")
    assert client.put(f"/api/plugins/tutorials/packs/{pack}", json=manifest).status_code == 400


def test_update_rejects_duplicate_lesson_ids(client, pack):
    manifest = make_manifest(pack, lessons=[{"id": "dup"}, {"id": "dup"}])
    assert client.put(f"/api/plugins/tutorials/packs/{pack}", json=manifest).status_code == 400


def test_update_rejects_invalid_lesson_id(client, pack):
    manifest = make_manifest(pack, lessons=[{"id": "../escape"}])
    assert client.put(f"/api/plugins/tutorials/packs/{pack}", json=manifest).status_code == 400


def test_update_rejects_null_pass_accuracy(client, pack):
    manifest = make_manifest(pack, lessons=[
        {"id": "l1", "pass": {"accuracy": None}},
    ])
    assert client.put(f"/api/plugins/tutorials/packs/{pack}", json=manifest).status_code == 400


def test_update_rejects_out_of_range_thresholds(client, pack):
    for lesson in (
        {"id": "l1", "pass": {"accuracy": 1.5}},
        {"id": "l1", "mastery": {"accuracy": -0.1}},
        {"id": "l1", "mastery": {"speed": 3.0}},
        {"id": "l1", "pass": {"accuracy": True}},
    ):
        manifest = make_manifest(pack, lessons=[lesson])
        r = client.put(f"/api/plugins/tutorials/packs/{pack}", json=manifest)
        assert r.status_code == 400, lesson


def test_update_rejects_oversized_manifest(client, pack):
    manifest = make_manifest(pack)
    manifest["padding"] = "x" * (300 * 1024)
    assert client.put(f"/api/plugins/tutorials/packs/{pack}", json=manifest).status_code == 413


def test_delete_pack(client, pack):
    assert client.delete(f"/api/plugins/tutorials/packs/{pack}").status_code == 200
    assert client.get(f"/api/plugins/tutorials/packs/{pack}").status_code == 404
    assert client.delete(f"/api/plugins/tutorials/packs/{pack}").status_code == 404
