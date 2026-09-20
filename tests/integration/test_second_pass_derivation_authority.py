from __future__ import annotations

import json
import sys
from copy import deepcopy
from unittest.mock import patch

import solostudio.definitions.presets as preset_definitions
from solostudio.kernel.costs import CostPlan
from solostudio.kernel.derivations import expected_fingerprint
from solostudio.kernel.errors import InvalidCommand
from tests.integration.job_test_support import JobTestCase


class SecondPassDerivationAuthorityTests(JobTestCase):
    def _prepare_revision(self):
        version = self.kernel.productions.working_state(self.production_id)["state_version"]
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=version,
            idempotency_key="second-pass-script",
            action="set_script",
            command_input={"text": "captured historical script", "status": "ready"},
        )
        version += 1
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=version,
            idempotency_key="second-pass-visuals",
            action="set_visual_plan",
            command_input={
                "items": [
                    {"item_id": "scene-01", "purpose": "explain", "prompt": "stable scene"},
                ]
            },
        )
        version += 1
        return self.kernel.user.capture_revision(
            production_id=self.production_id,
            expected_state_version=version,
            idempotency_key="second-pass-capture",
        )

    def test_historical_revision_uses_captured_definition_identity(self) -> None:
        revision = self._prepare_revision()
        payload = json.loads(self.kernel.productions.revision(revision.revision_id)["canonical_json"])
        captured = payload["captured_defaults"]["voice_profile"]
        self.assertTrue(captured["resolved"])

        changed = deepcopy(preset_definitions._VOICE_PROFILES["default"])
        changed["engine_profile"] = "changed-after-revision-capture"
        with patch.dict(preset_definitions._VOICE_PROFILES, {"default": changed}, clear=False):
            current_identity = preset_definitions.voice_profile_identity(None)
            self.assertNotEqual(current_identity["content_hash"], captured["content_hash"])
            plan = self.kernel.derivations.plan_revision(revision.revision_id)

        voice = next(item for item in plan if item.output_role == "voice.primary")
        job = self.kernel.jobs.job(voice.job_id)
        self.assertEqual(
            job["spec"]["semantic_inputs"]["voice_profile"],
            {
                "definition_id": captured["definition_id"],
                "content_hash": captured["content_hash"],
            },
        )

    def test_generic_worker_cannot_bypass_bound_deterministic_executor(self) -> None:
        revision = self._prepare_revision()
        voice = next(
            item
            for item in self.kernel.derivations.plan_revision(revision.revision_id)
            if item.output_role == "voice.primary"
        )
        with self.assertRaises(InvalidCommand):
            self.kernel.worker.run(voice.attempt_id, [sys.executable, "-c", "pass"])
        self.assertEqual(self.kernel.jobs.attempt(voice.attempt_id)["state"], "CREATED")
        self.assertEqual(self.kernel.jobs.job(voice.job_id)["state"], "QUEUED")
        self.assertEqual(self.kernel.costs.for_job(voice.job_id)["state"], "RESERVED")

    def test_executor_rejects_semantic_inputs_not_bound_to_revision(self) -> None:
        revision = self._prepare_revision()
        plan = self.kernel.derivations.plan_revision(revision.revision_id)
        by_role = {item.output_role: item for item in plan}
        mutations = {
            "voice.primary": lambda semantic: semantic.__setitem__("pace", "forged-pace"),
            "captions.primary": lambda semantic: semantic.__setitem__("language", "zz"),
            "visual.scene-01": lambda semantic: semantic["visual_style"].__setitem__(
                "content_hash", "0" * 64
            ),
        }

        for role, mutate in mutations.items():
            with self.subTest(role=role):
                original = by_role[role]
                job = self.kernel.jobs.job(original.job_id)
                spec = deepcopy(job["spec"])
                mutate(spec["semantic_inputs"])
                fingerprint = expected_fingerprint(
                    job["semantic_capability"],
                    output_role=spec["output_role"],
                    semantic_inputs=spec["semantic_inputs"],
                    source_object_digests=spec["source_object_digests"],
                    route=job["route"],
                )
                admission = self.kernel.jobs.admit(
                    production_id=self.production_id,
                    production_revision_id=revision.revision_id,
                    job_class="ARTIFACT",
                    job_type=job["job_type"],
                    semantic_capability=job["semantic_capability"],
                    spec=spec,
                    route=job["route"],
                    input_fingerprint=fingerprint,
                    cost_plan=CostPlan(job["semantic_capability"], 0, 0),
                )
                with self.assertRaises(InvalidCommand):
                    self.kernel.derivations.execute_job(admission.job_id)
                self.assertEqual(self.kernel.jobs.attempt(admission.attempt_id)["state"], "CREATED")
