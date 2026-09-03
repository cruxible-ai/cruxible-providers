"""The container entry shim: memory-backed secret in, inherited descriptor out.

A provider run is one process reading its run context from stdin and its
credential material from an inherited file descriptor the run context names
(:mod:`cruxible_provider_runtime.child`). Locally the executor is the child's
parent, so it opens the descriptor and hands it over with ``pass_fds``. A
container is a different story: a fresh container receives stdin, stdout and
stderr and nothing else, an executor cannot hand a descriptor across the
container boundary, and the no-mounts law forbids bind-mounting a secret file
into the image. Something inside the image has to turn a delivery the container
runtime *can* perform into the descriptor the child expects.

That is this module. It is the image's ``ENTRYPOINT``; the child stays in
``CMD``, and the shim ``execv``s whatever command argv it was given:

    python -m cruxible_provider_runtime.container_entry \\
        [--secret-path <path> | --secret-pipe-fd <n>] [--] <command> [args...]

Started with no secret flag it is a pass-through — it execs the command and the
run has no secret channel, which is exactly what an image built before this shim
existed did. That is what keeps adding it to the images a no-op for every
existing caller.

**Two deliveries, both memory-backed.**

``--secret-path``
    A file on a tmpfs/ramfs mount the runtime dropped into the container. The
    shim opens it, refuses unless the mount really is memory-backed, refuses a
    bundle over the channel cap or carrying a second name, **unlinks it before
    exec** — so provider code never sees a path it could read a second time or
    hand to something else — and then reads it, bounded, into an anonymous
    in-memory copy. The copy is what the child gets. Handing over the file's own
    descriptor would leave the cap a snapshot that a writer still holding the
    unlinked inode could append past.

``--secret-pipe-fd``
    A one-shot pipe the executor wrote and left open on a numbered descriptor.
    The shim drains it — bounded in bytes, so an oversized bundle refuses rather
    than being copied, and bounded in time, so a write end nobody closed refuses
    rather than hanging — and re-delivers it on an anonymous memory descriptor.
    The round trip is not ceremony: a pipe whose write end is still open
    somewhere never reaches EOF, and the child's reader blocks forever on it.
    Draining behind the descriptor sweep below turns that into a bounded read in
    the shim and hands the child a descriptor that ends.

**Nothing the shim opens or reads can block indefinitely.** The path is opened
``O_NONBLOCK`` so a FIFO planted in the delivery directory returns instead of
waiting for a writer, and every read runs against a deadline
(:data:`SECRET_DRAIN_TIMEOUT_SECONDS`, or ``--secret-pipe-timeout``). A run that
hangs is worse than one that refuses: it spends the wall clock of a run that
never started, and it is the one failure an operator cannot tell from a slow
provider.

**The descriptor number is fixed and public.** The run context has to name the
descriptor the child actually sees, and the executor building that context is
outside this image with no way to observe it. So the number is a constant here
(:data:`SECRET_CHANNEL_FD`) and :func:`container_secret_channel` is how the
executor writes it into the run context — never by hard-coding an integer on the
far side of the boundary, where it would drift silently the day this changes.

**Nothing about the material is on argv, in the environment, or on a listable
path when the child starts.** The flags name a path or a descriptor number, both
of which the shim consumes; the exec'd argv is the command argv unchanged; the
environment is passed through untouched (the executor is what strips it); the
path is unlinked and the pipe drained before exec.

Refusals are one line on stderr — ``shim_refused: <code>`` — and a fixed non-zero
status (:data:`SHIM_REFUSED_EXIT_STATUS`). No traceback, because a traceback from
a process holding a credential bundle is a place for bytes to end up, and the
executor is going to capture stderr into the run's trace either way.
"""

from __future__ import annotations

import contextlib
import ctypes
import fcntl
import math
import os
import platform
import select
import stat
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from .protocol import SecretChannelSpec, SecretRef
from .secrets import MAX_SECRET_BUNDLE_BYTES

__all__ = [
    "MEMORY_BACKED_FILESYSTEM_MAGICS",
    "SECRET_CHANNEL_FD",
    "SECRET_DRAIN_TIMEOUT_SECONDS",
    "SHIM_MODULE",
    "SHIM_REFUSED_EXIT_STATUS",
    "ShimRefusal",
    "ShimRefusalError",
    "close_stray_descriptors",
    "container_secret_channel",
    "exec_command",
    "filesystem_magic",
    "main",
]

SHIM_MODULE = "cruxible_provider_runtime.container_entry"
"""What the images name in ``ENTRYPOINT``."""

SECRET_CHANNEL_FD = 3
"""The descriptor the child reads credential material from, inside a container.

Three, for the reason three is always the answer to this question: it is the
first descriptor above the standard streams, so it is the lowest number that
cannot collide with them. It also cannot collide with the child's own
descriptor juggling. :func:`cruxible_provider_runtime.child.reserve_stdout`
duplicates stdout aside before importing any provider code, and ``dup`` returns
the lowest *unused* descriptor — with the bundle already open on three, the
reservation lands on four. The shim installs the bundle with ``dup2``, which
replaces whatever the container runtime happened to leave on three, so the
number is a fact rather than a hope.
"""

SHIM_REFUSED_EXIT_STATUS = 78
"""Fixed, and distinct from anything the child exits with.

The executor has to tell "the shim declined to start the run" apart from "the
provider process failed", and it has only the exit status and one line of stderr
to do it with before any envelope exists.
"""

SECRET_DRAIN_TIMEOUT_SECONDS = 5.0
"""How long the shim waits for a delivery to reach its end before refusing.

A delivery that never ends is the failure this bound exists for. A pipe whose
write end is held open outside the container never reaches EOF; the descriptor
sweep can close a copy that leaked *into* this process and nothing else, so
against an executor that forgot to close its own the shim has no defence except
a deadline. Without one the run hangs rather than fails, which is strictly the
worse outcome: it burns the wall-clock budget of a run that never started, and
it is the one failure mode an operator cannot tell from a slow provider.

Five seconds, because the bundle is capped at 64 KiB and both deliveries are
local — a memory-backed file already written, or a pipe the executor filled
before the container started. Anything slower than that is not a slow delivery,
it is a missing one.

Overridable by the executor with ``--secret-pipe-timeout <seconds>``, and by
nothing else. Deliberately not an environment variable: the environment belongs
to the provider process, a variable is inherited by everything downstream, and a
knob that loosens a safety bound has to be visible in the argv the run recorded.
"""

_TMPFS_MAGIC = 0x01021994
_RAMFS_MAGIC = 0x858458F6

MEMORY_BACKED_FILESYSTEM_MAGICS = frozenset({_TMPFS_MAGIC, _RAMFS_MAGIC})
"""``statfs`` filesystem types this shim accepts a secret path on.

tmpfs and ramfs, and nothing else. The point of the check is that credential
material must never be written to a filesystem that can persist it — an
overlayfs upper layer, a volume, a stray writable mount — and "the executor
promised it was tmpfs" is not a check.

**Memory-backed is not private to this run, and this cannot tell the
difference.** A ``/dev/shm`` shared through ``--ipc=host`` or a shared IPC
namespace is tmpfs, and another container reads the bundle out of it; ``/dev``
is devtmpfs, whose magic is tmpfs's, and it passes too. So this is a necessary
condition, not a sufficient one: it rejects a filesystem that can persist the
material, and it cannot say who else can reach the one it accepts. The executor
owes the run a mount private to its own container — see the obligations in the
package README.
"""

_READ_CHUNK = 65536


class ShimRefusal(StrEnum):
    """The closed set of reasons the shim declines to start a run.

    Deliberately a separate, tiny vocabulary from
    :class:`~cruxible_provider_runtime.errors.RefusalCode`: the shim runs before
    the child, so there is no run context, no run id and no result envelope to
    put a typed refusal into, and inventing one here would mean shipping a
    second half-envelope shape. ``secret_bundle_too_large`` is spelled to match
    its ``RefusalCode`` counterpart exactly, because it is the same rule being
    enforced one process earlier and the executor should not need a translation
    table for it.
    """

    NO_COMMAND = "no_command"
    MISSING_OPTION_VALUE = "missing_option_value"
    INVALID_OPTION_VALUE = "invalid_option_value"
    CONFLICTING_SECRET_DELIVERY = "conflicting_secret_delivery"
    SECRET_PATH_UNREADABLE = "secret_path_unreadable"
    SECRET_PATH_NOT_MEMORY_BACKED = "secret_path_not_memory_backed"
    SECRET_PATH_NOT_EXCLUSIVE = "secret_path_not_exclusive"
    SECRET_PATH_UNVERIFIABLE = "secret_path_unverifiable"
    SECRET_PIPE_FD_INVALID = "secret_pipe_fd_invalid"
    SECRET_PIPE_TIMEOUT = "secret_pipe_timeout"
    SECRET_BUNDLE_TOO_LARGE = "secret_bundle_too_large"
    SECRET_DELIVERY_FAILED = "secret_delivery_failed"
    EXEC_FAILED = "exec_failed"


class ShimRefusalError(Exception):
    """Raised internally; rendered as one stderr line and a fixed exit status."""

    def __init__(self, code: ShimRefusal) -> None:
        super().__init__(code.value)
        self.code = code


def container_secret_channel(refs: Iterable[SecretRef | str]) -> SecretChannelSpec:
    """Build the run context's secret channel for the container path.

    The executor calls this instead of writing a descriptor number of its own:
    the number belongs to the shim, and a copy of it on the far side of the
    container boundary is a copy that goes stale without anything failing until
    a provider reads the wrong descriptor.

    Refs are accepted as :class:`~cruxible_provider_runtime.protocol.SecretRef`
    or as bare ref strings, and come back sorted by ref, matching what
    :func:`cruxible_provider_runtime.execute.invoke` builds locally so the two
    paths produce byte-identical channel specs for the same bundle.
    """

    entries = [ref if isinstance(ref, SecretRef) else SecretRef(ref=ref) for ref in refs]
    return SecretChannelSpec(
        fd=SECRET_CHANNEL_FD,
        refs=tuple(sorted(entries, key=lambda entry: entry.ref)),
    )


def filesystem_magic(fd: int) -> int | None:
    """The ``statfs`` filesystem type behind ``fd``, or ``None`` if unknowable.

    ``fstatfs`` rather than ``statfs`` on the path, so that the answer describes
    the file the shim already holds open: checking a path and then opening it
    leaves a window for the path to become something else in between, and the
    whole check exists because the delivery is not trusted.

    **Platform limits, stated rather than papered over.** ``statfs`` is a Linux
    interface and its ``f_type`` magic numbers are Linux's; CPython exposes no
    binding for it, so this reaches libc through ``ctypes``. On any other
    platform — and on Linux if the call fails — this returns ``None``, and the
    caller refuses ``secret_path_unverifiable`` rather than guessing. Concretely
    that means ``--secret-path`` is a **Linux-only** delivery, which is where
    provider images run; ``--secret-pipe-fd`` is the portable one, and it is the
    delivery to reach for anywhere else.

    The platform is read through :func:`platform.system` rather than
    ``sys.platform`` so that the non-Linux branch stays live code on every
    checker and every host. A type checker resolves ``sys.platform`` to whatever
    it is running on and then reports the other branch as unreachable, which
    would mean this function's fail-closed half is checked on exactly one of the
    two platforms it exists to distinguish.
    """

    if platform.system() != "Linux":
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        # Only the first member of `struct statfs` is read. The struct is 120
        # bytes on the ABIs Linux provider images run on; the buffer is
        # deliberately larger, because a buffer the kernel can overrun is a much
        # worse bug than a few wasted bytes.
        buffer = ctypes.create_string_buffer(512)
        if libc.fstatfs(ctypes.c_int(fd), buffer) != 0:
            return None
    except (OSError, AttributeError, ValueError):  # pragma: no cover - platform dependent
        return None
    # Unsigned: ``RAMFS_MAGIC`` (0x858458F6) read through a signed word comes
    # back negative on a 32-bit Linux ABI, and a genuine ramfs would be refused.
    # Fail-closed, but wrong, and being right costs nothing here.
    return int(ctypes.c_ulong.from_buffer(buffer).value)


def _open_descriptors() -> list[int]:
    for directory in ("/proc/self/fd", "/dev/fd"):
        try:
            names = os.listdir(directory)
        except OSError:  # pragma: no cover - platform dependent
            continue
        return sorted(int(name) for name in names if name.isdigit())
    return []  # pragma: no cover - neither directory exists


def close_stray_descriptors(keep: Iterable[int]) -> None:
    """Close every descriptor this process holds except the ones named.

    Two things depend on this. A pipe whose write end leaked into the container
    never reaches EOF, so the drain below — and, without the drain, the child's
    reader — would block forever on a bundle that has already arrived in full.
    And anything else the container runtime left open is a handle provider code
    would otherwise inherit, which is the same class of mistake as passing a
    credential on argv.

    Descriptors are enumerated rather than closed by range: a range walk under a
    high ``RLIMIT_NOFILE`` is a million syscalls, and a range walk under a low
    one silently misses what is above it. Where neither ``/proc/self/fd`` nor
    ``/dev/fd`` exists nothing is closed, and that is survivable rather than
    safe: an unswept write end is a delivery that never ends, and what turns it
    into a refusal rather than a hang is the drain's deadline
    (:data:`SECRET_DRAIN_TIMEOUT_SECONDS`), not this sweep.
    """

    protected = set(keep)
    for descriptor in _open_descriptors():
        if descriptor in protected:
            continue
        with contextlib.suppress(OSError):
            os.close(descriptor)


def exec_command(argv: Sequence[str]) -> None:
    """Replace this process with ``argv``. Returns only if the exec failed.

    ``execvp`` rather than ``execv``: the command argv comes from the image's
    ``CMD``, which spells the interpreter as bare ``python`` and expects ``PATH``
    to resolve it. The environment is inherited exactly as it stands — the shim
    adds nothing to it and removes nothing from it, because the executor already
    decides what the run's environment is and a shim editing it would be a second
    opinion nobody asked for.
    """

    os.execvp(argv[0], list(argv))


_FALLBACK_MAX_DESCRIPTOR = 1 << 20


def _max_descriptor() -> int:
    """One past the highest descriptor number this process could hold.

    ``--secret-pipe-fd`` arrives as text from an executor, and an integer larger
    than a C ``int`` reaches ``os.fstat`` as an ``OverflowError`` rather than an
    ``OSError`` — a traceback and exit 1, which is precisely the status the shim
    exists to keep distinct from a crashed child. Range-checking at parse time
    turns it into the same typed refusal every other bad descriptor gets.
    """

    try:
        limit = os.sysconf("SC_OPEN_MAX")
    except (AttributeError, ValueError, OSError):  # pragma: no cover - platform dependent
        return _FALLBACK_MAX_DESCRIPTOR
    return limit if limit > 0 else _FALLBACK_MAX_DESCRIPTOR


def _parse_descriptor(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise ShimRefusalError(ShimRefusal.SECRET_PIPE_FD_INVALID) from exc
    if not 0 <= number < _max_descriptor():
        raise ShimRefusalError(ShimRefusal.SECRET_PIPE_FD_INVALID)
    return number


@dataclass(frozen=True)
class _Invocation:
    """The parsed command line: at most one delivery, plus the command to exec."""

    command: tuple[str, ...]
    secret_path: str | None = None
    secret_pipe_fd: int | None = None
    drain_timeout: float = SECRET_DRAIN_TIMEOUT_SECONDS


_DELIVERY_OPTIONS = ("--secret-path", "--secret-pipe-fd")
_SHIM_OPTIONS = (*_DELIVERY_OPTIONS, "--secret-pipe-timeout")


def _parse_timeout(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise ShimRefusalError(ShimRefusal.INVALID_OPTION_VALUE) from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ShimRefusalError(ShimRefusal.INVALID_OPTION_VALUE)
    return seconds


def _parse_argv(args: Sequence[str]) -> _Invocation:
    """Split leading shim options off the command argv.

    Options are recognised only at the front, and ``--`` ends them explicitly.
    Everything from the first unrecognised token onward is the command, verbatim
    — the shim is a launcher, not a wrapper that gets an opinion about what it
    launches.
    """

    secret_path: str | None = None
    secret_pipe_fd: int | None = None
    drain_timeout = SECRET_DRAIN_TIMEOUT_SECONDS
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            index += 1
            break
        if token not in _SHIM_OPTIONS:
            break
        if index + 1 >= len(args):
            raise ShimRefusalError(ShimRefusal.MISSING_OPTION_VALUE)
        value = args[index + 1]
        if token == "--secret-pipe-timeout":
            drain_timeout = _parse_timeout(value)
        elif secret_path is not None or secret_pipe_fd is not None:
            raise ShimRefusalError(ShimRefusal.CONFLICTING_SECRET_DELIVERY)
        elif token == "--secret-path":
            secret_path = value
        else:
            secret_pipe_fd = _parse_descriptor(value)
        index += 2
    command = tuple(args[index:])
    if not command:
        raise ShimRefusalError(ShimRefusal.NO_COMMAND)
    return _Invocation(
        command=command,
        secret_path=secret_path,
        secret_pipe_fd=secret_pipe_fd,
        drain_timeout=drain_timeout,
    )


def _open_secret_path(path: str) -> int:
    """Open, verify, cap and unlink a tmpfs-backed bundle; return its descriptor.

    Nothing is read here. The cap is enforced from ``fstat`` before a single byte
    moves, which is stricter than reading and counting: an oversized bundle never
    reaches this process's memory at all.

    **The open cannot block and the file's kind is settled before anything
    else.** ``O_NONBLOCK`` because ``open`` on a FIFO with no writer blocks
    indefinitely — and a FIFO is something ``mknod`` can put in the delivery
    directory on the very tmpfs this delivery requires, so a shim that opens
    first and checks the mode afterwards never reaches the check. On a regular
    file the flag is a no-op, which is the whole appeal. ``fstat`` then runs
    before the filesystem check rather than after it, so anything that is not a
    regular file — FIFO, directory, socket, device — is refused by kind on every
    platform instead of by whichever check happens to come first.
    """

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as exc:
        raise ShimRefusalError(ShimRefusal.SECRET_PATH_UNREADABLE) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ShimRefusalError(ShimRefusal.SECRET_PATH_UNREADABLE)
        magic = filesystem_magic(fd)
        if magic is None:
            raise ShimRefusalError(ShimRefusal.SECRET_PATH_UNVERIFIABLE)
        if magic not in MEMORY_BACKED_FILESYSTEM_MAGICS:
            raise ShimRefusalError(ShimRefusal.SECRET_PATH_NOT_MEMORY_BACKED)
        if info.st_size > MAX_SECRET_BUNDLE_BYTES:
            raise ShimRefusalError(ShimRefusal.SECRET_BUNDLE_TOO_LARGE)
        if info.st_nlink != 1:
            # `unlink` removes a name. A bundle with a second name is a bundle
            # that survives the delivery, readable by whoever holds the other
            # name for as long as they like — and the shim's claim is that
            # nothing readable is left behind, not that one name is gone.
            raise ShimRefusalError(ShimRefusal.SECRET_PATH_NOT_EXCLUSIVE)
        try:
            os.unlink(path)
        except OSError as exc:
            # The descriptor would still work, and that is the trap: exec'ing
            # here would leave provider code a readable path to the bundle.
            raise ShimRefusalError(ShimRefusal.SECRET_DELIVERY_FAILED) from exc
        try:
            remaining = os.fstat(fd).st_nlink
        except OSError as exc:  # pragma: no cover - the fd is held open above
            raise ShimRefusalError(ShimRefusal.SECRET_DELIVERY_FAILED) from exc
        if remaining != 0:
            # Asked of the descriptor, so the answer is about the inode the shim
            # holds rather than about the name it just removed. A link made in
            # the window between the check and the unlink lands here, and so
            # does the other half of the same gap: a rename racing the open
            # means the name unlinked was never this inode's, and the inode
            # still has one.
            raise ShimRefusalError(ShimRefusal.SECRET_PATH_NOT_EXCLUSIVE)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _path_delivery(path: str, *, timeout: float) -> int:
    """The whole ``--secret-path`` delivery: verify, unlink, copy, hand over.

    The copy is the point. Handing the child the file's own descriptor made the
    64 KiB cap a snapshot — ``fstat`` says 64 KiB, the child reads to EOF, and
    anyone still holding a write descriptor on the unlinked inode decides what
    EOF means. Reading the bundle here, bounded, and handing over an anonymous
    in-memory copy makes the cap bound the bytes the child can read rather than
    the bytes the file admitted to at one instant. It also makes the two
    deliveries the same delivery from the child's side, which is one shape to
    reason about instead of two.
    """

    return _memory_backed_descriptor(_drain_descriptor(_open_secret_path(path), timeout=timeout))


def _drain_descriptor(fd: int, *, timeout: float) -> bytes:
    """Read a delivery to its end, bounded in bytes and in time. Closes ``fd``.

    Two bounds, because a delivery can fail in two directions.

    The byte bound is the cap plus one. Reading first and measuring after would
    mean anything that reached the write end could make the shim hold an
    arbitrary amount of "credential" in memory before the refusal it was always
    going to get; reading exactly one byte past the cap is what tells "over the
    cap" from "exactly at it" without a second syscall.

    The time bound is :data:`SECRET_DRAIN_TIMEOUT_SECONDS`, or whatever the
    executor named on ``--secret-pipe-timeout``. It is checked around the read
    and again after every chunk, so neither a writer that sends nothing nor one
    that trickles can hold the run open past the deadline.
    """

    limit = MAX_SECRET_BUNDLE_BYTES
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    total = 0
    try:
        os.set_blocking(fd, False)
        while total <= limit:
            try:
                chunk = os.read(fd, min(_READ_CHUNK, limit + 1 - total))
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ShimRefusalError(ShimRefusal.SECRET_PIPE_TIMEOUT) from None
                select.select([fd], [], [], remaining)
                continue
            except OSError as exc:
                raise ShimRefusalError(ShimRefusal.SECRET_DELIVERY_FAILED) from exc
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if time.monotonic() >= deadline:
                raise ShimRefusalError(ShimRefusal.SECRET_PIPE_TIMEOUT)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
    if total > limit:
        raise ShimRefusalError(ShimRefusal.SECRET_BUNDLE_TOO_LARGE)
    return b"".join(chunks)


def _drain_secret_pipe(fd: int, *, timeout: float) -> bytes:
    """Check the descriptor really is the pipe the executor promised, then drain."""

    if fd <= 2:
        raise ShimRefusalError(ShimRefusal.SECRET_PIPE_FD_INVALID)
    try:
        info = os.fstat(fd)
    except OSError as exc:
        raise ShimRefusalError(ShimRefusal.SECRET_PIPE_FD_INVALID) from exc
    if not stat.S_ISFIFO(info.st_mode):
        raise ShimRefusalError(ShimRefusal.SECRET_PIPE_FD_INVALID)
    return _drain_descriptor(fd, timeout=timeout)


def _memory_backed_descriptor(payload: bytes) -> int:
    """A readable descriptor carrying ``payload``, never touching a filesystem.

    ``memfd_create`` where it exists — Linux, which is where provider images run
    — because the result is anonymous memory with an end, so the child's reader
    terminates without anyone having to close a write end afterwards. Elsewhere
    a plain pipe stands in, filled with a single non-blocking write: the bundle
    is capped at 64 KiB and a pipe buffer is at least that, and a write that
    would block refuses instead of leaving a half-delivered bundle nobody is
    coming back to finish.
    """

    memfd_create: Callable[..., int] | None = getattr(os, "memfd_create", None)
    if memfd_create is not None:
        flags = getattr(os, "MFD_CLOEXEC", 0) | getattr(os, "MFD_ALLOW_SEALING", 0)
        fd = memfd_create("cruxible-secret-bundle", flags)
        try:
            written = os.write(fd, payload)
            if written != len(payload):  # pragma: no cover - short write to memory
                raise ShimRefusalError(ShimRefusal.SECRET_DELIVERY_FAILED)
            os.lseek(fd, 0, os.SEEK_SET)
            _seal(fd)
        except BaseException:
            os.close(fd)
            raise
        return fd

    read_fd, write_fd = os.pipe()
    try:
        os.set_blocking(write_fd, False)
        written = os.write(write_fd, payload)
    except OSError as exc:
        os.close(read_fd)
        os.close(write_fd)
        raise ShimRefusalError(ShimRefusal.SECRET_DELIVERY_FAILED) from exc
    if written != len(payload):  # pragma: no cover - needs a full pipe buffer
        os.close(read_fd)
        os.close(write_fd)
        raise ShimRefusalError(ShimRefusal.SECRET_DELIVERY_FAILED)
    os.close(write_fd)
    return read_fd


def _seal(fd: int) -> None:
    """Freeze the anonymous copy, where the kernel offers a way to.

    Defence in depth rather than a boundary: the descriptor is already anonymous
    and reachable only from this process tree. Sealing means provider code that
    inherits it cannot rewrite the bundle for anything downstream that reads it
    again, and it costs one syscall. A kernel without seals is not a refusal —
    the copy is no worse than the file it replaced — so the failure is ignored
    rather than reported.
    """

    add_seals = getattr(fcntl, "F_ADD_SEALS", None)
    if add_seals is None:  # pragma: no cover - platform dependent
        return
    seals = (
        getattr(fcntl, "F_SEAL_WRITE", 0)
        | getattr(fcntl, "F_SEAL_SHRINK", 0)
        | getattr(fcntl, "F_SEAL_GROW", 0)
    )
    with contextlib.suppress(OSError):
        fcntl.fcntl(fd, add_seals, seals)


def _install_secret_channel(fd: int) -> None:
    """Put the bundle on :data:`SECRET_CHANNEL_FD` and let the child inherit it."""

    if fd != SECRET_CHANNEL_FD:
        try:
            os.dup2(fd, SECRET_CHANNEL_FD, inheritable=True)
        except OSError as exc:
            os.close(fd)
            raise ShimRefusalError(ShimRefusal.SECRET_DELIVERY_FAILED) from exc
        os.close(fd)
    # Explicit even after ``dup2(..., inheritable=True)``: close-on-exec is what
    # decides whether the child sees this descriptor at all, and it is worth one
    # unambiguous line rather than a keyword argument's default.
    os.set_inheritable(SECRET_CHANNEL_FD, True)


def _run(args: Sequence[str]) -> int:
    invocation = _parse_argv(args)
    if invocation.secret_path is not None:
        close_stray_descriptors((0, 1, 2))
        _install_secret_channel(
            _path_delivery(invocation.secret_path, timeout=invocation.drain_timeout)
        )
    elif invocation.secret_pipe_fd is not None:
        close_stray_descriptors((0, 1, 2, invocation.secret_pipe_fd))
        payload = _drain_secret_pipe(invocation.secret_pipe_fd, timeout=invocation.drain_timeout)
        _install_secret_channel(_memory_backed_descriptor(payload))
    else:
        # No delivery flag: the run has no secret channel and the shim is a
        # pass-through, which is precisely what these images did before it
        # existed. Adding the entrypoint changes nothing for such a caller.
        close_stray_descriptors((0, 1, 2))

    # stdin is never read here. The run context on it belongs to the child, and
    # a shim that consumed even one byte of it would leave the child parsing a
    # truncated document.
    with contextlib.suppress(OSError, ValueError):
        # ``ValueError`` alongside ``OSError``: ``execvp`` raises it, not an
        # ``OSError``, when the first token is empty — an argv the executor can
        # produce by accident and the one path here that could still end in a
        # traceback.
        exec_command(invocation.command)
    # Reached only if the exec did not happen: a successful one never returns.
    return _refuse(ShimRefusal.EXEC_FAILED)


def main(argv: list[str] | None = None) -> int:
    """Run the shim. Returns a status; a successful exec never comes back here.

    The catch-all is the contract, not defensive habit. Everything before the
    exec is driven by argv an executor wrote, and a traceback out of a process
    that has just drained a credential bundle is both a place for bytes to land
    and exit status 1 — the status of a crashed child, which is the one thing
    :data:`SHIM_REFUSED_EXIT_STATUS` exists to be distinguishable from. So no
    ``Exception`` escapes: an unforeseen one is rendered as
    ``secret_delivery_failed``, which is true of any failure that reaches here.

    ``BaseException`` is deliberately not caught — a ``KeyboardInterrupt`` or a
    ``SystemExit`` is the caller ending the process, not a delivery to report
    on. And nothing wraps the exec itself: after a successful handover this
    process is the child, and a traceback from it is the child's.
    """

    args = list(sys.argv[1:] if argv is None else argv)
    try:
        return _run(args)
    except ShimRefusalError as exc:
        return _refuse(exc.code)
    except Exception:
        return _refuse(ShimRefusal.SECRET_DELIVERY_FAILED)


def _refuse(code: ShimRefusal) -> int:
    sys.stderr.write(f"shim_refused: {code.value}\n")
    sys.stderr.flush()
    return SHIM_REFUSED_EXIT_STATUS


if __name__ == "__main__":  # pragma: no cover - process entry
    raise SystemExit(main())
