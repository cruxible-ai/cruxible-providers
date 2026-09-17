"""Read transferred wheel metadata without importing provider code."""

from __future__ import annotations

import hashlib
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import PathDistribution
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

from .registration import RegistrationBundle, registration_from_distribution
from .resolution import ResolvedDistribution


def wheel_pin(path: Path) -> ResolvedDistribution:
    name, version, _build, _tags = parse_wheel_filename(path.name)
    with ZipFile(path) as archive:
        metadata = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            raise ValueError("wheel must carry exactly one distribution metadata record")
        from email.parser import BytesParser

        record = BytesParser().parsebytes(archive.read(metadata[0]))
        if canonicalize_name(record["Name"]) != name or Version(record["Version"]) != version:
            raise ValueError("wheel filename and distribution metadata disagree")
    return ResolvedDistribution(
        name=str(name),
        version=str(version),
        kind="wheel",
        filename=path.name,
        artifact_id="sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        url=path.resolve().as_uri(),
    )


@contextmanager
def wheel_registration(path: Path) -> Iterator[RegistrationBundle]:
    """Inspect one wheel's data in disposable storage; execute no entry point."""
    wheel_pin(path)
    with TemporaryDirectory(prefix="cruxible-wheel-") as temporary, ZipFile(path) as archive:
        root = Path(temporary)
        seen: set[str] = set()
        if sum(item.file_size for item in archive.infolist()) > 128 * 1024 * 1024:
            raise ValueError("provider wheel metadata inspection exceeds its unpacked size limit")
        for item in archive.infolist():
            member = PurePosixPath(item.filename)
            if (
                member.is_absolute()
                or ".." in member.parts
                or "\\" in item.filename
                or str(member) in seen
                or stat.S_ISLNK(item.external_attr >> 16)
            ):
                raise ValueError("wheel contains an unsafe or duplicate member")
            seen.add(str(member))
            destination = root.joinpath(*member.parts)
            if item.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(item))
        metadata = tuple(root.glob("*.dist-info"))
        if len(metadata) != 1:
            raise ValueError("wheel must carry exactly one top-level distribution")
        yield registration_from_distribution(PathDistribution(metadata[0]))
