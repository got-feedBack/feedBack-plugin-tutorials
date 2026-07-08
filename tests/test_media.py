"""Video / cover / thumb upload guardrails and serving."""


def _upload(client, url, name, content=b"data", mime="video/mp4"):
    return client.post(url, files={"file": (name, content, mime)})


# ── Videos ────────────────────────────────────────────────────────────────────

def test_video_upload_and_fetch(client, pack):
    url = f"/api/plugins/tutorials/packs/{pack}/videos?lesson_id=l1"
    r = _upload(client, url, "clip.mp4", b"\x00" * 128, "video/mp4")
    assert r.status_code == 200
    body = r.json()
    assert body["filename"] == "l1.mp4"
    assert body["size"] == 128

    r = client.get(f"/api/plugins/tutorials/packs/{pack}/videos/l1.mp4")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("video/mp4")
    assert r.content == b"\x00" * 128


def test_video_swap_removes_stale_extension(client, pack):
    base = f"/api/plugins/tutorials/packs/{pack}/videos"
    _upload(client, f"{base}?lesson_id=l1", "a.mp4", b"x", "video/mp4")
    _upload(client, f"{base}?lesson_id=l1", "b.webm", b"y", "video/webm")
    assert client.get(f"{base}/l1.webm").status_code == 200
    assert client.get(f"{base}/l1.mp4").status_code == 404


def test_video_rejects_bad_extension_and_mime(client, pack):
    url = f"/api/plugins/tutorials/packs/{pack}/videos?lesson_id=l1"
    assert _upload(client, url, "evil.exe", b"x", "video/mp4").status_code == 400
    assert _upload(client, url, "clip.mp4", b"x", "text/html").status_code == 400


def test_video_rejects_empty_upload(client, pack):
    url = f"/api/plugins/tutorials/packs/{pack}/videos?lesson_id=l1"
    assert _upload(client, url, "clip.mp4", b"", "video/mp4").status_code == 400


def test_video_upload_to_missing_pack_404(client):
    url = "/api/plugins/tutorials/packs/ghost/videos?lesson_id=l1"
    assert _upload(client, url, "clip.mp4", b"x", "video/mp4").status_code == 404


def test_video_fetch_rejects_traversal_names(client, pack):
    base = f"/api/plugins/tutorials/packs/{pack}/videos"
    for bad in ("..%2F..%2Fpack.json", "..%5Cpack.json", ".hidden.mp4", "UPPER.mp4"):
        assert client.get(f"{base}/{bad}").status_code == 404, bad


# ── Covers ────────────────────────────────────────────────────────────────────

def test_cover_upload_fetch_delete(client, pack):
    base = f"/api/plugins/tutorials/packs/{pack}"
    r = _upload(client, f"{base}/cover", "cover.png", b"\x89PNG", "image/png")
    assert r.status_code == 200
    assert r.json()["filename"] == "cover.png"

    r = client.get(f"{base}/cover")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")

    # Summary + manifest now expose cover_url.
    packs = {p["id"]: p for p in client.get("/api/plugins/tutorials/packs").json()["packs"]}
    assert packs[pack]["cover_url"] == f"/api/plugins/tutorials/packs/{pack}/cover"
    assert "cover_url" in client.get(f"{base}").json()

    r = client.delete(f"{base}/cover")
    assert r.json()["deleted"] == ["cover.png"]
    assert client.get(f"{base}/cover").status_code == 404


def test_cover_swap_is_single_slot(client, pack):
    base = f"/api/plugins/tutorials/packs/{pack}/cover"
    _upload(client, base, "a.png", b"p", "image/png")
    _upload(client, base, "b.webp", b"w", "image/webp")
    r = client.get(base)
    assert r.headers["content-type"].startswith("image/webp")
    # Delete reports exactly one slot on disk.
    assert client.delete(base).json()["deleted"] == ["cover.webp"]


def test_cover_rejects_bad_type(client, pack):
    base = f"/api/plugins/tutorials/packs/{pack}/cover"
    assert _upload(client, base, "cover.gif", b"g", "image/gif").status_code == 400
    assert _upload(client, base, "cover.png", b"g", "text/html").status_code == 400


# ── Lesson thumbs ─────────────────────────────────────────────────────────────

def test_lesson_thumb_roundtrip(client, pack):
    base = f"/api/plugins/tutorials/packs/{pack}/lessons/l1/thumb"
    assert client.get(base).status_code == 404

    r = _upload(client, base, "t.jpg", b"j", "image/jpeg")
    assert r.status_code == 200
    assert r.json()["filename"] == "l1.jpg"

    r = client.get(base)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/jpeg")

    assert client.delete(base).json()["deleted"] == ["l1.jpg"]
    assert client.get(base).status_code == 404


def test_thumb_url_enriches_manifest(client, pack):
    from conftest import make_manifest
    manifest = make_manifest(pack, lessons=[{"id": "l1"}])
    client.put(f"/api/plugins/tutorials/packs/{pack}", json=manifest)
    thumb = f"/api/plugins/tutorials/packs/{pack}/lessons/l1/thumb"
    _upload(client, thumb, "t.png", b"p", "image/png")

    lesson = client.get(f"/api/plugins/tutorials/packs/{pack}").json()["lessons"][0]
    assert lesson["thumb_url"] == thumb
