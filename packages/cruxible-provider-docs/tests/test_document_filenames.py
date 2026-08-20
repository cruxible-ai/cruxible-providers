"""A caller's filename never decides where this process writes.

The OCR engine needs the document's bytes on disk before it can read them, so
the run input's filename reaches a path join. ``Path(directory) / name``
discards the directory when ``name`` is absolute and walks out of it when
``name`` contains ``..``, and the local backend is a dependency-isolation
mechanism rather than a security boundary — the write lands with the operator's
privileges, wherever the caller pointed it.

Two rules, tested here because both are pure and neither needs an engine: a
declared filename must be a plain name, and a temporary file takes nothing from
it but a suffix that has been checked to be a suffix.
"""

from __future__ import annotations

import base64

import pytest
from cruxible_provider_docs.documents import DEFAULT_SUFFIX, document_suffix, resolve_source
from cruxible_provider_runtime.errors import RefusalCode, RefusalError


def _inline(filename: str) -> dict[str, object]:
    return {
        "source": {
            "kind": "inline",
            "filename": filename,
            "media_type": "image/png",
            "content_base64": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode("ascii"),
        }
    }


HOSTILE_FILENAMES = [
    pytest.param("/etc/cron.d/payload", id="absolute"),
    pytest.param("../../../../etc/cron.d/payload", id="traversal"),
    pytest.param("nested/scan.png", id="separator"),
    pytest.param("nested\\scan.png", id="windows-separator"),
    pytest.param("..", id="parent"),
    pytest.param(".", id="here"),
    pytest.param(".hidden.png", id="leading-dot"),
    pytest.param("scan\x00.png", id="nul"),
    # Carries no separator at all and still names somewhere else.
    pytest.param("C:evil.png", id="drive-relative"),
]


@pytest.mark.parametrize("filename", HOSTILE_FILENAMES)
def test_a_filename_that_is_not_a_plain_name_refuses(filename: str) -> None:
    with pytest.raises(RefusalError) as exc:
        resolve_source(_inline(filename), interface_id="ocr.extract")
    assert exc.value.code is RefusalCode.PROVIDER_DECLINED


def test_an_ordinary_filename_still_resolves() -> None:
    """The check must not have become one that refuses everything."""

    resolved = resolve_source(_inline("scan-clean.png"), interface_id="ocr.extract")
    assert resolved.filename == "scan-clean.png"


@pytest.mark.parametrize(
    ("filename", "suffix"),
    [
        ("scan-clean.png", ".png"),
        ("report.PDF", ".PDF"),
        ("archive.tar.gz", ".gz"),
        ("no-extension", DEFAULT_SUFFIX),
        ("trailing.", DEFAULT_SUFFIX),
        ("odd.name with spaces", DEFAULT_SUFFIX),
        ("padded.p n g", DEFAULT_SUFFIX),
        ("long.abcdefghijklmnop", DEFAULT_SUFFIX),
        ("../../etc/passwd", DEFAULT_SUFFIX),
    ],
)
def test_only_a_validated_suffix_survives_into_a_temporary_name(filename: str, suffix: str) -> None:
    """Whatever fails the check contributes a fixed suffix, not itself."""

    assert document_suffix(filename) == suffix


def test_a_generated_temporary_name_carries_no_directory_component() -> None:
    """The property the engine relies on, stated as one assertion."""

    for filename in ("/etc/passwd.png", "../../x.png", "a/b/c.png"):
        generated = f"page{document_suffix(filename)}"
        assert "/" not in generated
        assert "\\" not in generated
        assert not generated.startswith(".")
