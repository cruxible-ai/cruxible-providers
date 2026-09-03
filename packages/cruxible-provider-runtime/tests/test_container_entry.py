"""The container entry shim, at protocol level. No container engine is used.

Two kinds of test here, and they answer different questions.

The in-process ones drive :func:`cruxible_provider_runtime.container_entry.main`
with the exec replaced by a recorder, which is the only way to observe the state
the shim hands over *at the moment of exec*: which descriptor the bundle is on,
whether the path is already gone, whether the descriptor survives an exec at
all. A real exec answers none of those, because there is no "after" to look at.

The subprocess ones run the shim for real — real ``execvp``, real descriptor
inheritance, real ``/proc/self/cmdline`` — and end in the actual child harness
reading the bundle through ``secrets.read_secrets``. Between the two, every
claim the shim makes is checked against the mechanism that would carry it.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from cruxible_provider_runtime import container_entry
from cruxible_provider_runtime.backends import CHILD_MODULE
from cruxible_provider_runtime.container_entry import (
    MEMORY_BACKED_FILESYSTEM_MAGICS,
    SECRET_CHANNEL_FD,
    SECRET_DRAIN_TIMEOUT_SECONDS,
    SHIM_MODULE,
    SHIM_REFUSED_EXIT_STATUS,
    ShimRefusal,
    container_secret_channel,
    filesystem_magic,
)
from cruxible_provider_runtime.errors import RefusalCode
from cruxible_provider_runtime.protocol import (
    PROTOCOL_VERSION,
    Budgets,
    RunContext,
    SecretRef,
    parse_result_envelope,
)
from cruxible_provider_runtime.secrets import MAX_SECRET_BUNDLE_BYTES, read_secrets

RUNTIME_SRC = Path(__file__).resolve().parents[1] / "src"

# Synthetic throughout. Nothing in this file is or resembles a real credential.
BUNDLE = {"provider.api_key": "dummy-credential-9f3c1a", "provider.token": "dummy-token-4b2e"}
BUNDLE_BYTES = json.dumps(BUNDLE, sort_keys=True, separators=(",", ":")).encode("utf-8")

TMPFS_MAGIC = 0x01021994
EXT4_MAGIC = 0xEF53


# --------------------------------------------------------------------------
# In-process harness
# --------------------------------------------------------------------------


@dataclass
class _ExecRecord:
    argv: tuple[str, ...]
    secret_fd_open: bool
    secret_bytes: bytes | None
    inheritable: bool | None
    watched_path_exists: bool | None
    secret_seals: int | None = None


def _descriptor_seals(fd: int) -> int | None:
    """The memfd seals on ``fd``, or ``None`` where the kernel has no such thing."""

    get_seals = getattr(fcntl, "F_GET_SEALS", None)
    if get_seals is None:
        return None
    try:
        return int(fcntl.fcntl(fd, get_seals))
    except OSError:
        return None


@dataclass
class _Harness:
    """Stands in for the exec, and observes what the child would have inherited."""

    watched_path: Path | None = None
    read_secret_fd: bool = True
    """Draining the delivery is destructive, so a test that wants to read it says so."""

    before_read: Callable[[], None] | None = None
    """Runs at the moment of exec, before the delivery is read.

    That instant is the whole window a race has to work in: the shim has
    finished checking and the child has not started reading. A test that wants
    to be the racing writer has to act here or not at all.
    """

    records: list[_ExecRecord] = field(default_factory=list)

    def exec_command(self, argv: Sequence[str]) -> None:
        if self.before_read is not None:
            self.before_read()
        secret_bytes: bytes | None = None
        inheritable: bool | None = None
        seals: int | None = None
        try:
            inheritable = os.get_inheritable(SECRET_CHANNEL_FD)
        except OSError:
            open_fd = False
            inheritable = None
        else:
            open_fd = True
            seals = _descriptor_seals(SECRET_CHANNEL_FD)
            if self.read_secret_fd:
                with os.fdopen(os.dup(SECRET_CHANNEL_FD), "rb", closefd=True) as handle:
                    secret_bytes = handle.read()
        self.records.append(
            _ExecRecord(
                argv=tuple(argv),
                secret_fd_open=open_fd,
                secret_bytes=secret_bytes,
                inheritable=inheritable,
                watched_path_exists=(
                    self.watched_path.exists() if self.watched_path is not None else None
                ),
                secret_seals=seals,
            )
        )

    @property
    def only(self) -> _ExecRecord:
        assert len(self.records) == 1, f"expected exactly one exec, got {len(self.records)}"
        return self.records[0]


@contextlib.contextmanager
def _detached_secret_descriptor() -> Iterator[None]:
    """Free :data:`SECRET_CHANNEL_FD` for the duration, and put it back after.

    The shim installs the bundle with ``dup2``, which would replace whatever the
    test runner has on that number. Parking it aside keeps "is the bundle on
    three" a real question rather than one the runner has already answered.
    """

    try:
        saved: int | None = os.dup(SECRET_CHANNEL_FD)
    except OSError:
        saved = None
    else:
        os.close(SECRET_CHANNEL_FD)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            os.close(SECRET_CHANNEL_FD)
        if saved is not None:
            os.dup2(saved, SECRET_CHANNEL_FD)
            os.close(saved)


@pytest.fixture()
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    recorder = _Harness()
    monkeypatch.setattr(container_entry, "exec_command", recorder.exec_command)
    # The real sweep closes every descriptor this process holds. In a child about
    # to exec that is the point; inside the test runner it would close the
    # runner's own. The subprocess tests below exercise it for real.
    monkeypatch.setattr(container_entry, "close_stray_descriptors", lambda keep: None)
    with _detached_secret_descriptor():
        yield recorder


def _pretend_filesystem(monkeypatch: pytest.MonkeyPatch, magic: int | None) -> None:
    monkeypatch.setattr(container_entry, "filesystem_magic", lambda fd: magic)


def _clear_of_the_secret_descriptor(fd: int) -> int:
    """Move ``fd`` above :data:`SECRET_CHANNEL_FD`, and hand back the new number.

    The delivery is installed with ``dup2`` onto three, and the harness frees
    three for the duration of a test, so a descriptor a test opens for its own
    purposes can land exactly where the bundle is about to. Silently, and then
    the test writes into the delivery.
    """

    if fd > SECRET_CHANNEL_FD:
        return fd
    moved = os.dup(fd)
    os.close(fd)
    return moved


def _bundle_file(directory: Path, payload: bytes = BUNDLE_BYTES) -> Path:
    path = directory / "secret-bundle.json"
    path.write_bytes(payload)
    return path


COMMAND = ("python", "-m", CHILD_MODULE)


# --------------------------------------------------------------------------
# The fixed descriptor and the channel helper
# --------------------------------------------------------------------------


def test_the_secret_descriptor_is_a_fixed_number_above_the_standard_streams() -> None:
    assert SECRET_CHANNEL_FD == 3
    assert SECRET_CHANNEL_FD > 2


def test_the_channel_helper_names_the_descriptor_the_child_will_see() -> None:
    """Nothing outside this image gets to spell the number itself."""

    channel = container_secret_channel(["provider.token", SecretRef(ref="provider.api_key")])
    assert channel.fd == SECRET_CHANNEL_FD
    assert channel.kind == "fd"
    assert [ref.ref for ref in channel.refs] == ["provider.api_key", "provider.token"]


def test_the_channel_helper_accepts_no_refs() -> None:
    assert container_secret_channel([]).refs == ()


def test_the_shim_vocabulary_matches_the_runtime_taxonomy_where_they_overlap() -> None:
    """One rule, enforced a process earlier, keeps one spelling."""

    assert ShimRefusal.SECRET_BUNDLE_TOO_LARGE.value == RefusalCode.SECRET_BUNDLE_TOO_LARGE.value


# --------------------------------------------------------------------------
# Pass-through: an image with the shim behaves like an image without it
# --------------------------------------------------------------------------


def test_without_a_secret_flag_the_command_is_exec_ed_unchanged(harness: _Harness) -> None:
    assert container_entry.main([*COMMAND]) == SHIM_REFUSED_EXIT_STATUS
    record = harness.only
    assert record.argv == COMMAND
    assert record.secret_fd_open is False


def test_a_double_dash_ends_the_shim_options(harness: _Harness) -> None:
    """So a command of its own that starts with a shim flag still reaches exec."""

    assert container_entry.main(["--", "--secret-path", "not-a-flag"]) == SHIM_REFUSED_EXIT_STATUS
    assert harness.only.argv == ("--secret-path", "not-a-flag")
    assert harness.only.secret_fd_open is False


# --------------------------------------------------------------------------
# tmpfs path delivery
# --------------------------------------------------------------------------


def test_a_memory_backed_path_lands_on_the_fixed_descriptor(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _pretend_filesystem(monkeypatch, TMPFS_MAGIC)
    path = _bundle_file(tmp_path)
    harness.watched_path = path

    container_entry.main(["--secret-path", str(path), *COMMAND])

    record = harness.only
    assert record.argv == COMMAND
    assert record.secret_fd_open is True
    assert record.secret_bytes == BUNDLE_BYTES
    assert record.inheritable is True


def test_the_path_is_gone_before_the_child_starts(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unlinked, not merely unreadable: provider code must find nothing there."""

    _pretend_filesystem(monkeypatch, TMPFS_MAGIC)
    path = _bundle_file(tmp_path)
    harness.watched_path = path

    container_entry.main(["--secret-path", str(path), *COMMAND])

    assert harness.only.watched_path_exists is False
    assert harness.only.secret_bytes == BUNDLE_BYTES


def test_a_path_on_a_disk_filesystem_refuses(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _pretend_filesystem(monkeypatch, EXT4_MAGIC)
    path = _bundle_file(tmp_path)

    status = container_entry.main(["--secret-path", str(path), *COMMAND])

    assert status == SHIM_REFUSED_EXIT_STATUS
    assert capsys.readouterr().err == "shim_refused: secret_path_not_memory_backed\n"
    assert harness.records == []
    assert path.exists(), "a refused delivery must not be consumed"


def test_a_filesystem_the_shim_cannot_identify_refuses(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fail closed where ``statfs`` is unavailable, rather than trusting the caller."""

    _pretend_filesystem(monkeypatch, None)
    path = _bundle_file(tmp_path)

    status = container_entry.main(["--secret-path", str(path), *COMMAND])

    assert status == SHIM_REFUSED_EXIT_STATUS
    assert capsys.readouterr().err == "shim_refused: secret_path_unverifiable\n"
    assert harness.records == []


def test_an_oversized_bundle_refuses_before_a_byte_is_read(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _pretend_filesystem(monkeypatch, TMPFS_MAGIC)
    path = _bundle_file(tmp_path, b"x" * (MAX_SECRET_BUNDLE_BYTES + 1))

    status = container_entry.main(["--secret-path", str(path), *COMMAND])

    assert status == SHIM_REFUSED_EXIT_STATUS
    assert capsys.readouterr().err == "shim_refused: secret_bundle_too_large\n"
    assert harness.records == []
    assert path.exists() and path.stat().st_size == MAX_SECRET_BUNDLE_BYTES + 1


def test_a_bundle_at_exactly_the_cap_is_delivered(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cap is a limit, not an off-by-one."""

    _pretend_filesystem(monkeypatch, TMPFS_MAGIC)
    payload = b"y" * MAX_SECRET_BUNDLE_BYTES
    container_entry.main(["--secret-path", str(_bundle_file(tmp_path, payload)), *COMMAND])
    assert harness.only.secret_bytes == payload


def test_a_missing_path_refuses(
    harness: _Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = container_entry.main(["--secret-path", str(tmp_path / "absent"), *COMMAND])
    assert status == SHIM_REFUSED_EXIT_STATUS
    assert capsys.readouterr().err == "shim_refused: secret_path_unreadable\n"
    assert harness.records == []


def test_a_symlinked_path_refuses(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``O_NOFOLLOW``: the delivery is a file the runtime placed, not a pointer."""

    _pretend_filesystem(monkeypatch, TMPFS_MAGIC)
    target = _bundle_file(tmp_path)
    link = tmp_path / "link.json"
    link.symlink_to(target)

    assert container_entry.main(["--secret-path", str(link), *COMMAND]) == SHIM_REFUSED_EXIT_STATUS
    assert harness.records == []
    assert target.exists()


def test_a_path_that_is_not_a_regular_file_refuses(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _pretend_filesystem(monkeypatch, TMPFS_MAGIC)
    directory = tmp_path / "bundle-dir"
    directory.mkdir()
    assert (
        container_entry.main(["--secret-path", str(directory), *COMMAND])
        == SHIM_REFUSED_EXIT_STATUS
    )
    assert harness.records == []


def test_a_socket_at_the_secret_path_refuses(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Refused by kind, before anything asks what filesystem it is on."""

    _pretend_filesystem(monkeypatch, TMPFS_MAGIC)
    path = tmp_path / "bundle.sock"
    # Bound relative from inside the directory: an ``AF_UNIX`` path is capped at
    # ~104 bytes and a temp directory is longer than that on some hosts.
    monkeypatch.chdir(tmp_path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
        endpoint.bind(path.name)
        status = container_entry.main(["--secret-path", str(path), *COMMAND])

    assert status == SHIM_REFUSED_EXIT_STATUS
    assert capsys.readouterr().err == "shim_refused: secret_path_unreadable\n"
    assert harness.records == []


@pytest.mark.skipif(not os.path.exists("/dev/zero"), reason="no /dev/zero on this host")
def test_a_character_device_at_the_secret_path_refuses(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A device is where an endless "bundle" comes from; the kind check is the bound.

    ``/dev`` is devtmpfs, whose magic is tmpfs's, so on Linux the memory-backed
    check accepts it and the regular-file check is the only thing that does not.
    """

    _pretend_filesystem(monkeypatch, TMPFS_MAGIC)
    status = container_entry.main(["--secret-path", "/dev/zero", *COMMAND])

    assert status == SHIM_REFUSED_EXIT_STATUS
    assert capsys.readouterr().err == "shim_refused: secret_path_unreadable\n"
    assert harness.records == []


def test_a_writer_appending_after_the_check_cannot_reach_the_child(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cap has to bound the bytes the child reads, not the bytes fstat saw.

    ``fstat`` measures the file at one instant. A writer still holding a
    descriptor on the inode — the unlink takes the name, not their handle —
    decides where EOF is afterwards, and the child's reader has no cap of its
    own. Probed at 263 168 bytes against a 65 536-byte cap before this closed.
    """

    _pretend_filesystem(monkeypatch, TMPFS_MAGIC)
    path = _bundle_file(tmp_path)
    appender = _clear_of_the_secret_descriptor(os.open(path, os.O_WRONLY | os.O_APPEND))
    harness.before_read = lambda: os.write(appender, b"x" * 263_168)

    try:
        container_entry.main(["--secret-path", str(path), *COMMAND])
    finally:
        os.close(appender)

    assert harness.only.secret_bytes == BUNDLE_BYTES


def test_a_bundle_far_over_the_cap_refuses_before_the_exec(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _pretend_filesystem(monkeypatch, TMPFS_MAGIC)
    path = _bundle_file(tmp_path, b"x" * 263_168)

    status = container_entry.main(["--secret-path", str(path), *COMMAND])

    assert status == SHIM_REFUSED_EXIT_STATUS
    assert capsys.readouterr().err == "shim_refused: secret_bundle_too_large\n"
    assert harness.records == []


@pytest.mark.skipif(
    not hasattr(os, "memfd_create"), reason="anonymous memory descriptors are a Linux interface"
)
def test_the_delivered_copy_is_sealed_against_rewriting(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _pretend_filesystem(monkeypatch, TMPFS_MAGIC)

    container_entry.main(["--secret-path", str(_bundle_file(tmp_path)), *COMMAND])

    seals = harness.only.secret_seals
    assert seals is not None
    assert seals & fcntl.F_SEAL_WRITE


@pytest.fixture()
def link_root(tmp_path: Path) -> Iterator[Path]:
    """A real tmpfs directory where the host has one, the temp dir otherwise.

    The hard-link gap is a property of names and inodes, not of tmpfs, so the
    temp dir tests the same thing; running it on the real delivery filesystem
    where one exists is what makes the answer about the mount the executor will
    actually use.
    """

    if MEMORY_BACKED_DIR is None:
        yield tmp_path
        return
    root = MEMORY_BACKED_DIR / f"cruxible-shim-links-{os.getpid()}"
    root.mkdir(exist_ok=True)
    try:
        yield root
    finally:
        for leftover in root.iterdir():
            leftover.unlink()
        root.rmdir()


def test_a_bundle_with_a_second_name_refuses(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    link_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unlink removes a name. A second name outlives the delivery entirely."""

    _pretend_filesystem(monkeypatch, TMPFS_MAGIC)
    path = _bundle_file(link_root)
    link = link_root / "second-name.json"
    try:
        os.link(path, link)
    except OSError as exc:  # pragma: no cover - filesystem dependent
        pytest.skip(f"this filesystem does not support hard links: {exc}")

    status = container_entry.main(["--secret-path", str(path), *COMMAND])

    assert status == SHIM_REFUSED_EXIT_STATUS
    assert capsys.readouterr().err == "shim_refused: secret_path_not_exclusive\n"
    assert harness.records == []
    assert path.exists(), "a refused delivery is not consumed"
    assert link.read_bytes() == BUNDLE_BYTES


def test_a_name_that_appears_between_the_check_and_the_unlink_refuses(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    link_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The window the first check cannot see, closed by asking the descriptor.

    ``st_nlink`` on the held descriptor after the unlink is the only question
    whose answer is about the inode the shim opened. It catches a link made in
    the window, and the same reading catches the other half of the name/inode
    gap: a rename racing the open means the name that was unlinked was some
    other file's, and this inode still has one.
    """

    _pretend_filesystem(monkeypatch, TMPFS_MAGIC)
    path = _bundle_file(link_root)
    survivor = link_root / "raced.json"
    real_unlink = os.unlink

    def _racing_unlink(target: Any, *args: Any, **kwargs: Any) -> None:
        if os.fspath(target) == str(path) and not survivor.exists():
            os.link(path, survivor)
        real_unlink(target, *args, **kwargs)

    try:
        os.link(path, survivor)
        survivor.unlink()
    except OSError as exc:  # pragma: no cover - filesystem dependent
        pytest.skip(f"this filesystem does not support hard links: {exc}")
    monkeypatch.setattr(os, "unlink", _racing_unlink)

    status = container_entry.main(["--secret-path", str(path), *COMMAND])

    assert status == SHIM_REFUSED_EXIT_STATUS
    assert capsys.readouterr().err == "shim_refused: secret_path_not_exclusive\n"
    assert harness.records == []
    assert survivor.read_bytes() == BUNDLE_BYTES


# --------------------------------------------------------------------------
# Pipe delivery
# --------------------------------------------------------------------------


def _filled_pipe(payload: bytes) -> int:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, payload)
    os.close(write_fd)
    return read_fd


def test_pipe_delivery_lands_on_the_fixed_descriptor(harness: _Harness) -> None:
    read_fd = _filled_pipe(BUNDLE_BYTES)

    container_entry.main(["--secret-pipe-fd", str(read_fd), *COMMAND])

    record = harness.only
    assert record.argv == COMMAND
    assert record.secret_bytes == BUNDLE_BYTES
    assert record.inheritable is True


def test_pipe_delivery_hands_the_child_a_descriptor_that_ends(harness: _Harness) -> None:
    """``read_secrets`` reads to EOF, so a delivery with no end is a hang."""

    harness.read_secret_fd = False
    container_entry.main(["--secret-pipe-fd", str(_filled_pipe(BUNDLE_BYTES)), *COMMAND])
    assert harness.only.secret_fd_open is True
    assert read_secrets(os.dup(SECRET_CHANNEL_FD)) == BUNDLE


def test_the_source_pipe_is_drained_to_completion(harness: _Harness) -> None:
    """One shot: nothing of the bundle is left on the pipe the executor named.

    Asserted through a second read end held open here rather than through the
    original descriptor number, which the re-delivery is free to have recycled by
    now — "is that number closed" would be a question with two right answers, and
    a test that accepts both asserts nothing.
    """

    read_fd = _filled_pipe(BUNDLE_BYTES)
    observer = os.dup(read_fd)
    try:
        container_entry.main(["--secret-pipe-fd", str(read_fd), *COMMAND])
        assert harness.only.secret_bytes == BUNDLE_BYTES
        assert os.read(observer, 1) == b"", "the pipe still holds bundle bytes"
    finally:
        os.close(observer)


def test_an_oversized_pipe_bundle_refuses(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bounded read: the refusal does not require holding the whole thing first."""

    read_fd, write_fd = os.pipe()
    payload = b"z" * (MAX_SECRET_BUNDLE_BYTES * 2)

    def _write() -> None:
        with contextlib.suppress(OSError):
            os.write(write_fd, payload)
        with contextlib.suppress(OSError):
            os.close(write_fd)

    writer = threading.Thread(target=_write, daemon=True)
    writer.start()
    try:
        status = container_entry.main(["--secret-pipe-fd", str(read_fd), *COMMAND])
    finally:
        writer.join(timeout=5)

    assert status == SHIM_REFUSED_EXIT_STATUS
    assert capsys.readouterr().err == "shim_refused: secret_bundle_too_large\n"
    assert harness.records == []


def test_a_descriptor_that_is_not_a_pipe_refuses(
    harness: _Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _bundle_file(tmp_path)
    fd = os.open(path, os.O_RDONLY)
    try:
        status = container_entry.main(["--secret-pipe-fd", str(fd), *COMMAND])
    finally:
        os.close(fd)
    assert status == SHIM_REFUSED_EXIT_STATUS
    assert capsys.readouterr().err == "shim_refused: secret_pipe_fd_invalid\n"
    assert harness.records == []


def test_a_closed_descriptor_refuses(harness: _Harness, capsys: pytest.CaptureFixture[str]) -> None:
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    os.close(write_fd)
    assert (
        container_entry.main(["--secret-pipe-fd", str(read_fd), *COMMAND])
        == SHIM_REFUSED_EXIT_STATUS
    )
    assert capsys.readouterr().err == "shim_refused: secret_pipe_fd_invalid\n"


@pytest.mark.parametrize("value", ["0", "1", "2"])
def test_a_standard_stream_is_never_the_secret_pipe(
    harness: _Harness, value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert container_entry.main(["--secret-pipe-fd", value, *COMMAND]) == SHIM_REFUSED_EXIT_STATUS
    assert capsys.readouterr().err == "shim_refused: secret_pipe_fd_invalid\n"


def test_a_non_numeric_descriptor_refuses(harness: _Harness) -> None:
    assert container_entry.main(["--secret-pipe-fd", "three", *COMMAND]) == SHIM_REFUSED_EXIT_STATUS
    assert harness.records == []


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def test_two_deliveries_at_once_refuse(
    harness: _Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = container_entry.main(
        ["--secret-path", str(tmp_path / "a"), "--secret-pipe-fd", "9", *COMMAND]
    )
    assert status == SHIM_REFUSED_EXIT_STATUS
    assert capsys.readouterr().err == "shim_refused: conflicting_secret_delivery\n"
    assert harness.records == []


def test_a_flag_without_a_value_refuses(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert container_entry.main(["--secret-path"]) == SHIM_REFUSED_EXIT_STATUS
    assert capsys.readouterr().err == "shim_refused: missing_option_value\n"


def test_no_command_refuses(harness: _Harness, capsys: pytest.CaptureFixture[str]) -> None:
    assert container_entry.main([]) == SHIM_REFUSED_EXIT_STATUS
    assert capsys.readouterr().err == "shim_refused: no_command\n"


def test_a_command_that_cannot_be_exec_ed_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real exec, against a command that is not there."""

    monkeypatch.setattr(container_entry, "close_stray_descriptors", lambda keep: None)
    with _detached_secret_descriptor():
        status = container_entry.main(["/nonexistent/cruxible-shim-test-binary"])
    assert status == SHIM_REFUSED_EXIT_STATUS
    assert capsys.readouterr().err == "shim_refused: exec_failed\n"


def test_an_empty_first_command_token_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``execvp`` raises ``ValueError`` here, not ``OSError``. Same refusal."""

    monkeypatch.setattr(container_entry, "close_stray_descriptors", lambda keep: None)
    with _detached_secret_descriptor():
        status = container_entry.main([""])
    assert status == SHIM_REFUSED_EXIT_STATUS
    assert capsys.readouterr().err == "shim_refused: exec_failed\n"


def test_a_descriptor_number_too_large_for_the_kernel_refuses(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    """An integer past a C ``int`` reaches ``fstat`` as ``OverflowError``."""

    status = container_entry.main(["--secret-pipe-fd", "99999999999999999999", *COMMAND])
    assert status == SHIM_REFUSED_EXIT_STATUS
    assert capsys.readouterr().err == "shim_refused: secret_pipe_fd_invalid\n"
    assert harness.records == []


def test_a_negative_descriptor_refuses(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    status = container_entry.main(["--secret-pipe-fd", "-1", *COMMAND])
    assert status == SHIM_REFUSED_EXIT_STATUS
    assert capsys.readouterr().err == "shim_refused: secret_pipe_fd_invalid\n"
    assert harness.records == []


def test_an_unforeseen_failure_is_still_one_typed_line(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No exception escapes as a traceback and exit 1 — a crashed child's status."""

    def _boom(keep: Sequence[int]) -> None:
        raise RuntimeError("a bug nobody foresaw")

    monkeypatch.setattr(container_entry, "close_stray_descriptors", _boom)

    status = container_entry.main([*COMMAND])

    assert status == SHIM_REFUSED_EXIT_STATUS
    assert capsys.readouterr().err == "shim_refused: secret_delivery_failed\n"
    assert harness.records == []


def test_the_drain_deadline_is_a_documented_constant() -> None:
    parsed = container_entry._parse_argv(["--secret-pipe-fd", "9", *COMMAND])
    assert SECRET_DRAIN_TIMEOUT_SECONDS == 5.0
    assert parsed.drain_timeout == SECRET_DRAIN_TIMEOUT_SECONDS


def test_only_an_executor_flag_moves_the_drain_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A knob that loosens a safety bound belongs in the argv the run recorded."""

    for name in (
        "SECRET_DRAIN_TIMEOUT_SECONDS",
        "CRUXIBLE_SECRET_DRAIN_TIMEOUT_SECONDS",
        "CRUXIBLE_SECRET_PIPE_TIMEOUT",
    ):
        monkeypatch.setenv(name, "0.001")

    assert container_entry._parse_argv([*COMMAND]).drain_timeout == SECRET_DRAIN_TIMEOUT_SECONDS
    flagged = container_entry._parse_argv(
        ["--secret-pipe-timeout", "0.25", "--secret-pipe-fd", "9", *COMMAND]
    )
    assert flagged.drain_timeout == 0.25
    assert flagged.secret_pipe_fd == 9


@pytest.mark.parametrize("value", ["", "soon", "0", "-1", "nan", "inf"])
def test_a_timeout_that_is_not_a_positive_number_refuses(
    harness: _Harness, value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    status = container_entry.main(["--secret-pipe-timeout", value, *COMMAND])
    assert status == SHIM_REFUSED_EXIT_STATUS
    assert capsys.readouterr().err == "shim_refused: invalid_option_value\n"
    assert harness.records == []


def test_the_timeout_flag_is_not_a_second_delivery(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """It sits beside a delivery flag rather than conflicting with it."""

    _pretend_filesystem(monkeypatch, TMPFS_MAGIC)
    path = _bundle_file(tmp_path)

    container_entry.main(["--secret-pipe-timeout", "3", "--secret-path", str(path), *COMMAND])

    assert harness.only.secret_bytes == BUNDLE_BYTES


# --------------------------------------------------------------------------
# The filesystem check itself
# --------------------------------------------------------------------------


def test_filesystem_magic_identifies_this_platform_consistently(tmp_path: Path) -> None:
    """Either a magic number, or ``None`` — and ``None`` is what refuses."""

    fd = os.open(_bundle_file(tmp_path), os.O_RDONLY)
    try:
        magic = filesystem_magic(fd)
    finally:
        os.close(fd)
    if sys.platform == "linux":
        assert isinstance(magic, int)
    else:
        assert magic is None


@pytest.mark.skipif(sys.platform != "linux", reason="statfs is a Linux interface")
def test_the_unpatched_check_refuses_a_disk_filesystem(
    harness: _Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The negative branch against a real mount, with nothing monkeypatched."""

    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        magic = filesystem_magic(fd)
    finally:
        os.close(fd)
    if magic in MEMORY_BACKED_FILESYSTEM_MAGICS:
        pytest.skip("this runner's temporary directory is itself memory-backed")

    status = container_entry.main(["--secret-path", str(_bundle_file(tmp_path)), *COMMAND])

    assert status == SHIM_REFUSED_EXIT_STATUS
    assert capsys.readouterr().err == "shim_refused: secret_path_not_memory_backed\n"
    assert harness.records == []


def _memory_backed_directory() -> Path | None:
    """A real tmpfs, or ``None``. ``/dev/shm`` is one on every Linux image."""

    candidate = Path("/dev/shm")
    if not candidate.is_dir():
        return None
    try:
        fd = os.open(candidate, os.O_RDONLY)
    except OSError:  # pragma: no cover - permission dependent
        return None
    try:
        magic = filesystem_magic(fd)
    finally:
        os.close(fd)
    return candidate if magic in MEMORY_BACKED_FILESYSTEM_MAGICS else None


MEMORY_BACKED_DIR = _memory_backed_directory()


@pytest.mark.skipif(MEMORY_BACKED_DIR is None, reason="no tmpfs mount on this platform")
def test_a_real_tmpfs_path_is_accepted_unpatched(harness: _Harness) -> None:
    """The statfs check against an actual memory-backed mount, no fake in sight."""

    assert MEMORY_BACKED_DIR is not None
    path = MEMORY_BACKED_DIR / f"cruxible-shim-{os.getpid()}.json"
    path.write_bytes(BUNDLE_BYTES)
    try:
        container_entry.main(["--secret-path", str(path), *COMMAND])
    finally:
        path.unlink(missing_ok=True)
    assert harness.only.secret_bytes == BUNDLE_BYTES


# --------------------------------------------------------------------------
# The shim for real: exec, descriptor inheritance, and the child harness
# --------------------------------------------------------------------------

_REPORT = """
import json, os, sys

watched = sys.argv[1]
try:
    with os.fdopen(os.dup(int(sys.argv[2])), "rb", closefd=True) as handle:
        bundle = handle.read().decode("utf-8")
except OSError:
    bundle = None
try:
    with open("/proc/self/cmdline", "rb") as handle:
        cmdline = handle.read().decode("utf-8", "replace")
except OSError:
    cmdline = ""
sys.stdout.write(json.dumps({
    "argv": sys.argv,
    "environ": dict(os.environ),
    "bundle": bundle,
    "cmdline": cmdline,
    "watched_exists": os.path.exists(watched) if watched else None,
    "stdin": sys.stdin.read(),
}))
"""

_PROVIDER = """
import hashlib


from cruxible_provider_runtime.provider_api import ProviderResult


class Fingerprint:
    interface_id = "conformance.secret_channel"

    def __call__(self, context):
        return ProviderResult.ok(
            {
                "refs": sorted(context.secrets),
                "fingerprints": {
                    ref: hashlib.sha256(value.encode("utf-8")).hexdigest()
                    for ref, value in sorted(context.secrets.items())
                },
            }
        )
"""


def _environment(extra_roots: Sequence[Path] = ()) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join(str(root) for root in (RUNTIME_SRC, *extra_roots)),
    }


def _run_shim(
    shim_args: Sequence[str],
    command: Sequence[str],
    *,
    stdin: bytes = b"",
    pass_fds: Sequence[int] = (),
    extra_roots: Sequence[Path] = (),
    timeout: float = 120,
) -> subprocess.CompletedProcess[bytes]:
    """Run the shim as a real process.

    ``timeout`` is never decoration: the two failures this suite has to be able
    to observe — an open that waits for a writer, a drain that waits for an EOF
    nobody is coming to send — are hangs, and a hang inside the runner is a
    suite that never reports. Out here it is one failed test.
    """

    return subprocess.run(
        [sys.executable, "-m", SHIM_MODULE, *shim_args, *command],
        input=stdin,
        capture_output=True,
        env=_environment(extra_roots),
        pass_fds=tuple(pass_fds),
        timeout=timeout,
    )


def _reporter(watched: str = "") -> list[str]:
    return [sys.executable, "-c", _REPORT, watched, str(SECRET_CHANNEL_FD)]


def test_the_real_shim_delivers_the_bundle_on_the_fixed_descriptor() -> None:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, BUNDLE_BYTES)
    os.close(write_fd)
    try:
        completed = _run_shim(["--secret-pipe-fd", str(read_fd)], _reporter(), pass_fds=(read_fd,))
    finally:
        with contextlib.suppress(OSError):
            os.close(read_fd)

    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    assert completed.stderr == b"", "a successful delivery says nothing at all"
    report = json.loads(completed.stdout)
    assert json.loads(report["bundle"]) == BUNDLE


def test_the_real_shim_without_a_flag_leaves_the_command_with_no_secret_channel() -> None:
    completed = _run_shim([], _reporter())
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    report = json.loads(completed.stdout)
    assert report["bundle"] is None


def test_the_pass_through_writes_nothing_to_stderr() -> None:
    """Byte-identical to the pre-shim process, stderr included.

    stdout and stderr are billed against one ``output_bytes`` budget, so a
    warning the shim emits on every start is a standing tax on every run and a
    permanent line in every trace — not cosmetic noise. The regression this
    guards is a package-level re-export of the shim module, which put it in
    ``sys.modules`` before ``runpy`` ran it as ``__main__`` and cost 232 bytes
    of ``RuntimeWarning`` per container start.
    """

    completed = _run_shim([], [sys.executable, "-c", "print('hi')"])

    assert completed.returncode == 0
    assert completed.stdout == b"hi\n"
    assert completed.stderr == b""


@pytest.mark.parametrize(
    ("shim_args", "command", "code"),
    [
        (
            ["--secret-pipe-fd", "99999999999999999999"],
            ["/bin/echo", "hi"],
            "secret_pipe_fd_invalid",
        ),
        ([], [""], "exec_failed"),
    ],
    ids=["descriptor-out-of-range", "empty-command-token"],
)
def test_an_argv_the_executor_got_wrong_exits_78_with_one_line(
    shim_args: Sequence[str], command: Sequence[str], code: str
) -> None:
    """Real process, real status. Exit 1 with a traceback is a crashed child."""

    completed = _run_shim(shim_args, command)

    assert completed.returncode == SHIM_REFUSED_EXIT_STATUS
    assert completed.stdout == b""
    assert completed.stderr == f"shim_refused: {code}\n".encode()
    assert b"Traceback" not in completed.stderr


def test_a_fifo_at_the_secret_path_refuses_instead_of_blocking(tmp_path: Path) -> None:
    """``open`` on a writer-less FIFO waits forever. ``O_NONBLOCK`` is why it does not.

    A FIFO is not exotic here: ``mknod`` works on tmpfs, which is the very
    filesystem this delivery insists on, so anything that can write into the
    delivery directory can put one where the bundle should be. Before the
    non-blocking open the shim never reached its own regular-file check.
    """

    fifo = tmp_path / "bundle.fifo"
    os.mkfifo(fifo)

    started = time.monotonic()
    completed = _run_shim(["--secret-path", str(fifo)], _reporter(), timeout=30)
    elapsed = time.monotonic() - started

    assert completed.returncode == SHIM_REFUSED_EXIT_STATUS
    assert completed.stderr == b"shim_refused: secret_path_unreadable\n"
    assert elapsed < 30
    assert fifo.exists(), "a refused delivery is not consumed"


def test_a_pipe_whose_write_end_never_closes_refuses_on_the_deadline() -> None:
    """The holder is outside the container, where the descriptor sweep cannot reach."""

    read_fd, write_fd = os.pipe()
    # A partial bundle and no close: exactly what an executor that crashed
    # between writing and closing leaves behind.
    os.write(write_fd, b'{"provider.api_key": "dummy-credential')
    try:
        started = time.monotonic()
        completed = _run_shim(
            ["--secret-pipe-timeout", "0.5", "--secret-pipe-fd", str(read_fd)],
            _reporter(),
            pass_fds=(read_fd,),
            timeout=30,
        )
        elapsed = time.monotonic() - started
    finally:
        os.close(write_fd)
        with contextlib.suppress(OSError):
            os.close(read_fd)

    assert completed.returncode == SHIM_REFUSED_EXIT_STATUS
    assert completed.stderr == b"shim_refused: secret_pipe_timeout\n"
    assert elapsed < 30


def test_the_package_exports_the_shim_constants_without_importing_the_shim() -> None:
    """The lazy re-export, checked where it matters: at package import time."""

    probe = (
        "import sys, cruxible_provider_runtime as runtime\n"
        "before = 'cruxible_provider_runtime.container_entry' in sys.modules\n"
        "fd = runtime.SECRET_CHANNEL_FD\n"
        "helper = runtime.container_secret_channel(['a.b']).fd\n"
        "after = 'cruxible_provider_runtime.container_entry' in sys.modules\n"
        "print(before, fd, helper, after)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        env=_environment(),
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    assert completed.stdout.split() == [b"False", b"3", b"3", b"True"]


def test_nothing_of_the_material_reaches_argv_the_environment_or_the_cmdline() -> None:
    """The leak assertion the whole descriptor channel exists to satisfy."""

    read_fd, write_fd = os.pipe()
    os.write(write_fd, BUNDLE_BYTES)
    os.close(write_fd)
    try:
        completed = _run_shim(["--secret-pipe-fd", str(read_fd)], _reporter(), pass_fds=(read_fd,))
    finally:
        with contextlib.suppress(OSError):
            os.close(read_fd)

    report = json.loads(completed.stdout)
    haystacks: list[Any] = [report["argv"], report["environ"], report["cmdline"], completed.stderr]
    for value in BUNDLE.values():
        assert value in report["bundle"]
        for haystack in haystacks:
            assert value not in json.dumps(haystack, default=str)


def test_stdin_reaches_the_command_untouched() -> None:
    """The run context is the child's to read; the shim never takes a byte of it."""

    payload = b'{"protocol_version":"1.0","run_id":"run-not-consumed"}'
    read_fd, write_fd = os.pipe()
    os.write(write_fd, BUNDLE_BYTES)
    os.close(write_fd)
    try:
        completed = _run_shim(
            ["--secret-pipe-fd", str(read_fd)],
            _reporter(),
            stdin=payload,
            pass_fds=(read_fd,),
        )
    finally:
        with contextlib.suppress(OSError):
            os.close(read_fd)

    assert json.loads(completed.stdout)["stdin"] == payload.decode("utf-8")


@pytest.mark.skipif(MEMORY_BACKED_DIR is None, reason="no tmpfs mount on this platform")
def test_the_real_shim_unlinks_a_tmpfs_bundle_before_the_command_runs() -> None:
    assert MEMORY_BACKED_DIR is not None
    path = MEMORY_BACKED_DIR / f"cruxible-shim-e2e-{os.getpid()}.json"
    path.write_bytes(BUNDLE_BYTES)
    try:
        completed = _run_shim(["--secret-path", str(path)], _reporter(str(path)))
    finally:
        path.unlink(missing_ok=True)

    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    report = json.loads(completed.stdout)
    assert report["watched_exists"] is False
    assert json.loads(report["bundle"]) == BUNDLE


def test_the_descriptor_sweep_closes_a_write_end_leaked_into_the_container() -> None:
    """Otherwise the bundle never reaches EOF and the run hangs on a full delivery."""

    read_fd, write_fd = os.pipe()
    os.write(write_fd, BUNDLE_BYTES)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            SHIM_MODULE,
            "--secret-pipe-fd",
            str(read_fd),
            *_reporter(),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_environment(),
        pass_fds=(read_fd, write_fd),
    )
    # Both ends were handed over; this process keeps neither, so the only write
    # end left anywhere is the one inside the "container".
    os.close(write_fd)
    os.close(read_fd)
    stdout, stderr = process.communicate(timeout=120)

    assert process.returncode == 0, stderr.decode("utf-8", "replace")
    assert json.loads(json.loads(stdout)["bundle"]) == BUNDLE


def test_the_child_harness_reads_the_bundle_the_shim_installed(tmp_path: Path) -> None:
    """End to end: shim, exec, ``read_secrets`` on the named fd, result envelope.

    The provider returns digests of the material rather than the material, since
    the child redacts every credential value out of the envelope on the way out.
    Digests are what makes "the bytes arrived intact" assertable at all.
    """

    (tmp_path / "shim_conformance_provider.py").write_text(_PROVIDER, encoding="utf-8")
    context = RunContext(
        protocol_version=PROTOCOL_VERSION.render(),
        run_id="run-shim-conformance",
        interface_id="conformance.secret_channel",
        interface_digest="sha256:" + "11" * 32,
        implementation_digest="sha256:" + "22" * 32,
        entrypoint="shim_conformance_provider:Fingerprint",
        input_bucket="size=tiny",
        budgets=Budgets(wall_clock_seconds=60.0, output_bytes=65536),
        secret_channel=container_secret_channel(BUNDLE),
    )

    read_fd, write_fd = os.pipe()
    os.write(write_fd, BUNDLE_BYTES)
    os.close(write_fd)
    try:
        completed = _run_shim(
            ["--secret-pipe-fd", str(read_fd)],
            [sys.executable, "-m", CHILD_MODULE],
            stdin=context.to_json(),
            pass_fds=(read_fd,),
            extra_roots=(tmp_path,),
        )
    finally:
        with contextlib.suppress(OSError):
            os.close(read_fd)

    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    envelope = parse_result_envelope(completed.stdout)
    assert envelope.status == "ok", envelope.model_dump_json()
    assert envelope.run_id == "run-shim-conformance"
    assert envelope.output is not None
    assert envelope.output["refs"] == sorted(BUNDLE)
    assert envelope.output["fingerprints"] == {
        ref: hashlib.sha256(value.encode("utf-8")).hexdigest()
        for ref, value in sorted(BUNDLE.items())
    }
    for value in BUNDLE.values():
        assert value not in completed.stdout.decode("utf-8", "replace")
