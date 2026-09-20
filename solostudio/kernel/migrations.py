from __future__ import annotations

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );

        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            creation_key TEXT NULL UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE productions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            title TEXT NOT NULL,
            production_type TEXT NOT NULL,
            state_version INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
            latest_captured_revision_id TEXT NULL REFERENCES production_revisions(id),
            status TEXT NOT NULL CHECK (status IN ('ACTIVE','ARCHIVED')),
            creation_key TEXT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(project_id, creation_key)
        );

        CREATE INDEX idx_productions_project ON productions(project_id);

        CREATE TABLE working_states (
            production_id TEXT PRIMARY KEY REFERENCES productions(id),
            schema_version INTEGER NOT NULL,
            state_version INTEGER NOT NULL CHECK (state_version >= 0),
            state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE production_command_receipts (
            id TEXT PRIMARY KEY,
            production_id TEXT NOT NULL REFERENCES productions(id),
            idempotency_key TEXT NOT NULL,
            principal_type TEXT NOT NULL CHECK (principal_type IN ('USER','AGENT','SYSTEM')),
            principal_id TEXT NULL,
            action TEXT NOT NULL,
            command_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('COMMITTED','REJECTED_STALE','REJECTED_INVALID')),
            expected_state_version INTEGER NOT NULL,
            previous_state_version INTEGER NULL,
            resulting_state_version INTEGER NULL,
            affected_json TEXT NOT NULL,
            error_code TEXT NULL,
            error_message TEXT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(production_id, idempotency_key)
        );

        CREATE INDEX idx_command_receipts_production_created
        ON production_command_receipts(production_id, created_at);

        CREATE TABLE production_revisions (
            id TEXT PRIMARY KEY,
            production_id TEXT NOT NULL REFERENCES productions(id),
            sequence INTEGER NOT NULL,
            parent_revision_id TEXT NULL REFERENCES production_revisions(id),
            state_version_at_capture INTEGER NOT NULL,
            schema_version INTEGER NOT NULL,
            canonical_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            UNIQUE(production_id, sequence)
        );

        CREATE INDEX idx_revision_content_hash
        ON production_revisions(production_id, content_hash);

        CREATE TABLE journal_entries (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            production_id TEXT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX idx_journal_production ON journal_entries(production_id, seq);
        CREATE INDEX idx_journal_entity ON journal_entries(entity_type, entity_id, seq);
        """,
    ),
    (
        2,
        """
        CREATE TABLE objects (
            digest_sha256 TEXT PRIMARY KEY,
            byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
            object_relpath TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );

        CREATE TABLE artifacts (
            id TEXT PRIMARY KEY,
            object_digest TEXT NOT NULL REFERENCES objects(digest_sha256),
            production_id TEXT NOT NULL REFERENCES productions(id),
            production_revision_id TEXT NULL REFERENCES production_revisions(id),
            variant_id TEXT NULL,
            kind TEXT NOT NULL,
            media_type TEXT NOT NULL,
            producer_stage TEXT NOT NULL,
            producer_job_id TEXT NULL,
            producer_attempt_id TEXT NULL,
            input_fingerprint TEXT NULL,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX idx_artifacts_production ON artifacts(production_id);
        CREATE INDEX idx_artifacts_revision ON artifacts(production_revision_id);
        CREATE INDEX idx_artifacts_variant ON artifacts(variant_id);
        CREATE INDEX idx_artifacts_fingerprint
        ON artifacts(production_id, kind, input_fingerprint);

        CREATE TABLE artifact_dependencies (
            artifact_id TEXT NOT NULL REFERENCES artifacts(id),
            source_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
            dependency_role TEXT NOT NULL,
            PRIMARY KEY (artifact_id, source_artifact_id, dependency_role),
            CHECK (artifact_id <> source_artifact_id)
        );

        CREATE INDEX idx_artifact_dependencies_source
        ON artifact_dependencies(source_artifact_id);
        """,
    ),
    (
        3,
        """
        CREATE TABLE job_specs (
            id TEXT PRIMARY KEY,
            production_id TEXT NOT NULL REFERENCES productions(id),
            job_class TEXT NOT NULL CHECK (job_class IN ('STATE_PROPOSAL','ARTIFACT')),
            source_state_version INTEGER NULL,
            production_revision_id TEXT NULL REFERENCES production_revisions(id),
            variant_id TEXT NULL,
            job_type TEXT NOT NULL,
            semantic_capability TEXT NOT NULL,
            spec_json TEXT NOT NULL,
            spec_hash TEXT NOT NULL,
            route_json TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','CANCELED')),
            max_attempts INTEGER NOT NULL DEFAULT 2 CHECK (max_attempts >= 1),
            created_at TEXT NOT NULL,
            finished_at TEXT NULL
        );

        CREATE INDEX idx_job_specs_state ON job_specs(state);
        CREATE INDEX idx_job_specs_fingerprint
        ON job_specs(production_id, semantic_capability, input_fingerprint);
        CREATE INDEX idx_job_specs_spec_hash
        ON job_specs(production_id, spec_hash);

        CREATE TABLE attempts (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES job_specs(id),
            attempt_number INTEGER NOT NULL,
            state TEXT NOT NULL CHECK (state IN (
                'CREATED','RUNNING','SUCCEEDED','FAILED','CANCELED','INTERRUPTED'
            )),
            temp_relpath TEXT NOT NULL,
            executor_identity TEXT NULL,
            result_json TEXT NULL,
            started_at TEXT NULL,
            finished_at TEXT NULL,
            error_code TEXT NULL,
            error_message TEXT NULL,
            progress_json TEXT NOT NULL,
            UNIQUE(job_id, attempt_number)
        );

        CREATE INDEX idx_attempts_job ON attempts(job_id, attempt_number);
        CREATE INDEX idx_attempts_state ON attempts(state);
        """,
    ),
    (
        4,
        """
        CREATE TABLE cost_ledger (
            id TEXT PRIMARY KEY,
            production_id TEXT NOT NULL REFERENCES productions(id),
            job_id TEXT NULL REFERENCES job_specs(id),
            capability TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('ESTIMATED','RESERVED','SETTLED','RELEASED','UNKNOWN')),
            estimated_microunits INTEGER NOT NULL DEFAULT 0 CHECK (estimated_microunits >= 0),
            reserved_microunits INTEGER NOT NULL DEFAULT 0 CHECK (reserved_microunits >= 0),
            settled_microunits INTEGER NULL CHECK (settled_microunits IS NULL OR settled_microunits >= 0),
            unit TEXT NOT NULL,
            created_at TEXT NOT NULL,
            settled_at TEXT NULL
        );

        CREATE INDEX idx_cost_ledger_job_state ON cost_ledger(job_id,state);
        CREATE INDEX idx_cost_ledger_production ON cost_ledger(production_id,created_at);
        CREATE UNIQUE INDEX idx_cost_ledger_active_job
        ON cost_ledger(job_id)
        WHERE job_id IS NOT NULL AND state IN ('RESERVED','UNKNOWN');
        """,
    ),
    (
        5,
        """
        CREATE TABLE delivery_variants (
            id TEXT PRIMARY KEY,
            production_id TEXT NOT NULL REFERENCES productions(id),
            source_revision_id TEXT NOT NULL REFERENCES production_revisions(id),
            parent_variant_id TEXT NULL REFERENCES delivery_variants(id),
            variant_type TEXT NOT NULL,
            intent_json TEXT NOT NULL,
            intent_hash TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('PROPOSED','READY','FAILED')),
            created_at TEXT NOT NULL,
            UNIQUE(production_id, source_revision_id, intent_hash)
        );

        CREATE INDEX idx_delivery_variants_revision
        ON delivery_variants(source_revision_id, created_at);
        CREATE INDEX idx_delivery_variants_parent
        ON delivery_variants(parent_variant_id);
        """,
    ),
    (
        6,
        """
        CREATE TRIGGER artifacts_variant_lineage_insert
        BEFORE INSERT ON artifacts
        WHEN NEW.variant_id IS NOT NULL
         AND NOT EXISTS (
            SELECT 1 FROM delivery_variants v
            WHERE v.id = NEW.variant_id
              AND v.production_id = NEW.production_id
              AND NEW.production_revision_id IS NOT NULL
              AND v.source_revision_id = NEW.production_revision_id
         )
        BEGIN
            SELECT RAISE(ABORT, 'artifact variant lineage violation');
        END;

        CREATE TRIGGER artifacts_variant_lineage_update
        BEFORE UPDATE OF variant_id,production_id,production_revision_id ON artifacts
        WHEN NEW.variant_id IS NOT NULL
         AND NOT EXISTS (
            SELECT 1 FROM delivery_variants v
            WHERE v.id = NEW.variant_id
              AND v.production_id = NEW.production_id
              AND NEW.production_revision_id IS NOT NULL
              AND v.source_revision_id = NEW.production_revision_id
         )
        BEGIN
            SELECT RAISE(ABORT, 'artifact variant lineage violation');
        END;

        CREATE TRIGGER job_specs_variant_lineage_insert
        BEFORE INSERT ON job_specs
        WHEN NEW.variant_id IS NOT NULL
         AND NOT EXISTS (
            SELECT 1 FROM delivery_variants v
            WHERE v.id = NEW.variant_id
              AND v.production_id = NEW.production_id
              AND NEW.production_revision_id IS NOT NULL
              AND v.source_revision_id = NEW.production_revision_id
         )
        BEGIN
            SELECT RAISE(ABORT, 'job variant lineage violation');
        END;

        CREATE TRIGGER job_specs_variant_lineage_update
        BEFORE UPDATE OF variant_id,production_id,production_revision_id ON job_specs
        WHEN NEW.variant_id IS NOT NULL
         AND NOT EXISTS (
            SELECT 1 FROM delivery_variants v
            WHERE v.id = NEW.variant_id
              AND v.production_id = NEW.production_id
              AND NEW.production_revision_id IS NOT NULL
              AND v.source_revision_id = NEW.production_revision_id
         )
        BEGIN
            SELECT RAISE(ABORT, 'job variant lineage violation');
        END;

        CREATE TRIGGER delivery_variants_restrict_delete
        BEFORE DELETE ON delivery_variants
        WHEN EXISTS (SELECT 1 FROM artifacts WHERE variant_id = OLD.id)
          OR EXISTS (SELECT 1 FROM job_specs WHERE variant_id = OLD.id)
        BEGIN
            SELECT RAISE(ABORT, 'delivery variant is referenced');
        END;
        """,
    ),
)
