"""Multi-process concurrency and crash-recovery tests.

These tests spawn at least three independent OS processes sharing one SQLite
database and synchronize at fixed barriers:

* plan generation interleaved with a new account write;
* two plans competing to commit;
* a batch resolve interleaved with the rule switch (old vs new snapshot);
* worker termination before commit and after commit but before the response.

Expected final ownership is always taken from tests/ref_oracle.py.
"""

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from solution import db  # noqa: E402
from solution.directory import Directory  # noqa: E402
from solution.plan import build_plan, precheck, submit_plan  # noqa: E402
from solution.normalization import V1, V2  # noqa: E402
from tests import ref_oracle  # noqa: E402
from tests.helpers import FULLWIDTH_A, TempWorkspace  # noqa: E402
from tests.process_util import WorkerProcess  # noqa: E402


def seed_v1_tenant(directory: Directory, tenant: str = "t1"):
    """Two V1-distinct accounts that collide under V2, plus a quiet account."""
    directory.create_tenant(tenant, V1)
    directory.create_account(tenant, "d-a1", "a1", "ABC")
    directory.create_account(tenant, "d-a2", "a2", FULLWIDTH_A + "BC")
    directory.create_account(tenant, "d-a3", "a3", "quiet")


def alternatives() -> list[dict]:
    return [
        {"account_id": "a1", "login": "alpha"},
        {"account_id": "a2", "login": "bravo"},
    ]


def expected_ownership(entries, alts: dict):
    return ref_oracle.ref_final_mapping(entries, alts)


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.ws = TempWorkspace()
        self.db_path = self.ws.path("shared.db")
        db.init_db(self.db_path)
        self.directory = Directory(self.db_path)
        seed_v1_tenant(self.directory)
        self.entries = [
            ("a1", "ABC"),
            ("a2", FULLWIDTH_A + "BC"),
            ("a3", "quiet"),
        ]

    def tearDown(self):
        self.ws.cleanup()

    # ------------------- scenario 1: precheck interleaved with account write
    def test_precheck_interleaved_with_new_account_write(self):
        # Process A generates the precheck against the current snapshot;
        # process B inserts an account while A is still before submit. The
        # plan must then be stale and must not overwrite B's account.
        worker_a = WorkerProcess(
            {"kind": "precheck", "db": self.db_path, "tenant_id": "t1"},
            self.ws.path("work"),
            "prechecker",
        ).start()
        report = worker_a.read_result()["result"]
        self.assertEqual(report["source_revision"], 3)

        # Process B (driven directly, but as a separate connection) writes.
        self.directory.create_account("t1", "d-a4", "a4", "latecomer")
        self.assertEqual(self.directory.get_tenant("t1")["revision"], 4)

        plan = build_plan(report, alternatives())
        from solution.errors import StaleSnapshot

        with self.assertRaises(StaleSnapshot):
            submit_plan(self.directory, plan, "stale-plan")
        # B's account is intact and the policy is still V1.
        ids = {a["account_id"] for a in self.directory.list_accounts("t1")}
        self.assertEqual(ids, {"a1", "a2", "a3", "a4"})
        self.assertEqual(self.directory.get_tenant("t1")["policy"], V1)
        self.assertEqual(worker_a.join(), 0)

    # ------------------- scenario 2: two competing plans, one winner
    def test_two_plans_compete_only_one_commits_one_revision_bump(self):
        # Both workers hold a precheck at revision 3. Worker 1 parks inside
        # its write transaction (pre-commit barrier); worker 2 tries to
        # commit concurrently and must block on the database lock rather than
        # corrupt state. When worker 1 commits, worker 2's re-check sees the
        # changed revision and fails stale.
        plan_request = {
            "kind": "submit_plan",
            "db": self.db_path,
            "tenant_id": "t1",
            "plan_id": "competitor-1",
            "alternatives": alternatives(),
        }
        worker1 = WorkerProcess(plan_request, self.ws.path("work"), "w1").start()
        worker1.wait_signal("pre_commit")

        # Worker 2 starts while worker 1 holds the write lock.
        plan_request2 = dict(plan_request, plan_id="competitor-2",
                             alternatives=[{"account_id": "a1", "login": "one"},
                                           {"account_id": "a2", "login": "two"}])
        worker2 = WorkerProcess(plan_request2, self.ws.path("work"), "w2").start()
        # Give worker2 time to block on BEGIN IMMEDIATE / stale re-check.
        time.sleep(1.0)
        self.assertTrue(worker2.is_alive())

        worker1.release("pre_commit")
        result1 = worker1.read_result()
        self.assertEqual(worker1.join(), 0)
        self.assertEqual(result1["result"]["status"], "committed")
        self.assertEqual(result1["result"]["new_revision"], 4)

        rc2 = worker2.join()
        result2 = worker2.read_result()
        # Worker 2 must not have applied anything: either stale rejection,
        # or a lock-busy failure -- never a successful second migration.
        self.assertNotEqual(
            (result2.get("ok"), result2.get("result", {}).get("status")),
            (True, "committed"),
        )
        if result2.get("ok"):
            self.fail(f"worker2 unexpectedly succeeded: {result2}")
        self.assertIn(
            result2["error"]["code"],
            ("PLAN_STALE", "PLAN_VALIDATION_FAILED"),
        )
        self.assertEqual(rc2, 1)

        tenant = self.directory.get_tenant("t1")
        self.assertEqual(tenant["policy"], V2)
        self.assertEqual(tenant["revision"], 4)  # exactly one revision bump
        ownership = {a["account_id"]: a["login_name"]
                     for a in self.directory.list_accounts("t1")}
        self.assertEqual(
            ownership,
            expected_ownership(
                self.entries, {"a1": "alpha", "a2": "bravo"}
            ),
        )

    # ------------------- scenario 3: batch resolve vs rule switch
    def test_batch_resolve_sees_one_complete_revision(self):
        # The resolver opens a read snapshot before the switch and parks
        # between its two lookups while another process commits the V2
        # migration. Every row in that resolve call must reflect the old V1
        # revision; a second resolve started afterwards must see only V2.
        old_queries = ["ABC", FULLWIDTH_A + "BC"]
        resolver = WorkerProcess(
            {
                "kind": "batch_resolve",
                "db": self.db_path,
                "tenant_id": "t1",
                "raw_logins": old_queries,
            },
            self.ws.path("work"),
            "resolver",
        ).start()
        resolver.wait_signal("resolve_0")  # snapshot held after first lookup

        report = precheck(self.directory, "t1")
        plan = build_plan(report, alternatives())
        commit = submit_plan(self.directory, plan, "switch-plan")
        self.assertEqual(commit["status"], "committed")

        resolver.release("resolve_0")
        rc = resolver.join()
        self.assertEqual(rc, 0)
        old_view = resolver.read_result()["result"]
        # Whole call reports the revision it started on, with V1 semantics:
        # 'ABC' -> a1, fullwidth -> a2 (distinct under V1).
        self.assertEqual(old_view["revision"], 3)
        self.assertEqual(old_view["policy"], V1)
        self.assertEqual(
            [r["account_id"] for r in old_view["results"]], ["a1", "a2"]
        )

        # A fresh resolve only sees the new revision, no historical aliases.
        new_view = self.directory.batch_resolve("t1", old_queries + ["alpha"])
        self.assertEqual(new_view["revision"], 4)
        self.assertEqual(new_view["policy"], V2)
        self.assertEqual(
            [r["account_id"] for r in new_view["results"]],
            [None, None, "a1"],
        )

    # ------------------- scenario 4a: kill BEFORE commit
    def test_kill_before_commit_leaves_no_half_migration(self):
        worker = WorkerProcess(
            {
                "kind": "submit_plan",
                "db": self.db_path,
                "tenant_id": "t1",
                "plan_id": "doomed-precommit",
                "alternatives": alternatives(),
            },
            self.ws.path("work"),
            "victim-pre",
        ).start()
        worker.wait_signal("pre_commit")  # inside the write transaction
        self.assertTrue(worker.is_alive())
        worker.terminate()
        self.assertFalse(worker.is_alive())

        # Restart visibility: a new connection must observe the complete old
        # version, with no partial mapping changes and no revision bump.
        fresh = Directory(self.db_path)
        tenant = fresh.get_tenant("t1")
        self.assertEqual(tenant["policy"], V1)
        self.assertEqual(tenant["revision"], 3)
        ownership = {a["account_id"]: a["login_name"]
                     for a in fresh.list_accounts("t1")}
        self.assertEqual(ownership, {"a1": "ABC", "a2": FULLWIDTH_A + "BC",
                                     "a3": "quiet"})
        # The plan_id was never recorded, so a later retry is a fresh attempt.
        report = precheck(fresh, "t1")
        plan = build_plan(report, alternatives())
        result = submit_plan(fresh, plan, "doomed-precommit")
        self.assertEqual(result["status"], "committed")
        self.assertEqual(result["new_revision"], 4)

    # ------------------- scenario 4b: kill AFTER commit, before response
    def test_kill_after_commit_before_response_is_idempotent_on_retry(self):
        # Persist the exact plan content before the worker starts so the
        # retry can resubmit byte-identical content (the plan_id is bound to
        # its digest, not to the process that first sent it).
        report = precheck(self.directory, "t1")
        plan_content = build_plan(report, alternatives())
        plan_file = self.ws.path("plan-content.json")
        with open(plan_file, "w", encoding="utf-8") as fh:
            json.dump(plan_content, fh, ensure_ascii=False)

        worker = WorkerProcess(
            {
                "kind": "submit_plan",
                "db": self.db_path,
                "tenant_id": "t1",
                "plan_id": "doomed-postcommit",
                "plan": plan_content,
            },
            self.ws.path("work"),
            "victim-post",
        ).start()
        worker.wait_signal("post_commit")  # COMMIT already durable
        worker.terminate()

        fresh = Directory(self.db_path)
        # The migration is fully visible: V2, exactly one revision bump.
        tenant = fresh.get_tenant("t1")
        self.assertEqual(tenant["policy"], V2)
        self.assertEqual(tenant["revision"], 4)
        ownership = {a["account_id"]: a["login_name"]
                     for a in fresh.list_accounts("t1")}
        self.assertEqual(
            ownership,
            expected_ownership(
                self.entries, {"a1": "alpha", "a2": "bravo"}
            ),
        )

        from solution.plan import get_plan

        stored = get_plan(fresh, "doomed-postcommit")
        self.assertEqual(stored["status"], "committed")

        # A new process retries the identical content with the same plan_id.
        with open(plan_file, encoding="utf-8") as fh:
            identical_plan = json.load(fh)
        replayer = WorkerProcess(
            {
                "kind": "submit_plan",
                "db": self.db_path,
                "tenant_id": "t1",
                "plan_id": "doomed-postcommit",
                "plan": identical_plan,
            },
            self.ws.path("work"),
            "replayer",
        ).start()
        outcome = replayer.read_result()
        self.assertEqual(replayer.join(), 0)
        self.assertTrue(outcome["ok"])
        self.assertTrue(outcome["result"]["replayed"])
        # Replay must not apply a second time.
        self.assertEqual(fresh.get_tenant("t1")["revision"], 4)
        self.assertEqual(
            {a["account_id"]: a["login_name"]
             for a in fresh.list_accounts("t1")},
            ownership,
        )


class ThreeProcessIsolationTest(unittest.TestCase):
    def test_three_workers_share_one_database_without_cross_tenant_leak(self):
        # A third tenant on the same database proves isolation under load.
        ws = TempWorkspace()
        try:
            db_path = ws.path("three.db")
            db.init_db(db_path)
            directory = Directory(db_path)
            seed_v1_tenant(directory, "t1")
            directory.create_tenant("t-other", V1)
            directory.create_account("t-other", "d-o1", "o1", "ABC")

            # Migrate t1 in a worker while t-other keeps mutating.
            worker = WorkerProcess(
                {
                    "kind": "submit_plan",
                    "db": db_path,
                    "tenant_id": "t1",
                    "plan_id": "iso-plan",
                    "alternatives": alternatives(),
                },
                ws.path("work"),
                "iso",
            ).start()
            worker.wait_signal("pre_commit")
            # Concurrent write to a different tenant proceeds (row-level via
            # separate tenant rows); it must not be rolled back.
            directory.create_account("t-other", "d-o2", "o2", "second")
            worker.release("pre_commit")
            outcome = worker.read_result()
            self.assertEqual(worker.join(), 0)
            self.assertEqual(outcome["result"]["status"], "committed")

            self.assertEqual(directory.get_tenant("t1")["policy"], V2)
            self.assertEqual(directory.get_tenant("t-other")["policy"], V1)
            other_ids = {a["account_id"]
                         for a in directory.list_accounts("t-other")}
            self.assertEqual(other_ids, {"o1", "o2"})
            # Same login 'ABC' exists independently in the V1 tenant.
            view = directory.batch_resolve("t-other", ["ABC"])
            self.assertEqual(view["results"][0]["account_id"], "o1")
        finally:
            ws.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
