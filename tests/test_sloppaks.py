"""Sloppak copy-into-pack and read-only serving."""


def _copy(client, pack, filename):
    return client.post(
        f"/api/plugins/tutorials/packs/{pack}/sloppaks",
        json={"filename": filename},
    )


def test_copy_sloppak_from_library(client, pack, dlc_dir):
    (dlc_dir / "riff one.sloppak").write_bytes(b"chart-data")

    r = _copy(client, pack, "riff one.sloppak")
    assert r.status_code == 200
    body = r.json()
    assert body["filename"] == "riff one.sloppak"
    assert "riff%20one.sloppak" in body["url"]

    r = client.get(body["url"])
    assert r.status_code == 200
    assert r.content == b"chart-data"


def test_copy_rejects_escape_and_wrong_type(client, pack, dlc_dir):
    (dlc_dir / "notes.txt").write_bytes(b"nope")
    assert _copy(client, pack, "../outside.sloppak").status_code == 400
    assert _copy(client, pack, "notes.txt").status_code == 400
    assert _copy(client, pack, "missing.sloppak").status_code == 404


def test_copy_into_missing_pack_404(client, dlc_dir):
    (dlc_dir / "a.sloppak").write_bytes(b"x")
    assert _copy(client, "ghost", "a.sloppak").status_code == 404


def test_get_sloppak_rejects_traversal(client, pack, dlc_dir):
    (dlc_dir / "a.sloppak").write_bytes(b"x")
    _copy(client, pack, "a.sloppak")
    base = f"/api/plugins/tutorials/packs/{pack}/sloppaks"
    for bad in ("..%2Fpack.json", ".hidden", "..%5C..%5Cprogress.json"):
        assert client.get(f"{base}/{bad}").status_code == 404, bad
