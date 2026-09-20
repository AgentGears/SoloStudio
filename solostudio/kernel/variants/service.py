from __future__ import annotations

import json
from typing import Any

from solostudio.kernel.clock import Clock
from solostudio.kernel.errors import InvalidCommand, NotFound
from solostudio.kernel.identity import canonical_hash, canonical_text
from solostudio.kernel.ids import IdSource
from solostudio.kernel.store import KernelStore


_ALLOWED_ASPECTS = {"9:16", "1:1"}
_ALLOWED_CAPTION_MODES = {"burned", "none"}
_ALLOWED_AUDIO_MODES = {"voiceover"}
_INTENT_FIELDS = {
    "aspect_ratio",
    "language",
    "duration_min_ms",
    "duration_max_ms",
    "caption_mode",
    "audio_mode",
}


class VariantService:
    def __init__(self, store: KernelStore, clock: Clock, ids: IdSource) -> None:
        self.store = store
        self.clock = clock
        self.ids = ids

    def create(
        self,
        *,
        production_id: str,
        source_revision_id: str,
        intent: dict[str, Any],
        parent_variant_id: str | None = None,
        variant_type: str = "DELIVERY",
    ) -> str:
        normalized = self._validate_intent(intent)
        if not isinstance(variant_type, str) or not variant_type.strip():
            raise InvalidCommand("variant_type must be a non-empty string")
        variant_type = variant_type.strip()
        intent_json = canonical_text(normalized)
        intent_hash = canonical_hash(normalized)
        now = self.clock.now()

        with self.store.write() as db:
            revision = db.execute(
                "SELECT production_id FROM production_revisions WHERE id = ?",
                (source_revision_id,),
            ).fetchone()
            if not revision:
                raise NotFound(f"revision not found: {source_revision_id}")
            if str(revision["production_id"]) != production_id:
                raise InvalidCommand("variant source revision belongs to another production")

            if parent_variant_id is not None:
                parent = db.execute(
                    "SELECT production_id FROM delivery_variants WHERE id = ?",
                    (parent_variant_id,),
                ).fetchone()
                if not parent:
                    raise NotFound(f"variant not found: {parent_variant_id}")
                if str(parent["production_id"]) != production_id:
                    raise InvalidCommand("parent variant belongs to another production")

            existing = db.execute(
                """
                SELECT id,parent_variant_id,variant_type,intent_json
                FROM delivery_variants
                WHERE production_id=? AND source_revision_id=? AND intent_hash=?
                """,
                (production_id, source_revision_id, intent_hash),
            ).fetchone()
            if existing:
                if str(existing["intent_json"]) != intent_json:
                    raise RuntimeError("variant intent hash collision detected")
                existing_parent = existing["parent_variant_id"]
                if existing_parent != parent_variant_id or str(existing["variant_type"]) != variant_type:
                    raise InvalidCommand("equivalent variant already exists with different lineage metadata")
                return str(existing["id"])

            variant_id = self.ids.new("var")
            db.execute(
                """
                INSERT INTO delivery_variants(
                    id,production_id,source_revision_id,parent_variant_id,variant_type,
                    intent_json,intent_hash,state,created_at
                ) VALUES (?,?,?,?,?,?,?,'PROPOSED',?)
                """,
                (
                    variant_id,
                    production_id,
                    source_revision_id,
                    parent_variant_id,
                    variant_type,
                    intent_json,
                    intent_hash,
                    now,
                ),
            )
            self._journal(
                db,
                production_id,
                variant_id,
                "DELIVERY_VARIANT_CREATED",
                {
                    "source_revision_id": source_revision_id,
                    "parent_variant_id": parent_variant_id,
                    "intent_hash": intent_hash,
                    "variant_type": variant_type,
                },
            )
            return variant_id

    def variant(self, variant_id: str) -> dict[str, Any]:
        with self.store.read() as db:
            row = db.execute("SELECT * FROM delivery_variants WHERE id=?", (variant_id,)).fetchone()
            if not row:
                raise NotFound(f"variant not found: {variant_id}")
            result = dict(row)
        result["intent"] = json.loads(str(result["intent_json"]))
        return result

    def variants(self, production_id: str) -> list[dict[str, Any]]:
        with self.store.read() as db:
            ids = [
                str(row["id"])
                for row in db.execute(
                    "SELECT id FROM delivery_variants WHERE production_id=? ORDER BY created_at,id",
                    (production_id,),
                )
            ]
        return [self.variant(variant_id) for variant_id in ids]

    @staticmethod
    def canvas(intent: dict[str, Any]) -> dict[str, Any]:
        aspect = intent.get("aspect_ratio")
        if aspect == "9:16":
            width, height = 1080, 1920
        elif aspect == "1:1":
            width, height = 1080, 1080
        else:
            raise InvalidCommand(f"unsupported M0 aspect ratio: {aspect}")
        return {"aspect_ratio": aspect, "width": width, "height": height, "fps": 30}

    @staticmethod
    def _validate_intent(intent: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(intent, dict):
            raise InvalidCommand("variant intent must be an object")
        if set(intent) != _INTENT_FIELDS:
            missing = sorted(_INTENT_FIELDS - set(intent))
            extra = sorted(set(intent) - _INTENT_FIELDS)
            raise InvalidCommand(f"variant intent fields mismatch: missing={missing}, extra={extra}")
        aspect = intent["aspect_ratio"]
        language = intent["language"]
        duration_min = intent["duration_min_ms"]
        duration_max = intent["duration_max_ms"]
        caption_mode = intent["caption_mode"]
        audio_mode = intent["audio_mode"]
        if aspect not in _ALLOWED_ASPECTS:
            raise InvalidCommand(f"unsupported M0 aspect ratio: {aspect}")
        if not isinstance(language, str) or not language.strip():
            raise InvalidCommand("variant language must be a non-empty string")
        if type(duration_min) is not int or type(duration_max) is not int:
            raise InvalidCommand("variant duration bounds must be integer milliseconds")
        if duration_min < 1000 or duration_max < duration_min:
            raise InvalidCommand("variant duration bounds are invalid")
        if duration_max > 90_000:
            raise InvalidCommand("variant duration exceeds the M0 hard media maximum")
        if caption_mode not in _ALLOWED_CAPTION_MODES:
            raise InvalidCommand(f"unsupported M0 caption mode: {caption_mode}")
        if audio_mode not in _ALLOWED_AUDIO_MODES:
            raise InvalidCommand(f"unsupported M0 audio mode: {audio_mode}")
        return {
            "aspect_ratio": aspect,
            "language": language.strip(),
            "duration_min_ms": duration_min,
            "duration_max_ms": duration_max,
            "caption_mode": caption_mode,
            "audio_mode": audio_mode,
        }

    def _journal(
        self,
        db: Any,
        production_id: str,
        variant_id: str,
        event_type: str,
        event: dict[str, Any],
    ) -> None:
        db.execute(
            "INSERT INTO journal_entries(production_id,entity_type,entity_id,event_type,event_json,created_at) VALUES (?,?,?,?,?,?)",
            (
                production_id,
                "delivery_variant",
                variant_id,
                event_type,
                canonical_text(event),
                self.clock.now(),
            ),
        )
