from __future__ import annotations

import io
import wave

from solostudio.kernel.derivations.service import _deterministic_png
from solostudio.kernel.errors import InvalidArtifact
from tests.integration.job_test_support import JobTestCase


class Slice8DeterministicByteAuthorityTests(JobTestCase):
    @staticmethod
    def _forged_wav() -> bytes:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(1)
            wav.setframerate(8000)
            wav.writeframes(b"\x80" * 800)
        return buffer.getvalue()

    def _revision_with_visual(self):
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=0,
            idempotency_key="byte-authority-script",
            action="set_script",
            command_input={"text": "deterministic byte authority"},
        )
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=1,
            idempotency_key="byte-authority-visual",
            action="set_visual_plan",
            command_input={
                "items": [
                    {"item_id": "scene-01", "description": "deterministic byte authority"},
                ]
            },
        )
        return self.kernel.user.capture_revision(
            production_id=self.production_id,
            expected_state_version=2,
            idempotency_key="byte-authority-capture",
        )

    def test_running_deterministic_derivation_attempt_cannot_register_forged_valid_bytes(self) -> None:
        revision = self._revision_with_visual()
        plans = self.kernel.derivations.plan_revision(revision.revision_id)
        by_capability = {plan.capability: plan for plan in plans}
        self.assertEqual(
            set(by_capability),
            {"speech.synthesize", "captions.generate", "image.generate"},
        )

        forged_payloads = {
            "speech.synthesize": self._forged_wav(),
            "captions.generate": b"WEBVTT\n\n00:00.000 --> 00:05.000\nforged caption bytes\n",
            "image.generate": _deterministic_png("forged-not-the-job-fingerprint"),
        }

        for capability, plan in by_capability.items():
            self.assertIsNotNone(plan.job_id)
            self.assertIsNotNone(plan.attempt_id)
            job_id = str(plan.job_id)
            attempt_id = str(plan.attempt_id)
            job = self.kernel.jobs.job(job_id)
            spec = job["spec"]
            filename = str(spec["filename"])
            temp_dir = self.kernel.jobs.start_attempt(
                attempt_id,
                "deterministic-artifact-provider",
            )
            (temp_dir / filename).write_bytes(forged_payloads[capability])

            with self.assertRaisesRegex(InvalidArtifact, "deterministic provider output"):
                self.kernel.jobs.complete_artifact_attempt(
                    attempt_id,
                    [
                        {
                            "role": str(spec["output_role"]),
                            "path": filename,
                            "kind": str(spec["kind"]),
                            "media_type": str(spec["media_type"]),
                            "producer_stage": "deterministic_artifact_provider",
                        }
                    ],
                )

            with self.kernel.store.read() as db:
                registered = db.execute(
                    "SELECT COUNT(*) FROM artifacts WHERE producer_job_id=?",
                    (job_id,),
                ).fetchone()[0]
            self.assertEqual(int(registered), 0)
            self.kernel.jobs.fail_attempt(
                attempt_id,
                "EXPECTED_TEST_FAILURE",
                "forged deterministic output must not register",
            )
