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
)
