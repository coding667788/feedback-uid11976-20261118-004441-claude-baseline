"""JSON case runner: ``python -m solution --input cases.json --output result.json``.

The input file contains a sequence of operations that are executed in order.
Results (including structured failures) are written to the output file; the
process exits non-zero only for malformed input/argument errors.

See README.md for the full input/output schema and error-code table.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import db
from .canonical import loads_strict
from .directory import Directory
from .errors import SchemaError, ToolError
from .plan import build_plan, digest_of, get_plan, precheck, submit_plan


def _run_operation(directory: Directory, op: dict[str, Any], ctx: dict[str, Any]) -> Any:
    if not isinstance(op, dict):
        raise SchemaError("each operation must be a JSON object")
    if "op" not in op:
        raise SchemaError("operation is missing the 'op' field")
    name = op["op"]
    args = op.get("args", {})
    if not isinstance(args, dict):
        raise SchemaError("operation 'args' must be an object")

    if name == "init_db":
        db.init_db(args["path"])
        return {"initialized": True, "path": args["path"]}
    if name == "create_tenant":
        return directory.create_tenant(args["tenant_id"], args["policy"])
    if name == "get_tenant":
        return directory.get_tenant(args["tenant_id"])
    if name == "create_account":
        return directory.create_account(
            args["tenant_id"], args["name"], args["account_id"], args["raw_login"]
        )
    if name == "rename_account":
        return directory.rename_account(
            args["tenant_id"], args["account_id"], args["raw_login"]
        )
    if name == "delete_account":
        return directory.delete_account(args["tenant_id"], args["account_id"])
    if name == "list_accounts":
        return {"accounts": directory.list_accounts(args["tenant_id"])}
    if name == "snapshot":
        return directory.snapshot(args["tenant_id"])
    if name == "batch_resolve":
        return directory.batch_resolve(args["tenant_id"], list(args["raw_logins"]))
    if name == "precheck":
        report = precheck(directory, args["tenant_id"])
        ref = args.get("store_as")
        if ref:
            if not isinstance(ref, str):
                raise SchemaError("store_as must be a string")
            ctx[ref] = report
        return report
    if name == "build_plan":
        report = _resolve_report(args, ctx)
        alternatives = args.get("alternatives", [])
        plan = build_plan(report, alternatives)
        ref = args.get("store_as")
        if ref:
            ctx[ref] = plan
        return {"plan": plan, "digest": digest_of(plan)}
    if name == "submit_plan":
        plan = _resolve_plan(args, ctx)
        result = submit_plan(directory, plan, args["plan_id"])
        return result
    if name == "get_plan":
        return get_plan(directory, args["plan_id"])
    raise SchemaError(f"unknown operation {name!r}", details={"op": name})


def _resolve_report(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    if "precheck_ref" in args:
        report = ctx.get(args["precheck_ref"])
        if report is None:
            raise SchemaError(
                f"unknown precheck_ref {args['precheck_ref']!r}",
                details={"reason": "unknown_ref"},
            )
        return report
    if "report" in args:
        return args["report"]
    raise SchemaError("build_plan requires precheck_ref or report")


def _resolve_plan(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    if "plan_ref" in args:
        plan = ctx.get(args["plan_ref"])
        if plan is None:
            raise SchemaError(
                f"unknown plan_ref {args['plan_ref']!r}",
                details={"reason": "unknown_ref"},
            )
        return plan
    if "plan" in args:
        return args["plan"]
    raise SchemaError("submit_plan requires plan_ref or plan")


def run_cases(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SchemaError("input root must be a JSON object")
    if "db" not in payload or not isinstance(payload["db"], str):
        raise SchemaError("input requires string field 'db'")
    if "operations" not in payload or not isinstance(payload["operations"], list):
        raise SchemaError("input requires list field 'operations'")
    directory = Directory(payload["db"])
    ctx: dict[str, Any] = {}
    results: list[dict[str, Any]] = []
    for index, op in enumerate(payload["operations"]):
        try:
            output = _run_operation(directory, op, ctx)
            results.append({"index": index, "op": op.get("op"), "ok": True, "result": output})
        except ToolError as exc:
            results.append(
                {
                    "index": index,
                    "op": op.get("op"),
                    "ok": False,
                    "error": exc.to_dict(),
                }
            )
    return {"db": payload["db"], "results": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m solution",
        description="Login-name Unicode rule upgrade case runner",
    )
    parser.add_argument("--input", help="input JSON file with operations")
    parser.add_argument("--output", help="output JSON file for results")
    parser.add_argument(
        "--init-db", metavar="PATH", help="initialize an empty database and exit"
    )
    ns = parser.parse_args(argv)

    if ns.init_db:
        db.init_db(ns.init_db)
        print(json.dumps({"initialized": True, "path": ns.init_db}, ensure_ascii=False))
        return 0

    if not ns.input or not ns.output:
        parser.error("--input and --output are required (or use --init-db)")

    input_path = Path(ns.input)
    output_path = Path(ns.output)
    try:
        payload = loads_strict(input_path.read_text(encoding="utf-8"))
        result = run_cases(payload)
    except ToolError as exc:
        result = {"fatal": exc.to_dict()}
        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"fatal input error: {exc.error_code}", file=sys.stderr)
        return 2

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
