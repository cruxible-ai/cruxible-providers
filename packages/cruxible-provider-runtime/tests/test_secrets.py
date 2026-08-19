"""Secret delivery over an inherited descriptor, and redaction."""

from __future__ import annotations

import json
import os

import pytest

from cruxible_provider_runtime.errors import RefusalCode, RefusalError
from cruxible_provider_runtime.secrets import (
    REDACTION_PLACEHOLDER,
    Redactor,
    assert_no_secret_leak,
    open_secret_channel,
    read_secrets,
)

DUMMY = {"provider.api_key": "dummy-credential-9f3c1a"}


def test_channel_round_trip() -> None:
    with open_secret_channel(DUMMY) as fd:
        assert read_secrets(os.dup(fd)) == DUMMY


def test_empty_channel_yields_no_secrets() -> None:
    with open_secret_channel({}) as fd:
        assert read_secrets(os.dup(fd)) == {}


def test_malformed_channel_payload_refuses() -> None:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"not json")
    os.close(write_fd)
    with pytest.raises(RefusalError) as exc:
        read_secrets(read_fd)
    assert exc.value.code is RefusalCode.UNRESOLVED_SECRET_REF


def test_non_flat_channel_payload_refuses() -> None:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, json.dumps({"ref": {"nested": True}}).encode())
    os.close(write_fd)
    with pytest.raises(RefusalError) as exc:
        read_secrets(read_fd)
    assert exc.value.code is RefusalCode.UNRESOLVED_SECRET_REF


def test_redactor_scrubs_nested_structures() -> None:
    redactor = Redactor(DUMMY)
    payload = {
        "trace": {"events": [{"note": "dummy-credential-9f3c1a used"}]},
        "list": ["dummy-credential-9f3c1a"],
    }
    scrubbed = redactor.scrub(payload)
    rendered = json.dumps(scrubbed)
    assert "dummy-credential-9f3c1a" not in rendered
    assert REDACTION_PLACEHOLDER in rendered


def test_redactor_prefers_longer_values_so_no_tail_survives() -> None:
    secrets = {"short": "abc", "long": "abcdef"}
    redactor = Redactor(secrets)
    assert "abc" not in redactor.text("abcdef")


def test_assert_no_secret_leak_refuses() -> None:
    with pytest.raises(RefusalError) as exc:
        assert_no_secret_leak(
            {"leaked": "dummy-credential-9f3c1a"}, DUMMY, where="result envelope"
        )
    assert exc.value.code is RefusalCode.SECRET_LEAK
    assert exc.value.refusal.detail["refs"] == ["provider.api_key"]


def test_assert_no_secret_leak_passes_on_clean_payload() -> None:
    assert_no_secret_leak({"clean": "nothing here"}, DUMMY, where="result envelope")
