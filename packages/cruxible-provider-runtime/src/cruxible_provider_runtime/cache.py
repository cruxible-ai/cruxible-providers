"""The materialization cache.

Rules the contract fixes:

* keyed by materialization digest;
* a sealed marker file carries the digest of the materialized tree and is
  re-verified before **every** bind — a directory name is never trusted;
* the cache root is user-owned ``0700``; wrong permissions refuse;
* no shared multi-user cache;
* materialize into a temp dir and atomically rename, under a per-digest lock;
* any verification failure refuses and rebuilds — it never falls back.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .canonical import SHA256_RE
from .errors import RefusalCode, refuse

__all__ = ["MaterializationCache", "SEAL_FILENAME", "tree_digest"]

SEAL_FILENAME = ".cruxible-seal.json"
SEAL_VERSION = 1

EnvironmentBuilder = Callable[[Path], None]
"""Populates a fresh directory with a materialized environment."""


def tree_digest(root: Path) -> str:
    """A content digest over a directory tree.

    Covers every regular file's path, executable bit, and content, plus symlink
    targets. The seal file itself is excluded, since it carries this value.
    """

    entries: list[tuple[str, str, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative == SEAL_FILENAME:
            continue
        if path.is_symlink():
            entries.append((relative, "link", os.readlink(path)))
            continue
        if path.is_dir():
            entries.append((relative, "dir", ""))
            continue
        mode = "x" if path.stat().st_mode & stat.S_IXUSR else "-"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append((relative, f"file:{mode}", digest))
    hasher = hashlib.sha256()
    for relative, kind, value in entries:
        hasher.update(relative.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(kind.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(value.encode("utf-8"))
        hasher.update(b"\n")
    return f"sha256:{hasher.hexdigest()}"


def _digest_dirname(materialization_digest: str) -> str:
    if not SHA256_RE.match(materialization_digest):
        raise refuse(
            RefusalCode.CACHE_INTEGRITY,
            f"materialization digest {materialization_digest!r} is not sha256:<hex>",
            digest=materialization_digest,
        )
    return materialization_digest.replace(":", "-")


class MaterializationCache:
    """A single-user cache of materialized provider environments."""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    # -- root hygiene ------------------------------------------------------

    def ensure_root(self) -> Path:
        """Create the cache root ``0700`` or refuse an unsafe existing one."""

        if not self._root.exists():
            self._root.mkdir(parents=True, mode=0o700)
            return self._root
        if not self._root.is_dir():
            raise refuse(
                RefusalCode.CACHE_PERMISSIONS,
                f"cache root {self._root} is not a directory",
                root=str(self._root),
            )
        info = self._root.stat()
        if info.st_uid != os.getuid():
            raise refuse(
                RefusalCode.CACHE_PERMISSIONS,
                f"cache root {self._root} is not owned by the current user; "
                "no shared multi-user cache",
                root=str(self._root),
                owner_uid=info.st_uid,
                current_uid=os.getuid(),
            )
        if info.st_mode & 0o077:
            raise refuse(
                RefusalCode.CACHE_PERMISSIONS,
                f"cache root {self._root} is group- or world-accessible; 0700 is required",
                root=str(self._root),
                mode=oct(info.st_mode & 0o777),
            )
        return self._root

    # -- paths -------------------------------------------------------------

    def path_for(self, materialization_digest: str) -> Path:
        return self._root / _digest_dirname(materialization_digest)

    def _lock_path(self, materialization_digest: str) -> Path:
        return self._root / (_digest_dirname(materialization_digest) + ".lock")

    # -- sealing -----------------------------------------------------------

    def _write_seal(self, path: Path, materialization_digest: str) -> None:
        seal: dict[str, Any] = {
            "seal_version": SEAL_VERSION,
            "materialization_digest": materialization_digest,
            "tree_digest": tree_digest(path),
        }
        (path / SEAL_FILENAME).write_text(
            json.dumps(seal, sort_keys=True, separators=(",", ":")), encoding="utf-8"
        )

    def verify(self, materialization_digest: str) -> Path:
        """Re-verify a sealed tree, refusing on any discrepancy."""

        path = self.path_for(materialization_digest)
        seal_path = path / SEAL_FILENAME
        if not seal_path.is_file():
            raise refuse(
                RefusalCode.CACHE_INTEGRITY,
                f"cache entry {path} carries no seal",
                digest=materialization_digest,
            )
        try:
            seal = json.loads(seal_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise refuse(
                RefusalCode.CACHE_INTEGRITY,
                f"cache entry {path} carries an unreadable seal",
                digest=materialization_digest,
            ) from exc
        if seal.get("seal_version") != SEAL_VERSION:
            raise refuse(
                RefusalCode.CACHE_INTEGRITY,
                f"cache entry {path} carries an unknown seal version",
                digest=materialization_digest,
                seal_version=seal.get("seal_version"),
            )
        if seal.get("materialization_digest") != materialization_digest:
            raise refuse(
                RefusalCode.CACHE_INTEGRITY,
                "cache entry seal names a different materialization digest; "
                "a directory name is never trusted",
                digest=materialization_digest,
                sealed=seal.get("materialization_digest"),
            )
        actual = tree_digest(path)
        if seal.get("tree_digest") != actual:
            raise refuse(
                RefusalCode.CACHE_INTEGRITY,
                f"cache entry {path} has been modified since it was sealed",
                digest=materialization_digest,
                sealed=seal.get("tree_digest"),
                actual=actual,
            )
        return path

    # -- locking -----------------------------------------------------------

    @contextmanager
    def _digest_lock(self, materialization_digest: str) -> Iterator[None]:
        import fcntl

        lock_path = self._lock_path(materialization_digest)
        handle = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                os.close(handle)

    # -- the bind path -----------------------------------------------------

    def get_or_materialize(
        self, materialization_digest: str, builder: EnvironmentBuilder
    ) -> Path:
        """Return a verified cache entry, materializing it once if absent.

        Concurrent binds contend on a per-digest lock: exactly one materializes,
        and both verify the same sealed tree.
        """

        self.ensure_root()
        path = self.path_for(materialization_digest)
        if path.exists():
            try:
                return self.verify(materialization_digest)
            except Exception:
                # Verification failure refuses the entry and rebuilds; it never
                # falls back to the unverified tree.
                pass
        with self._digest_lock(materialization_digest):
            if path.exists():
                try:
                    return self.verify(materialization_digest)
                except Exception:
                    shutil.rmtree(path, ignore_errors=True)
            staging = Path(
                tempfile.mkdtemp(prefix=".staging-", dir=str(self._root))
            )
            try:
                os.chmod(staging, 0o700)
                builder(staging)
                self._write_seal(staging, materialization_digest)
                try:
                    os.rename(staging, path)
                except OSError as exc:
                    if exc.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                        raise
                    shutil.rmtree(staging, ignore_errors=True)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        return self.verify(materialization_digest)
