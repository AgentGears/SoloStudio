from __future__ import annotations

SLICE9_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        8,
        """
        CREATE TABLE destination_accounts (
            id TEXT PRIMARY KEY,
            connector_type TEXT NOT NULL,
            display_name TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('ACTIVE','DISABLED','ERROR')),
            credential_ref TEXT NULL,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE destination_contract_snapshots (
            id TEXT PRIMARY KEY,
            destination_account_id TEXT NOT NULL REFERENCES destination_accounts(id),
            fingerprint TEXT NOT NULL,
            canonical_json TEXT NOT NULL,
            discovered_at TEXT NOT NULL,
            valid_until TEXT NULL,
            UNIQUE(destination_account_id, fingerprint)
        );

        CREATE INDEX idx_destination_contract_account
        ON destination_contract_snapshots(destination_account_id, discovered_at);

        CREATE TABLE package_revisions (
            id TEXT PRIMARY KEY,
            production_id TEXT NOT NULL REFERENCES productions(id),
            variant_id TEXT NOT NULL REFERENCES delivery_variants(id),
            destination_account_id TEXT NOT NULL REFERENCES destination_accounts(id),
            destination_contract_id TEXT NOT NULL REFERENCES destination_contract_snapshots(id),
            destination_contract_fingerprint TEXT NOT NULL,
            canonical_json TEXT NOT NULL,
            canonical_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(production_id, canonical_hash)
        );

        CREATE INDEX idx_package_revisions_variant
        ON package_revisions(variant_id, created_at);
        CREATE INDEX idx_package_revisions_destination
        ON package_revisions(destination_account_id, created_at);

        CREATE TABLE publication_envelopes (
            id TEXT PRIMARY KEY,
            package_revision_id TEXT NOT NULL REFERENCES package_revisions(id),
            publication_intent_id TEXT NOT NULL UNIQUE,
            canonical_json TEXT NOT NULL,
            canonical_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );

        CREATE INDEX idx_publication_envelopes_package
        ON publication_envelopes(package_revision_id, created_at);

        CREATE TRIGGER destination_contract_snapshots_immutable
        BEFORE UPDATE ON destination_contract_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'destination contract snapshots are immutable');
        END;

        CREATE TRIGGER package_revisions_lineage_insert
        BEFORE INSERT ON package_revisions
        WHEN NOT EXISTS (
            SELECT 1 FROM delivery_variants v
            WHERE v.id = NEW.variant_id
              AND v.production_id = NEW.production_id
        )
        OR NOT EXISTS (
            SELECT 1 FROM destination_contract_snapshots c
            WHERE c.id = NEW.destination_contract_id
              AND c.destination_account_id = NEW.destination_account_id
              AND c.fingerprint = NEW.destination_contract_fingerprint
        )
        BEGIN
            SELECT RAISE(ABORT, 'package revision lineage violation');
        END;

        CREATE TRIGGER package_revisions_immutable
        BEFORE UPDATE ON package_revisions
        BEGIN
            SELECT RAISE(ABORT, 'package revisions are immutable');
        END;

        CREATE TRIGGER publication_envelopes_immutable
        BEFORE UPDATE ON publication_envelopes
        BEGIN
            SELECT RAISE(ABORT, 'publication envelopes are immutable');
        END;
        """,
    ),
)
