from __future__ import annotations

import json

from solostudio.kernel.derivations.fingerprints import expected_fingerprint
from solostudio.kernel.errors import InvalidArtifact
from solostudio.kernel.identity import canonical_text
from tests.integration.job_test_support import JobTestCase


class Slice8ExactHeadAuthorityTests(JobTestCase):
    @staticmethod
    def _intent(*, language: str = "en") -> dict[str, object]:
        return {
            "aspect_ratio": "9:16",
            "language": language,
            "duration_min_ms": 1000,
            "duration_max_ms": 1500,
            "caption_mode": "none",
            "audio_mode": "voiceover",
        }

    def _voice_requirement(self, revision_id: str):
        revision = self.kernel.productions.revision(revision_id)
        payload = json.loads(str(revision["canonical_json"]))
        return next(
            requirement
            for requirement in self.kernel.derivations._requirements(
                revision_id,
                payload,
                execution_mode="PRIVATE",
            )
            if requirement.output_role == "voice.primary"
        )

    def _materialize_voice(self, revision_id: str, *, variant_id: str | None) -> str:
        requirement = self._voice_requirement(revision_id)
        admission = self.kernel.jobs.admit(
            production_id=self.production_id,
            production_revision_id=revision_id,
            variant_id=variant_id,
            job_class="ARTIFACT",
            job_type=requirement.job_type,
            semantic_capability=requirement.capability,
            spec={
                "schema_version": 1,
                "execution_mode": "PRIVATE",
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
            },
            route=requirement.route,
            input_fingerprint=requirement.input_fingerprint,
            max_attempts=1,
        )
        return self.kernel.derivations.execute_job(admission.job_id)

    def test_composition_registration_rejects_variant_language_not_bound_to_revision(self) -> None:
        revision = self.capture_revision()
        voice_id = self._materialize_voice(revision.revision_id, variant_id=None)
        voice = self.kernel.artifacts.artifact(voice_id, verify_bytes=True)
        revision_row = self.kernel.productions.revision(revision.revision_id)
        revision_payload = json.loads(str(revision_row["canonical_json"]))

        intent = self._intent(language="fr")
        variant_id = self.kernel.variants.create(
            production_id=self.production_id,
            source_revision_id=revision.revision_id,
            intent=intent,
        )
        variant = self.kernel.variants.variant(variant_id)
        route = self.kernel.capabilities.router.qualify(
            "composition.compile",
            execution_mode="PRIVATE",
        )
        semantic_inputs = {
            "variant_intent_hash": str(variant["intent_hash"]),
            "composition_preferences": revision_payload["composition_preferences"],
        }
        source_digests = {"voice": str(voice["object_digest"])}
        fingerprint = expected_fingerprint(
            "composition.compile",
            output_role="composition.primary",
            semantic_inputs=semantic_inputs,
            source_object_digests=source_digests,
            route=route,
        )
        admission = self.kernel.jobs.admit(
            production_id=self.production_id,
            production_revision_id=revision.revision_id,
            variant_id=variant_id,
            job_class="ARTIFACT",
            job_type="COMPOSITION_COMPILE",
            semantic_capability="composition.compile",
            spec={
                "schema_version": 1,
                "execution_mode": "PRIVATE",
                "output_role": "composition.primary",
                "kind": "composition_spec",
                "media_type": "application/json",
                "filename": "composition.json",
                "semantic_inputs": semantic_inputs,
                "source_object_digests": source_digests,
                "source_artifacts": [{"artifact_id": voice_id, "role": "voice"}],
            },
            route=route,
            input_fingerprint=fingerprint,
            max_attempts=1,
        )
        composition = {
            "schema_version": 1,
            "variant_intent_hash": str(variant["intent_hash"]),
            "composition_preferences": revision_payload["composition_preferences"],
            "canvas": self.kernel.variants.canvas(intent),
            "duration_ms": 1000,
            "tracks": [
                {"kind": "visual", "items": []},
                {"kind": "voice", "object_digest": str(voice["object_digest"])},
            ],
        }
        temp_dir = self.kernel.jobs.start_attempt(
            admission.attempt_id,
            "deterministic-artifact-provider",
        )
        (temp_dir / "composition.json").write_bytes(
            canonical_text(composition).encode("utf-8")
        )

        with self.assertRaisesRegex(InvalidArtifact, "language adaptation"):
            self.kernel.jobs.complete_artifact_attempt(
                admission.attempt_id,
                [
                    {
                        "role": "composition.primary",
                        "path": "composition.json",
                        "kind": "composition_spec",
                        "media_type": "application/json",
                        "producer_stage": "language-authority-test",
                    }
                ],
            )
        self.kernel.jobs.fail_attempt(
            admission.attempt_id,
            "EXPECTED_TEST_FAILURE",
            "variant language must match captured revision",
        )
        self.assertEqual(self.kernel.variants.variant(variant_id)["state"], "PROPOSED")

    def test_variant_input_reuse_skips_scoped_candidate_and_selects_unscoped_artifact(self) -> None:
        revision = self.capture_revision()
        variant_id = self.kernel.variants.create(
            production_id=self.production_id,
            source_revision_id=revision.revision_id,
            intent=self._intent(),
        )

        scoped_voice_id = self._materialize_voice(
            revision.revision_id,
            variant_id=variant_id,
        )
        unscoped_voice_id = self._materialize_voice(
            revision.revision_id,
            variant_id=None,
        )
        self.assertNotEqual(scoped_voice_id, unscoped_voice_id)
        self.assertEqual(
            self.kernel.artifacts.artifact(scoped_voice_id)["variant_id"],
            variant_id,
        )
        self.assertIsNone(self.kernel.artifacts.artifact(unscoped_voice_id)["variant_id"])

        plans = self.kernel.variant_pipeline.plan_inputs(variant_id)
        voice_plan = next(plan for plan in plans if plan.output_role == "voice.primary")
        self.assertEqual(voice_plan.disposition, "REUSED_ARTIFACT")
        self.assertEqual(voice_plan.artifact_id, unscoped_voice_id)
        self.assertNotEqual(voice_plan.artifact_id, scoped_voice_id)

    def test_composition_registration_rejects_duration_not_selected_by_compiler(self) -> None:
        revision = self.capture_revision()
        voice_id = self._materialize_voice(revision.revision_id, variant_id=None)
        voice = self.kernel.artifacts.artifact(voice_id, verify_bytes=True)
        revision_row = self.kernel.productions.revision(revision.revision_id)
        revision_payload = json.loads(str(revision_row["canonical_json"]))

        intent = self._intent()
        variant_id = self.kernel.variants.create(
            production_id=self.production_id,
            source_revision_id=revision.revision_id,
            intent=intent,
        )
        variant = self.kernel.variants.variant(variant_id)
        route = self.kernel.capabilities.router.qualify(
            "composition.compile",
            execution_mode="PRIVATE",
        )
        semantic_inputs = {
            "variant_intent_hash": str(variant["intent_hash"]),
            "composition_preferences": revision_payload["composition_preferences"],
        }
        source_digests = {"voice": str(voice["object_digest"])}
        fingerprint = expected_fingerprint(
            "composition.compile",
            output_role="composition.primary",
            semantic_inputs=semantic_inputs,
            source_object_digests=source_digests,
            route=route,
        )
        admission = self.kernel.jobs.admit(
            production_id=self.production_id,
            production_revision_id=revision.revision_id,
            variant_id=variant_id,
            job_class="ARTIFACT",
            job_type="COMPOSITION_COMPILE",
            semantic_capability="composition.compile",
            spec={
                "schema_version": 1,
                "execution_mode": "PRIVATE",
                "output_role": "composition.primary",
                "kind": "composition_spec",
                "media_type": "application/json",
                "filename": "composition.json",
                "semantic_inputs": semantic_inputs,
                "source_object_digests": source_digests,
                "source_artifacts": [{"artifact_id": voice_id, "role": "voice"}],
            },
            route=route,
            input_fingerprint=fingerprint,
            max_attempts=1,
        )
        composition = {
            "schema_version": 1,
            "variant_intent_hash": str(variant["intent_hash"]),
            "composition_preferences": revision_payload["composition_preferences"],
            "canvas": self.kernel.variants.canvas(intent),
            "duration_ms": 1500,
            "tracks": [
                {"kind": "visual", "items": []},
                {"kind": "voice", "object_digest": str(voice["object_digest"])},
            ],
        }
        temp_dir = self.kernel.jobs.start_attempt(
            admission.attempt_id,
            "deterministic-artifact-provider",
        )
        (temp_dir / "composition.json").write_bytes(
            canonical_text(composition).encode("utf-8")
        )

        with self.assertRaisesRegex(InvalidArtifact, "deterministic compiler selection"):
            self.kernel.jobs.complete_artifact_attempt(
                admission.attempt_id,
                [
                    {
                        "role": "composition.primary",
                        "path": "composition.json",
                        "kind": "composition_spec",
                        "media_type": "application/json",
                        "producer_stage": "duration-authority-test",
                    }
                ],
            )
        self.kernel.jobs.fail_attempt(
            admission.attempt_id,
            "EXPECTED_TEST_FAILURE",
            "composition duration must match deterministic compiler output",
        )
        self.assertEqual(self.kernel.variants.variant(variant_id)["state"], "PROPOSED")
