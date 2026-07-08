"""Run recording, pass/mastery thresholds, progress persistence."""

import json

import routes
from conftest import make_manifest

RUNS = "/api/plugins/tutorials/runs"
PROGRESS = "/api/plugins/tutorials/progress"


def _install_lesson(client, pack, lesson):
    manifest = make_manifest(pack, lessons=[lesson])
    r = client.put(f"/api/plugins/tutorials/packs/{pack}", json=manifest)
    assert r.status_code == 200, r.text


def _run(client, pack, lesson_id, score=100, accuracy=0.5, speed=1.0):
    return client.post(RUNS, json={
        "pack_id": pack, "lesson_id": lesson_id,
        "score": score, "accuracy": accuracy, "speed": speed,
    })


def test_run_below_pass_threshold(client, pack):
    _install_lesson(client, pack, {"id": "l1", "pass": {"accuracy": 0.7}})
    r = _run(client, pack, "l1", accuracy=0.5)
    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is False
    assert body["mastered"] is False


def test_run_pass_and_mastery(client, pack):
    _install_lesson(client, pack, {
        "id": "l1",
        "pass": {"accuracy": 0.7},
        "mastery": {"accuracy": 0.9, "speed": 1.0},
    })
    r = _run(client, pack, "l1", accuracy=0.95, speed=1.0)
    body = r.json()
    assert body["passed"] is True
    assert body["mastered"] is True
    assert body["first_pass"] is True
    assert body["first_mastery"] is True

    # Second mastery run is no longer "first".
    body = _run(client, pack, "l1", accuracy=0.95, speed=1.0).json()
    assert body["first_pass"] is False
    assert body["first_mastery"] is False


def test_mastery_requires_speed(client, pack):
    _install_lesson(client, pack, {
        "id": "l1", "mastery": {"accuracy": 0.9, "speed": 1.0},
    })
    body = _run(client, pack, "l1", accuracy=0.95, speed=0.8).json()
    assert body["passed"] is True
    assert body["mastered"] is False


def test_default_thresholds(client, pack):
    # No pass/mastery in manifest → defaults 0.7 / 0.9 / 1.0.
    _install_lesson(client, pack, {"id": "l1"})
    body = _run(client, pack, "l1", accuracy=0.7).json()
    assert body["passed"] is True
    assert body["thresholds"] == {
        "pass_accuracy": 0.7, "mastery_accuracy": 0.9, "mastery_speed": 1.0,
    }


def test_null_thresholds_in_manifest_do_not_crash(client, pack, tmp_path):
    # Simulate a manifest edited on disk to contain explicit nulls,
    # bypassing PUT validation: record_run must fall back to defaults.
    _install_lesson(client, pack, {"id": "l1"})
    mpath = routes._state["packs_dir"] / pack / "pack.json"
    data = json.loads(mpath.read_text(encoding="utf-8"))
    data["lessons"][0]["pass"] = {"accuracy": None}
    data["lessons"][0]["mastery"] = None
    mpath.write_text(json.dumps(data), encoding="utf-8")

    r = _run(client, pack, "l1", accuracy=0.8)
    assert r.status_code == 200
    assert r.json()["passed"] is True  # default 0.7


def test_best_score_is_monotonic(client, pack):
    _install_lesson(client, pack, {"id": "l1"})
    _run(client, pack, "l1", score=500, accuracy=0.8)
    _run(client, pack, "l1", score=200, accuracy=0.4)

    state = client.get(PROGRESS).json()["packs"][pack]["lessons"]["l1"]
    assert state["best_score"] == 500
    assert state["best_accuracy"] == 0.8
    assert state["last_accuracy"] == 0.4
    assert state["passed"] is True  # sticky across a later failing run


def test_run_unknown_pack_or_lesson_404(client, pack):
    _install_lesson(client, pack, {"id": "l1"})
    assert _run(client, "ghost", "l1").status_code == 404
    assert _run(client, pack, "ghost").status_code == 404


def test_run_validation_422(client, pack):
    _install_lesson(client, pack, {"id": "l1"})
    bad = [
        {"score": -1, "accuracy": 0.5, "speed": 1.0},
        {"score": 1, "accuracy": 1.5, "speed": 1.0},
        {"score": 1, "accuracy": 0.5, "speed": 5.0},
    ]
    for extra in bad:
        r = client.post(RUNS, json={"pack_id": pack, "lesson_id": "l1", **extra})
        assert r.status_code == 422, extra


def test_progress_survives_corrupt_file(client, pack):
    _install_lesson(client, pack, {"id": "l1"})
    _run(client, pack, "l1", score=10, accuracy=0.9)
    routes._state["progress_path"].write_text("{not json", encoding="utf-8")
    # Unreadable progress resets rather than 500s.
    r = client.get(PROGRESS)
    assert r.status_code == 200
    assert r.json() == {"packs": {}}
    # And a new run recreates it.
    assert _run(client, pack, "l1", score=5, accuracy=0.3).status_code == 200
    assert client.get(PROGRESS).json()["packs"][pack]["lessons"]["l1"]["best_score"] == 5
