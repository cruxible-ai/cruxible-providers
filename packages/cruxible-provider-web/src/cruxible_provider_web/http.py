"""The instrumented HTTP client both web-plane implementations use.

Egress recording is not something an adapter remembers to do; it is wired into
the client itself, on httpx's request event hook, so that every request the
client makes — including each hop of a redirect chain, which is where an
un-instrumented adapter quietly contacts a host nobody declared — reaches the
run's :class:`EgressRecorder`.

The response body is read with a cap. A provider that pulls an arbitrarily large
body into memory has made the executor's output-size budget the only thing
standing between a hostile origin and the machine, and that budget is measured
on the *result*, not on what was read to produce it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

import httpx
from cruxible_provider_runtime.egress import EgressRecorder

from .recordings import is_fixture_url, recording_for

__all__ = [
    "USER_AGENT",
    "ClientFactory",
    "HttpResponse",
    "RecordingClient",
    "ResponseTooLarge",
    "default_client_factory",
    "packaged_recording_transport",
]

USER_AGENT = "cruxible-provider-web/0.1 (+https://cruxible.ai)"


class ResponseTooLarge(Exception):
    """The origin sent more than the run's declared cap."""

    def __init__(self, cap_bytes: int, read_bytes: int) -> None:
        self.cap_bytes = cap_bytes
        self.read_bytes = read_bytes
        super().__init__(f"response exceeded the declared cap of {cap_bytes} bytes")


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes
    final_url: str
    from_recording: str | None = None
    """The id of the packaged recording that served this, when one did."""

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")


class RecordingClient:
    """An httpx client whose every request lands in the egress recorder."""

    def __init__(
        self,
        recorder: EgressRecorder,
        *,
        timeout_seconds: float,
        transport: httpx.BaseTransport | None = None,
        recording_id: str | None = None,
    ) -> None:
        self._recording_id = recording_id
        self._client = httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=True,
            transport=transport,
            headers={"user-agent": USER_AGENT},
            event_hooks={"request": [lambda request: recorder.record(str(request.url))]},
        )

    def __enter__(self) -> RecordingClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get(
        self, url: str, *, headers: Mapping[str, str] | None = None, cap_bytes: int
    ) -> HttpResponse:
        """GET ``url``, reading at most ``cap_bytes`` of body.

        The cap is enforced while streaming rather than after: a
        ``Content-Length`` an origin declares is a claim, and a body that keeps
        arriving after the cap is exactly the case the cap exists for.
        """

        with self._client.stream("GET", url, headers=dict(headers or {})) as response:
            chunks: list[bytes] = []
            read = 0
            for chunk in response.iter_bytes():
                read += len(chunk)
                if read > cap_bytes:
                    response.close()
                    raise ResponseTooLarge(cap_bytes, read)
                chunks.append(chunk)
            return HttpResponse(
                status_code=response.status_code,
                headers={key.lower(): value for key, value in response.headers.items()},
                body=b"".join(chunks),
                final_url=str(response.url),
                from_recording=self._recording_id,
            )


class ClientFactory(Protocol):
    """How an implementation obtains a client for one run.

    Injected so that a test can drive the adapter against an arbitrary
    transport. The default below is what runs in production, and what runs in
    the conformance suite too: the packaged-recording transport is selected by
    the reserved ``fixture.invalid`` host, not by a switch a caller could flip.
    """

    def __call__(
        self, recorder: EgressRecorder, *, url: str, timeout_seconds: float
    ) -> RecordingClient: ...


def packaged_recording_transport(url: str) -> tuple[httpx.BaseTransport, str] | None:
    """A transport serving the packaged recording for ``url``, if there is one."""

    if not is_fixture_url(url):
        return None
    recording = recording_for(url)
    if recording is None:
        return None
    recorded = recording.response

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=recorded.status_code,
            headers=recorded.headers,
            content=recorded.body.encode("utf-8"),
            request=request,
        )

    return httpx.MockTransport(handler), recording.id


def default_client_factory(
    recorder: EgressRecorder, *, url: str, timeout_seconds: float
) -> RecordingClient:
    """The production client, with one reserved-host exception.

    A request to ``fixture.invalid`` is served from the recording shipped in this
    distribution. The host is reserved by RFC 2606 and cannot resolve, so this
    branch can never intercept a resource a caller actually asked for; what it
    buys is a conformance suite that exercises the real client end to end
    without a socket, and a package that can prove its own fixtures after
    installation.
    """

    packaged = packaged_recording_transport(url)
    if packaged is None:
        return RecordingClient(recorder, timeout_seconds=timeout_seconds)
    transport, recording_id = packaged
    return RecordingClient(
        recorder, timeout_seconds=timeout_seconds, transport=transport, recording_id=recording_id
    )
