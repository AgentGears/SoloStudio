from __future__ import annotations

import hashlib
import json
from typing import Any

from solostudio.kernel.errors import InvalidCanonicalValue


def _utf16_sort_key(value: str) -> bytes:
    try:
        return value.encode("utf-16-be")
    except UnicodeEncodeError as exc:
        raise InvalidCanonicalValue("canonical strings must contain valid Unicode scalar values") from exc


def _canonical_structure(value: Any, path: str = "$") -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        _utf16_sort_key(value)
        return value
    if isinstance(value, float):
        raise InvalidCanonicalValue(f"floating-point value is not permitted at {path}")
    if isinstance(value, list):
        return [_canonical_structure(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise InvalidCanonicalValue(f"non-string object key at {path}")
            _utf16_sort_key(key)
        ordered: dict[str, Any] = {}
        for key in sorted(value, key=_utf16_sort_key):
            ordered[key] = _canonical_structure(value[key], f"{path}.{key}")
        return ordered
    raise InvalidCanonicalValue(f"unsupported canonical value {type(value).__name__} at {path}")


def canonical_text(value: Any) -> str:
    return json.dumps(
        _canonical_structure(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=False,
        separators=(",", ":"),
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_text(value).encode("utf-8")


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()
