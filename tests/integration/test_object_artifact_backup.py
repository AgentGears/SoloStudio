from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from solostudio.app.bootstrap import bootstrap
from solostudio.kernel.backup import restore_backup, verify_backup_bundle
from solostudio.kernel.clock import FixedClock
from solostudio.kernel.errors import (
    ArtifactDependencyCycle,
    ArtifactDigestMismatch,
    BackupClosureBroken,
    InvalidArtifact,
)
from solostudio.kernel.ids import SequenceIdSource
from solostudio.kernel.migrations import MIGRATIONS
from solostudio.kernel.store import _statements


class ObjectArtifactBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.kernel = bootstrap(self.root, clock=FixedClock(), ids=SequenceIdSource())
        self.project_id = self.kernel.productions.create_project("M0", "project")
        self.production_id = self.kernel.productions.create_production(
            self.project_id,
            "Explainer",
            "production",
        )

    def tearDown(self) -> None:
        try:
            self.kernel.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def command(self, version: int, key: str, action: str, data: dict):
        return self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=version,
            idempotency_key=key,
            action=action,
            command_input=data,
        )

    def capture(self, version: int, key: str):
        return self.kernel.user.capture_revision(
            production_id=self.production_id,
            expected_state_version=version,
            idempotency_key=key,
        )

    def test_capture_materializes_source_artifacts(self) -> None:
        self.command(0, "script", "set_script", {"text": "hello"})
        self.command(
            1,
            "visual-plan",
            "set_visual_plan",
            {"items": [{"item_id": "i1", "prompt": "diagram"}]},
        )
        revision = self.capture(2, "capture")

        artifacts = self.kernel.artifacts.revision_artifacts(
            revision.revision_id,
            verify_bytes=True,
        )
        self.assertEqual({artifact["kind"] for artifact in artifacts}, {"script_text", "visual_plan"})
        by_kind = {artifact["kind"]: artifact for artifact in artifacts}
        self.assertEqual(self.kernel.artifacts.read_bytes(by_kind["script_text"]["id"]), b"hello")
        self.assertEqual(
            json.loads(self.kernel.artifacts.read_bytes(by_kind["visual_plan"]["id"])),
            [{"item_id": "i1", "prompt": "diagram"}],
        )

    def test_same_bytes_share_object_but_not_artifact_provenance(self) -> None:
        self.command(0, "script-1", "set_script", {"text": "same"})
        first_revision = self.capture(1, "capture-1")

        other_production = self.kernel.productions.create_production(
            self.project_id,
            "Other",
            "production-2",
        )
        self.kernel.user.command(
            production_id=other_production,
            expected_state_version=0,
            idempotency_key="script-2",
            action="set_script",
            command_input={"text": "same"},
        )
        second_revision = self.kernel.user.capture_revision(
            production_id=other_production,
            expected_state_version=1,
            idempotency_key="capture-2",
        )

        first = self.kernel.artifacts.revision_artifacts(first_revision.revision_id)[0]
        second = self.kernel.artifacts.revision_artifacts(second_revision.revision_id)[0]
        self.assertEqual(first["object_digest"], second["object_digest"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first["production_id"], second["production_id"])
        with self.kernel.store.read() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM objects").fetchone()[0], 1)

    def test_orphan_object_is_not_artifact_and_backup_ignores_it(self) -> None:
        orphan = self.kernel.artifacts.prepare_bytes(
            b"orphan",
            kind="source_reference",
            media_type="application/octet-stream",
            producer_stage="test",
        )
        with self.kernel.store.read() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM objects").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0], 0)

        backup = self.kernel.backups.create_backup()
        self.assertEqual(backup.object_count, 0)
        self.assertTrue((self.root / orphan.object_record.object_relpath).exists())

    def test_capture_db_phase_rolls_back_while_prepared_object_may_remain_orphan(self) -> None:
        self.command(0, "script-atomic", "set_script", {"text": "atomic"})
        original = self.kernel.artifacts.register_prepared_in_tx

        def register_then_fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("injected registration failure")

        with patch.object(
            self.kernel.artifacts,
            "register_prepared_in_tx",
            side_effect=register_then_fail,
        ):
            with self.assertRaises(RuntimeError):
                self.capture(1, "capture-atomic")

        with self.kernel.store.read() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM production_revisions").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM objects").fetchone()[0], 0)
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM production_command_receipts WHERE idempotency_key = ?",
                    ("capture-atomic",),
                ).fetchone()[0],
                0,
            )

        object_files = [path for path in (self.root / "objects" / "sha256").rglob("*") if path.is_file()]
        self.assertEqual(len(object_files), 1)
        self.kernel.close()
        self.kernel = bootstrap(self.root, clock=FixedClock(), ids=SequenceIdSource())
        self.assertEqual(self.kernel.productions.revisions(self.production_id), [])
        self.assertEqual(self.kernel.backups.create_backup().object_count, 0)

    def test_registered_artifact_integrity_failure_is_exact(self) -> None:
        self.command(0, "script-integrity", "set_script", {"text": "verify me"})
        revision = self.capture(1, "capture-integrity")
        artifact = self.kernel.artifacts.revision_artifacts(revision.revision_id)[0]
        (self.root / artifact["object_relpath"]).write_bytes(b"tampered")
        with self.assertRaises(ArtifactDigestMismatch):
            self.kernel.artifacts.read_bytes(artifact["id"])

    def test_existing_digest_path_is_never_silently_overwritten(self) -> None:
        obj = self.kernel.objects.promote_bytes(b"original")
        (self.root / obj.object_relpath).write_bytes(b"corrupt!")
        with self.assertRaises(ArtifactDigestMismatch):
            self.kernel.objects.promote_bytes(b"original")

    def test_visual_plan_validator_rejects_noncanonical_json(self) -> None:
        with self.assertRaises(InvalidArtifact):
            self.kernel.artifacts.prepare_bytes(
                b'[ {"item_id":"x"} ]',
                kind="visual_plan",
                media_type="application/json",
                producer_stage="test",
            )

    def test_dependency_cycle_is_rejected(self) -> None:
        first = self.kernel.artifacts.promote_and_register(
            b"a",
            production_id=self.production_id,
            kind="source_reference",
            media_type="application/octet-stream",
            producer_stage="test",
        )
        second = self.kernel.artifacts.promote_and_register(
            b"b",
            production_id=self.production_id,
            kind="source_reference",
            media_type="application/octet-stream",
            producer_stage="test",
        )
        self.kernel.artifacts.add_dependency(first, second, "input")
        with self.assertRaises(ArtifactDependencyCycle):
            self.kernel.artifacts.add_dependency(second, first, "input")

    def test_dependency_cannot_cross_production_provenance(self) -> None:
        other_production = self.kernel.productions.create_production(
            self.project_id,
            "Other",
            "other-production",
        )
        first = self.kernel.artifacts.promote_and_register(
            b"a",
            production_id=self.production_id,
            kind="source_reference",
            media_type="application/octet-stream",
            producer_stage="test",
        )
        second = self.kernel.artifacts.promote_and_register(
            b"b",
            production_id=other_production,
            kind="source_reference",
            media_type="application/octet-stream",
            producer_stage="test",
        )
        with self.assertRaises(InvalidArtifact):
            self.kernel.artifacts.add_dependency(first, second, "input")

    def test_artifact_currentness_is_derived_not_stored(self) -> None:
        artifact_id = self.kernel.artifacts.promote_and_register(
            b"x",
            production_id=self.production_id,
            kind="source_reference",
            media_type="application/octet-stream",
            producer_stage="test",
            input_fingerprint="fp-1",
        )
        self.assertTrue(self.kernel.artifacts.is_current_for(artifact_id, "fp-1"))
        self.assertFalse(self.kernel.artifacts.is_current_for(artifact_id, "fp-2"))
        with self.kernel.store.read() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(artifacts)")}
        self.assertNotIn("status", columns)

    def test_backup_fails_when_referenced_object_is_corrupt(self) -> None:
        self.command(0, "script-backup-corrupt", "set_script", {"text": "hello"})
        revision = self.capture(1, "capture-backup-corrupt")
        artifact = self.kernel.artifacts.revision_artifacts(revision.revision_id)[0]
        (self.root / artifact["object_relpath"]).write_bytes(b"xxxxx")
        with self.assertRaises(BackupClosureBroken):
            self.kernel.backups.create_backup()

    def test_verified_backup_detects_missing_copied_object(self) -> None:
        self.command(0, "script-backup", "set_script", {"text": "hello"})
        self.capture(1, "capture-backup")
        backup = self.kernel.backups.create_backup()
        manifest = verify_backup_bundle(backup.path)
        (backup.path / manifest["objects"][0]["backup_relpath"]).unlink()
        with self.assertRaises(BackupClosureBroken):
            verify_backup_bundle(backup.path)

    def test_destructive_restore_reopens_history_and_bytes(self) -> None:
        self.command(0, "script-restore", "set_script", {"text": "restore me"})
        revision = self.capture(1, "capture-restore")
        original_artifact = self.kernel.artifacts.revision_artifacts(revision.revision_id)[0]
        backup = self.kernel.backups.create_backup()
        self.kernel.close()

        shutil.rmtree(self.root / "db")
        shutil.rmtree(self.root / "objects")
        shutil.rmtree(self.root / "object-tmp")
        restore_backup(backup.path, self.root)
        self.kernel = bootstrap(self.root, clock=FixedClock(), ids=SequenceIdSource())

        restored_revision = self.kernel.productions.revision(revision.revision_id)
        restored_artifact = self.kernel.artifacts.artifact(original_artifact["id"], verify_bytes=True)
        self.assertEqual(json.loads(restored_revision["canonical_json"])["script"]["text"], "restore me")
        self.assertEqual(self.kernel.artifacts.read_bytes(restored_artifact["id"]), b"restore me")
        with self.kernel.store.read() as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])


class StorageLayoutMigrationTests(unittest.TestCase):
    def test_legacy_database_location_is_relocated_and_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            legacy = root / "studio.db"
            db = sqlite3.connect(legacy)
            try:
                for statement in _statements(MIGRATIONS[0][1]):
                    db.execute(statement)
                db.execute(
                    "INSERT INTO schema_migrations(version,applied_at) VALUES (?,?)",
                    (1, "2026-09-19T20:00:00.000Z"),
                )
                db.execute(
                    "INSERT INTO projects(id,name,creation_key,created_at,updated_at) VALUES (?,?,?,?,?)",
                    ("prj_old", "Old", "key", "t", "t"),
                )
                db.commit()
            finally:
                db.close()

            kernel = bootstrap(root, clock=FixedClock(), ids=SequenceIdSource())
            try:
                self.assertFalse(legacy.exists())
                self.assertTrue((root / "db" / "studio.db").exists())
                with kernel.store.read() as connection:
                    self.assertEqual(
                        connection.execute("SELECT name FROM projects WHERE id = ?", ("prj_old",)).fetchone()[0],
                        "Old",
                    )
                    self.assertEqual(
                        connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0],
                        max(version for version, _ in MIGRATIONS),
                    )
            finally:
                kernel.close()


if __name__ == "__main__":
    unittest.main()
