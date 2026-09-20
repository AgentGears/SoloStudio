from __future__ import annotations

from pathlib import Path

from solostudio.kernel.artifacts import ArtifactService
from solostudio.kernel.clock import Clock
from solostudio.kernel.costs import CostService
from solostudio.kernel.ids import IdSource
from solostudio.kernel.productions import ProductionService
from solostudio.kernel.store import KernelStore
from solostudio.kernel.jobs.admission import AdmissionMixin
from solostudio.kernel.jobs.execution import ExecutionMixin
from solostudio.kernel.jobs.recovery import RecoveryMixin


class JobService(AdmissionMixin, ExecutionMixin, RecoveryMixin):
    def __init__(
        self,
        data_dir: Path,
        store: KernelStore,
        artifacts: ArtifactService,
        productions: ProductionService,
        costs: CostService,
        clock: Clock,
        ids: IdSource,
    ) -> None:
        self.data_dir = data_dir
        self.store = store
        self.artifacts = artifacts
        self.productions = productions
        self.costs = costs
        self.clock = clock
        self.ids = ids
        self.tmp_root = data_dir / "tmp"
        self.tmp_root.mkdir(parents=True, exist_ok=True)
