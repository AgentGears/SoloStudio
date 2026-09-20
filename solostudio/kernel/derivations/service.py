from __future__ import annotations

import hashlib
import io
import json
import struct
import wave
import zlib
from typing import Any

from solostudio.definitions import (
    caption_style_identity,
    visual_style_identity,
    voice_profile_identity,
)
from solostudio.kernel.capabilities import CapabilityRouter
from solostudio.kernel.costs import CostPlan
from solostudio.kernel.derivations.artifacts import DerivationArtifactService
from solostudio.kernel.derivations.fingerprints import expected_fingerprint
from solostudio.kernel.derivations.models import ArtifactRequirement, PlannedArtifact
from solostudio.kernel.errors import BudgetExceeded, InvalidArtifact, InvalidCommand, SoloStudioError
from solostudio.kernel.identity import canonical_hash, canonical_text
from solostudio.kernel.jobs import JobService
from solostudio.kernel.productions import ProductionService


_JOB_TYPES = {
    "speech.synthesize": "VOICE_SYNTHESIZE",
    "captions.generate": "CAPTIONS_GENERATE",
    "image.generate": "VISUAL_IMAGE_GENERATE",
}

_OUTPUTS = {
    "speech.synthesize": ("voice_audio", "audio/wav", "voice.wav"),
    "captions.generate": ("caption_track", "text/vtt; charset=utf-8", "captions.vtt"),
    "image.generate": ("visual_image", "image/png", "visual.png"),
}


class DerivationService:
    def __init__(
        self,
        productions: ProductionService,
        artifacts: DerivationArtifactService,
        jobs: JobService,
        router: CapabilityRouter,
    ) -> None:
        self.productions = productions
        self.artifacts = artifacts
        self.jobs = jobs
        self.router = router

    def plan_revision(
        self,
        revision_id: str,
        *,
        execution_mode: str = "PRIVATE",
        max_cost_microunits: int = 0,
        max_attempts: int = 2,
    ) -> list[PlannedArtifact]:
        if type(max_cost_microunits) is not int or max_cost_microunits < 0:
            raise InvalidCommand("max cost must be a non-negative integer microunit value")
        if type(max_attempts) is not int or max_attempts < 1:
            raise InvalidCommand("max_attempts must be a positive integer")

        revision = self.productions.revision(revision_id)
        payload = json.loads(str(revision["canonical_json"]))
        production_id = str(revision["production_id"])
        requirements = self._requirements(
            revision_id,
            payload,
            execution_mode=execution_mode,
        )

        planned: list[PlannedArtifact] = []
        for requirement in requirements:
            reusable = self.artifacts.find_reusable(
                production_id,
                requirement.kind,
                requirement.input_fingerprint,
            )
            if reusable is not None:
                planned.append(
                    PlannedArtifact(
                        requirement.output_role,
                        requirement.capability,
                        requirement.kind,
                        requirement.input_fingerprint,
                        "REUSED_ARTIFACT",
                        artifact_id=str(reusable["id"]),
                    )
                )
                continue

            estimate = requirement.route.get("estimated_cost_microunits")
            if type(estimate) is not int or estimate < 0:
                raise InvalidCommand("qualified route must provide a non-negative integer cost estimate")
            if estimate > max_cost_microunits:
                raise BudgetExceeded(
                    f"estimated cost {estimate} exceeds request limit {max_cost_microunits}"
                )
            spec = {
                "schema_version": 1,
                "execution_mode": execution_mode,
                "output_role": requirement.output_role,
                "kind": requirement.kind,
                "media_type": requirement.media_type,
                "filename": requirement.filename,
                "semantic_inputs": requirement.semantic_inputs,
                "source_object_digests": requirement.source_object_digests,
                "source_artifacts": [
                    {"artifact_id": artifact_id, "role": role}
                    for artifact_id, role in requirement.source_artifacts
                ],
            }
            admission = self.jobs.admit(
                production_id=production_id,
                production_revision_id=revision_id,
                job_class="ARTIFACT",
                job_type=requirement.job_type,
                semantic_capability=requirement.capability,
                spec=spec,
                route=requirement.route,
                input_fingerprint=requirement.input_fingerprint,
                max_attempts=max_attempts,
                cost_plan=CostPlan(requirement.capability, estimate, estimate),
            )
            planned.append(
                PlannedArtifact(
                    requirement.output_role,
                    requirement.capability,
                    requirement.kind,
                    requirement.input_fingerprint,
                    "REUSED_JOB" if admission.reused else "ADMITTED_JOB",
                    job_id=admission.job_id,
                    attempt_id=admission.attempt_id,
                )
            )
        return planned

    def execute_job(self, job_id: str) -> str:
        job = self.jobs.job(job_id)
        if job["job_class"] != "ARTIFACT":
            raise InvalidCommand("derivation executor requires an Artifact job")
        capability = str(job["semantic_capability"])
        if capability not in _JOB_TYPES:
            raise InvalidCommand(f"unsupported M0 derivation capability: {capability}")
        if job["state"] != "QUEUED":
            raise InvalidCommand("Artifact job must be queued before execution")

        spec = job["spec"]
        execution_mode = spec.get("execution_mode")
        if not isinstance(execution_mode, str):
            raise InvalidCommand("Artifact job is missing execution mode")
        expected_route = self.router.qualify(capability, execution_mode=execution_mode)
        if job["route"] != expected_route:
            raise InvalidCommand("persisted route does not match the qualified Artifact executor route")

        output_role = spec.get("output_role")
        semantic_inputs = spec.get("semantic_inputs")
        source_object_digests = spec.get("source_object_digests")
        if not isinstance(output_role, str) or not output_role:
            raise InvalidCommand("Artifact job is missing output role")
        if not isinstance(semantic_inputs, dict) or not isinstance(source_object_digests, dict):
            raise InvalidCommand("Artifact job fingerprint projection is incomplete")
        authoritative_fingerprint = expected_fingerprint(
            capability,
            output_role=output_role,
            semantic_inputs=semantic_inputs,
            source_object_digests=source_object_digests,
            route=expected_route,
        )
        if authoritative_fingerprint != str(job["input_fingerprint"]):
            raise InvalidCommand("Artifact job input fingerprint does not match its persisted projection")

        source_artifacts = self._validated_sources(job)
        payload = self._render(capability, job, source_artifacts)
        kind, media_type, default_filename = _OUTPUTS[capability]
        if spec.get("kind") != kind or spec.get("media_type") != media_type:
            raise InvalidCommand("Artifact job output contract does not match capability")
        filename = spec.get("filename", default_filename)
        if not isinstance(filename, str) or not filename or "/" in filename or "\\" in filename:
            raise InvalidCommand("Artifact job filename must be a simple relative name")

        attempts = self.jobs.attempts(job_id)
        if not attempts or attempts[-1]["state"] != "CREATED":
            raise InvalidCommand("queued Artifact job has no created attempt")
        attempt_id = str(attempts[-1]["id"])
        temp_dir = self.jobs.start_attempt(attempt_id, "deterministic-artifact-provider")
        try:
            (temp_dir / filename).write_bytes(payload)
            artifact_ids = self.jobs.complete_artifact_attempt(
                attempt_id,
                [
                    {
                        "role": output_role,
                        "path": filename,
                        "kind": kind,
                        "media_type": media_type,
                        "producer_stage": "deterministic_artifact_provider",
                    }
                ],
            )
        except (SoloStudioError, OSError) as exc:
            self.jobs.fail_attempt(
                attempt_id,
                getattr(exc, "code", "ARTIFACT_PROVIDER_FAILED"),
                str(exc),
            )
            raise
        if len(artifact_ids) != 1:
            raise RuntimeError("single-output M0 Artifact job registered an unexpected output count")
        return artifact_ids[0]

    def _requirements(
        self,
        revision_id: str,
        payload: dict[str, Any],
        *,
        execution_mode: str,
    ) -> list[ArtifactRequirement]:
        requirements: list[ArtifactRequirement] = []

        script = payload.get("script", {}).get("text", "")
        if isinstance(script, str) and script:
            script_bytes = script.encode("utf-8")
            script_digest = hashlib.sha256(script_bytes).hexdigest()
            script_source = self.artifacts.captured_source(
                revision_id,
                "script_text",
                expected_digest=script_digest,
            )

            voice = payload.get("voice", {})
            if not isinstance(voice, dict):
                raise InvalidCommand("captured voice preferences must be an object")
            voice_profile = voice_profile_identity(voice.get("voice_profile_ref"))
            pace = voice.get("pace")
            if not isinstance(pace, str) or not pace:
                raise InvalidCommand("captured voice pace must be a non-empty string")
            requirements.append(
                self._requirement(
                    capability="speech.synthesize",
                    output_role="voice.primary",
                    semantic_inputs={"voice_profile": voice_profile, "pace": pace},
                    source_object_digests={"script_text": script_digest},
                    source_artifacts=((str(script_source["id"]), "script_text"),),
                    execution_mode=execution_mode,
                )
            )

            captions = payload.get("captions", {})
            if not isinstance(captions, dict):
                raise InvalidCommand("captured caption preferences must be an object")
            enabled = captions.get("enabled", True)
            if type(enabled) is not bool:
                raise InvalidCommand("captured caption enabled flag must be boolean")
            if enabled:
                style = caption_style_identity(captions.get("style_preset_ref"))
                language = payload.get("brief", {}).get("primary_language")
                if not isinstance(language, str) or not language:
                    raise InvalidCommand("captured primary language must be a non-empty string")
                requirements.append(
                    self._requirement(
                        capability="captions.generate",
                        output_role="captions.primary",
                        semantic_inputs={"caption_style": style, "language": language},
                        source_object_digests={"script_text": script_digest},
                        source_artifacts=((str(script_source["id"]), "script_text"),),
                        execution_mode=execution_mode,
                    )
                )

        visual_plan = payload.get("visual_plan", [])
        if not isinstance(visual_plan, list):
            raise InvalidCommand("captured visual plan must be a list")
        if visual_plan:
            visual_plan_bytes = canonical_text(visual_plan).encode("utf-8")
            visual_plan_digest = hashlib.sha256(visual_plan_bytes).hexdigest()
            visual_source = self.artifacts.captured_source(
                revision_id,
                "visual_plan",
                expected_digest=visual_plan_digest,
            )
            visual_style = visual_style_identity()
            seen: set[str] = set()
            for item in visual_plan:
                if not isinstance(item, dict):
                    raise InvalidCommand("captured visual plan items must be objects")
                item_id = item.get("item_id")
                if not isinstance(item_id, str) or not item_id or item_id in seen:
                    raise InvalidCommand("captured visual plan item_id values must be unique non-empty strings")
                seen.add(item_id)
                requirements.append(
                    self._requirement(
                        capability="image.generate",
                        output_role=f"visual.{item_id}",
                        semantic_inputs={
                            "visual_plan_item_hash": canonical_hash(item),
                            "visual_style": visual_style,
                        },
                        source_object_digests={},
                        source_artifacts=((str(visual_source["id"]), "visual_plan"),),
                        execution_mode=execution_mode,
                    )
                )
        return requirements

    def _requirement(
        self,
        *,
        capability: str,
        output_role: str,
        semantic_inputs: dict[str, Any],
        source_object_digests: dict[str, str],
        source_artifacts: tuple[tuple[str, str], ...],
        execution_mode: str,
    ) -> ArtifactRequirement:
        route = self.router.qualify(capability, execution_mode=execution_mode)
        fingerprint = expected_fingerprint(
            capability,
            output_role=output_role,
            semantic_inputs=semantic_inputs,
            source_object_digests=source_object_digests,
            route=route,
        )
        kind, media_type, filename = _OUTPUTS[capability]
        if capability == "image.generate":
            filename = f"{output_role.replace('.', '-')}.png"
        return ArtifactRequirement(
            output_role,
            capability,
            _JOB_TYPES[capability],
            kind,
            media_type,
            filename,
            semantic_inputs,
            source_object_digests,
            source_artifacts,
            route,
            fingerprint,
        )

    def _validated_sources(self, job: dict[str, Any]) -> dict[str, dict[str, Any]]:
        capability = str(job["semantic_capability"])
        spec = job["spec"]
        sources = spec.get("source_artifacts")
        if not isinstance(sources, list) or not sources:
            raise InvalidCommand("Artifact job requires source_artifacts")
        expected_roles = {
            "speech.synthesize": {"script_text": "script_text"},
            "captions.generate": {"script_text": "script_text"},
            "image.generate": {"visual_plan": "visual_plan"},
        }[capability]
        result: dict[str, dict[str, Any]] = {}
        for source in sources:
            if not isinstance(source, dict):
                raise InvalidCommand("Artifact job source_artifacts entries must be objects")
            artifact_id = source.get("artifact_id")
            role = source.get("role")
            if not isinstance(artifact_id, str) or not isinstance(role, str) or not role:
                raise InvalidCommand("Artifact job source_artifact entry is invalid")
            if role not in expected_roles or role in result:
                raise InvalidCommand(f"Artifact job source role is invalid or duplicated: {role}")
            artifact = self.artifacts.artifact(artifact_id, verify_bytes=True)
            if str(artifact["production_id"]) != str(job["production_id"]):
                raise InvalidCommand("Artifact job source belongs to another production")
            if artifact["production_revision_id"] != job["production_revision_id"]:
                raise InvalidCommand("Artifact job source is not captured from its bound revision")
            if (
                artifact["variant_id"] is not None
                or artifact["producer_stage"] != "revision_capture"
                or artifact["producer_job_id"] is not None
                or artifact["producer_attempt_id"] is not None
            ):
                raise InvalidCommand("Artifact job source must be immutable revision-capture provenance")
            if str(artifact["kind"]) != expected_roles[role]:
                raise InvalidCommand(f"Artifact job source role {role} has the wrong Artifact kind")
            result[role] = artifact

        if set(result) != set(expected_roles):
            raise InvalidCommand("Artifact job source roles do not match the capability contract")

        source_object_digests = spec.get("source_object_digests")
        semantic_inputs = spec.get("semantic_inputs")
        if not isinstance(source_object_digests, dict) or not isinstance(semantic_inputs, dict):
            raise InvalidCommand("Artifact job fingerprint projection is incomplete")
        if capability in {"speech.synthesize", "captions.generate"}:
            expected_digest = source_object_digests.get("script_text")
            if (
                not isinstance(expected_digest, str)
                or str(result["script_text"]["object_digest"]) != expected_digest
            ):
                raise InvalidCommand("Artifact job script source does not match its fingerprint projection")
        else:
            visual_source = result["visual_plan"]
            try:
                visual_plan = json.loads(self.artifacts.read_bytes(str(visual_source["id"])).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise InvalidArtifact("visual_plan source is not valid UTF-8 JSON") from exc
            if not isinstance(visual_plan, list):
                raise InvalidArtifact("visual_plan source must decode to a list")
            output_role = spec.get("output_role")
            if not isinstance(output_role, str) or not output_role.startswith("visual."):
                raise InvalidCommand("image Artifact job output role is invalid")
            item_id = output_role.removeprefix("visual.")
            matches = [
                item for item in visual_plan
                if isinstance(item, dict) and item.get("item_id") == item_id
            ]
            if len(matches) != 1:
                raise InvalidCommand("image Artifact job source does not contain its visual-plan item")
            item_hash = semantic_inputs.get("visual_plan_item_hash")
            if not isinstance(item_hash, str) or canonical_hash(matches[0]) != item_hash:
                raise InvalidCommand("image Artifact job item source does not match its fingerprint projection")
        return result

    def _render(
        self,
        capability: str,
        job: dict[str, Any],
        sources: dict[str, dict[str, Any]],
    ) -> bytes:
        fingerprint = str(job["input_fingerprint"])
        if capability == "speech.synthesize":
            script = self._source_text(sources, "script_text")
            return _deterministic_wav(fingerprint, script)
        if capability == "captions.generate":
            script = self._source_text(sources, "script_text")
            language = str(job["spec"]["semantic_inputs"]["language"])
            style_id = str(job["spec"]["semantic_inputs"]["caption_style"]["definition_id"])
            text = " ".join(script.split())
            return (
                "WEBVTT\n\n"
                f"NOTE language={language} style={style_id}\n\n"
                "00:00.000 --> 00:05.000\n"
                f"{text}\n"
            ).encode("utf-8")
        if capability == "image.generate":
            if "visual_plan" not in sources:
                raise InvalidCommand("image generation requires visual_plan source provenance")
            return _deterministic_png(fingerprint)
        raise AssertionError(capability)

    def _source_text(self, sources: dict[str, dict[str, Any]], role: str) -> str:
        source = sources.get(role)
        if source is None:
            raise InvalidCommand(f"Artifact job is missing source role: {role}")
        try:
            return self.artifacts.read_bytes(str(source["id"])).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidArtifact(f"{role} source is not valid UTF-8") from exc


def _deterministic_wav(fingerprint: str, script: str) -> bytes:
    seed = hashlib.sha256((fingerprint + "\n" + script).encode("utf-8")).digest()
    frames = bytes(64 + (seed[i % len(seed)] % 128) for i in range(800))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(1)
        wav.setframerate(8000)
        wav.writeframes(frames)
    return buffer.getvalue()


def _deterministic_png(fingerprint: str) -> bytes:
    seed = hashlib.sha256(fingerprint.encode("utf-8")).digest()
    width = 2
    height = 2
    row = bytes([0, seed[0], seed[1], seed[2], seed[3], seed[4], seed[5]])
    raw = row * height
    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)),
            _png_chunk(b"IDAT", zlib.compress(raw)),
            _png_chunk(b"IEND", b""),
        ]
    )


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind)
    crc = zlib.crc32(data, crc) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)
