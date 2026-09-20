from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CostPlan:
    capability: str
    estimated_microunits: int
    reserved_microunits: int
    unit: str = "microunit"

    def __post_init__(self) -> None:
        if type(self.estimated_microunits) is not int or type(self.reserved_microunits) is not int:
            raise ValueError("cost values must be integer microunits")
        if self.estimated_microunits < 0 or self.reserved_microunits < 0:
            raise ValueError("cost values must be non-negative")
        if self.reserved_microunits < self.estimated_microunits:
            raise ValueError("reserved cost cannot be below estimate")
        if not self.capability or not self.unit:
            raise ValueError("capability and unit are required")
