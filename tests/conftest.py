import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# The plugin is a flat module directory (loaded via the host's plugin
# loader at runtime); make routes importable from tests.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import routes  # noqa: E402

# Builtin packs seeded by setup() from <plugin>/builtin/; tests filter
# these out where exact pack sets matter.
BUILTIN_PACK_IDS = sorted(
    p.name
    for p in (Path(routes.__file__).parent / "builtin").iterdir()
    if (p / "pack.json").is_file()
) if (Path(routes.__file__).parent / "builtin").is_dir() else []


@pytest.fixture
def dlc_dir(tmp_path):
    d = tmp_path / "dlc"
    d.mkdir()
    return d


@pytest.fixture
def client(tmp_path, dlc_dir):
    app = FastAPI()
    context = {
        "config_dir": str(tmp_path / "config"),
        "get_dlc_dir": lambda: str(dlc_dir),
    }
    routes.setup(app, context)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def pack(client):
    """A freshly created empty pack; returns its id."""
    r = client.post(
        "/api/plugins/tutorials/packs",
        json={"id": "testpack", "title": "Test Pack", "author": "pytest"},
    )
    assert r.status_code == 200, r.text
    return "testpack"


def make_manifest(pack_id, lessons=None):
    return {
        "schema": 1,
        "id": pack_id,
        "title": "Test Pack",
        "author": "pytest",
        "techniques": [],
        "lessons": lessons if lessons is not None else [],
    }
