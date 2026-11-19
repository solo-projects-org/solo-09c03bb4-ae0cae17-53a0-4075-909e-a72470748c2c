import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_TMP = tempfile.mkdtemp(prefix="track-matcher-tests-")
os.environ.setdefault("TRACK_MATCHER_DB", os.path.join(_TMP, "test.sqlite3"))
os.environ["TRACK_MATCHER_WORKER"] = "0"

from app import db, service  # noqa: E402


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "db.sqlite3"
    connection = db.connect(path)
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture()
def worker_client(tmp_path, monkeypatch):
    """App client whose background thread is driven manually by tests."""
    from fastapi.testclient import TestClient

    from app import main as main_module

    monkeypatch.setattr(main_module, "DB_PATH", str(tmp_path / "web.sqlite3"))
    main_module.WORKER_ENABLED = False
    with TestClient(main_module.app) as client:
        yield client, main_module


def run_job(conn):
    job = service.claim_job(conn)
    assert job is not None, "expected a pending job"
    result_id = service.execute_job(conn, job)
    return job, result_id
