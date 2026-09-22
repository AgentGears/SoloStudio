from __future__ import annotations

from copy import deepcopy
from typing import Any

from solostudio.kernel.errors import InvalidCommand


class FakeDestinationConnector:
    """Deterministic M0 destination contract source with two compatibility versions."""

    connector_type = "fake-short-video"
    default_account_id = "dest_fake_1"

    _CONTRACTS: dict[str, dict[str, Any]] = {
        "fake-v1": {
            "schema_version": 1,
            "destination": "fake-short-video",
            "supported_content_types": ["video"],
            "text": {"title_max": 100, "description_max": 2200},
            "media": {
                "video": {
                    "accepted_media_types": ["video/mp4"],
                    "max_items": 1,
                    "min_duration_ms": 1000,
                    "max_duration_ms": 90000,
                    "allowed_aspect_ratios": ["9:16", "1:1"],
                }
            },
            "settings_schema": {
                "visibility": {
                    "type": "enum",
                    "values": ["public", "private"],
                    "required": True,
                }
            },
            "dynamic_fields": [],
            "contract_version": "fake-v1",
        },
        "fake-v2": {
            "schema_version": 1,
            "destination": "fake-short-video",
            "supported_content_types": ["video"],
            "text": {"title_max": 80, "description_max": 2200},
            "media": {
                "video": {
                    "accepted_media_types": ["video/mp4"],
                    "max_items": 1,
                    "min_duration_ms": 1000,
                    "max_duration_ms": 90000,
                    "allowed_aspect_ratios": ["9:16"],
                }
            },
            "settings_schema": {
                "visibility": {
                    "type": "enum",
                    "values": ["public", "private"],
                    "required": True,
                }
            },
            "dynamic_fields": [],
            "contract_version": "fake-v2",
        },
    }

    @classmethod
    def get_destination_contract(cls, account_id: str, version: str) -> dict[str, Any]:
        if not isinstance(account_id, str) or not account_id:
            raise InvalidCommand("destination account id must be a non-empty string")
        template = cls._CONTRACTS.get(version)
        if template is None:
            raise InvalidCommand(f"unsupported fake destination contract version: {version}")
        contract = deepcopy(template)
        contract["account_id"] = account_id
        return contract

    @classmethod
    def contract(cls, account_id: str, version: str) -> dict[str, Any]:
        """Compatibility alias; kernel contract discovery uses the connector interface method."""
        return cls.get_destination_contract(account_id, version)

    @classmethod
    def versions(cls) -> tuple[str, ...]:
        return tuple(cls._CONTRACTS)
