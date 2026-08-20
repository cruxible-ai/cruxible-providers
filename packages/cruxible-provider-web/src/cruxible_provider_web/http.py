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

**Redirects are followed by hand.** httpx follows them for free, and forwarding
is exactly the problem: it carries every header it was given to the destination
and strips only ``Authorization``. ``web.fetch`` lets a caller name the header
its credential travels on, so a token on ``x-api-key`` would be handed to
whatever second host the first one names — and the recorder would learn that host
existed only after the credential had already gone to it. Hop by hop, this client
gets to decide before anything is sent.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from cruxible_provider_runtime.egress import EgressRecorder, normalize_endpoint
from cruxible_provider_runtime.errors import RefusalCode, refuse

from .recordings import is_fixture_url, recording_for

__all__ = [
    "MAX_REDIRECTS",
    "USER_AGENT",
    "ClientFactory",
    "HttpResponse",
    "RecordingClient",
    "ResponseTooLarge",
    "default_client_factory",
    "packaged_recording_transport",
]

USER_AGENT = "cruxible-provider-web/0.1 (+https://cruxible.ai)"

MAX_REDIRECTS = 20
"""How many hops a chain may take before this client stops following it.

httpx's own default, kept deliberately: the number is not the interesting part,
and a chain that loops is caught by it just as a chain that is merely long is —
one refusal, one meaning, rather than a cycle detector that has to decide whether
two URLs differing by a session parameter are the same place.
"""

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


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
            # Off, and :meth:`get` follows the chain itself. See the module
            # docstring: automatic following discloses a caller-named credential
            # header to the destination before this client can look at it.
            follow_redirects=False,
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
        """GET ``url``, following its redirect chain, reading at most ``cap_bytes``.

        The cap is enforced while streaming rather than after: a
        ``Content-Length`` an origin declares is a claim, and a body that keeps
        arriving after the cap is exactly the case the cap exists for.

        **Every header in ``headers`` is treated as credential-bearing.** A
        client cannot tell one caller-chosen header name from another —
        ``x-api-key`` carries exactly what ``authorization`` carries, and this
        interface lets the run name either — so the safe reading of a per-request
        header is that the adapter attached it because it had to, and both
        adapters in this plane attach nothing else. An authenticated fetch
        redirected to a **different origin** therefore refuses rather than
        following: see :func:`_next_hop` for why this plane declines instead of
        stripping.
        """

        carried = dict(headers or {})
        target = url
        for _ in range(MAX_REDIRECTS + 1):
            with self._client.stream("GET", target, headers=carried) as response:
                location = response.headers.get("location")
                if response.status_code not in REDIRECT_STATUSES or not location:
                    return self._read(response, cap_bytes)
                response.close()
            # Outside the `with`: the hop is decided on what the origin said, and
            # the connection it said it on is of no further use.
            target = _next_hop(str(response.url), location, credentialed=bool(carried))
        raise refuse(
            RefusalCode.REDIRECT_LIMIT,
            f"the redirect chain did not settle within {MAX_REDIRECTS} hops",
            url=url,
            next_url=target,
            max_redirects=MAX_REDIRECTS,
        )

    def _read(self, response: httpx.Response, cap_bytes: int) -> HttpResponse:
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


def _next_hop(current: str, location: str, *, credentialed: bool) -> str:
    """The URL a redirect names, judged before anything is sent to it.

    A credentialed fetch that changes origin is **refused, not stripped**. Both
    were available, and this plane already holds the position the refusal
    follows from: a run admitted into the ``access=authenticated`` bucket does
    not silently go out anonymous — the undelivered-credential refusal says so
    in as many words. Stripping would do precisely that, and would hand the
    caller a document retrieved under terms the receipt no longer describes,
    with nothing in it to distinguish that document from one the credential
    actually opened. Refusing leaves the caller a choice they can act on: reissue
    against the final URL, with the credential they meant to send there.

    The one origin change that is not a disclosure is an ``http`` to ``https``
    upgrade of the same host, and it is not refused. The credential has already
    crossed the wire in clear by then, so declining costs the run and protects
    nothing. httpx makes the same exception, for the same reason.
    """

    destination = str(httpx.URL(current).join(location))
    if urlsplit(destination).scheme not in {"http", "https"}:
        raise refuse(
            RefusalCode.UNSUPPORTED_REDIRECT_SCHEME,
            "the origin redirected to a scheme this interface does not retrieve",
            url=current,
            redirect_to=destination,
        )
    if credentialed and _crosses_origin(current, destination):
        raise refuse(
            RefusalCode.CROSS_ORIGIN_CREDENTIALED_REDIRECT,
            "the origin redirected an authenticated fetch to a different origin",
            url=normalize_endpoint(current),
            redirect_to=normalize_endpoint(destination),
        )
    return destination


def _crosses_origin(current: str, destination: str) -> bool:
    """Whether following ``destination`` would disclose to somebody new."""

    here = normalize_endpoint(current)
    there = normalize_endpoint(destination)
    if here == there:
        return False
    upgraded = here.replace("http://", "https://", 1) if here.startswith("http://") else here
    return there != upgraded


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
