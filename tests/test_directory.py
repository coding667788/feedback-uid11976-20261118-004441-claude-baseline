"""Tests for storage, tenant/account operations and revision accounting."""

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from solution import db  # noqa: E402
from solution.directory import Directory  # noqa: E402
from solution.errors import (  # noqa: E402
    AccountExists,
    AccountNotFound,
    InvalidLogin,
    LoginConflict,
    TenantExists,
    TenantNotFound,
    UnidataVersionMismatch,
)
from solution.normalization import V1, V2  # noqa: E402
from tests.helpers import (  # noqa: E402
    E_ACUTE_DEC,
    E_ACUTE_PRE,
    FULLWIDTH_A,
    IDEOGRAPHIC_SPACE,
    SOFT_HYPHEN,
    TempWorkspace,
)


class DirectoryTest(unittest.TestCase):
    def setUp(self):
        self.ws = TempWorkspace()
        self.db_path = self.ws.path("dir.db")
        db.init_db(self.db_path)
        self.directory = Directory(self.db_path)

    def tearDown(self):
        self.ws.cleanup()

    # ------------------------------------------------------------- storage
    def test_uninitialized_database_is_rejected(self):
        fresh = self.ws.path("empty.db")
        with self.assertRaises(db.DatabaseNotInitialized):
            Directory(fresh).get_tenant("t1")

    def test_unidata_version_guard_refuses_reads_and_writes(self):
        # Simulate a database produced by an interpreter with another Unicode
        # database release; every guarded connection must refuse to open.
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE meta SET value='0.0.0-fake' WHERE key='unidata_version'"
        )
        conn.commit()
        conn.close()
        with self.assertRaises(UnidataVersionMismatch):
            self.directory.get_tenant("t1")
        with self.assertRaises(UnidataVersionMismatch):
            db.init_db(self.db_path)
        # After restoring the real version, access works again.
        import unicodedata

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE meta SET value=? WHERE key='unidata_version'",
            (unicodedata.unidata_version,),
        )
        conn.commit()
        conn.close()
        self.directory.create_tenant("t1", V1)
        self.assertEqual(self.directory.get_tenant("t1")["policy"], V1)

    def test_init_db_is_idempotent(self):
        db.init_db(self.db_path)  # second run must not duplicate metadata
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0]
        conn.close()
        self.assertEqual(count, 2)

    # ------------------------------------------------------------- tenants
    def test_tenant_lifecycle_and_duplicate(self):
        self.assertEqual(
            self.directory.create_tenant("t1", V1),
            {"tenant_id": "t1", "policy": V1, "revision": 0},
        )
        with self.assertRaises(TenantExists):
            self.directory.create_tenant("t1", V1)
        with self.assertRaises(TenantNotFound):
            self.directory.get_tenant("ghost")

    # ------------------------------------------------------------- accounts
    def test_create_account_applies_v1_normalization_and_bumps_revision(self):
        self.directory.create_tenant("t1", V1)
        result = self.directory.create_account("t1", "display-1", "a1", E_ACUTE_DEC)
        self.assertEqual(result["login_name"], E_ACUTE_PRE)
        self.assertEqual(result["revision"], 1)
        again = self.directory.create_account("t1", "display-2", "a2", "john")
        self.assertEqual(again["revision"], 2)
        stored = self.directory.list_accounts("t1")
        self.assertEqual(
            stored,
            [
                {
                    "account_id": "a1",
                    "raw_login": E_ACUTE_DEC,
                    "login_name": E_ACUTE_PRE,
                },
                {"account_id": "a2", "raw_login": "john", "login_name": "john"},
            ],
        )

    def test_create_account_rejects_duplicate_id_and_conflicting_login(self):
        self.directory.create_tenant("t1", V1)
        self.directory.create_account("t1", "display-1", "a1", "john")
        with self.assertRaises(AccountExists):
            self.directory.create_account("t1", "display-1b", "a1", "mary")
        # V1 is case-sensitive and width-sensitive: these are distinct names.
        self.directory.create_account("t1", "display-2", "a2", "John")
        self.directory.create_account(
            "t1", "display-3", "a3", FULLWIDTH_A + "bc"
        )
        # But an NFC-equivalent decomposed spelling collides.
        self.directory.create_account(
            "t1", "display-4", "a4", E_ACUTE_PRE
        )
        self.directory.create_account(
            "t1", "display-5", "a5", E_ACUTE_PRE + "x"
        )
        with self.assertRaises(LoginConflict):
            self.directory.create_account(
                "t1", "display-4b", "a4b", E_ACUTE_DEC
            )
        with self.assertRaises(LoginConflict):
            self.directory.create_account(
                "t1", "display-6", "a6", E_ACUTE_DEC + "x"
            )

    def test_invalid_login_is_rejected_before_touching_revision(self):
        self.directory.create_tenant("t1", V1)
        with self.assertRaises(InvalidLogin):
            self.directory.create_account(
                "t1", "display-1", "a1", "bad" + IDEOGRAPHIC_SPACE
            )
        with self.assertRaises(InvalidLogin):
            self.directory.create_account(
                "t1", "display-1", "a1", "x" + SOFT_HYPHEN + "y"
            )
        self.assertEqual(self.directory.get_tenant("t1")["revision"], 0)
        self.assertEqual(self.directory.list_accounts("t1"), [])

    def test_rename_and_delete(self):
        self.directory.create_tenant("t1", V1)
        self.directory.create_account("t1", "display-1", "a1", "john")
        self.directory.create_account("t1", "display-2", "a2", "mary")
        moved = self.directory.rename_account("t1", "a1", "johnny")
        self.assertEqual(moved["login_name"], "johnny")
        self.assertEqual(moved["revision"], 3)
        with self.assertRaises(LoginConflict):
            self.directory.rename_account("t1", "a1", "mary")
        with self.assertRaises(AccountNotFound):
            self.directory.rename_account("t1", "ghost", "z")
        deleted = self.directory.delete_account("t1", "a2")
        self.assertEqual(deleted["revision"], 4)
        with self.assertRaises(AccountNotFound):
            self.directory.delete_account("t1", "a2")
        remaining = [a["account_id"] for a in self.directory.list_accounts("t1")]
        self.assertEqual(remaining, ["a1"])

    def test_rename_into_freed_name_is_allowed(self):
        self.directory.create_tenant("t1", V1)
        self.directory.create_account("t1", "display-1", "a1", "john")
        self.directory.create_account("t1", "display-2", "a2", "mary")
        self.directory.delete_account("t1", "a1")
        moved = self.directory.rename_account("t1", "a2", "john")
        self.assertEqual(moved["login_name"], "john")

    def test_tenants_are_isolated_and_may_share_login_names(self):
        self.directory.create_tenant("t1", V1)
        self.directory.create_tenant("t2", V2)
        self.directory.create_account("t1", "display-1", "a1", "john")
        # Same normalized login in another tenant is fine.
        self.directory.create_account("t2", "display-1", "a1", "JOHN")
        t1 = self.directory.batch_resolve("t1", ["john", "JOHN"])
        t2 = self.directory.batch_resolve("t2", ["john", "JOHN"])
        self.assertEqual([r["account_id"] for r in t1["results"]], ["a1", None])
        self.assertEqual([r["account_id"] for r in t2["results"]], ["a1", "a1"])
        self.assertEqual(t1["revision"], 1)
        self.assertEqual(t2["revision"], 1)

    def test_batch_resolve_reports_invalid_and_not_found(self):
        self.directory.create_tenant("t1", V1)
        self.directory.create_account("t1", "display-1", "a1", "john")
        result = self.directory.batch_resolve(
            "t1", ["john", "nobody", "bad" + IDEOGRAPHIC_SPACE]
        )
        self.assertEqual(
            [r["status"] for r in result["results"]],
            ["resolved", "not_found", "invalid"],
        )
        self.assertEqual(result["results"][0]["account_id"], "a1")
        self.assertIsNone(result["results"][1]["account_id"])
        self.assertEqual(result["results"][2]["reason"]["reason"], "whitespace")

    def test_snapshot_captures_policy_revision_and_entries(self):
        self.directory.create_tenant("t1", V1)
        self.directory.create_account("t1", "display-1", "a1", "john")
        snap = self.directory.snapshot("t1")
        self.assertEqual(snap["policy"], V1)
        self.assertEqual(snap["revision"], 1)
        self.assertEqual(snap["entries"], [("a1", "john")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
