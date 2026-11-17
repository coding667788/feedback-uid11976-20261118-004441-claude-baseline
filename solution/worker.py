"""Child-process worker used by the multi-process concurrency tests.

Run as ``python -m solution.worker --request req.json --result out.json``.

Synchronization between independent OS processes uses plain marker files
inside a barrier directory (one request gets its own directory):

* ``signal`` is created by the worker when it reaches the barrier;
* ``release`` must be created by the driver process to let the worker proceed.

Two request kinds support barriers:

* ``submit_plan`` -- ``pre_commit`` (inside the write transaction, before any
  mapping change) and ``post_commit`` (after the atomic commit, before the
  response is written);
* ``batch_resolve`` -- ``resolve_<index>`` (while the read snapshot is held
  open between lookups).

The driver may terminate the worker process at either barrier, which is how
the kill-before-commit / kill-after-commit tests are built.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .canonical import loads_strict
from .directory import Directory
from .errors import ToolError
from .plan import build_plan, precheck, submit_plan


def _wait_marker(barrier_dir: Path, name: str, timeout_seconds: float = 120.0) -> None:
    signal = barrier_dir / name / "signal"
    release = barrier_dir / name / "release"
    signal.parent.mkdir(parents=True, exist_ok=True)
    signal.write_text("1", encoding="utf-8")
    deadline = time.monotonic() + timeout_seconds
    while not release.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"barrier {name!r} not released in time")
        time.sleep(0.01)


def _barrier(barrier_dir: str | None, name: str) -> Callable[[], None] | None:
    if not barrier_dir:
        return None
    path = Path(barrier_dir)

    def _fire() -> None:
        _wait_marker(path, name)

    return _fire


def handle_request(request: dict[str, Any]) -> dict[str, Any]:
    directory = Directory(request["db"])
    kind = request["kind"]
    barrier_dir = request.get("barrier_dir")

    if kind == "submit_plan":
        # The plan can either be supplied inline or built inside this process
        # from a fresh precheck (used by the interleaving scenario).
        if "plan" in request:
            plan = request["plan"]
        else:
            report = precheck(directory, request["tenant_id"])
            plan = build_plan(report, request.get("alternatives", []))
        result = submit_plan(
            directory,
            plan,
            request["plan_id"],
            pre_commit_barrier=_barrier(barrier_dir, "pre_commit"),
            post_commit_barrier=_barrier(barrier_dir, "post_commit"),
        )
        return {"ok": True, "result": result, "digest_plan": plan}

    if kind == "batch_resolve":
        per_query = _barrier(barrier_dir, "resolve_0")

        def _index_barrier(index: int) -> None:
            hook = _barrier(barrier_dir, f"resolve_{index}")
            if hook is not None:
                hook()

        result = directory.batch_resolve(
            request["tenant_id"],
            list(request["raw_logins"]),
            barrier=_index_barrier if barrier_dir else None,
        )
        return {"ok": True, "result": result}

    if kind == "precheck":
        return {"ok": True, "result": precheck(directory, request["tenant_id"])}

    if kind == "snapshot":
        return {"ok": True, "result": directory.snapshot(request["tenant_id"])}

    raise ValueError(f"unknown worker request kind {kind!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m solution.worker")
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    ns = parser.parse_args(argv)

    request = loads_strict(Path(ns.request).read_text(encoding="utf-8"))
    try:
        outcome = handle_request(request)
    except ToolError as exc:
        outcome = {"ok": False, "error": exc.to_dict()}
    except Exception as exc:  # the driver needs every failure in the result file
        outcome = {"ok": False, "error": {"code": "WORKER_EXCEPTION", "message": str(exc)}}
    Path(ns.result).write_text(
        json.dumps(outcome, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if outcome.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
