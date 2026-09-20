from __future__ import annotations

import json
from typing import Any

from solostudio.kernel.artifacts import PreparedArtifact
from solostudio.kernel.identity import canonical_text
from solostudio.kernel.productions.models import RevisionResult


class CaptureSourceRepairMixin:
    """Ensure immutable revision source provenance exists, including migrated heads."""

    def capture_revision(self, *args: Any, **kwargs: Any):
        result = super().capture_revision(*args, **kwargs)
        if isinstance(result, RevisionResult):
            self._ensure_captured_sources(result.revision_id)
        return result

    def _ensure_captured_sources(self, revision_id: str) -> None:
        revision = self.revision(revision_id)
        payload = json.loads(str(revision["canonical_json"]))
        production_id = str(revision["production_id"])
        prepared = self._prepared_sources_from_revision(payload)
        if not prepared:
            return

        with self.store.write() as db:
            for item in prepared:
                expected_digest = item.object_record.digest_sha256
                existing = db.execute(
                    """
                    SELECT 1
                    FROM artifacts
                    WHERE production_revision_id = ?
                      AND variant_id IS NULL
                      AND kind = ?
                      AND media_type = ?
                      AND object_digest = ?
                      AND producer_stage = 'revision_capture'
                      AND producer_job_id IS NULL
                      AND producer_attempt_id IS NULL
                    LIMIT 1
                    """,
                    (revision_id, item.kind, item.media_type, expected_digest),
                ).fetchone()
                if existing:
                    continue
                artifact_id = self.artifacts.register_prepared_in_tx(
                    db,
                    item,
                    production_id=production_id,
                    production_revision_id=revision_id,
                )
                self._journal(
                    db,
                    production_id,
                    "artifact",
                    artifact_id,
                    "CAPTURE_SOURCE_BACKFILLED",
                    {
                        "revision_id": revision_id,
                        "kind": item.kind,
                        "object_digest": expected_digest,
                    },
                )

    def _prepared_sources_from_revision(self, payload: dict[str, Any]) -> list[PreparedArtifact]:
        prepared: list[PreparedArtifact] = []
        script = payload.get("script", {}).get("text", "")
        if script:
            prepared.append(
                self.artifacts.prepare_bytes(
                    script.encode("utf-8"),
                    kind="script_text",
                    media_type="text/plain; charset=utf-8",
                    producer_stage="revision_capture",
                    metadata={"source": "production_revision.script.text"},
                )
            )
        visual_plan = payload.get("visual_plan", [])
        if visual_plan:
            prepared.append(
                self.artifacts.prepare_bytes(
                    canonical_text(visual_plan).encode("utf-8"),
                    kind="visual_plan",
                    media_type="application/json",
                    producer_stage="revision_capture",
                    metadata={"source": "production_revision.visual_plan"},
                )
            )
        return prepared
