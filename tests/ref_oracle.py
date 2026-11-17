"""Independent reference oracle for the migration tests.

This module deliberately does NOT import :mod:`solution.plan` or
:mod:`solution.directory`. It re-derives V2 targets, conflict groups,
invalid names and final account ownership straight from the specification and
the standard-library ``unicodedata`` module, so test expectations are never
copied from the code under test.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata

MAX_UTF8_BYTES = 128
_FORBIDDEN = frozenset(("Cc", "Cf", "Cs"))


def ref_v2_normalize(raw: str) -> str:
    """Specification transcription: NFKC(NFKC(raw).casefold())."""
    return unicodedata.normalize(
        "NFKC", unicodedata.normalize("NFKC", raw).casefold()
    )


def ref_is_valid(raw: str, target: str | None = None) -> tuple[bool, str]:
    """Same accept/reject rules as the product, evaluated independently."""
    for stage, value in (("raw", raw), ("normalized", target if target is not None else raw)):
        if value == "":
            return False, "empty"
        for ch in value:
            if ch.isspace():
                return False, "whitespace"
            if unicodedata.category(ch) in _FORBIDDEN:
                return False, "forbidden_category"
        if len(value.encode("utf-8")) > MAX_UTF8_BYTES:
            return False, "too_long"
    return True, "ok"


def ref_evaluate(entries: list[tuple[str, str]]) -> dict[str, object]:
    """Expected precheck content for an account set (account_id, raw_login)."""
    valid: list[dict[str, str]] = []
    invalid: list[dict[str, str]] = []
    buckets: dict[str, list[str]] = {}
    for account_id, raw in entries:
        target = ref_v2_normalize(raw)
        ok, reason = ref_is_valid(raw, target)
        if not ok:
            invalid.append({"account_id": account_id, "raw_login": raw, "reason": reason})
            continue
        valid.append(
            {"account_id": account_id, "raw_login": raw, "target_login": target}
        )
        buckets.setdefault(target, []).append(account_id)
    groups = [sorted(ids) for ids in buckets.values() if len(ids) > 1]
    groups.sort(key=lambda g: (g[0], len(g), g))
    valid.sort(key=lambda e: e["account_id"])
    invalid.sort(key=lambda e: e["account_id"])
    return {
        "accounts": valid,
        "invalid_accounts": invalid,
        "conflict_groups": groups,
    }


def ref_final_mapping(
    entries: list[tuple[str, str]], alternatives: dict[str, str]
) -> dict[str, str]:
    """Expected post-commit ``account_id -> V2 login`` ownership table."""
    evaluated = ref_evaluate(entries)
    mapping = {a["account_id"]: a["target_login"] for a in evaluated["accounts"]}
    for account_id, raw_alt in alternatives.items():
        alt_target = ref_v2_normalize(raw_alt)
        ok, _ = ref_is_valid(raw_alt, alt_target)
        if not ok:
            raise AssertionError(f"test bug: alternative for {account_id} invalid")
        mapping[account_id] = alt_target
    targets = list(mapping.values())
    if len(targets) != len(set(targets)):
        raise AssertionError("test bug: expected final mapping still conflicts")
    return dict(sorted(mapping.items()))


def ref_snapshot_hash(entries: list[tuple[str, str]]) -> str:
    payload = json.dumps(
        sorted(entries), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
