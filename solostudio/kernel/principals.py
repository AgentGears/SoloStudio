from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PrincipalType(StrEnum):
    USER = "USER"
    AGENT = "AGENT"
    SYSTEM = "SYSTEM"


@dataclass(frozen=True, slots=True)
class Principal:
    type: PrincipalType
    id: str | None


USER_PRINCIPAL = Principal(PrincipalType.USER, "local-user")
AGENT_PRINCIPAL = Principal(PrincipalType.AGENT, "agent-runtime")
SYSTEM_PRINCIPAL = Principal(PrincipalType.SYSTEM, "kernel")
