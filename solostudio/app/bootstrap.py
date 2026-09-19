from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from solostudio.app.channel import KernelChannel
from solostudio.kernel.clock import Clock, SystemClock
from solostudio.kernel.ids import IdSource, RandomIdSource
from solostudio.kernel.principals import AGENT_PRINCIPAL, SYSTEM_PRINCIPAL, USER_PRINCIPAL
from solostudio.kernel.productions import ProductionService
from solostudio.kernel.store import KernelStore


@dataclass(slots=True)
class StudioKernel:
    data_dir: Path
    store: KernelStore
    productions: ProductionService
    user: KernelChannel
    agent: KernelChannel
    system: KernelChannel

    def close(self) -> None:
        self.store.close()


def bootstrap(data_dir: str | Path, *, clock: Clock | None = None, ids: IdSource | None = None) -> StudioKernel:
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "objects").mkdir(exist_ok=True)
    (root / "backups").mkdir(exist_ok=True)
    active_clock = clock or SystemClock()
    active_ids = ids or RandomIdSource()
    store = KernelStore(root / "studio.db", active_clock)
    productions = ProductionService(store, active_clock, active_ids)
    return StudioKernel(
        root,
        store,
        productions,
        KernelChannel(USER_PRINCIPAL, productions),
        KernelChannel(AGENT_PRINCIPAL, productions),
        KernelChannel(SYSTEM_PRINCIPAL, productions),
    )
