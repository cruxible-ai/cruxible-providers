"""Out-of-process budget enforcement.

Budgets are enforced by the executor around a child process, never by the
provider on itself: a provider that has run away is exactly the provider whose
self-policing cannot be trusted. A breach is a **typed refusal**, not a provider
error — the distinction matters because errors are attributed to the
implementation's track record as failed answers, while refusals are attributed
as declined ones.

Two caps are enforced here, wall clock and output size. Cost budgets travel in
the run context so a provider can report against them, but their enforcement
belongs to the metering substrate rather than to a local process supervisor.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .errors import RefusalCode, refuse
from .protocol import Budgets

__all__ = ["ProcessOutcome", "run_with_budget", "minimal_env"]

_READ_CHUNK = 65536


@dataclass(frozen=True)
class ProcessOutcome:
    stdout: bytes
    stderr: bytes
    returncode: int
    duration_seconds: float


def minimal_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the child's environment from scratch.

    The ambient environment is never inherited. That is a secret-hygiene rule
    first (credentials must not reach a child through the environment block) and
    a reproducibility rule second.
    """

    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if extra:
        env.update(extra)
    return env


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        pass


def run_with_budget(
    argv: Sequence[str],
    *,
    stdin_bytes: bytes,
    budgets: Budgets,
    pass_fds: Sequence[int] = (),
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> ProcessOutcome:
    """Run ``argv`` under wall-clock and output-size caps.

    Raises a typed refusal on breach; returns the outcome otherwise. The child
    is placed in its own process group so that a breach kills any grandchildren
    it spawned rather than orphaning them.
    """

    started = time.monotonic()
    process = subprocess.Popen(  # noqa: S603 - argv is executor-constructed
        list(argv),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=tuple(pass_fds),
        env=dict(env) if env is not None else minimal_env(),
        cwd=str(cwd) if cwd is not None else None,
        start_new_session=True,
        close_fds=True,
    )

    def _write_stdin() -> None:
        try:
            assert process.stdin is not None
            process.stdin.write(stdin_bytes)
            process.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass
        finally:
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

    writer = threading.Thread(target=_write_stdin, daemon=True)
    writer.start()

    assert process.stdout is not None
    assert process.stderr is not None
    stdout_fd = process.stdout.fileno()
    stderr_fd = process.stderr.fileno()
    buffers: dict[int, bytearray] = {stdout_fd: bytearray(), stderr_fd: bytearray()}
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    selector.register(process.stderr, selectors.EVENT_READ)

    deadline = started + budgets.wall_clock_seconds
    open_streams = 2
    breach: tuple[RefusalCode, str] | None = None

    try:
        while open_streams and breach is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                breach = (
                    RefusalCode.BUDGET_WALL_CLOCK,
                    f"provider exceeded its {budgets.wall_clock_seconds}s wall-clock budget",
                )
                break
            events = selector.select(timeout=min(remaining, 0.1))
            for key, _ in events:
                stream = key.fileobj
                fileno = stream.fileno()  # type: ignore[union-attr]
                chunk = os.read(fileno, _READ_CHUNK)
                if not chunk:
                    selector.unregister(stream)
                    open_streams -= 1
                    continue
                buffer = buffers[fileno]
                buffer.extend(chunk)
                if len(buffer) > budgets.output_bytes:
                    breach = (
                        RefusalCode.BUDGET_OUTPUT_SIZE,
                        f"provider exceeded its {budgets.output_bytes}-byte output budget",
                    )
                    break
        if breach is None and process.poll() is None:
            remaining = deadline - time.monotonic()
            try:
                process.wait(timeout=max(remaining, 0.0))
            except subprocess.TimeoutExpired:
                breach = (
                    RefusalCode.BUDGET_WALL_CLOCK,
                    f"provider exceeded its {budgets.wall_clock_seconds}s wall-clock budget",
                )
    finally:
        selector.close()
        if breach is not None or process.poll() is None:
            _terminate(process)
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:  # pragma: no cover - defensive
                pass
        writer.join(timeout=1)

    duration = time.monotonic() - started
    if breach is not None:
        code, message = breach
        raise refuse(
            code,
            message,
            duration_seconds=round(duration, 4),
            wall_clock_budget=budgets.wall_clock_seconds,
            output_budget=budgets.output_bytes,
        )

    return ProcessOutcome(
        stdout=bytes(buffers[stdout_fd]),
        stderr=bytes(buffers[stderr_fd]),
        returncode=process.returncode if process.returncode is not None else -1,
        duration_seconds=duration,
    )
