"""Deterministic canonicalization and content digests.

The migration digest ignores:

* JSON object key order;
* the ordering of accounts and of alternative-name entries;

while preserving every string, every type distinction and every account_id
difference. Duplicate object keys, unknown fields and implicit type
conversions are rejected *before* this module is reached (see
:func:`loads_strict`).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .errors import SchemaError

# Fields that may appear inside a migration plan. Anything else is rejected
# so plans cannot smuggle undeclared parameters past the digest.
PLAN_FIELDS = frozenset(
    (
        "tenant_id",
        "source_policy",
        "source_revision",
        "snapshot_hash",
        "accounts",
        "conflict_groups",
        "invalid_accounts",
        "alternatives",
    )
)
ACCOUNT_FIELDS = frozenset(("account_id", "raw_login", "target_login"))
ALTERNATIVE_FIELDS = frozenset(("account_id", "login"))


def loads_strict(text: str | bytes) -> Any:
    """Parse JSON rejecting duplicate object keys.

    ``json.loads`` silently keeps the last duplicate key; such input is
    ambiguous for digest purposes, so it is a schema error here.
    """

    def _check_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: set[str] = set()
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in seen:
                raise SchemaError(
                    f"duplicate JSON object key {key!r}",
                    details={"reason": "duplicate_key", "key": key},
                )
            seen.add(key)
            result[key] = value
        return result

    if isinstance(text, bytes):
        text = text.decode("utf-8")
    return json.loads(text, object_pairs_hook=_check_pairs)


def _check_keys(obj: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise SchemaError(
            f"unknown field(s) {unknown} in {where}",
            details={"reason": "unknown_field", "fields": unknown},
        )


def _require(value: Any, expected: type | tuple[type, ...], name: str) -> None:
    # bool is a subtype of int in Python; keep them distinct on the wire.
    if isinstance(value, bool) and not (
        isinstance(expected, tuple) and bool in expected
    ):
        raise SchemaError(
            f"field {name!r} must not be a boolean",
            details={"reason": "type_mismatch", "field": name},
        )
    if not isinstance(value, expected):
        raise SchemaError(
            f"field {name!r} has wrong type",
            details={"reason": "type_mismatch", "field": name},
        )


def canonicalize_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Validate and return the canonical (order-insensitive) plan shape."""
    _require(plan, dict, "plan")
    _check_keys(plan, PLAN_FIELDS, "plan")
    required = [
        "tenant_id",
        "source_policy",
        "source_revision",
        "snapshot_hash",
        "accounts",
        "conflict_groups",
        "invalid_accounts",
        "alternatives",
    ]
    missing = [f for f in required if f not in plan]
    if missing:
        raise SchemaError(
            f"missing plan field(s) {missing}",
            details={"reason": "missing_field", "fields": missing},
        )

    _require(plan["tenant_id"], str, "tenant_id")
    _require(plan["source_policy"], str, "source_policy")
    _require(plan["source_revision"], int, "source_revision")
    _require(plan["snapshot_hash"], str, "snapshot_hash")

    accounts: list[dict[str, Any]] = []
    for entry in plan["accounts"]:
        _require(entry, dict, "accounts[]")
        _check_keys(entry, ACCOUNT_FIELDS, "accounts[]")
        for field in ACCOUNT_FIELDS:
            if field not in entry:
                raise SchemaError(
                    f"missing account field {field!r}",
                    details={"reason": "missing_field", "field": field},
                )
            _require(entry[field], str, f"accounts[].{field}")
        accounts.append(
            {
                "account_id": entry["account_id"],
                "raw_login": entry["raw_login"],
                "target_login": entry["target_login"],
            }
        )

    groups: list[list[str]] = []
    for group in plan["conflict_groups"]:
        _require(group, list, "conflict_groups[]")
        ids = []
        for account_id in group:
            _require(account_id, str, "conflict_groups[][]")
            ids.append(account_id)
        groups.append(sorted(ids))

    invalid: list[str] = []
    for account_id in plan["invalid_accounts"]:
        _require(account_id, str, "invalid_accounts[]")
        invalid.append(account_id)

    alternatives: list[dict[str, str]] = []
    for entry in plan["alternatives"]:
        _require(entry, dict, "alternatives[]")
        _check_keys(entry, ALTERNATIVE_FIELDS, "alternatives[]")
        for field in ALTERNATIVE_FIELDS:
            if field not in entry:
                raise SchemaError(
                    f"missing alternative field {field!r}",
                    details={"reason": "missing_field", "field": field},
                )
            _require(entry[field], str, f"alternatives[].{field}")
        alternatives.append(
            {"account_id": entry["account_id"], "login": entry["login"]}
        )

    # Order-insensitive canonical shape: account_id ties every list together.
    accounts.sort(key=lambda e: e["account_id"])
    alternatives.sort(key=lambda e: e["account_id"])
    invalid.sort()
    groups.sort(key=lambda g: (g[0] if g else "", len(g), g))

    alt_ids = [e["account_id"] for e in alternatives]
    if len(alt_ids) != len(set(alt_ids)):
        raise SchemaError(
            "duplicate alternative entries for the same account_id",
            details={"reason": "duplicate_alternative"},
        )

    return {
        "tenant_id": plan["tenant_id"],
        "source_policy": plan["source_policy"],
        "source_revision": plan["source_revision"],
        "snapshot_hash": plan["snapshot_hash"],
        "accounts": accounts,
        "conflict_groups": groups,
        "invalid_accounts": invalid,
        "alternatives": alternatives,
    }


def canonical_json(plan: dict[str, Any]) -> str:
    """Stable JSON text: sorted keys, compact separators, no whitespace tricks."""
    return json.dumps(
        canonicalize_plan(plan),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def plan_digest(plan: dict[str, Any]) -> str:
    """SHA-256 hex digest of the canonical plan content."""
    return hashlib.sha256(canonical_json(plan).encode("utf-8")).hexdigest()


def snapshot_hash(entries: list[tuple[str, str]]) -> str:
    """Deterministic fingerprint of an account set (account_id, raw_login)."""
    payload = json.dumps(
        sorted(entries), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
