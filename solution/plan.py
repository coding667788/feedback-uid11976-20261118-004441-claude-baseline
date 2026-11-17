"""Precheck reports, deterministic migration plans and atomic commits.

Lifecycle::

    report  = precheck(directory, tenant)
            # binds tenant, source policy, source revision and account set
    plan    = build_plan(report, alternatives)
            # alternatives get full V2 validation; digest ignores ordering
    result  = submit_plan(directory, plan, plan_id)
            # stale snapshot -> PLAN_STALE (nothing written)
            # business error -> whole plan rejected and recorded
            # success        -> policy + every mapping switch in one commit,
            #                   revision bumped exactly once

A ``plan_id`` is permanently bound to its content on the first submit.
Retrying the same (plan_id, content) returns the stored result; reusing a
plan_id with changed content (even after a business rejection) is a
PLAN_ID_CONFLICT.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Callable

from . import db
from .canonical import canonical_json, canonicalize_plan, plan_digest, snapshot_hash
from .directory import Directory
from .errors import (
    PlanIdConflict,
    PlanValidationError,
    PolicyMismatch,
    StaleSnapshot,
)
from .normalization import V1, V2, InvalidLogin, normalize


# --------------------------------------------------------------------- precheck
def _evaluate_v2_targets(
    entries: list[tuple[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, Any]], list[list[str]]]:
    """Independent V2 evaluation of a snapshot: accounts, invalids, conflicts."""
    accounts: list[dict[str, str]] = []
    invalid: list[dict[str, Any]] = []
    by_target: dict[str, list[str]] = defaultdict(list)
    for account_id, raw_login in entries:
        try:
            target = normalize(raw_login, V2)
        except InvalidLogin as exc:
            invalid.append(
                {
                    "account_id": account_id,
                    "raw_login": raw_login,
                    "reason": exc.details or {"code": exc.error_code},
                }
            )
            continue
        accounts.append(
            {
                "account_id": account_id,
                "raw_login": raw_login,
                "target_login": target,
            }
        )
        by_target[target].append(account_id)
    conflict_groups = [
        sorted(ids) for ids in by_target.values() if len(ids) > 1
    ]
    conflict_groups.sort(key=lambda g: (g[0], len(g), g))
    accounts.sort(key=lambda e: e["account_id"])
    invalid.sort(key=lambda e: e["account_id"])
    return accounts, invalid, conflict_groups


def precheck(directory: Directory, tenant_id: str) -> dict[str, Any]:
    """Generate the precheck report bound to one immutable directory snapshot."""
    snap = directory.snapshot(tenant_id)
    if snap["policy"] != V1:
        raise PolicyMismatch(
            f"precheck requires a V1 tenant; tenant {tenant_id!r} uses "
            f"{snap['policy']!r}",
            details={"current_policy": snap["policy"]},
        )
    accounts, invalid, conflict_groups = _evaluate_v2_targets(snap["entries"])
    return {
        "tenant_id": tenant_id,
        "source_policy": V1,
        "source_revision": snap["revision"],
        "snapshot_hash": snapshot_hash(snap["entries"]),
        "accounts": accounts,
        "conflict_groups": conflict_groups,
        "invalid_accounts": invalid,
    }


# ------------------------------------------------------------------------ plan
def build_plan(
    report: dict[str, Any], alternatives: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    """Assemble a plan from a precheck report and operator alternatives."""
    plan = {
        "tenant_id": report["tenant_id"],
        "source_policy": report["source_policy"],
        "source_revision": report["source_revision"],
        "snapshot_hash": report["snapshot_hash"],
        "accounts": report["accounts"],
        "conflict_groups": report["conflict_groups"],
        "invalid_accounts": [e["account_id"] for e in report["invalid_accounts"]],
        "alternatives": list(alternatives or []),
    }
    # Strict field/type validation and order-insensitive canonical shape.
    return canonicalize_plan(plan)


def digest_of(plan: dict[str, Any]) -> str:
    return plan_digest(plan)


# ------------------------------------------------------------- business checks
def _validate_business(plan: dict[str, Any]) -> dict[str, str]:
    """Full V2 validation; return the effective target map or raise."""
    account_ids = {a["account_id"] for a in plan["accounts"]}
    invalid_ids = set(plan["invalid_accounts"])
    # Sanity invariant: invalid accounts are never part of valid account rows.
    if invalid_ids & account_ids:
        raise PlanValidationError(
            "invalid account also appears as a valid account",
            details={"reason": "inconsistent_report"},
        )
    known_ids = account_ids | invalid_ids

    effective: dict[str, str] = {
        a["account_id"]: a["target_login"] for a in plan["accounts"]
    }

    alt_accounts: set[str] = set()
    for alt in plan["alternatives"]:
        account_id = alt["account_id"]
        if account_id not in known_ids:
            raise PlanValidationError(
                f"alternative targets unknown account_id {account_id!r}",
                details={"reason": "unknown_account", "account_id": account_id},
            )
        if account_id in alt_accounts:  # canonicalizer also guards this
            raise PlanValidationError(
                f"duplicate alternative for {account_id!r}",
                details={"reason": "duplicate_alternative", "account_id": account_id},
            )
        alt_accounts.add(account_id)
        try:
            # Alternatives are validated by the *target* policy, in full.
            alt_login = normalize(alt["login"], V2)
        except InvalidLogin as exc:
            raise PlanValidationError(
                f"alternative for {account_id!r} fails V2 validation",
                details={
                    "reason": "invalid_alternative",
                    "account_id": account_id,
                    "validation": exc.details or {"code": exc.error_code},
                },
            ) from exc
        effective[account_id] = alt_login

    # Every invalid V2 name must get an alternative -- the account itself is
    # never merged away and its credential stays bound to its account_id.
    unresolved = sorted(invalid_ids - alt_accounts)
    if unresolved:
        raise PlanValidationError(
            "accounts with invalid V2 names have no alternative login",
            details={"reason": "missing_alternative", "account_ids": unresolved},
        )

    # One final, conflict-free target set across the whole tenant.
    seen: dict[str, str] = {}
    for account_id in sorted(effective):
        target = effective[account_id]
        if target in seen:
            raise PlanValidationError(
                f"V2 target {target!r} is claimed by both {seen[target]!r} "
                f"and {account_id!r}",
                details={
                    "reason": "target_conflict",
                    "target_login": target,
                    "account_ids": sorted((seen[target], account_id)),
                },
            )
        seen[target] = account_id
    return effective


# -------------------------------------------------------------------- submit
def submit_plan(
    directory: Directory,
    plan: dict[str, Any],
    plan_id: str,
    *,
    pre_commit_barrier: Callable[[], None] | None = None,
    post_commit_barrier: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Validate and atomically apply a migration plan (idempotent by plan_id)."""
    if not isinstance(plan_id, str) or not plan_id:
        raise ValueError("plan_id must be a non-empty string")
    canonical = canonicalize_plan(plan)
    digest = plan_digest(canonical)

    conn = db.connect(directory.db_path)
    try:
        # 1) Idempotency / plan_id binding check.
        stored = conn.execute(
            "SELECT plan_id, digest, status, result_json FROM plans "
            "WHERE plan_id=?",
            (plan_id,),
        ).fetchone()
        if stored is not None:
            if stored["digest"] != digest:
                raise PlanIdConflict(
                    f"plan_id {plan_id!r} is already bound to different content",
                    details={
                        "reason": "plan_id_reused",
                        "stored_digest": stored["digest"],
                        "submitted_digest": digest,
                    },
                )
            # Same content: replay the original outcome, never re-apply.
            return {
                **json.loads(stored["result_json"]),
                "replayed": True,
            }

        # 2) Re-verify the snapshot is still current (revision + policy + set).
        tenant = conn.execute(
            "SELECT policy, revision FROM tenants WHERE tenant_id=?",
            (canonical["tenant_id"],),
        ).fetchone()
        if tenant is None:
            from .errors import TenantNotFound

            raise TenantNotFound(f"tenant {canonical['tenant_id']!r} does not exist")
        if tenant["policy"] != canonical["source_policy"]:
            raise StaleSnapshot(
                "tenant policy changed after precheck",
                details={
                    "reason": "policy_changed",
                    "stored_policy": tenant["policy"],
                    "plan_policy": canonical["source_policy"],
                },
            )
        if tenant["revision"] != canonical["source_revision"]:
            raise StaleSnapshot(
                "directory revision changed after precheck",
                details={
                    "reason": "revision_changed",
                    "current_revision": tenant["revision"],
                    "plan_revision": canonical["source_revision"],
                },
            )
        rows = conn.execute(
            "SELECT account_id, raw_login FROM accounts "
            "WHERE tenant_id=? ORDER BY account_id",
            (canonical["tenant_id"],),
        ).fetchall()
        current_hash = snapshot_hash(
            [(r["account_id"], r["raw_login"]) for r in rows]
        )
        if current_hash != canonical["snapshot_hash"]:
            raise StaleSnapshot(
                "account set changed after precheck (add/rename/delete)",
                details={
                    "reason": "account_set_changed",
                    "current_hash": current_hash,
                    "plan_hash": canonical["snapshot_hash"],
                },
            )

        # 3) Full V2 business validation (alternatives, invalids, conflicts).
        try:
            effective = _validate_business(canonical)
        except PlanValidationError as exc:
            result = {
                "status": "rejected",
                "plan_id": plan_id,
                "tenant_id": canonical["tenant_id"],
                "error": exc.to_dict(),
            }
            with db.transaction(conn):
                conn.execute(
                    "INSERT INTO plans(plan_id, tenant_id, digest, status, "
                    "result_json, source_revision) VALUES (?, ?, ?, 'rejected', ?, ?)",
                    (
                        plan_id,
                        canonical["tenant_id"],
                        digest,
                        json.dumps(result, ensure_ascii=False),
                        canonical["source_revision"],
                    ),
                )
            return {**result, "replayed": False}

        # 4) Atomic switch: policy V1->V2 and all mappings in one commit,
        #    revision incremented exactly once. Delete+insert lets two
        #    accounts legally swap login names without mid-statement UNIQUE
        #    conflicts.
        with db.transaction(conn):
            if pre_commit_barrier is not None:
                pre_commit_barrier()
            tenant_id = canonical["tenant_id"]
            raw_by_id = {a["account_id"]: a["raw_login"] for a in canonical["accounts"]}
            raw_by_id.update(
                {
                    alt["account_id"]: alt["login"]
                    for alt in canonical["alternatives"]
                }
            )
            conn.execute(
                "DELETE FROM accounts WHERE tenant_id=?", (tenant_id,)
            )
            for account_id in sorted(effective):
                conn.execute(
                    "INSERT INTO accounts(tenant_id, account_id, raw_login, "
                    "login_name) VALUES (?, ?, ?, ?)",
                    (
                        tenant_id,
                        account_id,
                        raw_by_id[account_id],
                        effective[account_id],
                    ),
                )
            new_revision = tenant["revision"] + 1
            conn.execute(
                "UPDATE tenants SET policy='V2', revision=? WHERE tenant_id=?",
                (new_revision, tenant_id),
            )
            result = {
                "status": "committed",
                "plan_id": plan_id,
                "tenant_id": tenant_id,
                "source_revision": canonical["source_revision"],
                "new_revision": new_revision,
                "mapping": [
                    {"account_id": aid, "login_name": effective[aid]}
                    for aid in sorted(effective)
                ],
            }
            conn.execute(
                "INSERT INTO plans(plan_id, tenant_id, digest, status, "
                "result_json, source_revision) VALUES (?, ?, ?, 'committed', ?, ?)",
                (
                    plan_id,
                    tenant_id,
                    digest,
                    json.dumps(result, ensure_ascii=False),
                    canonical["source_revision"],
                ),
            )
        if post_commit_barrier is not None:
            post_commit_barrier()
        return {**result, "replayed": False}
    finally:
        conn.close()


def get_plan(directory: Directory, plan_id: str) -> dict[str, Any]:
    conn = db.connect(directory.db_path)
    try:
        row = conn.execute(
            "SELECT plan_id, tenant_id, digest, status, result_json, "
            "source_revision FROM plans WHERE plan_id=?",
            (plan_id,),
        ).fetchone()
        if row is None:
            from .errors import PlanNotFound

            raise PlanNotFound(f"no plan with id {plan_id!r}")
        return {
            "plan_id": row["plan_id"],
            "tenant_id": row["tenant_id"],
            "digest": row["digest"],
            "status": row["status"],
            "source_revision": row["source_revision"],
            "result": json.loads(row["result_json"]),
        }
    finally:
        conn.close()
