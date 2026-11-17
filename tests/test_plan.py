"""Migration plan tests: precheck, conflicts, alternatives, stale/idempotency.

Expected conflict membership and final account ownership come from
tests/ref_oracle.py, which re-derives the V2 rules independently and never
imports the code under test's migration logic.
"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from solution import db  # noqa: E402
from solution.canonical import canonical_json, loads_strict, plan_digest  # noqa: E402
from solution.directory import Directory  # noqa: E402
from solution.errors import (  # noqa: E402
    PlanIdConflict,
    PlanValidationError,
    PolicyMismatch,
    SchemaError,
    StaleSnapshot,
)
from solution.plan import (  # noqa: E402
    build_plan,
    digest_of,
    get_plan,
    precheck,
    submit_plan,
)
from solution.normalization import V1, V2, normalize  # noqa: E402
from tests import ref_oracle  # noqa: E402
from tests.helpers import (  # noqa: E402
    CAPITAL_I_DOT,
    CYRILLIC_A,
    E_ACUTE_DEC,
    E_ACUTE_PRE,
    FULLWIDTH_1,
    FULLWIDTH_A,
    SHARP_S,
    SOFT_HYPHEN,
    TempWorkspace,
)


def make_directory(path):
    db.init_db(path)
    return Directory(path)


class PrecheckTest(unittest.TestCase):
    def setUp(self):
        self.ws = TempWorkspace()
        self.directory = make_directory(self.ws.path("dir.db"))
        self.directory.create_tenant("t1", V1)

    def tearDown(self):
        self.ws.cleanup()

    def _seed(self, entries):
        for account_id, raw in entries:
            self.directory.create_account("t1", "d-" + account_id, account_id, raw)

    def test_full_half_width_and_casefold_conflict_groups_match_oracle(self):
        entries = [
            ("a1", "ABC123"),
            ("a2", FULLWIDTH_A + "BC" + FULLWIDTH_1 + "23"),
            ("a3", "abc123"),
            ("a4", "Stra" + SHARP_S + "e"),
            ("a5", "strasse"),
            ("a6", "solo-user"),
        ]
        self._seed(entries)
        report = precheck(self.directory, "t1")
        expected = ref_oracle.ref_evaluate(entries)
        self.assertEqual(report["conflict_groups"], expected["conflict_groups"])
        self.assertEqual(report["accounts"], expected["accounts"])
        self.assertEqual(report["invalid_accounts"], expected["invalid_accounts"])
        # Explicit membership assertions, not just equality to oracle output.
        self.assertEqual(report["conflict_groups"], [
            ["a1", "a2", "a3"],
            ["a4", "a5"],
        ])
        self.assertEqual(report["source_policy"], V1)
        self.assertEqual(report["source_revision"], 6)
        self.assertEqual(
            report["snapshot_hash"], ref_oracle.ref_snapshot_hash(entries)
        )

    def test_casefold_collision_and_distinct_lookalikes(self):
        # These pairs are distinct under V1 (NFC, case-sensitive) but collapse
        # under V2 via casefold/NFKC. The composed/decomposed equivalence
        # itself is covered in tests/test_normalization.py.
        entries = [
            ("a1", "John"),
            ("a2", "john"),
            ("a3", "Stra" + SHARP_S + "e"),
            ("a4", "strasse"),
            ("a5", "paypal"),
            ("a6", "p" + CYRILLIC_A + "ypal"),
        ]
        self._seed(entries)
        report = precheck(self.directory, "t1")
        expected = ref_oracle.ref_evaluate(entries)
        self.assertEqual(report["conflict_groups"], expected["conflict_groups"])
        self.assertEqual(report["conflict_groups"], [
            ["a1", "a2"],
            ["a3", "a4"],
        ])
        # The visual look-alike pair stays distinct under both rule sets.
        targets = {a["account_id"]: a["target_login"] for a in report["accounts"]}
        self.assertNotEqual(targets["a5"], targets["a6"])

    def test_invalid_v2_names_are_listed_not_silently_dropped(self):
        # V1-valid names that become invalid/overlong under V2, plus a Cf char
        # that NFKC would otherwise erase.
        entries = [
            ("a1", "good-user"),
            ("a2", CAPITAL_I_DOT * 43),       # V2 expansion > 128 bytes
            ("a3", "x" + SOFT_HYPHEN + "y"),  # Cf rejected raw under both
        ]
        # a3 cannot be created at all (raw-stage rejection), so seed only V1-valid.
        self._seed(entries[:2])
        report = precheck(self.directory, "t1")
        self.assertEqual(report["invalid_accounts"], [
            {"account_id": "a2", "raw_login": CAPITAL_I_DOT * 43,
             "reason": report["invalid_accounts"][0]["reason"]},
        ])
        self.assertEqual(
            report["invalid_accounts"][0]["reason"]["reason"], "too_long"
        )
        self.assertEqual(report["conflict_groups"], [])

    def test_precheck_requires_v1_tenant(self):
        self.directory.create_tenant("t2", V2)
        with self.assertRaises(PolicyMismatch):
            precheck(self.directory, "t2")


class PlanValidationTest(unittest.TestCase):
    def setUp(self):
        self.ws = TempWorkspace()
        self.directory = make_directory(self.ws.path("dir.db"))
        self.directory.create_tenant("t1", V1)

    def tearDown(self):
        self.ws.cleanup()

    def _seed(self, entries):
        for account_id, raw in entries:
            self.directory.create_account("t1", "d-" + account_id, account_id, raw)

    def test_unresolved_conflict_makes_commit_reject_whole_plan(self):
        entries = [("a1", "ABC"), ("a2", FULLWIDTH_A + "BC"), ("a3", "lone")]
        self._seed(entries)
        report = precheck(self.directory, "t1")
        plan = build_plan(report, [])
        result = submit_plan(self.directory, plan, "plan-1")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error"]["code"], "PLAN_VALIDATION_FAILED")
        # Rejection changes nothing: still V1, revision untouched.
        tenant = self.directory.get_tenant("t1")
        self.assertEqual(tenant["policy"], V1)
        self.assertEqual(tenant["revision"], 3)
        stored = get_plan(self.directory, "plan-1")
        self.assertEqual(stored["status"], "rejected")

    def test_alternative_names_resolve_conflict_and_commit_atomically(self):
        entries = [("a1", "ABC"), ("a2", FULLWIDTH_A + "BC"), ("a3", "lone")]
        self._seed(entries)
        report = precheck(self.directory, "t1")
        alternatives = [
            {"account_id": "a1", "login": "alpha"},
            {"account_id": "a2", "login": "bravo"},
        ]
        plan = build_plan(report, alternatives)
        result = submit_plan(self.directory, plan, "plan-ok")
        self.assertEqual(result["status"], "committed")
        self.assertFalse(result["replayed"])
        self.assertEqual(result["new_revision"], 4)  # exactly one bump
        expected = ref_oracle.ref_final_mapping(
            entries, {"a1": "alpha", "a2": "bravo"}
        )
        tenant = self.directory.get_tenant("t1")
        self.assertEqual(tenant["policy"], V2)
        self.assertEqual(tenant["revision"], 4)
        actual = {a["account_id"]: a["login_name"]
                  for a in self.directory.list_accounts("t1")}
        self.assertEqual(actual, expected)
        # Every mapping is V2-normalized as stored.
        for account_id, login in actual.items():
            self.assertEqual(login, normalize(login, V2))

    def test_alternative_itself_conflicting_rejects_entire_plan(self):
        entries = [("a1", "ABC"), ("a2", FULLWIDTH_A + "BC"), ("a3", "lone")]
        self._seed(entries)
        report = precheck(self.directory, "t1")
        # a1 takes 'lone', which still belongs to a3's target -> conflict.
        plan = build_plan(report, [
            {"account_id": "a1", "login": "lone"},
            {"account_id": "a2", "login": "bravo"},
        ])
        result = submit_plan(self.directory, plan, "plan-bad-alt")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(
            result["error"]["details"]["reason"], "target_conflict"
        )
        self.assertEqual(
            sorted(result["error"]["details"]["account_ids"]), ["a1", "a3"]
        )
        self.assertEqual(self.directory.get_tenant("t1")["policy"], V1)

    def test_invalid_alternative_fails_full_v2_validation(self):
        entries = [("a1", "ABC"), ("a2", FULLWIDTH_A + "BC")]
        self._seed(entries)
        report = precheck(self.directory, "t1")
        plan = build_plan(report, [
            {"account_id": "a1", "login": "has space"},
            {"account_id": "a2", "login": "bravo"},
        ])
        result = submit_plan(self.directory, plan, "plan-invalid-alt")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(
            result["error"]["details"]["reason"], "invalid_alternative"
        )

    def test_login_swap_between_two_accounts_is_allowed(self):
        # Two accounts whose V2 targets collide may swap to each other's
        # *distinct* names through alternatives in one atomic plan.
        entries = [("a1", "USER"), ("a2", "user"), ("a3", "other")]
        self._seed(entries)
        report = precheck(self.directory, "t1")
        self.assertEqual(report["conflict_groups"], [["a1", "a2"]])
        alternatives = [
            {"account_id": "a1", "login": "user-one"},
            {"account_id": "a2", "login": "user-two"},
        ]
        plan = build_plan(report, alternatives)
        result = submit_plan(self.directory, plan, "plan-swap")
        self.assertEqual(result["status"], "committed")
        expected = ref_oracle.ref_final_mapping(
            entries, {"a1": "user-one", "a2": "user-two"}
        )
        actual = {a["account_id"]: a["login_name"]
                  for a in self.directory.list_accounts("t1")}
        self.assertEqual(actual, expected)

    def test_invalid_v2_account_without_alternative_blocks_commit(self):
        entries = [("a1", "good"), ("a2", CAPITAL_I_DOT * 43)]
        self._seed(entries)
        report = precheck(self.directory, "t1")
        plan = build_plan(report, [])
        result = submit_plan(self.directory, plan, "plan-missing-alt")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(
            result["error"]["details"]["reason"], "missing_alternative"
        )
        self.assertEqual(result["error"]["details"]["account_ids"], ["a2"])
        # Providing the alternative makes the same content committable, but
        # only under a NEW plan_id (the old one is bound to the rejection).
        plan2 = build_plan(report, [{"account_id": "a2", "login": "fixed-name"}])
        with self.assertRaises(PlanIdConflict):
            submit_plan(self.directory, plan2, "plan-missing-alt")
        result2 = submit_plan(self.directory, plan2, "plan-fixed")
        self.assertEqual(result2["status"], "committed")

    def test_alternative_for_unknown_account_rejected(self):
        entries = [("a1", "solo")]
        self._seed(entries)
        report = precheck(self.directory, "t1")
        plan = build_plan(report, [{"account_id": "ghost", "login": "x"}])
        result = submit_plan(self.directory, plan, "plan-ghost")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error"]["details"]["reason"], "unknown_account")


class StaleAndIdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.ws = TempWorkspace()
        self.directory = make_directory(self.ws.path("dir.db"))
        self.directory.create_tenant("t1", V1)
        for account_id, raw in [("a1", "ABC"), ("a2", FULLWIDTH_A + "BC")]:
            self.directory.create_account("t1", "d-" + account_id, account_id, raw)

    def tearDown(self):
        self.ws.cleanup()

    def _resolvable_plan(self, report):
        return build_plan(report, [
            {"account_id": "a1", "login": "alpha"},
            {"account_id": "a2", "login": "bravo"},
        ])

    def test_add_after_precheck_makes_plan_stale(self):
        report = precheck(self.directory, "t1")
        plan = self._resolvable_plan(report)
        self.directory.create_account("t1", "d-a3", "a3", "latecomer")
        with self.assertRaises(StaleSnapshot) as ctx:
            submit_plan(self.directory, plan, "plan-stale-add")
        self.assertEqual(ctx.exception.error_code, "PLAN_STALE")
        self.assertEqual(ctx.exception.details["reason"], "revision_changed")
        self.assertEqual(self.directory.get_tenant("t1")["policy"], V1)
        # A stale attempt never binds the plan_id: retrying unchanged content
        # after no further change still fails stale rather than idempotently.
        with self.assertRaises(StaleSnapshot):
            submit_plan(self.directory, plan, "plan-stale-add")

    def test_rename_and_delete_after_precheck_make_plan_stale(self):
        # Rename and delete both bump the revision, so the revision guard is
        # the first to fire for those mutations.
        scenarios = [
            lambda d: d.rename_account("t1", "a1", "ABC-renamed"),
            lambda d: d.delete_account("t1", "a2"),
        ]
        for mutation in scenarios:
            with self.subTest(mutation=mutation):
                ws = TempWorkspace()
                try:
                    directory = make_directory(ws.path("dir.db"))
                    directory.create_tenant("t1", V1)
                    directory.create_account("t1", "d-a1", "a1", "ABC")
                    directory.create_account(
                        "t1", "d-a2", "a2", FULLWIDTH_A + "BC"
                    )
                    report = precheck(directory, "t1")
                    plan = self._resolvable_plan(report)
                    mutation(directory)
                    with self.assertRaises(StaleSnapshot) as ctx:
                        submit_plan(directory, plan, "plan-stale")
                    self.assertEqual(
                        ctx.exception.details["reason"], "revision_changed"
                    )
                    self.assertEqual(directory.get_tenant("t1")["policy"], V1)
                finally:
                    ws.cleanup()

    def test_account_set_change_with_unchanged_revision_is_still_stale(self):
        # Reaching the hash guard requires the revision number to match while
        # the account set differs; craft it by resetting the revision counter.
        import sqlite3

        report = precheck(self.directory, "t1")
        plan = self._resolvable_plan(report)
        conn = sqlite3.connect(self.directory.db_path)
        conn.execute(
            "UPDATE accounts SET raw_login='tampered', login_name='tampered' "
            "WHERE account_id='a1'"
        )
        conn.execute("UPDATE tenants SET revision=? ", (report["source_revision"],))
        conn.commit()
        conn.close()
        with self.assertRaises(StaleSnapshot) as ctx:
            submit_plan(self.directory, plan, "plan-hash-stale")
        self.assertEqual(
            ctx.exception.details["reason"], "account_set_changed"
        )

    def test_same_plan_id_retry_replays_without_double_revision_bump(self):
        report = precheck(self.directory, "t1")
        plan = self._resolvable_plan(report)
        first = submit_plan(self.directory, plan, "plan-idem")
        self.assertEqual(first["status"], "committed")
        self.assertFalse(first["replayed"])
        revision_after_first = self.directory.get_tenant("t1")["revision"]
        second = submit_plan(self.directory, plan, "plan-idem")
        self.assertTrue(second["replayed"])
        self.assertEqual(second["status"], "committed")
        self.assertEqual(
            self.directory.get_tenant("t1")["revision"], revision_after_first
        )

    def test_reused_plan_id_with_changed_content_is_conflict(self):
        report = precheck(self.directory, "t1")
        plan_a = self._resolvable_plan(report)
        first = submit_plan(self.directory, plan_a, "plan-reuse")
        self.assertEqual(first["status"], "committed")
        plan_b = build_plan(report, [
            {"account_id": "a1", "login": "alpha2"},
            {"account_id": "a2", "login": "bravo2"},
        ])
        with self.assertRaises(PlanIdConflict):
            submit_plan(self.directory, plan_b, "plan-reuse")

    def test_rejected_plan_id_cannot_be_reused_with_new_parameters(self):
        report = precheck(self.directory, "t1")
        bad_plan = build_plan(report, [])  # unresolved conflict
        rejected = submit_plan(self.directory, bad_plan, "plan-once")
        self.assertEqual(rejected["status"], "rejected")
        good_plan = self._resolvable_plan(report)
        with self.assertRaises(PlanIdConflict):
            submit_plan(self.directory, good_plan, "plan-once")
        # A fresh id works.
        ok = submit_plan(self.directory, good_plan, "plan-once-v2")
        self.assertEqual(ok["status"], "committed")

    def test_old_login_names_have_no_hidden_aliases_after_commit(self):
        report = precheck(self.directory, "t1")
        plan = self._resolvable_plan(report)
        submit_plan(self.directory, plan, "plan-no-alias")
        # V2 is active now: the old V1 spellings must resolve per V2 only,
        # never as undeclared historical aliases to another account.
        result = self.directory.batch_resolve("t1", ["ABC", FULLWIDTH_A + "BC"])
        for row in result["results"]:
            self.assertIsNone(row["account_id"])
        # New names resolve to their declared owners.
        result2 = self.directory.batch_resolve("t1", ["alpha", "bravo"])
        self.assertEqual(
            [r["account_id"] for r in result2["results"]], ["a1", "a2"]
        )


class DigestDeterminismTest(unittest.TestCase):
    def setUp(self):
        self.ws = TempWorkspace()
        self.directory = make_directory(self.ws.path("dir.db"))
        self.directory.create_tenant("t1", V1)
        entries = [("a1", "ABC"), ("a2", FULLWIDTH_A + "BC"), ("a3", "zzz")]
        for account_id, raw in entries:
            self.directory.create_account("t1", "d-" + account_id, account_id, raw)
        self.entries = entries

    def tearDown(self):
        self.ws.cleanup()

    def test_digest_ignores_ordering_but_preserves_content(self):
        report = precheck(self.directory, "t1")
        alts = [
            {"account_id": "a1", "login": "alpha"},
            {"account_id": "a2", "login": "bravo"},
        ]
        plan1 = build_plan(report, alts)
        shuffled = copy.deepcopy(plan1)
        shuffled["accounts"] = list(reversed(shuffled["accounts"]))
        shuffled["alternatives"] = list(reversed(shuffled["alternatives"]))
        self.assertEqual(plan_digest(plan1), plan_digest(shuffled))
        self.assertEqual(canonical_json(plan1), canonical_json(shuffled))

        changed = copy.deepcopy(plan1)
        changed["alternatives"][0]["login"] = "alpha-changed"
        self.assertNotEqual(plan_digest(plan1), plan_digest(changed))

        other_id = copy.deepcopy(plan1)
        other_id["accounts"][0]["account_id"] = "a1-different"
        self.assertNotEqual(plan_digest(plan1), plan_digest(other_id))

        # Type differences are preserved: int revision as string differs.
        typed = copy.deepcopy(plan1)
        typed["source_revision"] = str(typed["source_revision"])
        with self.assertRaises(SchemaError):
            plan_digest(typed)

    def test_duplicate_and_unknown_json_keys_rejected(self):
        report = precheck(self.directory, "t1")
        plan = build_plan(report, [])
        text = canonical_json(plan)
        tampered = text.replace(
            '"tenant_id":"t1"', '"tenant_id":"t1","tenant_id":"t1"', 1
        )
        with self.assertRaises(SchemaError):
            loads_strict(tampered)
        unknown = copy.deepcopy(plan)
        unknown["surprise"] = 1
        with self.assertRaises(SchemaError):
            plan_digest(unknown)

    def test_equivalent_inputs_produce_identical_plan_and_digest(self):
        r1 = precheck(self.directory, "t1")
        r2 = precheck(self.directory, "t1")
        p1 = build_plan(r1, [
            {"account_id": "a2", "login": "bravo"},
            {"account_id": "a1", "login": "alpha"},
        ])
        p2 = build_plan(r2, [
            {"account_id": "a1", "login": "alpha"},
            {"account_id": "a2", "login": "bravo"},
        ])
        self.assertEqual(canonical_json(p1), canonical_json(p2))
        self.assertEqual(digest_of(p1), digest_of(p2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
