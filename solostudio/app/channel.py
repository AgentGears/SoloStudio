from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from solostudio.kernel.principals import Principal
from solostudio.kernel.productions.service import CommandResult, ProductionService, RevisionResult


@dataclass(frozen=True, slots=True)
class KernelChannel:
    principal: Principal
    productions: ProductionService

    def command(
        self,
        *,
        production_id: str,
        expected_state_version: int,
        idempotency_key: str,
        action: str,
        command_input: dict[str, Any],
    ) -> CommandResult:
        return self.productions.handle_command(
            principal=self.principal,
            production_id=production_id,
            expected_state_version=expected_state_version,
            idempotency_key=idempotency_key,
            action=action,
            command_input=command_input,
        )

    def capture_revision(
        self,
        *,
        production_id: str,
        expected_state_version: int,
        idempotency_key: str,
    ) -> RevisionResult | CommandResult:
        return self.productions.capture_revision(
            principal=self.principal,
            production_id=production_id,
            expected_state_version=expected_state_version,
            idempotency_key=idempotency_key,
        )
