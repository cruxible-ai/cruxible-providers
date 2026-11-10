"""The private-address guard on ``web.fetch`` and ``web.fetch.pages``.

Three properties, each asserted where it would fail:

* **classification** — every blocked address class refuses, in IPv4 and IPv6,
  spelled literally in the URL and returned by a resolver; public addresses pass;
* **every hop** — a redirect onto a private address is refused before anything
  is sent to it, for one fetch and for each page of a paged one;
* **pinning** — the socket opens to the address that was checked, whatever the
  resolver says afterwards, while ``Host`` and SNI keep naming the host.

``web.render.dom``'s use of the same guard is held in ``test_web_render_dom.py``,
beside the page double it needs.

No test here touches real DNS or opens a socket: resolvers are stubs, and the
pinning tests observe the connection through an httpcore network backend double.
"""

from __future__ import annotations

import functools
from collections.abc import Iterable, Mapping, Sequence
from ipaddress import ip_address
from typing import Any

import httpcore
import httpx
import pytest
from cruxible_provider_runtime.egress import EgressRecorder
from cruxible_provider_runtime.errors import Declined
from cruxible_provider_web.addresses import (
    ADDRESS_CLASSES,
    PRIVATE_ADDRESS_MESSAGE,
    AddressGuard,
    Resolver,
    address_class,
)
from cruxible_provider_web.fetch import WebFetch
from cruxible_provider_web.http import (
    PinnedTransport,
    RecordingClient,
    default_client_factory,
    guarded_client_factory,
)
from cruxible_provider_web.operations import PRIVATE_OR_INTERNAL_ADDRESS
from cruxible_provider_web.pages import WebFetchPages
from cruxible_provider_web.search import SearxngSearch

from .test_web_secrets import _context

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:4700:4700::1111"
CAP_BYTES = 1_000_000
DOCUMENT = b"<html><body><main><p>the public document</p></main></body></html>"

BLOCKED = [
    # (address, class)
    ("127.0.0.1", "loopback"),
    ("127.255.0.9", "loopback"),
    ("10.0.0.7", "private"),
    ("172.16.4.2", "private"),
    ("192.168.1.1", "private"),
    ("169.254.169.254", "link_local"),
    ("0.0.0.0", "unspecified"),
    ("0.1.2.3", "unspecified"),
    ("100.64.0.1", "carrier_grade_nat"),
    ("224.0.0.251", "multicast"),
    ("240.0.0.1", "reserved"),
    ("255.255.255.255", "reserved"),
    ("192.0.2.10", "reserved"),
    ("198.18.0.1", "reserved"),
    ("::1", "loopback"),
    ("::", "unspecified"),
    ("fc00::1", "private"),
    ("fd12:3456:789a::1", "private"),
    ("fec0::1", "private"),
    ("fe80::1", "link_local"),
    ("febf::1", "link_local"),
    ("ff02::1", "multicast"),
    ("2001:db8::1", "reserved"),
    ("100::1", "reserved"),
    # IPv4 forms carried inside IPv6.
    ("::ffff:127.0.0.1", "ipv4_embedded_loopback"),
    ("::ffff:169.254.169.254", "ipv4_embedded_link_local"),
    ("::ffff:10.0.0.1", "ipv4_embedded_private"),
    ("::ffff:100.64.0.1", "ipv4_embedded_carrier_grade_nat"),
    ("::127.0.0.1", "ipv4_embedded_loopback"),
    ("::10.0.0.1", "ipv4_embedded_private"),
    ("64:ff9b::7f00:1", "ipv4_embedded_loopback"),
    ("64:ff9b::a9fe:a9fe", "ipv4_embedded_link_local"),
    ("2002:c0a8:101::1", "ipv4_embedded_private"),
    ("2001:0:4136:e378:8000:63bf:f5ff:fffe", "ipv4_embedded_private"),
]

PUBLIC = [PUBLIC_V4, "1.1.1.1", "8.8.8.8", PUBLIC_V6, "::ffff:1.1.1.1", "64:ff9b::101:101"]


def _resolver(answers: Mapping[str, Sequence[str]] | None = None) -> Resolver:
    table = dict(answers or {})

    def resolve(host: str, port: int) -> Sequence[str]:
        del port
        return table.get(host, (PUBLIC_V4,))

    return resolve


def _url_host(address: str) -> str:
    return f"[{address}]" if ":" in address else address


def _refused(exc: pytest.ExceptionInfo[Declined]) -> dict[str, Any]:
    assert exc.value.code == PRIVATE_OR_INTERNAL_ADDRESS
    return dict(exc.value.refusal.detail)


# -- classification ----------------------------------------------------------


@pytest.mark.parametrize(("address", "expected"), BLOCKED)
def test_every_blocked_class_is_classified(address: str, expected: str) -> None:
    assert address_class(ip_address(address)) == expected
    assert expected in ADDRESS_CLASSES


#: IETF-reserved IPv6 space outside every explicit table. Python reports each of
#: these as ``is_global`` *and* ``is_reserved`` (3.11 to 3.13), so a backstop on
#: ``is_global`` alone would admit them.
RESERVED_OUTSIDE_THE_TABLES = [
    "200::1",
    "400::1",
    "800::1",
    "1000::1",
    "4000::1",
    "6000::1",
    "8000::1",
    "a000::1",
    "c000::1",
    "e000::1",
    "f000::1",
    "f800::1",
    "fe00::1",
]


@pytest.mark.parametrize("address", RESERVED_OUTSIDE_THE_TABLES)
def test_reserved_ipv6_outside_the_tables_is_refused(address: str) -> None:
    parsed = ip_address(address)
    assert parsed.is_reserved
    assert address_class(parsed) == "reserved"

    def no_dns(host: str, port: int) -> Sequence[str]:
        raise AssertionError("a literal address was handed to the resolver")

    with pytest.raises(Declined) as exc:
        AddressGuard(no_dns).vet(f"https://[{address}]/")
    assert _refused(exc)["address_class"] == "reserved"
    with pytest.raises(Declined):
        AddressGuard(_resolver({"reserved.example": (address,)})).vet("https://reserved.example/")


def test_a_public_ipv4_host_in_a_translated_form_still_passes() -> None:
    """The backstop does not catch translations: NAT64 (``64:ff9b::/96``) sits in
    reserved IPv6 space, and the IPv4 host it carries is the whole verdict."""

    nat64 = ip_address("64:ff9b::101:101")
    assert nat64.is_reserved
    assert address_class(nat64) is None
    assert address_class(ip_address("::ffff:1.1.1.1")) is None


@pytest.mark.parametrize("address", PUBLIC)
def test_public_addresses_pass(address: str) -> None:
    assert address_class(ip_address(address)) is None


@pytest.mark.parametrize(("address", "expected"), BLOCKED)
def test_a_literal_blocked_address_refuses_without_resolving(address: str, expected: str) -> None:
    def no_dns(host: str, port: int) -> Sequence[str]:
        raise AssertionError(f"a literal address was handed to the resolver: {host}")

    with pytest.raises(Declined) as exc:
        AddressGuard(no_dns).vet(f"http://{_url_host(address)}:8080/admin")

    detail = _refused(exc)
    assert detail["address_class"] == expected
    assert ip_address(detail["address"]) == ip_address(address)


@pytest.mark.parametrize(("address", "expected"), BLOCKED)
def test_a_name_resolving_to_a_blocked_address_refuses(address: str, expected: str) -> None:
    guard = AddressGuard(_resolver({"innocent.example": (address,)}))

    with pytest.raises(Declined) as exc:
        guard.vet("https://innocent.example/report")

    detail = _refused(exc)
    assert detail["address_class"] == expected
    assert detail["host"] == "innocent.example"


@pytest.mark.parametrize(
    "host",
    ["127.1", "2130706433", "0x7f.1", "0x7f000001", "017700000001"],
)
def test_shorthand_ipv4_spellings_are_read_as_the_address_they_are(host: str) -> None:
    with pytest.raises(Declined) as exc:
        AddressGuard(_resolver()).vet(f"http://{host}/")
    assert _refused(exc)["address_class"] == "loopback"


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST",
        "localhost.",
        "api.localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "printer.local",
        "metadata.google.internal",
        "router.home.arpa",
    ],
)
def test_local_names_refuse_whatever_a_resolver_would_say(host: str) -> None:
    guard = AddressGuard(_resolver({host.lower(): (PUBLIC_V4,)}))

    with pytest.raises(Declined) as exc:
        guard.vet(f"http://{host}/")

    detail = _refused(exc)
    assert detail["address_class"] == "local_name"
    # Refused by name: there is no address, and an optional detail is absent.
    assert "address" not in detail


def test_a_scoped_link_local_literal_refuses() -> None:
    with pytest.raises(Declined) as exc:
        AddressGuard(_resolver()).vet("http://[fe80::1%25en0]/")
    assert _refused(exc)["address_class"] == "link_local"


def test_an_answer_mixing_public_and_private_addresses_refuses() -> None:
    """The connection could land on either, so the answer is judged as a whole."""

    guard = AddressGuard(_resolver({"split.example": (PUBLIC_V4, PUBLIC_V6, "10.0.0.1")}))

    with pytest.raises(Declined) as exc:
        guard.vet("https://split.example/")

    assert _refused(exc)["address"] == "10.0.0.1"


def test_a_public_answer_is_pinned_in_full() -> None:
    guard = AddressGuard(_resolver({"dual.example": (PUBLIC_V4, PUBLIC_V6)}))

    assert guard.vet("https://dual.example/page") == (PUBLIC_V4, PUBLIC_V6)
    assert guard.pinned("dual.example") == (PUBLIC_V4, PUBLIC_V6)


def test_an_unresolvable_name_is_a_connection_failure_not_a_refusal() -> None:
    def nxdomain(host: str, port: int) -> Sequence[str]:
        raise OSError("nodename nor servname provided")

    with pytest.raises(httpx.ConnectError):
        AddressGuard(nxdomain).vet("https://nowhere.example/")


def test_a_refusal_says_why_and_names_the_path_for_internal_sources() -> None:
    """A declined run should tell its author what to do instead."""

    with pytest.raises(Declined) as exc:
        AddressGuard(_resolver({"wiki.corp.example": ("10.0.0.9",)})).vet(
            "https://wiki.corp.example/page"
        )

    refusal = exc.value.refusal
    assert refusal.code == PRIVATE_OR_INTERNAL_ADDRESS
    assert refusal.message == PRIVATE_ADDRESS_MESSAGE
    assert "declares that endpoint" in refusal.message
    assert refusal.detail == {
        "url": "https://wiki.corp.example",
        "host": "wiki.corp.example",
        "address": "10.0.0.9",
        "address_class": "private",
    }


def test_a_refusal_names_the_origin_and_never_the_path() -> None:
    with pytest.raises(Declined) as exc:
        AddressGuard(_resolver()).vet("http://127.0.0.1:8080/admin?token=abc")
    assert _refused(exc)["url"] == "http://127.0.0.1:8080"
    assert "token" not in repr(exc.value.refusal)


# -- every hop ---------------------------------------------------------------


class _Origins:
    """Canned answers keyed on host, and a record of who was asked."""

    def __init__(self, routes: Mapping[str, httpx.Response]) -> None:
        self._routes = dict(routes)
        self.hosts: list[str] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.hosts.append(request.url.host)
        answer = self._routes.get(request.url.host)
        if answer is None:  # pragma: no cover - defensive
            raise AssertionError(f"the client contacted an unrouted host: {request.url.host}")
        return httpx.Response(
            status_code=answer.status_code,
            headers=answer.headers,
            content=answer.content,
            request=request,
        )


def _guarded(origins: _Origins, resolver: Resolver, recorder: EgressRecorder) -> RecordingClient:
    return RecordingClient(
        recorder,
        timeout_seconds=5.0,
        transport=origins.transport(),
        guard=AddressGuard(resolver),
    )


REDIRECTS_ONTO_PRIVATE = [
    ("http://169.254.169.254/latest/meta-data/", {}),
    ("http://[::ffff:127.0.0.1]:6379/", {}),
    ("http://[::1]:8080/", {}),
    ("http://[fd00:ec2::254]/latest/meta-data/", {}),
    ("http://[fe80::a9fe:a9fe]/", {}),
    ("http://localhost:8080/", {}),
    ("https://internal.example/admin", {"internal.example": ("172.20.0.5",)}),
    ("https://internal6.example/admin", {"internal6.example": ("fc00::5",)}),
]
REDIRECT_IDS = [
    "literal-metadata",
    "mapped-loopback",
    "ipv6-loopback",
    "ipv6-unique-local-metadata",
    "ipv6-link-local",
    "localhost",
    "resolved-private",
    "resolved-unique-local",
]


@pytest.mark.parametrize(("location", "answers"), REDIRECTS_ONTO_PRIVATE, ids=REDIRECT_IDS)
def test_a_redirect_onto_a_private_address_is_refused_before_it_is_sent(
    location: str, answers: dict[str, tuple[str, ...]]
) -> None:
    origins = _Origins({"public.example": httpx.Response(302, headers={"location": location})})
    recorder = EgressRecorder()

    with (
        _guarded(origins, _resolver(answers), recorder) as client,
        pytest.raises(Declined) as exc,
    ):
        client.get("https://public.example/start", cap_bytes=CAP_BYTES)

    assert exc.value.code == PRIVATE_OR_INTERNAL_ADDRESS
    assert origins.hosts == ["public.example"]
    assert recorder.observed() == ["https://public.example"]


def test_a_private_first_hop_contacts_nobody() -> None:
    origins = _Origins({})
    recorder = EgressRecorder()

    resolver = _resolver({"lan.example": ("192.168.0.10",)})

    with _guarded(origins, resolver, recorder) as client, pytest.raises(Declined):
        client.get("https://lan.example/", cap_bytes=CAP_BYTES)

    assert origins.hosts == []
    assert recorder.observed() == []


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/iam/",
        "http://[fe80::1]/",
        "http://[fc00::1]/",
        "http://localhost/",
        "http://2130706433/",
        "http://[::ffff:10.0.0.1]/",
    ],
)
def test_web_fetch_refuses_private_targets_with_its_production_defaults(url: str) -> None:
    """No seam is replaced: this is the client a deployed ``web.fetch`` builds."""

    context = _context("web.fetch", input={"url": url})
    result = WebFetch()(context)

    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code == PRIVATE_OR_INTERNAL_ADDRESS
    assert result.refusal.detail["address_class"]
    assert context.egress.observed() == []


def test_web_fetch_refuses_a_name_resolving_privately() -> None:
    factory = functools.partial(
        guarded_client_factory, resolver=_resolver({"wiki.corp.example": ("10.20.30.40",)})
    )

    result = WebFetch(client_factory=factory)(
        _context("web.fetch", input={"url": "https://wiki.corp.example/page"})
    )

    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code == PRIVATE_OR_INTERNAL_ADDRESS
    assert result.refusal.detail["address_class"] == "private"


def test_search_web_is_not_guarded() -> None:
    """SearXNG is an endpoint the operator configured, and it may well be local."""

    unguarded = default_client_factory(
        EgressRecorder(), url="http://127.0.0.1:8888/search", timeout_seconds=1.0
    )
    guarded = guarded_client_factory(
        EgressRecorder(), url="http://127.0.0.1:8888/search", timeout_seconds=1.0
    )
    try:
        unguarded.vet("http://127.0.0.1:8888/search")
        with pytest.raises(Declined):
            guarded.vet("http://127.0.0.1:8888/search")
    finally:
        unguarded.close()
        guarded.close()
    assert SearxngSearch()._client_factory is default_client_factory
    assert WebFetch()._client_factory is guarded_client_factory
    assert WebFetchPages()._client_factory is guarded_client_factory


def test_a_packaged_recording_needs_no_guard_and_opens_no_socket() -> None:
    """``fixture.invalid`` is served from the distribution, never from a socket."""

    client = guarded_client_factory(
        EgressRecorder(),
        url="https://fixture.invalid/articles/tide-gauge-recalibration",
        timeout_seconds=1.0,
    )
    try:
        assert client._guard is None
    finally:
        client.close()


# -- pinning -----------------------------------------------------------------


class _Wire(httpcore.MockStream):
    """A connection double: replays an answer, and remembers what crossed it."""

    def __init__(self, buffer: list[bytes], backend: _Backend) -> None:
        super().__init__(buffer)
        self._backend = backend

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._backend.written.append(buffer)

    def start_tls(
        self, ssl_context: Any, server_hostname: str | None = None, timeout: float | None = None
    ) -> httpcore.NetworkStream:
        self._backend.server_hostnames.append(server_hostname)
        return self

    def close(self) -> None:
        self._backend.closed += 1
        super().close()


class _Backend(httpcore.NetworkBackend):
    """Stands in for the socket layer, recording where each connection went."""

    def __init__(self, answers: Iterable[bytes]) -> None:
        self._answers = list(answers)
        self.connected: list[tuple[str, int]] = []
        self.server_hostnames: list[str | None] = []
        self.written: list[bytes] = []
        self.closed = 0

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        self.connected.append((host, port))
        return _Wire([self._answers.pop(0)], self)

    def sleep(self, seconds: float) -> None:  # pragma: no cover - never retried
        del seconds


def _answer(status: str = "200 OK", body: bytes = DOCUMENT, **headers: str) -> bytes:
    lines = [f"HTTP/1.1 {status}", f"Content-Length: {len(body)}"]
    lines += [f"{name.replace('_', '-')}: {value}" for name, value in headers.items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


class _Rebinding:
    """Answers public the first time it is asked, and private every time after."""

    def __init__(self, private: str = "127.0.0.1") -> None:
        self.calls = 0
        self._private = private

    def __call__(self, host: str, port: int) -> Sequence[str]:
        self.calls += 1
        return (PUBLIC_V4,) if self.calls == 1 else (self._private,)


def _pinned(resolver: Resolver, backend: _Backend, recorder: EgressRecorder) -> RecordingClient:
    guard = AddressGuard(resolver)
    return RecordingClient(
        recorder,
        timeout_seconds=5.0,
        transport=PinnedTransport(guard, network_backend=backend),
        guard=guard,
    )


def test_the_connection_goes_to_the_address_that_was_checked() -> None:
    """DNS rebinding: public when checked, private when a second lookup would run.

    The resolver is asked once. The socket opens to the public address it gave,
    and the name still travels as ``Host`` and as SNI — so a rebinding answer
    arriving later is never consulted at all.
    """

    resolver = _Rebinding()
    backend = _Backend([_answer(Content_Type="text/html")])

    with _pinned(resolver, backend, EgressRecorder()) as client:
        response = client.get("https://rebind.example/doc", cap_bytes=CAP_BYTES)

    assert response.status_code == 200
    assert response.body == DOCUMENT
    assert resolver.calls == 1
    assert backend.connected == [(PUBLIC_V4, 443)]
    assert backend.server_hostnames == ["rebind.example"]
    assert b"Host: rebind.example" in b"".join(backend.written)


def test_a_rebinding_answer_on_a_later_hop_is_refused() -> None:
    """Each hop is vetted afresh, so a name re-pointed mid-chain is caught."""

    resolver = _Rebinding(private="169.254.169.254")
    backend = _Backend([_answer("302 Found", b"", Location="https://rebind.example/next")])

    with _pinned(resolver, backend, EgressRecorder()) as client, pytest.raises(Declined) as exc:
        client.get("https://rebind.example/doc", cap_bytes=CAP_BYTES)

    assert _refused(exc)["address_class"] == "link_local"
    assert backend.connected == [(PUBLIC_V4, 443)]


def test_the_transport_connects_nowhere_for_a_host_nobody_vetted() -> None:
    """Pinning is enforced in the transport, not only in the caller's habits."""

    backend = _Backend([_answer()])
    transport = PinnedTransport(AddressGuard(_resolver()), network_backend=backend)

    with httpx.Client(transport=transport) as client, pytest.raises(httpx.ConnectError):
        client.get("https://unvetted.example/")

    assert backend.connected == []


def test_a_public_fetch_still_works_end_to_end() -> None:
    """The ordinary case, through ``WebFetch`` and the pinned transport."""

    backend = _Backend([_answer(Content_Type="text/html; charset=utf-8")])

    def factory(recorder: EgressRecorder, *, url: str, timeout_seconds: float) -> RecordingClient:
        del url
        guard = AddressGuard(_resolver())
        return RecordingClient(
            recorder,
            timeout_seconds=timeout_seconds,
            transport=PinnedTransport(guard, network_backend=backend),
            guard=guard,
        )

    context = _context("web.fetch", input={"url": "https://news.example/article"})
    result = WebFetch(client_factory=factory)(context)

    assert result.status == "ok", result
    assert backend.connected == [(PUBLIC_V4, 443)]
    assert context.egress.observed() == ["https://news.example"]


# -- web.fetch.pages, page by page ---------------------------------------------


def _pinned_factory(backend: _Backend, resolver: Resolver) -> Any:
    """One guarded, pinned client per page, every connection through ``backend``."""

    def factory(recorder: EgressRecorder, *, url: str, timeout_seconds: float) -> RecordingClient:
        del url
        guard = AddressGuard(resolver)
        return RecordingClient(
            recorder,
            timeout_seconds=timeout_seconds,
            transport=PinnedTransport(guard, network_backend=backend),
            guard=guard,
        )

    return factory


PAGED = {
    "url": "https://api.example/epss",
    "query": [{"name": "cve", "values": [f"CVE-2024-{index:05d}" for index in range(3)]}],
    "page_param": "cve",
    "max_url_chars": len("https://api.example/epss?cve=CVE-2024-00000"),
}


def test_a_page_redirected_onto_a_private_address_is_that_page_s_refusal() -> None:
    """The middle page bounces to the metadata service; the other two are captured.

    The refused hop is never connected to, and every connection the call did
    open is closed by the time it returns.
    """

    json_ok = _answer(Content_Type="application/json", body=b'{"data": []}')
    backend = _Backend(
        [
            json_ok,
            _answer("302 Found", b"", Location="http://169.254.169.254/latest/meta-data/"),
            json_ok,
        ]
    )
    context = _context("web.fetch.pages", input=PAGED)
    result = WebFetchPages(client_factory=_pinned_factory(backend, _resolver()))(context)

    assert result.status == "ok", result
    assert len(result.output["captures"]) == 2
    (failed,) = result.output["failed_pages"]
    assert failed["page_index"] == 1
    assert failed["outcome"] == "refused"
    assert failed["code"] == PRIVATE_OR_INTERNAL_ADDRESS
    assert result.incomplete_captures == ["captures"]
    assert backend.connected == [(PUBLIC_V4, 443)] * 3
    assert backend.closed == len(backend.connected)
    assert context.egress.observed() == ["https://api.example"]


def test_a_page_whose_host_is_re_pointed_privately_is_refused_alone() -> None:
    """Each page's client resolves afresh; a name that turns private mid-call is caught."""

    calls = {"count": 0}

    def flips(host: str, port: int) -> Sequence[str]:
        del host, port
        calls["count"] += 1
        return ("10.9.8.7",) if calls["count"] == 3 else (PUBLIC_V4,)

    json_ok = _answer(Content_Type="application/json", body=b'{"data": []}')
    backend = _Backend([json_ok, json_ok])
    result = WebFetchPages(client_factory=_pinned_factory(backend, flips))(
        _context("web.fetch.pages", input=PAGED)
    )

    assert result.status == "ok", result
    (failed,) = result.output["failed_pages"]
    assert failed["page_index"] == 2
    assert failed["code"] == PRIVATE_OR_INTERNAL_ADDRESS
    assert backend.connected == [(PUBLIC_V4, 443)] * 2


def test_every_page_of_a_private_target_is_refused_and_nothing_is_connected() -> None:
    backend = _Backend([])
    resolver = _resolver({"api.example": ("10.0.0.1",)})
    result = WebFetchPages(client_factory=_pinned_factory(backend, resolver))(
        _context("web.fetch.pages", input=PAGED)
    )

    assert result.status == "error" and result.error is not None
    assert result.error.kind == "AllPagesFailed"
    pages = result.error.detail["failed_pages"]
    assert [page["code"] for page in pages] == [PRIVATE_OR_INTERNAL_ADDRESS] * 3
    assert backend.connected == []


def test_a_one_page_private_target_is_the_call_s_refusal_with_production_defaults() -> None:
    context = _context(
        "web.fetch.pages",
        input={**PAGED, "url": "http://[fd00::1]/epss", "max_url_chars": 8192},
    )
    result = WebFetchPages()(context)

    assert result.status == "refused" and result.refusal is not None
    assert result.refusal.code == PRIVATE_OR_INTERNAL_ADDRESS
    assert result.refusal.detail["address_class"] == "private"
    assert context.egress.observed() == []
