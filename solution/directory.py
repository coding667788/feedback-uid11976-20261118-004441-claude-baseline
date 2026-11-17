"""Tenant/account directory operations.

Each mutating call runs in one ``BEGIN IMMEDIATE`` transaction and bumps the
tenant ``revision`` exactly once. Read calls (snapshot, batch resolve) run in
a single read transaction so a concurrent migration can never expose a mix of
old and new mappings within one call.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable

from . import db
from .errors import (
    AccountExists,
    AccountNotFound,
    InvalidLogin,
    LoginConflict,
    PolicyMismatch,
    TenantExists,
    TenantNotFound,
)
from .normalization import POLICIES, V1, normalize


class Directory:
    """High-level API over one SQLite database file."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)

    # ---------------------------------------------------------------- tenants
    def create_tenant(self, tenant_id: str, policy: str) -> dict[str, Any]:
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ValueError("tenant_id must be a non-empty string")
        if policy not in POLICIES:
            raise PolicyMismatch(f"policy must be one of {POLICIES}")
        conn = db.connect(self.db_path)
        try:
            with db.transaction(conn):
                exists = conn.execute(
                    "SELECT 1 FROM tenants WHERE tenant_id=?", (tenant_id,)
                ).fetchone()
                if exists:
                    raise TenantExists(f"tenant {tenant_id!r} already exists")
                conn.execute(
                    "INSERT INTO tenants(tenant_id, policy, revision) "
                    "VALUES (?, ?, 0)",
                    (tenant_id, policy),
                )
            return {"tenant_id": tenant_id, "policy": policy, "revision": 0}
        finally:
            conn.close()

    def _load_tenant(self, conn: sqlite3.Connection, tenant_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT tenant_id, policy, revision FROM tenants WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()
        if row is None:
            raise TenantNotFound(f"tenant {tenant_id!r} does not exist")
        return row

    def get_tenant(self, tenant_id: str) -> dict[str, Any]:
        conn = db.connect(self.db_path)
        try:
            row = self._load_tenant(conn, tenant_id)
            return dict(row)
        finally:
            conn.close()

    # ---------------------------------------------------------------- accounts
    def create_account(
        self, tenant_id: str, name: str, account_id: str, raw_login: str
    ) -> dict[str, Any]:
        """Create an account.

        ``name`` is an opaque display-name placeholder kept for API
        symmetry; only the login-name -> account_id mapping is managed, so
        ``name`` is never normalized or stored. Credentials bind to
        ``account_id`` elsewhere and are out of scope here.
        """
        if not isinstance(name, str):
            raise ValueError("name must be a string")
        tenant = self.get_tenant(tenant_id)
        login_name = normalize(raw_login, tenant["policy"])
        conn = db.connect(self.db_path)
        try:
            with db.transaction(conn):
                row = self._load_tenant(conn, tenant_id)
                self._insert_account(conn, tenant_id, account_id, raw_login, login_name)
                new_revision = row["revision"] + 1
                conn.execute(
                    "UPDATE tenants SET revision=? WHERE tenant_id=?",
                    (new_revision, tenant_id),
                )
            return {
                "tenant_id": tenant_id,
                "account_id": account_id,
                "raw_login": raw_login,
                "login_name": login_name,
                "revision": new_revision,
            }
        finally:
            conn.close()

    def rename_account(
        self, tenant_id: str, account_id: str, new_raw_login: str
    ) -> dict[str, Any]:
        tenant = self.get_tenant(tenant_id)
        login_name = normalize(new_raw_login, tenant["policy"])
        conn = db.connect(self.db_path)
        try:
            with db.transaction(conn):
                row = self._load_tenant(conn, tenant_id)
                existing = conn.execute(
                    "SELECT 1 FROM accounts WHERE tenant_id=? AND account_id=?",
                    (tenant_id, account_id),
                ).fetchone()
                if existing is None:
                    raise AccountNotFound(
                        f"account {account_id!r} not found in tenant {tenant_id!r}"
                    )
                clash = conn.execute(
                    "SELECT account_id FROM accounts "
                    "WHERE tenant_id=? AND login_name=? AND account_id<>?",
                    (tenant_id, login_name, account_id),
                ).fetchone()
                if clash is not None:
                    raise LoginConflict(
                        f"login {login_name!r} already belongs to "
                        f"{clash['account_id']!r}",
                        details={"account_id": clash["account_id"]},
                    )
                conn.execute(
                    "UPDATE accounts SET raw_login=?, login_name=? "
                    "WHERE tenant_id=? AND account_id=?",
                    (new_raw_login, login_name, tenant_id, account_id),
                )
                new_revision = row["revision"] + 1
                conn.execute(
                    "UPDATE tenants SET revision=? WHERE tenant_id=?",
                    (new_revision, tenant_id),
                )
            return {
                "tenant_id": tenant_id,
                "account_id": account_id,
                "raw_login": new_raw_login,
                "login_name": login_name,
                "revision": new_revision,
            }
        finally:
            conn.close()

    def delete_account(self, tenant_id: str, account_id: str) -> dict[str, Any]:
        conn = db.connect(self.db_path)
        try:
            with db.transaction(conn):
                row = self._load_tenant(conn, tenant_id)
                cur = conn.execute(
                    "DELETE FROM accounts WHERE tenant_id=? AND account_id=?",
                    (tenant_id, account_id),
                )
                if cur.rowcount == 0:
                    raise AccountNotFound(
                        f"account {account_id!r} not found in tenant {tenant_id!r}"
                    )
                new_revision = row["revision"] + 1
                conn.execute(
                    "UPDATE tenants SET revision=? WHERE tenant_id=?",
                    (new_revision, tenant_id),
                )
            return {
                "tenant_id": tenant_id,
                "account_id": account_id,
                "revision": new_revision,
            }
        finally:
            conn.close()

    @staticmethod
    def _insert_account(
        conn: sqlite3.Connection,
        tenant_id: str,
        account_id: str,
        raw_login: str,
        login_name: str,
    ) -> None:
        dup_id = conn.execute(
            "SELECT 1 FROM accounts WHERE tenant_id=? AND account_id=?",
            (tenant_id, account_id),
        ).fetchone()
        if dup_id is not None:
            raise AccountExists(
                f"account_id {account_id!r} already exists in tenant {tenant_id!r}"
            )
        clash = conn.execute(
            "SELECT account_id FROM accounts WHERE tenant_id=? AND login_name=?",
            (tenant_id, login_name),
        ).fetchone()
        if clash is not None:
            raise LoginConflict(
                f"login {login_name!r} already belongs to {clash['account_id']!r}",
                details={"account_id": clash["account_id"]},
            )
        conn.execute(
            "INSERT INTO accounts(tenant_id, account_id, raw_login, login_name) "
            "VALUES (?, ?, ?, ?)",
            (tenant_id, account_id, raw_login, login_name),
        )

    # ------------------------------------------------------------------- reads
    def list_accounts(self, tenant_id: str) -> list[dict[str, str]]:
        conn = db.connect(self.db_path)
        try:
            self._load_tenant(conn, tenant_id)
            rows = conn.execute(
                "SELECT account_id, raw_login, login_name FROM accounts "
                "WHERE tenant_id=? ORDER BY account_id",
                (tenant_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def snapshot(self, tenant_id: str) -> dict[str, Any]:
        """One consistent read of policy, revision and the full account set."""
        conn = db.connect(self.db_path)
        try:
            conn.execute("BEGIN")
            tenant = self._load_tenant(conn, tenant_id)
            rows = conn.execute(
                "SELECT account_id, raw_login, login_name FROM accounts "
                "WHERE tenant_id=? ORDER BY account_id",
                (tenant_id,),
            ).fetchall()
            entries = [(r["account_id"], r["raw_login"]) for r in rows]
            conn.execute("COMMIT")
            return {
                "tenant_id": tenant_id,
                "policy": tenant["policy"],
                "revision": tenant["revision"],
                "entries": entries,
                "accounts": [dict(r) for r in rows],
            }
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def batch_resolve(
        self, tenant_id: str, raw_logins: list[str], *,
        barrier: Callable[[int], None] | None = None,
    ) -> dict[str, Any]:
        """Resolve many login names against one immutable snapshot revision.

        A single read transaction is opened before any lookup, so a migration
        committed by another process cannot make later queries in the same
        call see the new revision -- callers observe either the complete old
        version or the complete new one.
        """
        conn = db.connect(self.db_path)
        try:
            conn.execute("BEGIN")
            tenant = self._load_tenant(conn, tenant_id)
            policy = tenant["policy"]
            results: list[dict[str, Any]] = []
            for index, raw in enumerate(raw_logins):
                try:
                    login_name = normalize(raw, policy)
                except InvalidLogin as exc:
                    results.append(
                        {
                            "query": raw,
                            "status": "invalid",
                            "reason": exc.details or {"code": exc.error_code},
                        }
                    )
                    continue
                row = conn.execute(
                    "SELECT account_id FROM accounts "
                    "WHERE tenant_id=? AND login_name=?",
                    (tenant_id, login_name),
                ).fetchone()
                results.append(
                    {
                        "query": raw,
                        "login_name": login_name,
                        "account_id": row["account_id"] if row else None,
                        "status": "resolved" if row else "not_found",
                    }
                )
                if barrier is not None:
                    # The test driver parks here holding the read snapshot
                    # open while another process performs the migration.
                    barrier(index)
            conn.execute("COMMIT")
            return {
                "tenant_id": tenant_id,
                "policy": policy,
                "revision": tenant["revision"],
                "results": results,
            }
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
