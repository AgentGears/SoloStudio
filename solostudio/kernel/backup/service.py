from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solostudio.kernel.artifacts import ObjectStore
from solostudio.kernel.clock import Clock
from solostudio.kernel.errors import ArtifactDigestMismatch, BackupClosureBroken, BackupVerificationFailed, MissingRetainedArtifact
from solostudio.kernel.identity import canonical_text
from solostudio.kernel.ids import IdSource
from solostudio.kernel.migrations import MIGRATIONS
from solostudio.kernel.store import KernelStore


@dataclass(frozen=True, slots=True)
class BackupResult:
    backup_id: str
    path: Path
    object_count: int
    total_object_bytes: int


class BackupService:
    def __init__(
        self,
        data_dir: Path,
        store: KernelStore,
        objects: ObjectStore,
        clock: Clock,
        ids: IdSource,
    ) -> None:
        self.data_dir = data_dir
        self.store = store
        self.objects = objects
        self.clock = clock
        self.ids = ids
        self.backups_root = data_dir / "backups"
        self.backups_root.mkdir(parents=True, exist_ok=True)

    def create_backup(self) -> BackupResult:
        backup_id = self.ids.new("backup")
        stage = self.backups_root / f".{backup_id}.partial"
        final = self.backups_root / backup_id
        if stage.exists():
            shutil.rmtree(stage)
        if final.exists():
            raise BackupVerificationFailed(f"backup destination already exists: {backup_id}")
        (stage / "db").mkdir(parents=True)
        database_path = stage / "db" / "studio.db"
        self.store.backup_to(database_path)

        closure = _database_object_closure(database_path)
        object_entries: list[dict[str, Any]] = []
        total_bytes = 0
        try:
            for row in closure:
                digest = str(row["digest_sha256"])
                byte_size = int(row["byte_size"])
                relpath = str(row["object_relpath"])
                self.objects.verify(digest, byte_size, relpath)
                source = self.data_dir / relpath
                destination = stage / relpath
                _copy_file_durable(source, destination)
                copied_digest, copied_size = _hash_file(destination)
                if copied_digest != digest or copied_size != byte_size:
                    raise BackupClosureBroken(f"copied backup object failed verification: {digest}")
                object_entries.append(
                    {"sha256": digest, "byte_size": byte_size, "backup_relpath": relpath}
                )
                total_bytes += byte_size
        except (ArtifactDigestMismatch, MissingRetainedArtifact) as exc:
            shutil.rmtree(stage, ignore_errors=True)
            raise BackupClosureBroken("live retained object closure is broken") from exc
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise

        db_digest, db_size = _hash_file(database_path)
        manifest = {
            "schema_version": 1,
            "app_schema_version": max(version for version, _ in MIGRATIONS),
            "backup_id": backup_id,
            "created_at": self.clock.now(),
            "database": {"path": "db/studio.db", "sha256": db_digest, "byte_size": db_size},
            "object_count": len(object_entries),
            "total_object_bytes": total_bytes,
            "objects": object_entries,
            "definition_files": [],
        }
        manifest_path = stage / "manifest.json"
        _write_file_durable(manifest_path, canonical_text(manifest).encode("utf-8"))
        verify_backup_bundle(stage)
        os.replace(stage, final)
        _fsync_directory(final.parent)
        return BackupResult(backup_id, final, len(object_entries), total_bytes)

    def verify(self, backup: str | Path) -> dict[str, Any]:
        path = self.backups_root / backup if isinstance(backup, str) else Path(backup)
        return verify_backup_bundle(path)


def verify_backup_bundle(backup_dir: Path) -> dict[str, Any]:
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.is_file():
        raise BackupVerificationFailed("backup manifest is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupVerificationFailed("backup manifest is unreadable") from exc
    if manifest.get("schema_version") != 1:
        raise BackupVerificationFailed("unsupported backup manifest version")
    database = manifest.get("database")
    if not isinstance(database, dict) or database.get("path") != "db/studio.db":
        raise BackupVerificationFailed("backup database descriptor is invalid")
    database_path = backup_dir / "db" / "studio.db"
    db_digest, db_size = _hash_file_required(database_path, "database snapshot")
    if db_digest != database.get("sha256") or db_size != database.get("byte_size"):
        raise BackupClosureBroken("database snapshot digest or size does not match manifest")
    _verify_database(database_path)

    closure = _database_object_closure(database_path)
    closure_by_digest = {str(row["digest_sha256"]): row for row in closure}
    entries = manifest.get("objects")
    if not isinstance(entries, list):
        raise BackupVerificationFailed("backup object manifest is invalid")
    manifest_by_digest: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise BackupVerificationFailed("backup object entry is invalid")
        digest = entry.get("sha256")
        relpath = entry.get("backup_relpath")
        if not isinstance(digest, str) or not isinstance(relpath, str):
            raise BackupVerificationFailed("backup object identity is invalid")
        _safe_object_relpath(relpath, digest)
        if digest in manifest_by_digest:
            raise BackupVerificationFailed(f"duplicate backup object entry: {digest}")
        manifest_by_digest[digest] = entry
    if set(manifest_by_digest) != set(closure_by_digest):
        raise BackupClosureBroken("backup object set does not match database artifact closure")

    total = 0
    for digest, row in closure_by_digest.items():
        entry = manifest_by_digest[digest]
        expected_size = int(row["byte_size"])
        expected_relpath = str(row["object_relpath"])
        if entry.get("byte_size") != expected_size or entry.get("backup_relpath") != expected_relpath:
            raise BackupClosureBroken(f"backup object metadata mismatch: {digest}")
        obj_path = backup_dir / expected_relpath
        actual_digest, actual_size = _hash_file_required(obj_path, f"object {digest}")
        if actual_digest != digest or actual_size != expected_size:
            raise BackupClosureBroken(f"backup object bytes are invalid: {digest}")
        total += actual_size
    if manifest.get("object_count") != len(closure_by_digest) or manifest.get("total_object_bytes") != total:
        raise BackupClosureBroken("backup object totals do not match verified closure")
    return manifest


def restore_backup(backup_dir: Path, data_dir: Path) -> None:
    manifest = verify_backup_bundle(backup_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    db_dir = data_dir / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    active_db = db_dir / "studio.db"
    for suffix in ("", "-wal", "-shm"):
        (db_dir / f"studio.db{suffix}").unlink(missing_ok=True)

    object_root = data_dir / "objects" / "sha256"
    if object_root.exists():
        shutil.rmtree(object_root)
    object_root.mkdir(parents=True, exist_ok=True)
    (data_dir / "object-tmp").mkdir(parents=True, exist_ok=True)

    _copy_file_durable(backup_dir / "db" / "studio.db", active_db)
    for entry in manifest["objects"]:
        relpath = str(entry["backup_relpath"])
        _copy_file_durable(backup_dir / relpath, data_dir / relpath)
    _verify_database(active_db)
    for entry in manifest["objects"]:
        digest = str(entry["sha256"])
        size = int(entry["byte_size"])
        relpath = str(entry["backup_relpath"])
        actual_digest, actual_size = _hash_file_required(data_dir / relpath, f"restored object {digest}")
        if actual_digest != digest or actual_size != size:
            raise BackupClosureBroken(f"restored object failed verification: {digest}")


def _database_object_closure(database_path: Path) -> list[sqlite3.Row]:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "objects" not in tables or "artifacts" not in tables:
            return []
        return list(
            connection.execute(
                """
                SELECT DISTINCT o.digest_sha256,o.byte_size,o.object_relpath
                FROM objects o JOIN artifacts a ON a.object_digest = o.digest_sha256
                ORDER BY o.digest_sha256
                """
            )
        )
    finally:
        connection.close()


def _verify_database(database_path: Path) -> None:
    connection = sqlite3.connect(database_path)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise BackupVerificationFailed("database integrity check failed")
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise BackupVerificationFailed("database foreign-key check failed")
    finally:
        connection.close()


def _hash_file_required(path: Path, label: str) -> tuple[str, int]:
    if not path.is_file():
        raise BackupClosureBroken(f"backup closure is missing {label}")
    return _hash_file(path)


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _copy_file_durable(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + ".partial")
    with source.open("rb") as src, temp.open("wb") as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())
    os.replace(temp, destination)
    _fsync_directory(destination.parent)


def _write_file_durable(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".partial")
    with temp.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    _fsync_directory(path.parent)


def _safe_object_relpath(relpath: str, digest: str) -> None:
    path = Path(relpath)
    if path.is_absolute() or ".." in path.parts or relpath != ObjectStore.relpath_for(digest):
        raise BackupVerificationFailed(f"invalid backup object path: {relpath}")


def _fsync_directory(path: Path) -> None:
    flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
