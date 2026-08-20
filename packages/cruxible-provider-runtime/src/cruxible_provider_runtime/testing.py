"""Fakes and harness pieces for conformance testing.

Shipped inside the runtime, not the test tree, because every plane package needs
the same fakes to run the same conformance suite against its own adapters.

Nothing here reaches the network, a container engine, or a package index. The
fake index is filesystem-backed; the fake container driver runs the ordinary
child harness in a local subprocess so that the container path is covered at
protocol level; the injected environment builder seals a small tree and hands
back the running interpreter instead of synthesising a venv.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .artifact import ImageProvenance
from .backends import CHILD_MODULE, MaterializationRequest, verify_environment
from .budget import ProcessOutcome, minimal_env, run_with_budget
from .canonical import canonical_json
from .errors import RefusalCode, refuse
from .index import TransportResponse
from .protocol import Budgets
from .resolution import MarkerEnvironment, ResolvedSet

__all__ = [
    "ENGINE_MARKER_ENVIRONMENT",
    "FakeContainerDriver",
    "FakeIndexTransport",
    "InjectedEnvironmentBuilder",
]

_GLIBC_PLATFORMS = (
    "manylinux1_x86_64",
    "manylinux2010_x86_64",
    "manylinux2014_x86_64",
    *(f"manylinux_2_{minor}_x86_64" for minor in (5, 12, 17, 18, 24, 25, 26, 27, 28)),
)

ENGINE_MARKER_ENVIRONMENT = MarkerEnvironment(
    id="linux-cp311-engines",
    markers={
        "implementation_name": "cpython",
        "os_name": "posix",
        "platform_machine": "x86_64",
        "python_full_version": "3.11.9",
        "python_version": "3.11",
        "sys_platform": "linux",
    },
    tags=(
        *(
            f"{python}-{abi}-{platform}"
            for python, abi in (("cp311", "cp311"), ("cp311", "abi3"), ("py3", "none"))
            for platform in _GLIBC_PLATFORMS
        ),
        "py3-none-any",
    ),
)
"""A marker environment broad enough to pin an environment containing an engine.

Shared by every plane package that puts a heavy engine behind an extra, because
the launch environments in ``ci/marker-environments.json`` cannot pin one: they
list three tags each, and a real binary closure reaches for a dozen glibc
platform tags across its packages — a browser driver ships
``py3-none-manylinux1_x86_64``, a tensor library ships
``cp311-cp311-manylinux_2_28_x86_64``, an accelerator runtime ships
``py3-none-manylinux_2_18_x86_64``.

The list is long because tag matching here is **exact string membership**, and
the platform-tag scheme it is matching is not a set of names but an ordering: a
``manylinux_2_17`` wheel is installable on a ``manylinux_2_28`` host, and PEP 600
says so. Teaching the resolver that ordering is a change to what a materialization
digest *means*, so it is not made here; enumerating the tags is the honest
alternative, and this constant is the enumeration plus a note saying why it
exists. The narrowness of the launch environment list is recorded as a finding
for whoever owns the tag vocabulary.
"""


@dataclass
class FakeIndexTransport:
    """A filesystem-backed stand-in for a package index.

    ``redirects`` lets a test exercise the redirect refusal without a server, and
    ``corrupt`` lets it exercise the hash-mismatch refusal.
    """

    files: dict[str, bytes] = field(default_factory=dict)
    redirects: dict[str, str] = field(default_factory=dict)
    statuses: dict[str, int] = field(default_factory=dict)
    requested: list[str] = field(default_factory=list)

    def get(self, url: str) -> TransportResponse:
        self.requested.append(url)
        if url in self.redirects:
            return TransportResponse(status=302, final_url=self.redirects[url], body=b"")
        status = self.statuses.get(url, 200)
        return TransportResponse(status=status, final_url=url, body=self.files.get(url, b""))


@dataclass
class InjectedEnvironmentBuilder:
    """A local backend builder that reuses the running interpreter.

    Materialising a real environment needs the network; a conformance suite must
    not. This builder writes the resolution it was asked to materialise into the
    cached tree — so the seal still covers real, digest-derived content — and
    points the child process at explicitly listed source roots.
    """

    python_path_roots: Sequence[Path] = ()
    interpreter_path: Path = field(default_factory=lambda: Path(sys.executable))
    builds: list[str] = field(default_factory=list)
    stage_divergent_tree: bool = False
    """Populate a site-packages that does NOT match the resolution, for tests."""

    def build(self, request: MaterializationRequest) -> None:
        target = request.target
        resolved = request.resolved
        self.builds.append(resolved.marker_environment.id)
        (target / "resolution.json").write_bytes(canonical_json(resolved.triples()))
        (target / "marker-environment.json").write_bytes(
            canonical_json(resolved.marker_environment.digest_payload())
        )
        self._stage_site_packages(target, resolved)
        # A builder must check its own output before the cache seals it. This
        # one stages a synthetic tree, so the check is over what it staged.
        verify_environment(target, resolved)

    def _stage_site_packages(self, target: Path, resolved: ResolvedSet) -> None:
        site_packages = target / ".venv" / "lib" / "python3.11" / "site-packages"
        site_packages.mkdir(parents=True)
        for entry in resolved.distributions:
            if entry.is_local_source:
                continue
            version = "0.0.0-wrong" if self.stage_divergent_tree else entry.version
            info = site_packages / f"{entry.name}-{version}.dist-info"
            info.mkdir()
            (info / "METADATA").write_text(
                f"Name: {entry.name}\nVersion: {version}\n", encoding="utf-8"
            )

    def interpreter(self, env_path: Path) -> Path:
        del env_path
        return self.interpreter_path

    def child_env(self, env_path: Path) -> Mapping[str, str]:
        del env_path
        return minimal_env(
            {"PYTHONPATH": ":".join(str(Path(root).resolve()) for root in self.python_path_roots)}
        )


@dataclass
class FakeContainerDriver:
    """Covers the container backend at protocol level, with no container engine.

    ``inspect`` returns whatever provenance the test says the image carries, so
    the provenance-mismatch refusal is exercised without building an image.
    ``run`` executes the same child harness a real image would, which is what
    makes "both backend kinds" a real assertion rather than a mock.
    """

    provenance: ImageProvenance
    python_path_roots: Sequence[Path] = ()
    interpreter_path: Path = field(default_factory=lambda: Path(sys.executable))
    known_digests: tuple[str, ...] = ()
    runs: list[str] = field(default_factory=list)

    def inspect(self, image_digest: str) -> ImageProvenance:
        if self.known_digests and image_digest not in self.known_digests:
            raise refuse(
                RefusalCode.IMAGE_PROVENANCE_MISMATCH,
                f"no image with digest {image_digest} is available",
                image_digest=image_digest,
            )
        return self.provenance

    def run(
        self,
        image_digest: str,
        *,
        argv: Sequence[str],
        stdin_bytes: bytes,
        budgets: Budgets,
        pass_fds: Sequence[int],
    ) -> ProcessOutcome:
        self.runs.append(image_digest)
        entrypoint_argv = list(argv)
        # Replace the image's "python" with the interpreter this fake can start.
        if entrypoint_argv and entrypoint_argv[0] == "python":
            entrypoint_argv[0] = str(self.interpreter_path)
        if "-m" not in entrypoint_argv:  # pragma: no cover - defensive
            raise refuse(
                RefusalCode.PROVIDER_PROTOCOL_VIOLATION,
                "container argv does not invoke the child harness",
                argv=list(argv),
            )
        assert entrypoint_argv[entrypoint_argv.index("-m") + 1] == CHILD_MODULE
        return run_with_budget(
            entrypoint_argv,
            stdin_bytes=stdin_bytes,
            budgets=budgets,
            pass_fds=pass_fds,
            env=minimal_env(
                {
                    "PYTHONPATH": ":".join(
                        str(Path(root).resolve()) for root in self.python_path_roots
                    )
                }
            ),
        )
