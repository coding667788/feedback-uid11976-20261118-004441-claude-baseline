"""Helpers for driving independent worker processes in concurrency tests.

The tests spawn real OS processes via ``sys.executable`` running
``python -m solution.worker``; they communicate only through the SQLite
database and marker files, never through shared memory, so they genuinely
exercise cross-process locking and WAL snapshot semantics.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _wait_for(path: Path, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out waiting for {path}")
        time.sleep(0.01)


class WorkerProcess:
    """One ``solution.worker`` child process with file-based barriers."""

    def __init__(self, request: dict, work_dir: str | Path, label: str):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.label = label
        self.request_path = self.work_dir / f"{label}-request.json"
        self.result_path = self.work_dir / f"{label}-result.json"
        self.barrier_dir = self.work_dir / f"{label}-barriers"
        request = dict(request)
        request["barrier_dir"] = str(self.barrier_dir)
        self.request_path.write_text(
            json.dumps(request, ensure_ascii=False), encoding="utf-8"
        )
        self.proc: subprocess.Popen | None = None

    def start(self) -> "WorkerProcess":
        # -I isolates the child from PYTHONPATH/site-packages; cwd is the
        # project root so 'solution' resolves from source, not a planted file.
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-m",
                "solution.worker",
                "--request",
                str(self.request_path),
                "--result",
                str(self.result_path),
            ],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return self

    def wait_signal(self, name: str, timeout: float = 60.0) -> None:
        _wait_for(self.barrier_dir / name / "signal", timeout)

    def release(self, name: str) -> None:
        marker = self.barrier_dir / name / "release"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("1", encoding="utf-8")

    def join(self, timeout: float = 60.0) -> int:
        assert self.proc is not None
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise

    def terminate(self) -> None:
        """Kill a process we started (used by the crash-recovery tests)."""
        assert self.proc is not None
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)

    def read_result(self, timeout: float = 60.0) -> dict:
        _wait_for(self.result_path, timeout)
        return json.loads(self.result_path.read_text(encoding="utf-8"))

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def pid(self) -> int:
        assert self.proc is not None
        return self.proc.pid
