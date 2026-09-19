from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from solostudio.kernel.artifacts.objects import ObjectRecord, ObjectStore
from solostudio.kernel.clock import Clock
from solostudio.kernel.errors import ArtifactDependencyCycle, InvalidArtifact, NotFound
from solostudio.kernel.identity import canonical_text
from solostudio.kernel.ids import IdSource
from solostudio.kernel.store import KernelStore


@dataclass(frozen=True, slots=True)
class PreparedArtifact:
    object_record: ObjectRecord
    kind: str
    media_type: str
    producer_stage: str
    input_fingerprint: str | None = None
    metadata: dict[str, Any] | None = None


class ArtifactService:
    def __init__(self, store: KernelStore, objects: ObjectStore, clock: Clock, ids: IdSource) -> None:
        self.store = store
        self.objects = objects
        self.clock = clock
        self.ids = ids

    def prepare_bytes(
        self,
        payload: bytes,
        *,
        kind: str,
        media_type: str,
        producer_stage: str,
        input_fingerprint: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PreparedArtifact:
        self._validate(kind, media_type, payload)
        obj = self.objects.promote_bytes(payload)
        return PreparedArtifact(obj, kind, media_type, producer_stage, input_fingerprint, metadata or {})

    def register_prepared_in_tx(
        self,
        db: Any,
        prepared: PreparedArtifact,
        *,
        production_id: str,
        production_revision_id: str | None = None,
        variant_id: str | None = None,
        producer_job_id: str | None = None,
        producer_attempt_id: str | None = None,
        dependencies: Iterable[tuple[str, str]] = (),
    ) -> str:
        self.objects.verify(
            prepared.object_record.digest_sha256,
            prepared.object_record.byte_size,
            prepared.object_record.object_relpath,
        )
        if production_revision_id is not None:
            revision = db.execute("SELECT production_id FROM production_revisions WHERE id = ?", (production_revision_id,)).fetchone()
            if not revision:
                raise NotFound(f"revision not found: {production_revision_id}")
            if str(revision["production_id"]) != production_id:
                raise InvalidArtifact("artifact revision must belong to the same production")
        if producer_job_id is not None:
            job = db.execute("SELECT production_id FROM job_specs WHERE id = ?", (producer_job_id,)).fetchone()
            if not job:
                raise NotFound(f"job not found: {producer_job_id}")
            if str(job["production_id"]) != production_id:
                raise InvalidArtifact("artifact job must belong to the same production")
        if producer_attempt_id is not None:
            if producer_job_id is None:
                raise InvalidArtifact("producer attempt requires producer job")
            attempt = db.execute("SELECT job_id FROM attempts WHERE id = ?", (producer_attempt_id,)).fetchone()
            if not attempt:
                raise NotFound(f"attempt not found: {producer_attempt_id}")
            if str(attempt["job_id"]) != producer_job_id:
                raise InvalidArtifact("artifact attempt must belong to producer job")
        now = self.clock.now()
        db.execute(
            "INSERT OR IGNORE INTO objects(digest_sha256,byte_size,object_relpath,created_at) VALUES (?,?,?,?)",
            (
                prepared.object_record.digest_sha256,
                prepared.object_record.byte_size,
                prepared.object_record.object_relpath,
                now,
            ),
        )
        object_row = db.execute(
            "SELECT byte_size,object_relpath FROM objects WHERE digest_sha256 = ?",
            (prepared.object_record.digest_sha256,),
        ).fetchone()
        if not object_row or int(object_row["byte_size"]) != prepared.object_record.byte_size or object_row["object_relpath"] != prepared.object_record.object_relpath:
            raise InvalidArtifact("object registration metadata does not match retained bytes")

        artifact_id = self.ids.new("art")
        db.execute(
            """
            INSERT INTO artifacts(
                id,object_digest,production_id,production_revision_id,variant_id,kind,media_type,producer_stage,
                producer_job_id,producer_attempt_id,input_fingerprint,metadata_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                artifact_id,
                prepared.object_record.digest_sha256,
                production_id,
                production_revision_id,
                variant_id,
                prepared.kind,
                prepared.media_type,
                prepared.producer_stage,
                producer_job_id,
                producer_attempt_id,
                prepared.input_fingerprint,
                canonical_text(prepared.metadata or {}),
                now,
            ),
        )
        for source_artifact_id, role in dependencies:
            self._add_dependency_in_tx(db, artifact_id, source_artifact_id, role)
        return artifact_id

    def promote_and_register(
        self,
        payload: bytes,
        *,
        production_id: str,
        kind: str,
        media_type: str,
        producer_stage: str,
        production_revision_id: str | None = None,
        input_fingerprint: str | None = None,
        metadata: dict[str, Any] | None = None,
        dependencies: Iterable[tuple[str, str]] = (),
    ) -> str:
        prepared = self.prepare_bytes(
            payload,
            kind=kind,
            media_type=media_type,
            producer_stage=producer_stage,
            input_fingerprint=input_fingerprint,
            metadata=metadata,
        )
        with self.store.write() as db:
            artifact_id = self.register_prepared_in_tx(
                db,
                prepared,
                production_id=production_id,
                production_revision_id=production_revision_id,
                dependencies=dependencies,
            )
            self._journal(db, production_id, artifact_id, "ARTIFACT_REGISTERED", {"kind": kind, "object_digest": prepared.object_record.digest_sha256})
            return artifact_id

    def artifact(self, artifact_id: str, *, verify_bytes: bool = False) -> dict[str, Any]:
        with self.store.read() as db:
            row = db.execute(
                """
                SELECT a.*,o.byte_size,o.object_relpath
                FROM artifacts a JOIN objects o ON o.digest_sha256 = a.object_digest
                WHERE a.id = ?
                """,
                (artifact_id,),
            ).fetchone()
            if not row:
                raise NotFound(f"artifact not found: {artifact_id}")
            result = dict(row)
        if verify_bytes:
            self.objects.verify(str(result["object_digest"]), int(result["byte_size"]), str(result["object_relpath"]))
        result["metadata"] = json.loads(result.pop("metadata_json"))
        return result

    def revision_artifacts(self, revision_id: str, *, verify_bytes: bool = False) -> list[dict[str, Any]]:
        with self.store.read() as db:
            ids = [str(row["id"]) for row in db.execute("SELECT id FROM artifacts WHERE production_revision_id = ? ORDER BY id", (revision_id,))]
        return [self.artifact(artifact_id, verify_bytes=verify_bytes) for artifact_id in ids]

    def read_bytes(self, artifact_id: str) -> bytes:
        artifact = self.artifact(artifact_id)
        return self.objects.read_bytes(str(artifact["object_digest"]), int(artifact["byte_size"]), str(artifact["object_relpath"]))

    def is_current_for(self, artifact_id: str, expected_fingerprint: str | None) -> bool:
        artifact = self.artifact(artifact_id, verify_bytes=True)
        return artifact["input_fingerprint"] == expected_fingerprint

    def add_dependency(self, artifact_id: str, source_artifact_id: str, role: str) -> None:
        with self.store.write() as db:
            self._add_dependency_in_tx(db, artifact_id, source_artifact_id, role)
            artifact = db.execute("SELECT production_id FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
            if not artifact:
                raise NotFound(f"artifact not found: {artifact_id}")
            self._journal(db, str(artifact["production_id"]), artifact_id, "ARTIFACT_DEPENDENCY_ADDED", {"source_artifact_id": source_artifact_id, "role": role})

    def _add_dependency_in_tx(self, db: Any, artifact_id: str, source_artifact_id: str, role: str) -> None:
        if artifact_id == source_artifact_id:
            raise ArtifactDependencyCycle("artifact cannot depend on itself")
        rows = {}
        for candidate in (artifact_id, source_artifact_id):
            row = db.execute("SELECT production_id FROM artifacts WHERE id = ?", (candidate,)).fetchone()
            if not row:
                raise NotFound(f"artifact not found: {candidate}")
            rows[candidate] = str(row["production_id"])
        if rows[artifact_id] != rows[source_artifact_id]:
            raise InvalidArtifact("artifact dependencies cannot cross production provenance")
        cycle = db.execute(
            """
            WITH RECURSIVE deps(id) AS (
                SELECT source_artifact_id FROM artifact_dependencies WHERE artifact_id = ?
                UNION
                SELECT ad.source_artifact_id
                FROM artifact_dependencies ad JOIN deps d ON ad.artifact_id = d.id
            )
            SELECT 1 FROM deps WHERE id = ? LIMIT 1
            """,
            (source_artifact_id, artifact_id),
        ).fetchone()
        if cycle:
            raise ArtifactDependencyCycle(f"dependency would create a cycle: {artifact_id} -> {source_artifact_id}")
        db.execute(
            "INSERT OR IGNORE INTO artifact_dependencies(artifact_id,source_artifact_id,dependency_role) VALUES (?,?,?)",
            (artifact_id, source_artifact_id, role),
        )

    @staticmethod
    def _validate(kind: str, media_type: str, payload: bytes) -> None:
        if kind == "script_text":
            if media_type != "text/plain; charset=utf-8":
                raise InvalidArtifact("script_text requires UTF-8 text media type")
            try:
                payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise InvalidArtifact("script_text bytes are not valid UTF-8") from exc
            return
        if kind == "visual_plan":
            if media_type != "application/json":
                raise InvalidArtifact("visual_plan requires JSON media type")
            try:
                value = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise InvalidArtifact("visual_plan is not valid UTF-8 JSON") from exc
            if not isinstance(value, list):
                raise InvalidArtifact("visual_plan must be a JSON array")
            if canonical_text(value).encode("utf-8") != payload:
                raise InvalidArtifact("visual_plan bytes must use canonical JSON")

    def _journal(self, db: Any, production_id: str | None, artifact_id: str, event_type: str, event: dict[str, Any]) -> None:
        db.execute(
            "INSERT INTO journal_entries(production_id,entity_type,entity_id,event_type,event_json,created_at) VALUES (?,?,?,?,?,?)",
            (production_id, "artifact", artifact_id, event_type, canonical_text(event), self.clock.now()),
        )
