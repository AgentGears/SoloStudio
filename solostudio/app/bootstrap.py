from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from solostudio.app.channel import KernelChannel
from solostudio.kernel.artifacts import ObjectStore
from solostudio.kernel.backup import BackupService
from solostudio.kernel.capabilities import CapabilityService
from solostudio.kernel.clock import Clock, SystemClock
from solostudio.kernel.costs import CostService
from solostudio.kernel.derivations import DerivationArtifactService, DerivationService
from solostudio.kernel.derivations.final_authority import Slice8ArtifactAuthorityService
from solostudio.kernel.ids import IdSource, RandomIdSource
from solostudio.kernel.jobs import JobService, SupervisedMediaWorker
from solostudio.kernel.principals import AGENT_PRINCIPAL, SYSTEM_PRINCIPAL, USER_PRINCIPAL
from solostudio.kernel.productions import ProductionService
from solostudio.kernel.store import KernelStore
from solostudio.kernel.variants import VariantPipelineService, VariantService


@dataclass(slots=True)
class StudioKernel:
    data_dir: Path
    store: KernelStore
    objects: ObjectStore
    artifacts: DerivationArtifactService
    backups: BackupService
    productions: ProductionService
    costs: CostService
    jobs: JobService
    capabilities: CapabilityService
    derivations: DerivationService
    variants: VariantService
    variant_pipeline: VariantPipelineService
    worker: SupervisedMediaWorker
    user: KernelChannel
    agent: KernelChannel
    system: KernelChannel

    def close(self) -> None:
        self.store.close()


def bootstrap(data_dir: str | Path, *, clock: Clock | None = None, ids: IdSource | None = None) -> StudioKernel:
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    for name in ("db", "objects", "object-tmp", "tmp", "backups", "exports", "logs", "runtime"):
        (root / name).mkdir(parents=True, exist_ok=True)

    active_clock = clock or SystemClock()
    active_ids = ids or RandomIdSource()
    _prepare_database_layout(root)

    store = KernelStore(root / "db" / "studio.db", active_clock)
    objects = ObjectStore(root)
    artifacts = Slice8ArtifactAuthorityService(store, objects, active_clock, active_ids)
    productions = ProductionService(store, active_clock, active_ids, artifacts)
    costs = CostService(store, active_clock, active_ids)
    jobs = JobService(root, store, artifacts, productions, costs, active_clock, active_ids)
    jobs.recover_startup()
    costs.recover_unbound_reservations()
    capabilities = CapabilityService(productions, jobs)
    derivations = DerivationService(productions, artifacts, jobs, capabilities.router)
    variants = VariantService(store, active_clock, active_ids)
    variant_pipeline = VariantPipelineService(productions, variants, derivations, artifacts, jobs)
    worker = SupervisedMediaWorker(jobs)
    backups = BackupService(root, store, objects, active_clock, active_ids)
    return StudioKernel(
        root,
        store,
        objects,
        artifacts,
        backups,
        productions,
        costs,
        jobs,
        capabilities,
        derivations,
        variants,
        variant_pipeline,
        worker,
        KernelChannel(USER_PRINCIPAL, productions),
        KernelChannel(AGENT_PRINCIPAL, productions),
        KernelChannel(SYSTEM_PRINCIPAL, productions),
    )


def _prepare_database_layout(root: Path) -> None:
    legacy = root / "studio.db"
    target = root / "db" / "studio.db"
    if legacy.exists() and target.exists():
        raise RuntimeError("database layout is ambiguous: both legacy and current locations exist")
    if not legacy.exists():
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        source = root / f"studio.db{suffix}"
        if source.exists():
            os.replace(source, target.parent / source.name)
