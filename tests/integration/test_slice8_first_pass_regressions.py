from __future__ import annotations

import sqlite3

from solostudio.kernel.errors import InvalidCommand
from tests.integration.job_test_support import JobTestCase


class Slice8FirstPassRegressionTests(JobTestCase):
    @staticmethod
    def _intent(*, caption_mode: str = "burned") -> dict[str, object]:
        return {
            "aspect_ratio": "9:16",
            "language": "en",
            "duration_min_ms": 45_000,
            "duration_max_ms": 60_000,
            "caption_mode": caption_mode,
            "audio_mode": "voiceover",
        }

    def test_database_trigger_rejects_dangling_variant_job_lineage(self) -> None:
        revision = self.capture_revision()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.kernel.store.write() as db:
                db.execute(
                    """
                    INSERT INTO job_specs(
                        id,production_id,job_class,source_state_version,production_revision_id,variant_id,
                        job_type,semantic_capability,spec_json,spec_hash,route_json,input_fingerprint,
                        state,max_attempts,created_at,finished_at
                    ) VALUES (?,?, 'ARTIFACT',NULL,?,?,'TEST','test.capability','{}','spec','{}','fingerprint','QUEUED',1,?,NULL)
                    """,
                    (
                        "job_dangling_variant",
                        self.production_id,
                        revision.revision_id,
                        "var_missing",
                        self.kernel.jobs.clock.now(),
                    ),
                )

    def test_nonvariant_admission_does_not_reuse_variant_scoped_active_job(self) -> None:
        revision = self.capture_revision()
        variant_id = self.kernel.variants.create(
            production_id=self.production_id,
            source_revision_id=revision.revision_id,
            intent=self._intent(),
        )
        route = self.kernel.capabilities.router.qualify("composition.compile", execution_mode="PRIVATE")
        common = dict(
            production_id=self.production_id,
            production_revision_id=revision.revision_id,
            job_class="ARTIFACT",
            job_type="TEST_EQUIVALENT",
            semantic_capability="test.equivalent",
            spec={"schema_version": 1},
            route=route,
            input_fingerprint="f" * 64,
            max_attempts=2,
        )
        variant_job = self.kernel.jobs.admit(variant_id=variant_id, **common)
        nonvariant_job = self.kernel.jobs.admit(variant_id=None, **common)
        self.assertNotEqual(variant_job.job_id, nonvariant_job.job_id)
        self.assertFalse(nonvariant_job.reused)

    def test_caption_free_variant_plans_no_caption_job_and_allows_empty_visual_plan(self) -> None:
        revision = self.capture_revision()
        variant_id = self.kernel.variants.create(
            production_id=self.production_id,
            source_revision_id=revision.revision_id,
            intent=self._intent(caption_mode="none"),
        )
        plans = self.kernel.variant_pipeline.plan_inputs(variant_id)
        self.assertEqual([plan.output_role for plan in plans], ["voice.primary"])
        with self.kernel.store.read() as db:
            caption_jobs = db.execute(
                "SELECT COUNT(*) FROM job_specs WHERE production_id=? AND semantic_capability='captions.generate'",
                (self.production_id,),
            ).fetchone()[0]
        self.assertEqual(caption_jobs, 0)

    def test_unhashable_variant_enum_values_are_domain_errors(self) -> None:
        revision = self.capture_revision()
        for field in ("aspect_ratio", "caption_mode", "audio_mode"):
            with self.subTest(field=field):
                intent = self._intent()
                intent[field] = []
                with self.assertRaises(InvalidCommand):
                    self.kernel.variants.create(
                        production_id=self.production_id,
                        source_revision_id=revision.revision_id,
                        intent=intent,
                    )
