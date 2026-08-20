"""Secret delivery over an inherited descriptor, and redaction."""

from __future__ import annotations

import json
import os
import threading

import pytest
from cruxible_provider_runtime.errors import RefusalCode, RefusalError
from cruxible_provider_runtime.secrets import (
    MAX_SECRET_BUNDLE_BYTES,
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


def _open_channel_off_thread(secrets: dict[str, str], *, timeout: float = 15.0) -> list[object]:
    """Open a channel on a worker thread and report what happened, or that nothing did.

    The pre-fix channel wrote the whole bundle into the pipe before yielding, so
    an undeliverable bundle parked the caller forever. Asserting "this returned
    at all" needs a thread; asserting it from the thread that would be parked
    does not work.
    """

    outcome: list[object] = []

    def attempt() -> None:
        try:
            with open_secret_channel(secrets) as fd:
                outcome.append(read_secrets(os.dup(fd)))
        except RefusalError as exc:
            outcome.append(exc.code)

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(timeout=timeout)
    assert not worker.is_alive(), "opening the secret channel blocked on an undrained pipe"
    return outcome


def test_an_oversized_bundle_refuses_rather_than_blocking() -> None:
    """No unbounded write before a reader exists — and no reader exists yet."""

    oversized = {"provider.api_key": "x" * (MAX_SECRET_BUNDLE_BYTES * 4)}
    assert _open_channel_off_thread(oversized) == [RefusalCode.SECRET_BUNDLE_TOO_LARGE]


def test_a_bundle_larger_than_the_pipe_buffer_still_delivers() -> None:
    """Delivery is a bounded writer thread, so pipe capacity is not the limit."""

    bundle = {"provider.api_key": "k" * (MAX_SECRET_BUNDLE_BYTES - 64)}
    assert _open_channel_off_thread(bundle) == [bundle]


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
        assert_no_secret_leak({"leaked": "dummy-credential-9f3c1a"}, DUMMY, where="result envelope")
    assert exc.value.code is RefusalCode.SECRET_LEAK
    assert exc.value.refusal.detail["refs"] == ["provider.api_key"]


def test_assert_no_secret_leak_passes_on_clean_payload() -> None:
    assert_no_secret_leak({"clean": "nothing here"}, DUMMY, where="result envelope")


# Credentials that broke the original JSON-substring detector. A real secret may
# contain any of these, and a detector that fails on exactly the values an
# attacker would pick is worse than none, because it reads as coverage.
HOSTILE_CREDENTIALS = [
    pytest.param('quote"inside', id="double-quote"),
    pytest.param("back\\slash", id="backslash"),
    pytest.param("line\nbreak", id="newline"),
    pytest.param("tab\there", id="tab"),
    pytest.param("ünïcode-Ω-値", id="non-ascii"),
    pytest.param("emoji-\U0001f511-key", id="astral"),
    pytest.param('{"looks":"like json"}', id="json-shaped"),
]


@pytest.mark.parametrize("credential", HOSTILE_CREDENTIALS)
def test_leak_detection_survives_hostile_credentials(credential: str) -> None:
    secrets = {"provider.api_key": credential}
    assert Redactor(secrets).leaks({"trace": {"note": credential}}) == [credential]
    with pytest.raises(RefusalError) as exc:
        assert_no_secret_leak({"leaked": credential}, secrets, where="result envelope")
    assert exc.value.code is RefusalCode.SECRET_LEAK


@pytest.mark.parametrize("credential", HOSTILE_CREDENTIALS)
def test_redaction_survives_hostile_credentials(credential: str) -> None:
    secrets = {"provider.api_key": credential}
    redactor = Redactor(secrets)
    scrubbed = redactor.scrub(
        {"a": credential, credential: ["x", credential], "b": {"c": credential}}
    )
    assert redactor.leaks(scrubbed) == []


@pytest.mark.parametrize("credential", HOSTILE_CREDENTIALS)
def test_hostile_credentials_round_trip_through_the_channel(credential: str) -> None:
    secrets = {"provider.api_key": credential}
    with open_secret_channel(secrets) as fd:
        assert read_secrets(os.dup(fd)) == secrets


def test_leak_detection_reaches_inside_bytes() -> None:
    """Trace material is not always ``str``; a bytes blob leaks just as well."""

    secrets = {"provider.api_key": "dummy-credential-9f3c1a"}
    assert Redactor(secrets).leaks({"blob": b"prefix dummy-credential-9f3c1a suffix"})
    assert Redactor(secrets).leaks({"blob": bytearray(b"dummy-credential-9f3c1a")})


def test_bytes_are_scrubbed_not_merely_detected() -> None:
    secrets = {"provider.api_key": "dummy-credential-9f3c1a"}
    redactor = Redactor(secrets)
    scrubbed = redactor.scrub({"blob": b"x dummy-credential-9f3c1a y"})
    assert redactor.leaks(scrubbed) == []


def test_leak_detection_reaches_inside_sets_and_objects() -> None:
    secrets = {"provider.api_key": "dummy-credential-9f3c1a"}
    redactor = Redactor(secrets)
    assert redactor.leaks({"s": {"dummy-credential-9f3c1a"}})

    class Opaque:
        def __repr__(self) -> str:
            return "Opaque(token='dummy-credential-9f3c1a')"

    assert redactor.leaks({"o": Opaque()})


def test_a_clean_payload_is_still_clean() -> None:
    """The detector must not have become one that fires on everything."""

    secrets = {"provider.api_key": "dummy-credential-9f3c1a"}
    redactor = Redactor(secrets)
    assert redactor.leaks({"a": "nothing", "b": [1, 2.5, None, True], "c": b"bytes"}) == []
