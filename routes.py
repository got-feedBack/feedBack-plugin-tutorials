"""Tutorials plugin — pack CRUD, video upload, local progress tracking.

State lives under `<config_dir>/tutorials/`:
  packs/<pack_id>/pack.json     manifest
  packs/<pack_id>/videos/<lesson_id>.<ext>   uploaded videos (file mode)
  packs/<pack_id>/sloppaks/...  copies of library sloppaks
  progress.json                 cross-pack best score + pass/mastery state

XP is awarded by the minigames plugin. The frontend posts each run to
both /api/plugins/minigames/runs (XP) and /api/plugins/tutorials/runs
(local progress) — no server-side relay, no cross-plugin coupling.

Endpoints (under /api/plugins/tutorials/):
  GET    /packs                     list packs (manifest summaries)
  GET    /packs/{pack}              full manifest
  POST   /packs                     create empty pack from {id,title,...}
  PUT    /packs/{pack}              replace manifest (atomic)
  DELETE /packs/{pack}              remove a pack
  POST   /packs/{pack}/videos       upload webm/mp4 for ?lesson_id=...
  GET    /packs/{pack}/videos/{f}   stream uploaded video
  GET    /packs/{pack}/sloppaks/{f} stream a copied sloppak
  POST   /packs/{pack}/sloppaks     copy a library sloppak in by filename
  POST   /runs                      record local best score for a lesson
  GET    /progress                  per-pack/per-lesson progress map
"""

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote as _url_quote

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

PLUGIN_ID = "tutorials"

# Pack and lesson IDs land in filesystem paths and URLs, so restrict
# to a narrow charset. Generous enough for "intro-bends_v2" style IDs;
# strict enough to make path traversal structurally impossible.
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

ALLOWED_VIDEO_EXTS = {"mp4", "webm"}
ALLOWED_VIDEO_MIMES = {"video/mp4", "video/webm"}
MAX_VIDEO_BYTES = 100 * 1024 * 1024  # 100 MB — short tutorial clips at modest bitrate

ALLOWED_COVER_EXTS  = {"png", "jpg", "jpeg", "webp"}
ALLOWED_COVER_MIMES = {"image/png", "image/jpeg", "image/webp"}
COVER_EXT_TO_MIME   = {
    "png":  "image/png",
    "jpg":  "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}
MAX_COVER_BYTES = 4 * 1024 * 1024  # 4 MB — covers should be small

MAX_MANIFEST_BYTES = 256 * 1024  # 256 KB — fits a very large pack

_lock = threading.Lock()
_state: dict = {
    "config_dir":   None,
    "packs_dir":    None,
    "progress_path": None,
    "log":          logging.getLogger("slopsmith.plugin.tutorials"),
}


# ── Pydantic models ───────────────────────────────────────────────────────────

class PackCreate(BaseModel):
    id:    str
    title: str = Field(min_length=1, max_length=200)
    author: str = Field(default="", max_length=200)


class RunRecord(BaseModel):
    pack_id:   str
    lesson_id: str
    score:     int = Field(ge=0)
    accuracy:  float = Field(ge=0.0, le=1.0)
    speed:     float = Field(ge=0.1, le=2.0, default=1.0)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _validate_id(value: str, kind: str) -> None:
    if not isinstance(value, str) or not ID_RE.match(value):
        raise HTTPException(400, f"Invalid {kind} id: must match {ID_RE.pattern}")


def _pack_dir(pack_id: str) -> Path:
    _validate_id(pack_id, "pack")
    return _state["packs_dir"] / pack_id


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON via temp+fsync+rename. Mirrors minigames profile save."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tut-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_manifest(pack_id: str) -> dict:
    pdir = _pack_dir(pack_id)
    mpath = pdir / "pack.json"
    if not mpath.is_file():
        raise HTTPException(404, f"Pack not found: {pack_id}")
    try:
        raw = mpath.read_text(encoding="utf-8")
        if len(raw) > MAX_MANIFEST_BYTES:
            raise HTTPException(413, "Manifest exceeds size cap")
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"Manifest is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise HTTPException(500, "Manifest is not a JSON object")
    return data


def _validate_manifest(manifest: dict, pack_id: str) -> None:
    """Light schema validation. Frontend authors the manifest; this catches
    structural breakage that would otherwise blow up the lesson player."""
    if manifest.get("schema") != 1:
        raise HTTPException(400, "Manifest schema must be 1")
    if manifest.get("id") != pack_id:
        raise HTTPException(400, "Manifest id does not match URL pack id")
    lessons = manifest.get("lessons", [])
    if not isinstance(lessons, list):
        raise HTTPException(400, "lessons must be an array")
    seen_ids = set()
    for i, lesson in enumerate(lessons):
        if not isinstance(lesson, dict):
            raise HTTPException(400, f"lesson #{i} is not an object")
        lid = lesson.get("id")
        if not isinstance(lid, str) or not ID_RE.match(lid):
            raise HTTPException(400, f"lesson #{i} has an invalid id")
        if lid in seen_ids:
            raise HTTPException(400, f"duplicate lesson id: {lid}")
        seen_ids.add(lid)
        # Validate threshold shapes so that record_run never hits a
        # TypeError when comparing run.accuracy against a null/non-numeric
        # threshold saved via a manual PUT.  We check for the key being
        # present (via `in`) rather than truthy so that explicit JSON null
        # (`"accuracy": null`) is caught — `.get()` returns None for both
        # missing and null-valued keys.
        pass_thr = lesson.get("pass")
        if pass_thr is not None:
            if not isinstance(pass_thr, dict):
                raise HTTPException(400, f"lesson {lid}: pass must be an object")
            if "accuracy" in pass_thr:
                acc = pass_thr["accuracy"]
                if not isinstance(acc, (int, float)) or isinstance(acc, bool):
                    raise HTTPException(400, f"lesson {lid}: pass.accuracy must be numeric")
                if not (0.0 <= float(acc) <= 1.0):
                    raise HTTPException(400, f"lesson {lid}: pass.accuracy must be in [0, 1]")
        mastery = lesson.get("mastery")
        if mastery is not None:
            if not isinstance(mastery, dict):
                raise HTTPException(400, f"lesson {lid}: mastery must be an object")
            for field in ("accuracy", "speed"):
                if field in mastery:
                    val = mastery[field]
                    if not isinstance(val, (int, float)) or isinstance(val, bool):
                        raise HTTPException(
                            400, f"lesson {lid}: mastery.{field} must be numeric"
                        )
                    if field == "accuracy" and not (0.0 <= float(val) <= 1.0):
                        raise HTTPException(
                            400, f"lesson {lid}: mastery.accuracy must be in [0, 1]"
                        )
                    if field == "speed" and (float(val) <= 0 or float(val) > 2.0):
                        raise HTTPException(
                            400,
                            f"lesson {lid}: mastery.speed must be in (0, 2] "
                            "(matches RunRecord.speed upper bound)"
                        )


def _find_cover_file(pack_id: str) -> Path | None:
    """Return the on-disk cover image path for a pack, if one exists.
    Cover is stored as `cover.<ext>` directly inside the pack dir.
    Searched in a fixed order so a webp upload supersedes a png leftover
    only after the older slot is cleaned up — same single-slot pattern
    used for video uploads."""
    pdir = _pack_dir(pack_id)
    for ext in ("webp", "png", "jpg", "jpeg"):
        candidate = pdir / f"cover.{ext}"
        if candidate.is_file():
            return candidate
    return None


def _find_lesson_thumb(pack_id: str, lesson_id: str) -> Path | None:
    """Per-lesson thumbnail. Stored at `<pack>/thumbs/<lesson_id>.<ext>`."""
    if not ID_RE.match(lesson_id):
        return None
    thumbs_dir = _pack_dir(pack_id) / "thumbs"
    for ext in ("webp", "png", "jpg", "jpeg"):
        candidate = thumbs_dir / f"{lesson_id}.{ext}"
        if candidate.is_file():
            return candidate
    return None


def _enrich_manifest(pack_id: str, manifest: dict) -> dict:
    """Decorate a manifest with derived URLs (cover, per-lesson thumb) so
    the frontend doesn't need to issue a separate HEAD per lesson to know
    whether a thumb exists. The manifest on disk stays clean — these
    fields are computed at read-time from filesystem state."""
    out = dict(manifest)
    if _find_cover_file(pack_id) is not None:
        out["cover_url"] = f"/api/plugins/{PLUGIN_ID}/packs/{pack_id}/cover"
    lessons = []
    for lesson in manifest.get("lessons", []) or []:
        if not isinstance(lesson, dict):
            lessons.append(lesson)
            continue
        lid = lesson.get("id")
        annotated = dict(lesson)
        if isinstance(lid, str) and _find_lesson_thumb(pack_id, lid) is not None:
            annotated["thumb_url"] = (
                f"/api/plugins/{PLUGIN_ID}/packs/{pack_id}/lessons/{lid}/thumb"
            )
        lessons.append(annotated)
    out["lessons"] = lessons
    return out


def _summarise(pack_id: str, manifest: dict) -> dict:
    """Slim manifest representation for the /packs grid."""
    summary = {
        "id":          manifest.get("id"),
        "title":       manifest.get("title", ""),
        "author":      manifest.get("author", ""),
        "techniques":  manifest.get("techniques", []),
        "lesson_count": len(manifest.get("lessons", []) or []),
        "cover_url":   None,
    }
    if _find_cover_file(pack_id) is not None:
        summary["cover_url"] = f"/api/plugins/{PLUGIN_ID}/packs/{pack_id}/cover"
    return summary


def _load_progress() -> dict:
    path = _state["progress_path"]
    if path and path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError) as e:
            _state["log"].warning("progress.json unreadable, recreating: %s", e)
    return {"packs": {}}


def _save_progress(progress: dict) -> None:
    _atomic_write_json(_state["progress_path"], progress)


def _seed_builtin_packs(plugin_dir: Path, dlc_dir: Path | None = None) -> None:
    """Copy any pack under <plugin>/builtin/ into <CONFIG_DIR>/tutorials/packs/.
    Idempotent: if the destination pack.json already exists, skip the pack.

    If dlc_dir is provided, the pack's exercise sloppaks are also copied into
    ``<dlc_dir>/tutorials-builtin/<pack_id>/`` so that ``window.playSong``
    (which resolves filenames against DLC_DIR) can find them without requiring
    the user to put tutorial sloppaks in their own library folder.  Sloppaks
    already present at the destination are skipped."""
    builtin = plugin_dir / "builtin"
    if not builtin.is_dir():
        return
    for src in builtin.iterdir():
        if not src.is_dir():
            continue
        if not ID_RE.match(src.name):
            _state["log"].warning("builtin pack %r has an invalid id; skipping", src.name)
            continue
        if not (src / "pack.json").is_file():
            continue
        dst = _state["packs_dir"] / src.name
        if not (dst / "pack.json").is_file():
            try:
                shutil.copytree(src, dst)
                _state["log"].info("seeded builtin pack: %s", src.name)
            except OSError as e:
                _state["log"].warning("failed to seed builtin pack %s: %s", src.name, e)
                continue

        # Ensure exercise sloppaks are reachable by the core highway WS.
        # We copy each sloppak from <builtin>/<pack>/sloppaks/ into
        # <dlc_dir>/tutorials-builtin/<pack>/ so playSong can resolve them.
        if dlc_dir is None:
            continue
        src_sloppaks = src / "sloppaks"
        if not src_sloppaks.is_dir():
            continue
        dlc_dst = dlc_dir / "tutorials-builtin" / src.name
        try:
            dlc_dst.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            _state["log"].warning(
                "cannot create DLC subdir for builtin pack %s: %s", src.name, e
            )
            continue
        for sloppak in src_sloppaks.iterdir():
            if not sloppak.is_file():
                continue
            target = dlc_dst / sloppak.name
            if target.exists():
                continue
            try:
                shutil.copy2(sloppak, target)
                _state["log"].info(
                    "installed builtin sloppak to library: %s/%s", src.name, sloppak.name
                )
            except OSError as e:
                _state["log"].warning(
                    "failed to install builtin sloppak %s/%s: %s",
                    src.name, sloppak.name, e,
                )


# ── FastAPI wiring ────────────────────────────────────────────────────────────

def setup(app: FastAPI, context: dict) -> None:
    config_dir = Path(context["config_dir"])
    base = config_dir / PLUGIN_ID
    packs_dir = base / "packs"
    packs_dir.mkdir(parents=True, exist_ok=True)

    _state["config_dir"]    = config_dir
    _state["packs_dir"]     = packs_dir
    _state["progress_path"] = base / "progress.json"
    _state["log"]           = context.get("log") or _state["log"]

    log = _state["log"]
    log.info("tutorials backend ready: packs=%s progress=%s",
             packs_dir, _state["progress_path"])

    # Seed builtin packs on first run.  Pass dlc_dir so the seeder can also
    # install exercise sloppaks into the library where playSong can find them.
    plugin_dir = Path(__file__).resolve().parent
    get_dlc_dir = context.get("get_dlc_dir")
    dlc_raw = get_dlc_dir() if callable(get_dlc_dir) else None
    dlc_seed_dir = Path(dlc_raw) if dlc_raw else None
    _seed_builtin_packs(plugin_dir, dlc_seed_dir)

    # ── Pack CRUD ────────────────────────────────────────────────────────

    @app.get(f"/api/plugins/{PLUGIN_ID}/packs")
    def list_packs():
        items = []
        for child in sorted(packs_dir.iterdir()) if packs_dir.exists() else []:
            if not child.is_dir():
                continue
            if not ID_RE.match(child.name):
                continue
            mpath = child / "pack.json"
            if not mpath.is_file():
                continue
            try:
                data = json.loads(mpath.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                log.warning("skipping pack %s: %s", child.name, e)
                continue
            if isinstance(data, dict):
                items.append(_summarise(child.name, data))
        return {"packs": items}

    @app.get(f"/api/plugins/{PLUGIN_ID}/packs/{{pack_id}}")
    def get_pack(pack_id: str):
        return _enrich_manifest(pack_id, _read_manifest(pack_id))

    @app.post(f"/api/plugins/{PLUGIN_ID}/packs")
    def create_pack(body: PackCreate):
        _validate_id(body.id, "pack")
        pdir = packs_dir / body.id
        if pdir.exists():
            raise HTTPException(409, f"Pack already exists: {body.id}")
        with _lock:
            pdir.mkdir(parents=True, exist_ok=False)
            manifest = {
                "schema":     1,
                "id":         body.id,
                "title":      body.title,
                "author":     body.author,
                "techniques": [],
                "lessons":    [],
                "created_at": int(time.time()),
            }
            _atomic_write_json(pdir / "pack.json", manifest)
        return manifest

    @app.put(f"/api/plugins/{PLUGIN_ID}/packs/{{pack_id}}")
    def update_pack(pack_id: str, manifest: dict):
        _validate_id(pack_id, "pack")
        _validate_manifest(manifest, pack_id)
        pdir = _pack_dir(pack_id)
        if not pdir.is_dir():
            raise HTTPException(404, f"Pack not found: {pack_id}")
        # Cap manifest size before writing.
        serialised = json.dumps(manifest, indent=2)
        if len(serialised.encode("utf-8")) > MAX_MANIFEST_BYTES:
            raise HTTPException(413, "Manifest exceeds size cap")
        with _lock:
            _atomic_write_json(pdir / "pack.json", manifest)
        return {"ok": True}

    @app.delete(f"/api/plugins/{PLUGIN_ID}/packs/{{pack_id}}")
    def delete_pack(pack_id: str):
        pdir = _pack_dir(pack_id)
        if not pdir.is_dir():
            raise HTTPException(404, f"Pack not found: {pack_id}")
        # Defense-in-depth: confirm the resolved path stays inside packs_dir
        # so a future regex regression or a symlink trick cannot widen the
        # blast radius beyond the packs subtree.
        try:
            resolved = pdir.resolve()
            resolved.relative_to(packs_dir.resolve())
        except (OSError, ValueError):
            raise HTTPException(400, "Invalid pack path")
        with _lock:
            shutil.rmtree(resolved)
        return {"ok": True}

    # ── Video upload / serve ─────────────────────────────────────────────

    @app.post(f"/api/plugins/{PLUGIN_ID}/packs/{{pack_id}}/videos")
    async def upload_video(pack_id: str, lesson_id: str, file: UploadFile = File(...)):
        _validate_id(pack_id, "pack")
        _validate_id(lesson_id, "lesson")
        pdir = packs_dir / pack_id
        if not pdir.is_dir():
            raise HTTPException(404, f"Pack not found: {pack_id}")
        videos_dir = pdir / "videos"
        videos_dir.mkdir(parents=True, exist_ok=True)

        ext = (Path(file.filename or "").suffix.lstrip(".") or "").lower()
        if ext not in ALLOWED_VIDEO_EXTS:
            raise HTTPException(400, "Filename must end in .mp4 or .webm")
        if (file.content_type
                and file.content_type != "application/octet-stream"
                and file.content_type not in ALLOWED_VIDEO_MIMES):
            raise HTTPException(400, "Only MP4 and WebM are allowed")

        out_name = f"{lesson_id}.{ext}"
        out_path = videos_dir / out_name
        fd, tmp_name = await run_in_threadpool(
            tempfile.mkstemp, dir=str(videos_dir), prefix="upload-", suffix=".part",
        )
        tmp_path = Path(tmp_name)
        bytes_read = 0
        try:
            try:
                tmpf = await run_in_threadpool(os.fdopen, fd, "wb")
            except BaseException:
                try:
                    await run_in_threadpool(os.close, fd)
                except OSError:
                    pass
                raise
            try:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    bytes_read += len(chunk)
                    if bytes_read > MAX_VIDEO_BYTES:
                        raise HTTPException(
                            413,
                            f"Video exceeds {MAX_VIDEO_BYTES // (1024 * 1024)} MB cap",
                        )
                    await run_in_threadpool(tmpf.write, chunk)
            finally:
                await run_in_threadpool(tmpf.close)

            if bytes_read == 0:
                raise HTTPException(400, "Empty upload — file is 0 bytes")

            await run_in_threadpool(os.replace, str(tmp_path), str(out_path))

            # Clean up the alternate-extension slot so we never have stale
            # `<lesson>.mp4` and `<lesson>.webm` side by side after a swap.
            other = "webm" if ext == "mp4" else "mp4"
            stale = videos_dir / f"{lesson_id}.{other}"
            if stale.exists():
                try:
                    await run_in_threadpool(stale.unlink)
                except OSError:
                    pass
        except BaseException:
            try:
                await run_in_threadpool(tmp_path.unlink)
            except OSError:
                pass
            raise
        finally:
            try:
                await file.close()
            except Exception:
                pass

        return {
            "url":      f"/api/plugins/{PLUGIN_ID}/packs/{pack_id}/videos/{out_name}",
            "filename": out_name,
            "size":     bytes_read,
        }

    @app.get(f"/api/plugins/{PLUGIN_ID}/packs/{{pack_id}}/videos/{{filename}}")
    def get_video(pack_id: str, filename: str):
        _validate_id(pack_id, "pack")
        # Filename is `<lesson_id>.<ext>` — enforce both pieces structurally.
        m = re.match(r"^([a-z0-9][a-z0-9_-]{0,63})\.(mp4|webm)$", filename)
        if not m:
            raise HTTPException(404, "Not found")
        pdir = _pack_dir(pack_id)
        path = pdir / "videos" / filename
        try:
            resolved = path.resolve()
            resolved.relative_to((pdir / "videos").resolve())
        except (OSError, ValueError):
            raise HTTPException(404, "Not found")
        if not resolved.is_file():
            raise HTTPException(404, "Not found")
        media = "video/mp4" if filename.endswith(".mp4") else "video/webm"
        return FileResponse(
            resolved,
            media_type=media,
            headers={
                "Cache-Control":         "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )

    # ── Cover image upload / serve ───────────────────────────────────────

    @app.post(f"/api/plugins/{PLUGIN_ID}/packs/{{pack_id}}/cover")
    async def upload_cover(pack_id: str, file: UploadFile = File(...)):
        _validate_id(pack_id, "pack")
        pdir = packs_dir / pack_id
        if not pdir.is_dir():
            raise HTTPException(404, f"Pack not found: {pack_id}")

        ext = (Path(file.filename or "").suffix.lstrip(".") or "").lower()
        if ext == "jpeg":
            ext = "jpg"
        if ext not in ALLOWED_COVER_EXTS:
            raise HTTPException(400, "Cover must be PNG, JPEG, or WebP")
        if (file.content_type
                and file.content_type != "application/octet-stream"
                and file.content_type not in ALLOWED_COVER_MIMES):
            raise HTTPException(400, "Cover MIME must be image/png, image/jpeg, or image/webp")

        out_name = f"cover.{ext}"
        out_path = pdir / out_name
        fd, tmp_name = await run_in_threadpool(
            tempfile.mkstemp, dir=str(pdir), prefix="cover-", suffix=".part",
        )
        tmp_path = Path(tmp_name)
        bytes_read = 0
        try:
            try:
                tmpf = await run_in_threadpool(os.fdopen, fd, "wb")
            except BaseException:
                try:
                    await run_in_threadpool(os.close, fd)
                except OSError:
                    pass
                raise
            try:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    bytes_read += len(chunk)
                    if bytes_read > MAX_COVER_BYTES:
                        raise HTTPException(
                            413,
                            f"Cover exceeds {MAX_COVER_BYTES // (1024 * 1024)} MB cap",
                        )
                    await run_in_threadpool(tmpf.write, chunk)
            finally:
                await run_in_threadpool(tmpf.close)

            if bytes_read == 0:
                raise HTTPException(400, "Empty upload — file is 0 bytes")

            await run_in_threadpool(os.replace, str(tmp_path), str(out_path))

            # Single-slot cleanup: remove any stale alt-extension covers so we
            # don't end up with cover.png AND cover.webp side by side.
            for stale in pdir.glob("cover.*"):
                if stale != out_path:
                    try:
                        await run_in_threadpool(stale.unlink)
                    except OSError:
                        pass
        except BaseException:
            try:
                await run_in_threadpool(tmp_path.unlink)
            except OSError:
                pass
            raise
        finally:
            try:
                await file.close()
            except Exception:
                pass

        return {
            "url":      f"/api/plugins/{PLUGIN_ID}/packs/{pack_id}/cover",
            "filename": out_name,
            "size":     bytes_read,
        }

    @app.get(f"/api/plugins/{PLUGIN_ID}/packs/{{pack_id}}/cover")
    def get_cover(pack_id: str):
        _validate_id(pack_id, "pack")
        cover_path = _find_cover_file(pack_id)
        if cover_path is None:
            raise HTTPException(404, "No cover set")
        ext = cover_path.suffix.lstrip(".").lower()
        media = COVER_EXT_TO_MIME.get(ext, "application/octet-stream")
        return FileResponse(
            cover_path,
            media_type=media,
            headers={
                "Cache-Control":         "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.delete(f"/api/plugins/{PLUGIN_ID}/packs/{{pack_id}}/cover")
    def delete_cover(pack_id: str):
        _validate_id(pack_id, "pack")
        pdir = _pack_dir(pack_id)
        if not pdir.is_dir():
            raise HTTPException(404, f"Pack not found: {pack_id}")
        deleted = []
        for stale in pdir.glob("cover.*"):
            try:
                stale.unlink()
                deleted.append(stale.name)
            except OSError:
                pass
        return {"deleted": deleted}

    # ── Per-lesson thumbnail upload / serve ──────────────────────────────

    @app.post(f"/api/plugins/{PLUGIN_ID}/packs/{{pack_id}}/lessons/{{lesson_id}}/thumb")
    async def upload_lesson_thumb(pack_id: str, lesson_id: str, file: UploadFile = File(...)):
        _validate_id(pack_id, "pack")
        _validate_id(lesson_id, "lesson")
        pdir = packs_dir / pack_id
        if not pdir.is_dir():
            raise HTTPException(404, f"Pack not found: {pack_id}")
        thumbs_dir = pdir / "thumbs"
        thumbs_dir.mkdir(parents=True, exist_ok=True)

        ext = (Path(file.filename or "").suffix.lstrip(".") or "").lower()
        if ext == "jpeg":
            ext = "jpg"
        if ext not in ALLOWED_COVER_EXTS:
            raise HTTPException(400, "Thumb must be PNG, JPEG, or WebP")
        if (file.content_type
                and file.content_type != "application/octet-stream"
                and file.content_type not in ALLOWED_COVER_MIMES):
            raise HTTPException(400, "Thumb MIME must be image/png, image/jpeg, or image/webp")

        out_name = f"{lesson_id}.{ext}"
        out_path = thumbs_dir / out_name
        fd, tmp_name = await run_in_threadpool(
            tempfile.mkstemp, dir=str(thumbs_dir), prefix="thumb-", suffix=".part",
        )
        tmp_path = Path(tmp_name)
        bytes_read = 0
        try:
            try:
                tmpf = await run_in_threadpool(os.fdopen, fd, "wb")
            except BaseException:
                try:
                    await run_in_threadpool(os.close, fd)
                except OSError:
                    pass
                raise
            try:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    bytes_read += len(chunk)
                    if bytes_read > MAX_COVER_BYTES:
                        raise HTTPException(
                            413,
                            f"Thumb exceeds {MAX_COVER_BYTES // (1024 * 1024)} MB cap",
                        )
                    await run_in_threadpool(tmpf.write, chunk)
            finally:
                await run_in_threadpool(tmpf.close)

            if bytes_read == 0:
                raise HTTPException(400, "Empty upload — file is 0 bytes")

            await run_in_threadpool(os.replace, str(tmp_path), str(out_path))

            # Clean up stale alt-extension thumbs for this lesson_id.
            for stale in thumbs_dir.glob(f"{lesson_id}.*"):
                if stale != out_path:
                    try:
                        await run_in_threadpool(stale.unlink)
                    except OSError:
                        pass
        except BaseException:
            try:
                await run_in_threadpool(tmp_path.unlink)
            except OSError:
                pass
            raise
        finally:
            try:
                await file.close()
            except Exception:
                pass

        return {
            "url": f"/api/plugins/{PLUGIN_ID}/packs/{pack_id}/lessons/{lesson_id}/thumb",
            "filename": out_name,
            "size": bytes_read,
        }

    @app.get(f"/api/plugins/{PLUGIN_ID}/packs/{{pack_id}}/lessons/{{lesson_id}}/thumb")
    def get_lesson_thumb(pack_id: str, lesson_id: str):
        _validate_id(pack_id, "pack")
        _validate_id(lesson_id, "lesson")
        thumb_path = _find_lesson_thumb(pack_id, lesson_id)
        if thumb_path is None:
            raise HTTPException(404, "No thumb set")
        ext = thumb_path.suffix.lstrip(".").lower()
        media = COVER_EXT_TO_MIME.get(ext, "application/octet-stream")
        return FileResponse(
            thumb_path,
            media_type=media,
            headers={
                "Cache-Control":          "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.delete(f"/api/plugins/{PLUGIN_ID}/packs/{{pack_id}}/lessons/{{lesson_id}}/thumb")
    def delete_lesson_thumb(pack_id: str, lesson_id: str):
        _validate_id(pack_id, "pack")
        _validate_id(lesson_id, "lesson")
        pdir = _pack_dir(pack_id)
        if not pdir.is_dir():
            raise HTTPException(404, f"Pack not found: {pack_id}")
        thumbs_dir = pdir / "thumbs"
        deleted = []
        if thumbs_dir.is_dir():
            for stale in thumbs_dir.glob(f"{lesson_id}.*"):
                try:
                    stale.unlink()
                    deleted.append(stale.name)
                except OSError:
                    pass
        return {"deleted": deleted}

    # ── Sloppak copy / serve (read-only for the runtime) ─────────────────

    @app.get(f"/api/plugins/{PLUGIN_ID}/packs/{{pack_id}}/sloppaks/{{filename}}")
    def get_sloppak(pack_id: str, filename: str):
        # Sloppak filenames come from the library and can include unicode +
        # spaces, so we can't restrict to ID_RE here. Instead, normalise and
        # confirm the resolved path stays inside the pack's sloppaks subdir.
        if "/" in filename or "\\" in filename or filename.startswith("."):
            raise HTTPException(404, "Not found")
        pdir = _pack_dir(pack_id)
        sloppaks_dir = pdir / "sloppaks"
        path = sloppaks_dir / filename
        try:
            resolved = path.resolve()
            resolved.relative_to(sloppaks_dir.resolve())
        except (OSError, ValueError):
            raise HTTPException(404, "Not found")
        if not resolved.is_file():
            raise HTTPException(404, "Not found")
        return FileResponse(
            resolved,
            media_type="application/octet-stream",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    class SloppakCopyRequest(BaseModel):
        filename: str = Field(min_length=1, max_length=300)

    @app.post(f"/api/plugins/{PLUGIN_ID}/packs/{{pack_id}}/sloppaks")
    def copy_sloppak(pack_id: str, body: SloppakCopyRequest):
        """Copy a library sloppak into the pack. Author mode calls this when
        the user picks a sloppak from the library — keeping a copy inside the
        pack means the pack is self-contained and re-distributable."""
        pdir = _pack_dir(pack_id)
        if not pdir.is_dir():
            raise HTTPException(404, f"Pack not found: {pack_id}")
        sloppaks_dir = pdir / "sloppaks"
        sloppaks_dir.mkdir(parents=True, exist_ok=True)

        # Pull the source from the library via the get_dlc_dir helper the
        # plugin loader provides. Treat the filename as opaque and rely on
        # resolved-path checks to keep the read inside the DLC dir.
        get_dlc_dir = context.get("get_dlc_dir")
        if not callable(get_dlc_dir):
            raise HTTPException(500, "DLC directory resolver unavailable")
        dlc_raw = get_dlc_dir()
        if not dlc_raw:
            raise HTTPException(409, "No DLC directory configured")
        dlc_dir = Path(dlc_raw)
        src = (dlc_dir / body.filename)
        try:
            src_resolved = src.resolve()
            src_resolved.relative_to(dlc_dir.resolve())
        except (OSError, ValueError):
            raise HTTPException(400, "Filename escapes DLC directory")
        if not src_resolved.is_file():
            raise HTTPException(404, f"Library sloppak not found: {body.filename}")
        if src_resolved.suffix.lower() != ".sloppak":
            raise HTTPException(400, "Only .sloppak files may be copied into a pack")

        dst = sloppaks_dir / src_resolved.name
        with _lock:
            shutil.copy2(src_resolved, dst)
        encoded_name = _url_quote(src_resolved.name, safe="")
        return {
            "filename": src_resolved.name,
            "url":      f"/api/plugins/{PLUGIN_ID}/packs/{pack_id}/sloppaks/{encoded_name}",
        }

    # ── Local progress ───────────────────────────────────────────────────

    @app.post(f"/api/plugins/{PLUGIN_ID}/runs")
    def record_run(run: RunRecord):
        _validate_id(run.pack_id, "pack")
        _validate_id(run.lesson_id, "lesson")

        # Read the pack manifest so we can mark pass/mastery against the
        # lesson's declared thresholds.
        manifest = _read_manifest(run.pack_id)
        lessons = {l["id"]: l for l in manifest.get("lessons", []) if isinstance(l, dict) and l.get("id")}
        lesson = lessons.get(run.lesson_id)
        if not lesson:
            raise HTTPException(404, f"Lesson not found: {run.lesson_id}")

        # Use `or` defaults to guard against explicit JSON null values in
        # on-disk manifests edited outside the API (e.g., direct file edits
        # that bypass _validate_manifest).  `.get(key, default)` returns None
        # when the key is present but null-valued; the `or` below coerces that
        # None to the safe default so the comparisons below never raise TypeError.
        _pass = lesson.get("pass") or {}
        pass_thr = _pass.get("accuracy") if _pass.get("accuracy") is not None else 0.7
        mastery = lesson.get("mastery") or {}
        mastery_acc = mastery.get("accuracy") if mastery.get("accuracy") is not None else 0.9
        mastery_speed = mastery.get("speed") if mastery.get("speed") is not None else 1.0

        passed = run.accuracy >= pass_thr
        mastered = (run.accuracy >= mastery_acc) and (run.speed >= mastery_speed)

        with _lock:
            progress = _load_progress()
            packs = progress.setdefault("packs", {})
            if not isinstance(packs, dict):
                packs = {}
                progress["packs"] = packs
            pack_state = packs.setdefault(run.pack_id, {"lessons": {}})
            if not isinstance(pack_state, dict):
                pack_state = {"lessons": {}}
                packs[run.pack_id] = pack_state
            lesson_states = pack_state.setdefault("lessons", {})
            if not isinstance(lesson_states, dict):
                lesson_states = {}
                pack_state["lessons"] = lesson_states

            prev = lesson_states.get(run.lesson_id) or {}
            best_score = max(int(prev.get("best_score", 0)), run.score)
            best_accuracy = max(float(prev.get("best_accuracy", 0.0)), run.accuracy)
            prev_passed = bool(prev.get("passed", False))
            prev_mastered = bool(prev.get("mastered", False))

            lesson_states[run.lesson_id] = {
                "best_score":     best_score,
                "best_accuracy":  best_accuracy,
                "last_accuracy":  run.accuracy,
                "last_speed":     run.speed,
                "passed":         prev_passed or passed,
                "mastered":       prev_mastered or mastered,
                "last_run_at":    int(time.time()),
            }
            _save_progress(progress)

        return {
            "ok":       True,
            "passed":   passed,
            "mastered": mastered,
            "first_pass":     passed and not prev_passed,
            "first_mastery":  mastered and not prev_mastered,
            "thresholds": {
                "pass_accuracy":    pass_thr,
                "mastery_accuracy": mastery_acc,
                "mastery_speed":    mastery_speed,
            },
        }

    @app.get(f"/api/plugins/{PLUGIN_ID}/progress")
    def get_progress():
        return _load_progress()
