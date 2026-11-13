"""报告制品持久化的验收测试：本地 SQLite + 持久卷下的迁移、重启、领取与投递。

运行：cd backend && .venv/bin/python -m pytest tests -q
每个用例用独立临时数据目录，不碰真实部署数据。
"""
from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    """把数据目录指到临时目录，并按依赖顺序重建配置与持久层模块。"""
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    from app import config

    importlib.reload(config)
    from app import db

    importlib.reload(db)
    from app.services import artifacts

    importlib.reload(artifacts)
    from app import main

    importlib.reload(main)
    return main.app, db, artifacts, tmp_path


def _client(app) -> TestClient:
    return TestClient(app)


def _make_record(
    *,
    template_id: int,
    template_code: str,
    artifact_id: str,
    fmt: str = "json",
    state: str = "staged",
    sha256: str,
    size: int,
    final_relpath: str,
    temp_relpath: str | None,
    idempotency_key: str,
    fingerprint: str = "test-fingerprint",
) -> dict:
    now = "2026-01-01T00:00:00+00:00"
    return {
        "artifact_id": artifact_id,
        "template_id": template_id,
        "template_code": template_code,
        "idempotency_key": idempotency_key,
        "request_fingerprint": fingerprint,
        "state": state,
        "format": fmt,
        "content_type": "application/json; charset=utf-8",
        "filename": f"artifact-{artifact_id}.json",
        "sha256": sha256,
        "size_bytes": size,
        "final_relpath": final_relpath,
        "temp_relpath": temp_relpath,
        "error": None,
        "created_at": now,
        "staged_at": now if state in ("staged", "ready") else None,
        "ready_at": now if state == "ready" else None,
        "confirmed_at": now,
        "verified_at": now if state == "ready" else None,
        "updated_at": now,
    }


def test_seed_templates_migrate_without_fake_history(app_env):
    app, db, artifacts, tmp_path = app_env
    with _client(app):
        templates, total = db.list_templates(size=100)
        assert total == 3
        assert [item["id"] for item in templates] == [1, 2, 3]
        assert [item["模板编号"] for item in templates] == ["REPO-0001", "REPO-0002", "REPO-0003"]
        assert db.list_artifact_rows()[1] == 0
        state = db.seed_migration_state()
        assert state["artifact_history"] == "none"
        assert state["template_count"] == 3


def test_existing_template_api_stays_compatible(app_env):
    app, _db, _artifacts, _tmp_path = app_env
    with _client(app) as client:
        payload = client.get("/api/reportgen").json()
        assert payload["total"] == 3 and payload["page"] == 1
        assert {"items", "total", "page", "size"} == set(payload)
        assert client.get("/api/reportgen/export").json()["total"] == 3
        assert client.get("/api/reportgen/1").status_code == 200
        created = client.post(
            "/api/reportgen",
            json={"values": {"模板编号": "REPO-9001", "模板名称": "新模板", "包含图表": "图表A"}},
        ).json()
        assert created["ok"] is True and created["entry"]["id"] == 4
        duplicate = client.post(
            "/api/reportgen",
            json={"values": {"模板编号": "REPO-9001", "模板名称": "重复", "包含图表": "图表A"}},
        ).json()
        assert duplicate["ok"] is False and "已存在" in duplicate["message"]
        action = client.post("/api/reportgen/4/actions", json={"values": {"action": "提交审核"}})
        assert action.json()["entry"]["status"] == "待审核"


def test_generate_download_claim_and_idempotency(app_env):
    app, db, artifacts, _tmp_path = app_env
    with _client(app) as client:
        first = client.post("/api/reportgen/1/artifacts", json={"idempotency_key": "confirm-1"})
        assert first.status_code == 202
        artifact = first.json()
        assert artifact["state"] == "ready" and artifact["available"] is True
        assert "REPO-0001" not in artifact["download_url"]
        assert artifact["download_url"].endswith(f"/artifacts/{artifact['artifact_id']}/download")

        second = client.post("/api/reportgen/1/artifacts", json={"idempotency_key": "confirm-1"})
        assert second.status_code == 202
        replay = second.json()
        assert replay["artifact_id"] == artifact["artifact_id"]
        assert replay["replayed"] is True
        assert db.list_artifact_rows(template_id=1)[1] == 1

        download = client.get(artifact["download_url"])
        assert download.status_code == 200
        digest = hashlib.sha256(download.content).hexdigest()
        assert digest == artifact["sha256"]

        claim = client.post(artifact["claim_url"], json={"claimer": "值班管理员"})
        assert claim.status_code == 200
        assert claim.json()["claim_id"] == 1
        again = client.post(
            artifact["claim_url"],
            json={"claimer": "值班管理员", "idempotency_key": "claim-1"},
        )
        assert again.json()["claim_id"] == 2
        repeated = client.post(
            artifact["claim_url"],
            json={"claimer": "值班管理员", "idempotency_key": "claim-1"},
        )
        assert repeated.json()["claim_id"] == 2

        mismatch = client.post(
            "/api/reportgen/1/artifacts",
            json={"idempotency_key": "confirm-1", "format": "html"},
        )
        assert mismatch.status_code == 409


def test_ready_artifact_survives_restart_and_can_be_claimed(app_env):
    app, db, artifacts, _tmp_path = app_env
    with _client(app) as client:
        artifact = client.post(
            "/api/reportgen/2/artifacts", json={"idempotency_key": "restart-key"}
        ).json()
    # 新的应用上下文 = 新的启动生命周期，模拟部署重启。
    with _client(app) as client:
        listed = client.get("/api/reportgen/artifacts").json()
        assert listed["total"] == 1
        assert listed["items"][0]["artifact_id"] == artifact["artifact_id"]
        assert listed["items"][0]["available"] is True
        assert client.get(artifact["download_url"]).status_code == 200
        claim = client.post(artifact["claim_url"], json={"claimer": "换班"})
        assert claim.status_code == 200
        assert claim.json()["artifact_id"] == artifact["artifact_id"]


def test_crash_between_temp_write_and_metadata_write_recovers(app_env):
    app, db, artifacts, tmp_path = app_env
    from app.config import settings

    artifacts._ensure_dirs()
    content = b'{"ok": true}\n'
    sha = hashlib.sha256(content).hexdigest()
    artifact_id = "a" * 32
    final_relpath = f"blobs/{artifact_id}/{sha}.json"
    temp_relpath = f"tmp/{artifact_id}.part"
    (tmp_path / "artifacts" / temp_relpath).write_bytes(content)
    db.insert_artifact_staged(
        _make_record(
            template_id=1,
            template_code="REPO-0001",
            artifact_id=artifact_id,
            sha256=sha,
            size=len(content),
            final_relpath=final_relpath,
            temp_relpath=temp_relpath,
            idempotency_key="crash-1",
        )
    )
    # 元数据仍是 staged、临时文件完整：重启应补完提交并变成 ready。
    with _client(app) as client:
        listed = client.get("/api/reportgen/artifacts").json()["items"][0]
        assert listed["state"] == "ready" and listed["available"] is True
        assert (tmp_path / "artifacts" / final_relpath).exists()
        assert not (tmp_path / "artifacts" / temp_relpath).exists()
        assert client.get(f"/api/reportgen/artifacts/{artifact_id}/download").status_code == 200


@pytest.mark.parametrize("state", ["pending", "staged"])
def test_crash_with_corrupt_temp_rolls_back(app_env, state):
    app, db, artifacts, tmp_path = app_env
    artifacts._ensure_dirs()
    content = b'{"ok": true}\n'
    sha = hashlib.sha256(content).hexdigest()
    artifact_id = "b" * 32
    final_relpath = f"blobs/{artifact_id}/{sha}.json"
    temp_relpath = f"tmp/{artifact_id}.part"
    (tmp_path / "artifacts" / temp_relpath).write_bytes(b"broken-bytes")
    db.insert_artifact_staged(
        _make_record(
            template_id=1,
            template_code="REPO-0001",
            artifact_id=artifact_id,
            state=state,
            sha256=sha,
            size=len(content),
            final_relpath=final_relpath,
            temp_relpath=temp_relpath,
            idempotency_key=f"crash-{state}",
        )
    )
    with _client(app) as client:
        listed = client.get("/api/reportgen/artifacts").json()["items"][0]
        assert listed["state"] == "failed"
        assert listed["available"] is False
        assert not (tmp_path / "artifacts" / temp_relpath).exists()
        assert client.get(f"/api/reportgen/artifacts/{artifact_id}/download").status_code == 409


def test_pending_without_file_rolls_back(app_env):
    app, db, artifacts, _tmp_path = app_env
    artifacts._ensure_dirs()
    artifact_id = "c" * 32
    db.insert_artifact_staged(
        _make_record(
            template_id=1,
            template_code="REPO-0001",
            artifact_id=artifact_id,
            state="pending",
            sha256="0" * 64,
            size=10,
            final_relpath=f"blobs/{artifact_id}/{'0' * 64}.json",
            temp_relpath=None,
            idempotency_key="crash-pending",
        )
    )
    with _client(app) as client:
        listed = client.get("/api/reportgen/artifacts").json()["items"][0]
        assert listed["state"] == "failed"
        assert listed["available"] is False


def test_missing_file_is_never_advertised_as_deliverable(app_env):
    app, db, artifacts, tmp_path = app_env
    with _client(app) as client:
        artifact = client.post(
            "/api/reportgen/1/artifacts", json={"idempotency_key": "file-gone"}
        ).json()
    path = tmp_path / "artifacts" / "blobs" / artifact["artifact_id"] / f"{artifact['sha256']}.json"
    path.unlink()
    with _client(app) as client:
        listed = client.get("/api/reportgen/artifacts").json()["items"][0]
        assert listed["state"] == "missing"
        assert listed["available"] is False
        assert listed["download_url"] is None
        assert client.get(f"/api/reportgen/artifacts/{artifact['artifact_id']}/download").status_code == 409
        assert client.post(f"/api/reportgen/artifacts/{artifact['artifact_id']}/claim", json={}).status_code == 409
        assert client.post(
            "/api/notify/deliveries", json={"artifact_id": artifact["artifact_id"]}
        ).status_code == 409


def test_orphan_file_is_quarantined_not_published(app_env):
    app, db, artifacts, tmp_path = app_env
    with _client(app) as client:
        # 启动之后再放一个没有元数据引用的文件，验证隔离逻辑本身。
        orphan_dir = tmp_path / "artifacts" / "blobs" / "deadbeef"
        orphan_dir.mkdir(parents=True)
        orphan = orphan_dir / "orphan.json"
        orphan.write_bytes(b"not-registered")
        summary = client.post("/api/reportgen/artifacts/reconcile").json()
        assert not orphan.exists()
        quarantined = list((tmp_path / "artifacts" / "_quarantine").iterdir())
        assert any(item.suffix == ".quarantine" for item in quarantined)
        assert client.get("/api/reportgen/artifacts").json()["total"] == 0
        assert db.list_artifact_rows()[1] == 0
        assert summary["orphans_quarantined"]


def test_notify_delivery_uses_artifact_identity(app_env):
    app, db, artifacts, _tmp_path = app_env
    with _client(app) as client:
        artifact = client.post(
            "/api/reportgen/3/artifacts", json={"idempotency_key": "notify-key"}
        ).json()
        delivered = client.post(
            "/api/notify/deliveries",
            json={
                "artifact_id": artifact["artifact_id"],
                "rule_id": 1,
                "recipients": "值班组",
                "delivery_key": "deliver-1",
            },
        )
        assert delivered.status_code == 201
        record = delivered.json()
        assert record["artifact_id"] == artifact["artifact_id"]
        assert record["artifact_sha256"] == artifact["sha256"]
        assert "REPO-0003" not in record["download_url"]
        assert record["download_url"] == f"/api/reportgen/artifacts/{artifact['artifact_id']}/download"
        replay = client.post(
            "/api/notify/deliveries",
            json={"artifact_id": artifact["artifact_id"], "delivery_key": "deliver-1"},
        ).json()
        assert replay["delivery_id"] == record["delivery_id"] and replay["replayed"] is True
    with _client(app) as client:
        listed = client.get("/api/notify/deliveries").json()
        assert listed["total"] == 1
        assert listed["items"][0]["download_url"] == record["download_url"]


def test_artifact_id_path_traversal_is_rejected(app_env):
    app, _db, _artifacts, _tmp_path = app_env
    with _client(app) as client:
        assert client.get("/api/reportgen/artifacts/..%2F..%2Fetc%2Fpasswd/download").status_code == 404
        assert client.get("/api/reportgen/artifacts/not-a-uuid/download").status_code == 404


def test_migration_is_idempotent_across_restarts(app_env):
    app, db, artifacts, _tmp_path = app_env
    with _client(app) as client:
        created = client.post(
            "/api/reportgen",
            json={"values": {"模板编号": "REPO-7001", "模板名称": "迁移后新增", "包含图表": "图表B"}},
        ).json()
        assert created["ok"] is True
        assert client.get("/api/reportgen").json()["total"] == 4
    # 第二次启动不重复迁移存量模板，也不覆盖启动后新增的模板。
    with _client(app) as client:
        payload = client.get("/api/reportgen").json()
        assert payload["total"] == 4
        codes = sorted(item["模板编号"] for item in payload["items"])
        assert codes == ["REPO-0001", "REPO-0002", "REPO-0003", "REPO-7001"]
        assert db.list_artifact_rows()[1] == 0


def test_existing_migrated_template_has_no_artifact_history(app_env):
    app, db, artifacts, _tmp_path = app_env
    with _client(app) as client:
        for entry_id in (1, 2, 3):
            detail = client.get(f"/api/reportgen/{entry_id}").json()
            assert "artifact_id" not in detail
            assert "download_url" not in detail
        listed = client.get("/api/reportgen/artifacts").json()
        assert listed["total"] == 0
        # 存量模板只有真实生成并校验后才会出现可用制品。
        generated = client.post(
            "/api/reportgen/1/artifacts", json={"idempotency_key": "real-generation"}
        ).json()
        assert generated["state"] == "ready"
        assert client.get("/api/reportgen/artifacts").json()["total"] == 1
        assert db.list_artifact_rows(template_id=2)[1] == 0


def test_partial_write_recovery_after_process_kill(app_env, monkeypatch):
    """进程在“临时文件已写完、元数据还没提交”时被强杀，重启后补完提交。"""
    app, db, artifacts, tmp_path = app_env
    with _client(app):
        pass
    template = db.get_template(1)

    def hard_exit(*_args, **_kwargs):
        # 用 SystemExit 模拟进程直接退出：不是 Exception，业务回滚分支不会执行。
        raise SystemExit("simulated process death")

    monkeypatch.setattr(artifacts, "_commit_file", hard_exit)
    with pytest.raises(SystemExit):
        artifacts.generate_artifact(template=template, idempotency_key="killed-mid-submit")

    row = db.list_artifact_rows()[0][0]
    artifact_id = str(row["artifact_id"])
    assert row["state"] == "staged"
    temp_path = tmp_path / "artifacts" / "tmp" / f"{artifact_id}.part"
    assert temp_path.exists() and row["temp_relpath"] == f"tmp/{artifact_id}.part"

    # 重启恢复：staged + 完整临时文件必须补完提交，而不是留下半成品。
    monkeypatch.undo()
    summary = artifacts.reconcile_all()
    assert summary is not None and artifact_id in summary["recovered"]
    row = db.get_artifact_row(artifact_id)
    assert row["state"] == "ready"
    assert not temp_path.exists()
    with _client(app) as client:
        artifact = client.get(f"/api/reportgen/artifacts/{artifact_id}").json()
        assert artifact["available"] is True
        assert client.get(artifact["download_url"]).status_code == 200
        assert client.post(artifact["claim_url"], json={}).status_code == 200


def test_concurrent_duplicate_confirm_only_accepts_once(app_env):
    import concurrent.futures

    app, db, artifacts, _tmp_path = app_env
    with _client(app):
        pass
    template = db.get_template(1)

    def attempt(_index: int):
        try:
            payload, replayed = artifacts.generate_artifact(
                template=template, idempotency_key="race-key"
            )
            return payload["artifact_id"], replayed
        except artifacts.ArtifactError as exc:
            return ("error", exc.message)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(attempt, range(6)))
    successes = {item for item in results if item[0] != "error"}
    assert len({item[0] for item in successes}) == 1
    assert sum(1 for item in results if item[1] is False) == 1
    assert db.list_artifact_rows(template_id=1)[1] == 1


def test_orphan_temp_file_is_removed_not_published(app_env):
    app, db, artifacts, tmp_path = app_env
    with _client(app) as client:
        tmp_dir = tmp_path / "artifacts" / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        stray = tmp_dir / "ffffffffffffffffffffffffffffffff.part"
        stray.write_bytes(b"half-written")
        summary = client.post("/api/reportgen/artifacts/reconcile").json()
        assert summary is not None
        assert not stray.exists()
        assert summary["temp_removed"]
        assert client.get("/api/reportgen/artifacts").json()["total"] == 0
        assert (tmp_path / "artifacts" / "_quarantine").exists()


def test_storage_endpoint_reports_state(app_env):
    app, db, artifacts, _tmp_path = app_env
    with _client(app) as client:
        client.post("/api/reportgen/1/artifacts", json={"idempotency_key": "state-key"})
        payload = client.get("/api/storage").json()
        assert payload["states"]["ready"] == 1
        assert "artifact_root" in payload and "sqlite_path" in payload
