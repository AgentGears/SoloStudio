from __future__ import annotations

from solostudio.app.bootstrap import bootstrap
from solostudio.kernel.clock import FixedClock
from solostudio.kernel.errors import ContractExpired, InvalidArtifact, InvalidCommand, VariantRequired
from solostudio.kernel.identity import canonical_hash, canonical_text
from tests.integration.job_test_support import JobTestCase


class DestinationPackageEnvelopeTests(JobTestCase):
    @staticmethod
    def _intent(aspect_ratio: str = "9:16") -> dict[str, object]:
        return {
            "aspect_ratio": aspect_ratio,
            "language": "en",
            "duration_min_ms": 1000,
            "duration_max_ms": 1500,
            "caption_mode": "burned",
            "audio_mode": "voiceover",
        }

    def _ready_variant(self, *, aspect_ratio: str = "9:16") -> tuple[str, str]:
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=0,
            idempotency_key=f"script-{aspect_ratio}",
            action="set_script",
            command_input={"text": "package boundary script"},
        )
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=1,
            idempotency_key=f"visual-{aspect_ratio}",
            action="set_visual_plan",
            command_input={"items": [{"item_id": "scene-1", "description": "stable visual"}]},
        )
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=2,
            idempotency_key=f"duration-{aspect_ratio}",
            action="update_brief",
            command_input={"brief": {"duration_min_ms": 1000, "duration_max_ms": 1500}},
        )
        revision = self.kernel.user.capture_revision(
            production_id=self.production_id,
            expected_state_version=3,
            idempotency_key=f"capture-{aspect_ratio}",
        )
        variant_id = self.kernel.variants.create(
            production_id=self.production_id,
            source_revision_id=revision.revision_id,
            intent=self._intent(aspect_ratio),
        )
        render_id = self._materialize_variant(variant_id)
        return variant_id, render_id

    def _materialize_variant(self, variant_id: str) -> str:
        for plan in self.kernel.variant_pipeline.plan_inputs(variant_id):
            if plan.job_id is not None:
                job = self.kernel.jobs.job(str(plan.job_id))
                if job["state"] == "QUEUED":
                    self.kernel.derivations.execute_job(str(plan.job_id))
        composition = self.kernel.variant_pipeline.plan_composition(variant_id)
        if composition.job_id is not None:
            self.kernel.variant_pipeline.execute_job(str(composition.job_id))
        render = self.kernel.variant_pipeline.plan_render(variant_id)
        if render.job_id is not None:
            render_id = self.kernel.variant_pipeline.execute_job(str(render.job_id))
        else:
            render_id = str(render.artifact_id)
        self.assertEqual(self.kernel.variants.variant(variant_id)["state"], "READY")
        return render_id

    def _build_package(
        self,
        variant_id: str,
        render_id: str,
        *,
        title: str = "Short title",
        scheduled_for: str | None = None,
    ) -> str:
        return self.kernel.packaging.build_package(
            variant_id=variant_id,
            destination_account_id="dest_fake_1",
            render_artifact_id=render_id,
            metadata={"title": title, "description": "Description"},
            settings={"visibility": "public"},
            scheduled_for=scheduled_for,
        )

    def test_contract_snapshot_persists_refreshes_same_fingerprint_and_discovers_v2(self) -> None:
        snapshot1 = self.kernel.destinations.discover_contract("dest_fake_1", valid_for_seconds=60)
        self.assertEqual(snapshot1["contract"]["contract_version"], "fake-v1")
        self.assertEqual(snapshot1["fingerprint"], canonical_hash(snapshot1["contract"]))

        self.kernel.close()
        self.kernel = bootstrap(
            self.root,
            clock=FixedClock("2026-09-19T20:00:30.000Z"),
            ids=self.ids,
        )
        replay = self.kernel.destinations.current_contract("dest_fake_1", refresh=False)
        self.assertEqual(replay["id"], snapshot1["id"])

        self.kernel.destinations.clock.value = "2026-09-19T20:02:00.000Z"
        with self.assertRaises(ContractExpired):
            self.kernel.destinations.current_contract("dest_fake_1", refresh=False)
        renewed = self.kernel.destinations.current_contract("dest_fake_1", refresh=True)
        self.assertEqual(renewed["id"], snapshot1["id"])
        self.assertEqual(renewed["fingerprint"], snapshot1["fingerprint"])
        self.assertNotEqual(renewed["valid_until"], snapshot1["valid_until"])
        self.assertFalse(self.kernel.destinations.is_expired(str(renewed["id"])))

        self.kernel.destinations.set_fake_contract_version("dest_fake_1", "fake-v2")
        snapshot2 = self.kernel.destinations.current_contract("dest_fake_1", refresh=True)
        self.assertEqual(snapshot2["contract"]["contract_version"], "fake-v2")
        self.assertNotEqual(snapshot2["fingerprint"], snapshot1["fingerprint"])
        with self.kernel.store.read() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM destination_contract_snapshots WHERE destination_account_id='dest_fake_1'"
            ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_package_does_not_transform_media_and_review_uses_exact_envelope_and_bytes(self) -> None:
        variant_id, render_id = self._ready_variant()
        package_id = self._build_package(
            variant_id,
            render_id,
            scheduled_for="2026-09-22T12:00:00Z",
        )
        package = self.kernel.packaging.package(package_id)
        package_body = package["package"]
        render = self.kernel.artifacts.artifact(render_id, verify_bytes=True)
        self.assertEqual(package_body["media"][0]["artifact_id"], render_id)
        self.assertEqual(package_body["media"][0]["sha256"], render["object_digest"])
        self.assertEqual(package_body["media"][0]["byte_size"], render["byte_size"])
        self.assertEqual(package_body["scheduled_for"], "2026-09-22T12:00:00Z")

        envelope_id = self.kernel.packaging.build_envelope(
            package_revision_id=package_id,
            publication_intent_id="pubintent-one",
            cost_ceiling_microunits=0,
        )
        envelope_row = self.kernel.packaging.envelope(envelope_id)
        review = self.kernel.packaging.review_payload(envelope_id)
        self.assertEqual(envelope_row["envelope"]["scheduled_for"], package_body["scheduled_for"])
        self.assertEqual(review["envelope_hash"], envelope_row["canonical_hash"])
        self.assertEqual(
            review["envelope_canonical_bytes"],
            canonical_text(envelope_row["envelope"]).encode("utf-8"),
        )
        self.assertEqual(
            review["media"][0]["preview_bytes"],
            self.kernel.artifacts.read_bytes(render_id),
        )
        self.assertEqual(review["media"][0]["sha256"], render["object_digest"])

    def test_schedule_change_requires_new_package_and_cannot_be_overridden_by_envelope(self) -> None:
        variant_id, render_id = self._ready_variant()
        package1 = self._build_package(
            variant_id,
            render_id,
            scheduled_for="2026-09-22T12:00:00Z",
        )
        package2 = self._build_package(
            variant_id,
            render_id,
            scheduled_for="2026-09-22T13:00:00Z",
        )
        self.assertNotEqual(package1, package2)
        self.assertNotEqual(
            self.kernel.packaging.package(package1)["canonical_hash"],
            self.kernel.packaging.package(package2)["canonical_hash"],
        )
        with self.assertRaises(InvalidCommand):
            self.kernel.packaging.build_envelope(
                package_revision_id=package1,
                publication_intent_id="pubintent-wrong-schedule",
                scheduled_for="2026-09-22T13:00:00Z",
            )
        envelope = self.kernel.packaging.build_envelope(
            package_revision_id=package1,
            publication_intent_id="pubintent-package-schedule",
        )
        self.assertEqual(
            self.kernel.packaging.envelope(envelope)["envelope"]["scheduled_for"],
            "2026-09-22T12:00:00Z",
        )

    def test_publication_intent_is_identity_bearing_and_replay_is_exact(self) -> None:
        variant_id, render_id = self._ready_variant()
        package_id = self._build_package(variant_id, render_id)
        first = self.kernel.packaging.build_envelope(
            package_revision_id=package_id,
            publication_intent_id="pubintent-one",
        )
        replay = self.kernel.packaging.build_envelope(
            package_revision_id=package_id,
            publication_intent_id="pubintent-one",
        )
        self.assertEqual(first, replay)
        first_row = self.kernel.packaging.envelope(first)

        with self.assertRaises(InvalidCommand):
            self.kernel.packaging.build_envelope(
                package_revision_id=package_id,
                publication_intent_id="pubintent-one",
                cost_ceiling_microunits=1,
            )

        second = self.kernel.packaging.build_envelope(
            package_revision_id=package_id,
            publication_intent_id="pubintent-two",
        )
        second_row = self.kernel.packaging.envelope(second)
        self.assertNotEqual(first, second)
        self.assertNotEqual(first_row["canonical_hash"], second_row["canonical_hash"])
        self.assertNotEqual(first_row["canonical_json"], second_row["canonical_json"])

    def test_contract_v2_revalidation_distinguishes_unchanged_package_and_variant_change(self) -> None:
        variant_id, render_id = self._ready_variant()
        short_package = self._build_package(variant_id, render_id, title="Fits v2")
        short_envelope = self.kernel.packaging.build_envelope(
            package_revision_id=short_package,
            publication_intent_id="pubintent-short",
        )
        long_package = self._build_package(variant_id, render_id, title="x" * 90)
        long_envelope = self.kernel.packaging.build_envelope(
            package_revision_id=long_package,
            publication_intent_id="pubintent-long",
        )

        self.kernel.destinations.set_fake_contract_version("dest_fake_1", "fake-v2")
        v2 = self.kernel.destinations.discover_contract("dest_fake_1")
        unchanged = self.kernel.packaging.revalidate_envelope(short_envelope)
        metadata_change = self.kernel.packaging.revalidate_envelope(long_envelope)
        self.assertEqual(unchanged.disposition, "UNCHANGED_VALID")
        self.assertEqual(unchanged.current_contract_fingerprint, v2["fingerprint"])
        self.assertEqual(metadata_change.disposition, "PACKAGE_REQUIRED")

        self.kernel.destinations.set_fake_contract_version("dest_fake_1", "fake-v1")
        square_variant, square_render = self._ready_variant(aspect_ratio="1:1")
        square_package = self._build_package(square_variant, square_render)
        square_envelope = self.kernel.packaging.build_envelope(
            package_revision_id=square_package,
            publication_intent_id="pubintent-square",
        )
        self.kernel.destinations.set_fake_contract_version("dest_fake_1", "fake-v2")
        material_change = self.kernel.packaging.revalidate_envelope(square_envelope)
        self.assertEqual(material_change.disposition, "VARIANT_REQUIRED")

    def test_package_build_returns_variant_required_instead_of_transforming(self) -> None:
        variant_id, render_id = self._ready_variant(aspect_ratio="1:1")
        self.kernel.destinations.set_fake_contract_version("dest_fake_1", "fake-v2")
        self.kernel.destinations.discover_contract("dest_fake_1")
        with self.assertRaises(VariantRequired):
            self._build_package(variant_id, render_id)
        with self.kernel.store.read() as db:
            count = db.execute("SELECT COUNT(*) FROM package_revisions").fetchone()[0]
        self.assertEqual(count, 0)

    def test_package_rejects_render_from_another_ready_variant(self) -> None:
        variant1, render1 = self._ready_variant()
        source_revision_id = str(self.kernel.variants.variant(variant1)["source_revision_id"])
        variant2 = self.kernel.variants.create(
            production_id=self.production_id,
            source_revision_id=source_revision_id,
            parent_variant_id=variant1,
            intent=self._intent("1:1"),
        )
        self._materialize_variant(variant2)
        self.assertEqual(self.kernel.variants.variant(variant2)["state"], "READY")

        with self.assertRaises(InvalidArtifact):
            self.kernel.packaging.build_package(
                variant_id=variant2,
                destination_account_id="dest_fake_1",
                render_artifact_id=render1,
                metadata={"title": "x", "description": "y"},
                settings={"visibility": "public"},
            )

    def test_required_slice9_journal_event_families_are_emitted(self) -> None:
        self.kernel.destinations.discover_contract("dest_fake_1")
        variant_id, render_id = self._ready_variant()
        package_id = self._build_package(variant_id, render_id)
        self.kernel.packaging.build_envelope(
            package_revision_id=package_id,
            publication_intent_id="pubintent-journal",
        )
        with self.kernel.store.read() as db:
            events = {
                str(row[0])
                for row in db.execute(
                    "SELECT event_type FROM journal_entries WHERE event_type IN (?,?,?)",
                    (
                        "DESTINATION_CONTRACT_SNAPSHOTTED",
                        "PACKAGE_CREATED",
                        "ENVELOPE_CREATED",
                    ),
                )
            }
        self.assertEqual(
            events,
            {
                "DESTINATION_CONTRACT_SNAPSHOTTED",
                "PACKAGE_CREATED",
                "ENVELOPE_CREATED",
            },
        )

    def test_persisted_contract_content_package_and_envelope_rows_are_immutable(self) -> None:
        snapshot = self.kernel.destinations.discover_contract("dest_fake_1")
        variant_id, render_id = self._ready_variant()
        package_id = self._build_package(variant_id, render_id)
        envelope_id = self.kernel.packaging.build_envelope(
            package_revision_id=package_id,
            publication_intent_id="pubintent-immutable",
        )
        with self.assertRaises(Exception):
            with self.kernel.store.write() as db:
                db.execute(
                    "UPDATE destination_contract_snapshots SET canonical_json='{}' WHERE id=?",
                    (snapshot["id"],),
                )
        with self.assertRaises(Exception):
            with self.kernel.store.write() as db:
                db.execute(
                    "UPDATE package_revisions SET canonical_hash=? WHERE id=?",
                    ("0" * 64, package_id),
                )
        with self.assertRaises(Exception):
            with self.kernel.store.write() as db:
                db.execute(
                    "UPDATE publication_envelopes SET canonical_hash=? WHERE id=?",
                    ("0" * 64, envelope_id),
                )
