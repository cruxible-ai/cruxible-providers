"""Generates the committed bucket fixtures, deterministically.

The medium fixtures are tens of kilobytes of base64, which nobody can review by
eye. So the fixtures are not hand-written: this module derives every payload from
a fixed rule, runs the adapter over it, and writes what it produced. A test
asserts that regenerating is a byte-identical no-op, which is what makes the
committed blobs reviewable -- the review is of this file.

    uv run python packages/cruxible-provider-workspace/tests/fixture_generation.py
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

from cruxible_provider_runtime.canonical import canonical_json, sha256_hex
from cruxible_provider_runtime.egress import EgressRecorder
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.provider_api import ProviderRunContext
from cruxible_provider_workspace.file import WorkspaceFile
from cruxible_provider_workspace.fixtures import FIXTURES_DIR
from cruxible_provider_workspace.interface import (
    INTERFACE_DIGEST,
    INTERFACE_ID,
    VOCABULARY,
    classify,
)

COMMITMENT_DIGEST = "sha256:" + "c0" * 32
"""A stand-in for the G4 derived-request commitment; the adapter echoes it, nothing more."""


def _text_lines(count: int, *, newline: str, trailing: bool, bom: bool = False) -> bytes:
    lines = [
        f"line {index:05d}: the quick brown fox jumps over the lazy dog" for index in range(count)
    ]
    text = newline.join(lines) + (newline if trailing else "")
    return ("\ufeff" if bom else "").encode("utf-8") + text.encode("utf-8")


def _pseudo_random(length: int, *, seed: str) -> bytes:
    """A sha256 chain: deterministic, incompressible, and not UTF-8 for long."""

    out = bytearray()
    block = seed.encode("utf-8")
    while len(out) < length:
        block = hashlib.sha256(block).digest()
        out.extend(block)
    return bytes(out[:length])


PAYLOADS: dict[str, tuple[str, bytes]] = {
    "workspace-file-text-tiny": (
        "The smallest text case: three lines, LF-terminated, one non-ASCII character, "
        "a trailing newline. What a README fragment looks like.",
        "# Reach readings\n\nUpper reach: 4.1 mg/l nitrate \u2014 see the tide-gauge report.\n".encode(
            "utf-8"
        ),
    ),
    "workspace-file-text-small": (
        "CRLF text between 4 KiB and 64 KiB: the line view must drop the carriage "
        "return before each feed and report the style rather than rewriting it.",
        _text_lines(600, newline="\r\n", trailing=True),
    ),
    "workspace-file-text-medium": (
        "LF text between 64 KiB and 1 MiB with a byte-order mark and no trailing "
        "newline: the BOM is reported and kept, the last line has no terminator.",
        _text_lines(1200, newline="\n", trailing=False, bom=True),
    ),
    "workspace-file-binary-tiny": (
        "A NUL byte makes bytes binary whatever else they decode as: a PNG signature "
        "followed by an IHDR chunk header.",
        bytes.fromhex("89504e470d0a1a0a0000000d49484452000000100000001008060000001ff3ff61"),
    ),
    "workspace-file-binary-small": (
        "Incompressible bytes between 4 KiB and 64 KiB, from a sha256 chain.",
        _pseudo_random(5_000, seed="workspace-file-binary-small"),
    ),
    "workspace-file-binary-medium": (
        "Incompressible bytes between 64 KiB and 1 MiB, from a sha256 chain.",
        _pseudo_random(70_000, seed="workspace-file-binary-medium"),
    ),
}


def fixture_input(logical_source: str, data: bytes) -> dict[str, Any]:
    """The exact run input core would hand the adapter for ``data``."""

    return {
        "logical_source": logical_source,
        "commitment_digest": COMMITMENT_DIGEST,
        "content_encoding": "base64",
        "bytes": base64.b64encode(data).decode("ascii"),
        "byte_length": len(data),
        "bytes_digest": sha256_hex(data),
    }


def run_adapter(payload: dict[str, Any], bucket_id: str) -> dict[str, Any]:
    context = ProviderRunContext(
        run_id="fixture",
        interface_id=INTERFACE_ID,
        interface_digest=INTERFACE_DIGEST,
        implementation_digest="sha256:" + "11" * 32,
        input_bucket=bucket_id,
        input=payload,
        coordinates={},
        budgets=Budgets(wall_clock_seconds=30.0, output_bytes=8_000_000),
        declared_endpoints=(),
        capture_contract="workspace.file.capture.v1",
        secrets={},
        egress=EgressRecorder(),
    )
    result = WorkspaceFile()(context)
    assert result.status == "ok", result.refusal
    assert result.output is not None
    return result.output


def render(fixture_id: str) -> bytes:
    note, data = PAYLOADS[fixture_id]
    payload = fixture_input(f"fixtures/{fixture_id}", data)
    assignment = classify(payload)
    assert assignment is not None
    bucket_id = VOCABULARY.bucket_id(assignment)
    output = run_adapter(payload, bucket_id)
    content = output["content"]
    expect: dict[str, Any] = {
        "status": "ok",
        "kind": content["kind"],
        "bytes_digest": payload["bytes_digest"],
        "output_digest": sha256_hex(canonical_json(output)),
    }
    if content["kind"] == "text":
        expect.update(
            {
                "bom": content["bom"],
                "newline": content["newline"],
                "trailing_newline": content["trailing_newline"],
                "line_count": content["line_count"],
                "character_count": content["character_count"],
                "first_line": content["lines"][0],
                "last_line": content["lines"][-1],
            }
        )
    document = {
        "id": fixture_id,
        "note": note,
        "interface_id": INTERFACE_ID,
        "bucket_selector": bucket_id,
        "bucket_id": bucket_id,
        "input": payload,
        "expect": expect,
    }
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def main(target: Path = FIXTURES_DIR) -> int:
    target.mkdir(parents=True, exist_ok=True)
    for fixture_id in PAYLOADS:
        (target / f"{fixture_id}.json").write_bytes(render(fixture_id))
        print(f"wrote {fixture_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
