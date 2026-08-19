"""Materialization cache integrity, permissions, and concurrency."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from cruxible_provider_runtime.cache import SEAL_FILENAME, MaterializationCache, tree_digest
from cruxible_provider_runtime.errors import RefusalCode, RefusalError

DIGEST_A = "sha256:" + "a1" * 32
DIGEST_B = "sha256:" + "b2" * 32


def _builder(content: bytes = b"payload") -> "object":
    def build(target: Path) -> None:
        (target / "lib").mkdir()
        (target / "lib" / "module.py").write_bytes(content)
        (target / "resolution.json").write_bytes(b"[]")

    return build


def test_root_is_created_0700(tmp_path: Path) -> None:
    cache = MaterializationCache(tmp_path / "cache")
    root = cache.ensure_root()
    assert oct(root.stat().st_mode & 0o777) == "0o700"


def test_group_readable_root_refuses(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    root.mkdir(mode=0o755)
    cache = MaterializationCache(root)
    with pytest.raises(RefusalError) as exc:
        cache.ensure_root()
    assert exc.value.code is RefusalCode.CACHE_PERMISSIONS


def test_materialize_then_verify(tmp_path: Path) -> None:
    cache = MaterializationCache(tmp_path / "cache")
    path = cache.get_or_materialize(DIGEST_A, _builder())  # type: ignore[arg-type]
    assert (path / "lib" / "module.py").read_bytes() == b"payload"
    assert (path / SEAL_FILENAME).is_file()
    assert cache.verify(DIGEST_A) == path


def test_second_bind_does_not_rebuild(tmp_path: Path) -> None:
    cache = MaterializationCache(tmp_path / "cache")
    calls: list[int] = []

    def build(target: Path) -> None:
        calls.append(1)
        (target / "marker").write_text("built")

    cache.get_or_materialize(DIGEST_A, build)
    cache.get_or_materialize(DIGEST_A, build)
    assert len(calls) == 1


def test_tampered_tree_refuses_verification(tmp_path: Path) -> None:
    cache = MaterializationCache(tmp_path / "cache")
    path = cache.get_or_materialize(DIGEST_A, _builder())  # type: ignore[arg-type]
    (path / "lib" / "module.py").write_bytes(b"tampered")
    with pytest.raises(RefusalError) as exc:
        cache.verify(DIGEST_A)
    assert exc.value.code is RefusalCode.CACHE_INTEGRITY


def test_tampered_tree_is_rebuilt_never_reused(tmp_path: Path) -> None:
    cache = MaterializationCache(tmp_path / "cache")
    path = cache.get_or_materialize(DIGEST_A, _builder())  # type: ignore[arg-type]
    (path / "lib" / "module.py").write_bytes(b"tampered")
    rebuilt = cache.get_or_materialize(DIGEST_A, _builder())  # type: ignore[arg-type]
    assert (rebuilt / "lib" / "module.py").read_bytes() == b"payload"


def test_directory_name_is_never_trusted(tmp_path: Path) -> None:
    """A tree sealed under one digest cannot be served under another."""

    cache = MaterializationCache(tmp_path / "cache")
    cache.get_or_materialize(DIGEST_A, _builder())  # type: ignore[arg-type]
    os.rename(cache.path_for(DIGEST_A), cache.path_for(DIGEST_B))
    with pytest.raises(RefusalError) as exc:
        cache.verify(DIGEST_B)
    assert exc.value.code is RefusalCode.CACHE_INTEGRITY
    assert exc.value.refusal.detail["sealed"] == DIGEST_A


def test_missing_seal_refuses(tmp_path: Path) -> None:
    cache = MaterializationCache(tmp_path / "cache")
    path = cache.get_or_materialize(DIGEST_A, _builder())  # type: ignore[arg-type]
    (path / SEAL_FILENAME).unlink()
    with pytest.raises(RefusalError) as exc:
        cache.verify(DIGEST_A)
    assert exc.value.code is RefusalCode.CACHE_INTEGRITY


def test_seal_excludes_itself_from_the_tree_digest(tmp_path: Path) -> None:
    cache = MaterializationCache(tmp_path / "cache")
    path = cache.get_or_materialize(DIGEST_A, _builder())  # type: ignore[arg-type]
    before = tree_digest(path)
    assert (path / SEAL_FILENAME).is_file()
    assert tree_digest(path) == before


def test_racing_binds_materialize_exactly_once(tmp_path: Path) -> None:
    """Two binds race; exactly one materializes and both verify the same tree."""

    cache = MaterializationCache(tmp_path / "cache")
    cache.ensure_root()
    counter = tmp_path / "builds.log"
    counter.touch()
    start = threading.Barrier(4)
    results: dict[int, Path] = {}
    failures: list[BaseException] = []

    def build(target: Path) -> None:
        with counter.open("a") as handle:
            handle.write("build\n")
        time.sleep(0.05)  # widen the window the lock has to cover
        (target / "lib").mkdir()
        (target / "lib" / "module.py").write_bytes(b"payload")

    def bind(index: int) -> None:
        try:
            start.wait(timeout=10)
            results[index] = cache.get_or_materialize(DIGEST_A, build)
        except BaseException as exc:  # noqa: BLE001 - reported below
            failures.append(exc)

    threads = [threading.Thread(target=bind, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not failures, failures
    assert len(results) == 4
    assert len(set(results.values())) == 1
    assert counter.read_text().count("build") == 1
    assert cache.verify(DIGEST_A) == results[0]


def test_failed_build_leaves_no_entry(tmp_path: Path) -> None:
    cache = MaterializationCache(tmp_path / "cache")

    def build(target: Path) -> None:
        (target / "half-written").write_text("x")
        raise RuntimeError("build failed")

    with pytest.raises(RuntimeError):
        cache.get_or_materialize(DIGEST_A, build)
    assert not cache.path_for(DIGEST_A).exists()
    assert not any(p.name.startswith(".staging-") for p in cache.root.iterdir())


def test_malformed_digest_refuses(tmp_path: Path) -> None:
    cache = MaterializationCache(tmp_path / "cache")
    with pytest.raises(RefusalError) as exc:
        cache.path_for("not-a-digest")
    assert exc.value.code is RefusalCode.CACHE_INTEGRITY
