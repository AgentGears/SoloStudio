from __future__ import annotations

from copy import deepcopy
from unittest.mock import patch

from solostudio.kernel.costs import CostPlan
from solostudio.kernel.derivations import expected_fingerprint
from solostudio.kernel.errors import ArtifactDigestMismatch, InvalidCommand
from tests.integration.job_test_support import JobTestCase


class DependencyReuseArtifactJobTests(JobTestCase):
    def _prepare_revision(self, script: str = "first script", *, pace: str = "normal"):
        version = self.kernel.productions.working_state(self.production_id)["state_version"]
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=version,
            idempotency_key=f"script-{version}-{script}",
            action="set_script",
            command_input={"text": script, "status": "ready"},
        )
        version += 1
        if pace != "normal":
            self.kernel.user.command(
                production_id=self.production_id,
                expected_state_version=version,
                idempotency_key=f"pace-{version}-{pace}",
                action="set_voice_preferences",
                command_input={"voice": {"pace": pace}},
            )
            version += 1
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=version,
            idempotency_key=f"visuals-{version}",
            action="set_visual_plan",
            command_input={
                "items": [
                    {"item_id": "scene-01", "purpose": "hook", "prompt": "stable opening"},
                    {"item_id": "scene-02", "purpose": "explain", "prompt": "stable explanation"},
                ]
            },
        )
        version += 1
        return self.kernel.user.capture_revision(
            production_id=self.production_id,
            expected_state_version=version,
            idempotency_key=f"capture-{version}",
        )

    def _execute_plan(self, plan):
        resolved = {}
        for item in plan:
            if item.artifact_id is not None:
                resolved[item.output_role] = item.artifact_id
            else:
                resolved[item.output_role] = self.kernel.derivations.execute_job(item.job_id)
        return resolved

    def test_plan_executes_registers_dependencies_and_reuses_artifacts(self) -> None:
        revision = self._prepare_revision()
        plan = self.kernel.derivations.plan_revision(revision.revision_id)
        self.assertEqual(
            {item.output_role for item in plan},
            {"voice.primary", "captions.primary", "visual.scene-01", "visual.scene-02"},
        )
        self.assertEqual({item.disposition for item in plan}, {"ADMITTED_JOB"})

        artifacts = self._execute_plan(plan)
        for role, artifact_id in artifacts.items():
            artifact = self.kernel.artifacts.artifact(artifact_id, verify_bytes=True)
            self.assertEqual(
                artifact["input_fingerprint"],
                next(item.input_fingerprint for item in plan if item.output_role == role),
            )
            dependencies = self.kernel.artifacts.dependencies(artifact_id)
            self.assertEqual(len(dependencies), 1)
            self.assertIn(dependencies[0]["role"], {"script_text", "visual_plan"})

        second = self.kernel.derivations.plan_revision(revision.revision_id)
        self.assertEqual({item.disposition for item in second}, {"REUSED_ARTIFACT"})
        self.assertEqual(
            {item.output_role: item.artifact_id for item in second},
            artifacts,
        )

    def test_active_equivalent_plan_reuses_jobs_without_duplicate_cost_rows(self) -> None:
        revision = self._prepare_revision()
        first = self.kernel.derivations.plan_revision(revision.revision_id)
        second = self.kernel.derivations.plan_revision(revision.revision_id)
        self.assertEqual({item.disposition for item in second}, {"REUSED_JOB"})
        self.assertEqual(
            {item.output_role: item.job_id for item in first},
            {item.output_role: item.job_id for item in second},
        )
        with self.kernel.store.read() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM cost_ledger").fetchone()[0],
                len(first),
            )

    def test_script_change_recomputes_voice_and_captions_but_reuses_visuals(self) -> None:
        r1 = self._prepare_revision("script one")
        first = self.kernel.derivations.plan_revision(r1.revision_id)
        a1 = self._execute_plan(first)

        version = self.kernel.productions.working_state(self.production_id)["state_version"]
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=version,
            idempotency_key="script-two",
            action="set_script",
            command_input={"text": "script two", "status": "ready"},
        )
        r2 = self.kernel.user.capture_revision(
            production_id=self.production_id,
            expected_state_version=version + 1,
            idempotency_key="capture-two",
        )
        second = self.kernel.derivations.plan_revision(r2.revision_id)
        by_role = {item.output_role: item for item in second}

        self.assertEqual(by_role["voice.primary"].disposition, "ADMITTED_JOB")
        self.assertEqual(by_role["captions.primary"].disposition, "ADMITTED_JOB")
        for role in ("visual.scene-01", "visual.scene-02"):
            self.assertEqual(by_role[role].disposition, "REUSED_ARTIFACT")
            self.assertEqual(by_role[role].artifact_id, a1[role])
            self.assertEqual(
                self.kernel.artifacts.artifact(a1[role])["production_revision_id"],
                r1.revision_id,
            )

    def test_voice_preference_change_only_recomputes_voice(self) -> None:
        r1 = self._prepare_revision()
        first = self.kernel.derivations.plan_revision(r1.revision_id)
        a1 = self._execute_plan(first)

        version = self.kernel.productions.working_state(self.production_id)["state_version"]
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=version,
            idempotency_key="pace-slow",
            action="set_voice_preferences",
            command_input={"voice": {"pace": "slow"}},
        )
        r2 = self.kernel.user.capture_revision(
            production_id=self.production_id,
            expected_state_version=version + 1,
            idempotency_key="capture-voice-change",
        )
        second = {
            item.output_role: item
            for item in self.kernel.derivations.plan_revision(r2.revision_id)
        }
        self.assertEqual(second["voice.primary"].disposition, "ADMITTED_JOB")
        self.assertEqual(second["captions.primary"].artifact_id, a1["captions.primary"])
        self.assertEqual(second["visual.scene-01"].artifact_id, a1["visual.scene-01"])
        self.assertEqual(second["visual.scene-02"].artifact_id, a1["visual.scene-02"])

    def test_visual_item_change_recomputes_only_affected_image(self) -> None:
        r1 = self._prepare_revision()
        first = self.kernel.derivations.plan_revision(r1.revision_id)
        a1 = self._execute_plan(first)

        version = self.kernel.productions.working_state(self.production_id)["state_version"]
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=version,
            idempotency_key="change-one-visual",
            action="set_visual_plan",
            command_input={
                "items": [
                    {"item_id": "scene-01", "purpose": "hook", "prompt": "changed opening"},
                    {"item_id": "scene-02", "purpose": "explain", "prompt": "stable explanation"},
                ]
            },
        )
        r2 = self.kernel.user.capture_revision(
            production_id=self.production_id,
            expected_state_version=version + 1,
            idempotency_key="capture-visual-change",
        )
        second = {
            item.output_role: item
            for item in self.kernel.derivations.plan_revision(r2.revision_id)
        }
        self.assertEqual(second["voice.primary"].artifact_id, a1["voice.primary"])
        self.assertEqual(second["captions.primary"].artifact_id, a1["captions.primary"])
        self.assertEqual(second["visual.scene-01"].disposition, "ADMITTED_JOB")
        self.assertEqual(second["visual.scene-02"].artifact_id, a1["visual.scene-02"])

    def test_unknown_voice_definition_rejects_before_job_admission(self) -> None:
        version = self.kernel.productions.working_state(self.production_id)["state_version"]
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=version,
            idempotency_key="script",
            action="set_script",
            command_input={"text": "seed", "status": "ready"},
        )
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=version + 1,
            idempotency_key="unknown-voice",
            action="set_voice_preferences",
            command_input={"voice": {"voice_profile_ref": "missing"}},
        )
        revision = self.kernel.user.capture_revision(
            production_id=self.production_id,
            expected_state_version=version + 2,
            idempotency_key="capture",
        )
        with self.assertRaises(InvalidCommand):
            self.kernel.derivations.plan_revision(revision.revision_id)
        with self.kernel.store.read() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM job_specs").fetchone()[0], 0)

    def test_tampered_artifact_fingerprint_is_rejected_before_attempt_start(self) -> None:
        revision = self._prepare_revision()
        valid = self.kernel.derivations.plan_revision(revision.revision_id)[0]
        job = self.kernel.jobs.job(valid.job_id)
        admission = self.kernel.jobs.admit(
            production_id=self.production_id,
            production_revision_id=revision.revision_id,
            job_class="ARTIFACT",
            job_type=job["job_type"],
            semantic_capability=job["semantic_capability"],
            spec=job["spec"],
            route=job["route"],
            input_fingerprint="tampered-fingerprint",
            cost_plan=CostPlan(job["semantic_capability"], 0, 0),
        )
        with self.assertRaises(InvalidCommand):
            self.kernel.derivations.execute_job(admission.job_id)
        self.assertEqual(self.kernel.jobs.attempt(admission.attempt_id)["state"], "CREATED")

    def test_invalid_caption_enabled_type_rejects_before_job_admission(self) -> None:
        version = self.kernel.productions.working_state(self.production_id)["state_version"]
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=version,
            idempotency_key="script-caption-type",
            action="set_script",
            command_input={"text": "seed", "status": "ready"},
        )
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=version + 1,
            idempotency_key="bad-caption-enabled",
            action="set_caption_preferences",
            command_input={"captions": {"enabled": "yes"}},
        )
        revision = self.kernel.user.capture_revision(
            production_id=self.production_id,
            expected_state_version=version + 2,
            idempotency_key="capture-bad-caption-enabled",
        )
        with self.assertRaises(InvalidCommand):
            self.kernel.derivations.plan_revision(revision.revision_id)
        with self.kernel.store.read() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM job_specs").fetchone()[0], 0)

    def test_executor_rejects_source_artifact_not_bound_to_projection(self) -> None:
        revision = self._prepare_revision()
        plan = self.kernel.derivations.plan_revision(revision.revision_id)
        voice = next(item for item in plan if item.output_role == "voice.primary")
        self.kernel.derivations.execute_job(voice.job_id)
        job = self.kernel.jobs.job(voice.job_id)
        visual_source = next(
            artifact
            for artifact in self.kernel.artifacts.revision_artifacts(revision.revision_id)
            if artifact["kind"] == "visual_plan" and artifact["producer_stage"] == "revision_capture"
        )
        spec = deepcopy(job["spec"])
        spec["source_artifacts"] = [
            {"artifact_id": visual_source["id"], "role": "script_text"}
        ]
        admission = self.kernel.jobs.admit(
            production_id=self.production_id,
            production_revision_id=revision.revision_id,
            job_class="ARTIFACT",
            job_type=job["job_type"],
            semantic_capability=job["semantic_capability"],
            spec=spec,
            route=job["route"],
            input_fingerprint=job["input_fingerprint"],
            cost_plan=CostPlan(job["semantic_capability"], 0, 0),
        )
        with self.assertRaises(InvalidCommand):
            self.kernel.derivations.execute_job(admission.job_id)
        self.assertEqual(self.kernel.jobs.attempt(admission.attempt_id)["state"], "CREATED")

    def test_executor_rejects_image_item_hash_not_present_in_source_plan(self) -> None:
        revision = self._prepare_revision()
        plan = self.kernel.derivations.plan_revision(revision.revision_id)
        image = next(item for item in plan if item.output_role == "visual.scene-01")
        job = self.kernel.jobs.job(image.job_id)
        spec = deepcopy(job["spec"])
        spec["semantic_inputs"]["visual_plan_item_hash"] = "0" * 64
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

    def test_artifact_integrity_failure_after_start_fails_job_instead_of_wedging(self) -> None:
        revision = self._prepare_revision()
        plan = self.kernel.derivations.plan_revision(revision.revision_id)
        voice = next(item for item in plan if item.output_role == "voice.primary")
        with patch.object(
            self.kernel.artifacts,
            "prepare_bytes",
            side_effect=ArtifactDigestMismatch("simulated retained-object corruption"),
        ):
            with self.assertRaises(ArtifactDigestMismatch):
                self.kernel.derivations.execute_job(voice.job_id)
        self.assertEqual(self.kernel.jobs.job(voice.job_id)["state"], "FAILED")
        attempt = self.kernel.jobs.attempt(voice.attempt_id)
        self.assertEqual(attempt["state"], "FAILED")
        self.assertEqual(attempt["error_code"], "ARTIFACT_DIGEST_MISMATCH")
        self.assertEqual(self.kernel.costs.for_job(voice.job_id)["state"], "RELEASED")
