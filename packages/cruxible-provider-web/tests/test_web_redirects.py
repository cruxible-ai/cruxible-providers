"""Redirects, and what a credential is allowed to survive.

A redirect is the one place where the host an adapter was told to talk to and the
host it ends up talking to come apart, and it comes apart *inside* the client —
after the headers have gone out. Two properties are asserted here.

The credential one: a header carrying secret material does not cross an origin
boundary. The client cannot tell one caller-chosen header name from another, so
it treats every per-request header as credential-bearing and declines the hop
rather than following it anonymously.

The recording one: following a chain by hand must not cost the receipt anything.
Every hop is a request through the same instrumented client, so every hop's
origin is still in the recorder when the run ends.

The sentinel below is a made-up string. It is asserted absent from what the
second origin received, which is a property no assertion about the first origin
can substitute for.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

import httpx
import pytest
from cruxible_provider_runtime.egress import EgressRecorder
from cruxible_provider_runtime.errors import RefusalCode, RefusalError
from cruxible_provider_web.http import MAX_REDIRECTS, RecordingClient

SENTINEL = "sentinel-not-a-real-key-do-not-use"
CREDENTIAL_HEADER = "x-api-key"
CAP_BYTES = 1_000_000

FIRST = "https://a.example/doc"
SECOND = "https://b.example/doc"
ANSWER = b"<html><body><main><p>the document</p></main></body></html>"


class _Origins:
    """Canned answers, and a record of what each was sent.

    Routing is keyed on whatever the case is about — the host, usually, but the
    path or the scheme where the case is a redirect that changes one of those
    and nothing else.
    """

    def __init__(
        self,
        routes: Mapping[str, httpx.Response],
        *,
        route_on: Callable[[httpx.Request], str] = lambda request: request.url.host,
    ) -> None:
        self._routes = dict(routes)
        self._route_on = route_on
        self.seen: list[tuple[str, dict[str, str]]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def headers_sent_to(self, key: str) -> list[dict[str, str]]:
        return [headers for seen, headers in self.seen if seen == key]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        key = self._route_on(request)
        self.seen.append((key, {name.lower(): value for name, value in request.headers.items()}))
        answer = self._routes.get(key)
        if answer is None:  # pragma: no cover - defensive
            raise AssertionError(f"the client contacted an unrouted endpoint: {key}")
        return httpx.Response(
            status_code=answer.status_code,
            headers=answer.headers,
            content=answer.content,
            request=request,
        )


def _redirect(location: str, *, status_code: int = 302) -> httpx.Response:
    return httpx.Response(status_code=status_code, headers={"location": location})


def _document() -> httpx.Response:
    return httpx.Response(status_code=200, headers={"content-type": "text/html"}, content=ANSWER)


def _client(origins: _Origins, recorder: EgressRecorder) -> RecordingClient:
    return RecordingClient(recorder, timeout_seconds=5.0, transport=origins.transport())


def test_a_credential_never_reaches_the_second_origin_of_a_redirect() -> None:
    """The disclosure itself, asserted where it would happen.

    An origin that answers 302 chooses the next host. Handing it the caller's
    credential means an origin the run never named decides who reads it, and
    recording that origin afterwards documents the disclosure rather than
    preventing it.
    """

    origins = _Origins({"a.example": _redirect(SECOND), "b.example": _document()})
    recorder = EgressRecorder()

    with _client(origins, recorder) as client, pytest.raises(RefusalError):
        client.get(FIRST, headers={CREDENTIAL_HEADER: SENTINEL}, cap_bytes=CAP_BYTES)

    assert origins.headers_sent_to("a.example")[0][CREDENTIAL_HEADER] == SENTINEL
    for headers in origins.headers_sent_to("b.example"):
        assert CREDENTIAL_HEADER not in headers
        assert SENTINEL not in repr(headers)


def test_the_cross_origin_credentialed_redirect_refuses_rather_than_downgrading() -> None:
    """Typed, and named: this run declined under a rule, and says which.

    Refusing rather than stripping is the plane's existing position — a run
    admitted as authenticated does not silently go out anonymous — and it leaves
    the caller something to act on rather than a document retrieved under terms
    the receipt no longer describes.
    """

    origins = _Origins({"a.example": _redirect(SECOND), "b.example": _document()})
    recorder = EgressRecorder()

    with _client(origins, recorder) as client, pytest.raises(RefusalError) as exc:
        client.get(FIRST, headers={CREDENTIAL_HEADER: SENTINEL}, cap_bytes=CAP_BYTES)

    assert exc.value.code is RefusalCode.CROSS_ORIGIN_CREDENTIALED_REDIRECT
    assert exc.value.refusal.detail["redirect_to"] == "https://b.example"
    assert SENTINEL not in repr(exc.value.refusal)
    # The origin that was contacted is recorded; the one that was not, is not.
    assert recorder.observed() == ["https://a.example"]


def test_a_same_origin_redirect_keeps_the_credential() -> None:
    """The rule is about disclosure, not about redirects.

    An origin moving a caller between its own paths has been told the credential
    already, and refusing there would delete the ordinary case — a canonical URL,
    a trailing slash — for no gain.
    """

    origins = _Origins(
        {"/doc": _redirect("https://a.example/doc/"), "/doc/": _document()},
        route_on=lambda request: request.url.path,
    )
    recorder = EgressRecorder()

    with _client(origins, recorder) as client:
        response = client.get(FIRST, headers={CREDENTIAL_HEADER: SENTINEL}, cap_bytes=CAP_BYTES)

    assert response.status_code == 200
    assert response.final_url == "https://a.example/doc/"
    assert origins.headers_sent_to("/doc/")[0][CREDENTIAL_HEADER] == SENTINEL


def test_an_unauthenticated_chain_is_followed_and_every_hop_is_recorded() -> None:
    """Following by hand must not cost the receipt anything.

    The recording is what governs a dynamic endpoint declaration, so a chain that
    crosses two hosts has to leave both of them in the recorder — which is what
    the client's event hook gave us for free while httpx was doing the following.
    """

    origins = _Origins(
        {
            "a.example": _redirect(SECOND),
            "b.example": _redirect("https://c.example/doc", status_code=301),
            "c.example": _document(),
        }
    )
    recorder = EgressRecorder()

    with _client(origins, recorder) as client:
        response = client.get(FIRST, cap_bytes=CAP_BYTES)

    assert response.status_code == 200
    assert response.body == ANSWER
    assert response.final_url == "https://c.example/doc"
    assert recorder.observed() == [
        "https://a.example",
        "https://b.example",
        "https://c.example",
    ]


def test_an_http_to_https_upgrade_is_not_treated_as_a_new_origin() -> None:
    """The credential has already crossed the wire in clear by then.

    Declining here would cost the run the single most common redirect on the web
    and protect nothing that was not already exposed.
    """

    origins = _Origins(
        {"http": _redirect("https://a.example/doc", status_code=301), "https": _document()},
        route_on=lambda request: request.url.scheme,
    )
    recorder = EgressRecorder()

    with _client(origins, recorder) as client:
        response = client.get(
            "http://a.example/doc", headers={CREDENTIAL_HEADER: SENTINEL}, cap_bytes=CAP_BYTES
        )

    assert response.status_code == 200
    assert origins.headers_sent_to("https")[0][CREDENTIAL_HEADER] == SENTINEL


def test_a_redirect_to_a_scheme_this_interface_does_not_retrieve_refuses() -> None:
    """``web.fetch`` needs an http or https url, and so does every hop of one."""

    origins = _Origins({"a.example": _redirect("ftp://a.example/doc")})
    recorder = EgressRecorder()

    with _client(origins, recorder) as client, pytest.raises(RefusalError) as exc:
        client.get(FIRST, cap_bytes=CAP_BYTES)

    assert exc.value.code is RefusalCode.UNSUPPORTED_REDIRECT_SCHEME


def test_a_chain_that_never_settles_refuses_instead_of_running_forever() -> None:
    """A loop and a very long chain are one condition with one bound."""

    origins = _Origins({"a.example": _redirect(FIRST)})
    recorder = EgressRecorder()

    with _client(origins, recorder) as client, pytest.raises(RefusalError) as exc:
        client.get(FIRST, cap_bytes=CAP_BYTES)

    assert exc.value.code is RefusalCode.REDIRECT_LIMIT
    assert len(origins.seen) == MAX_REDIRECTS + 1
