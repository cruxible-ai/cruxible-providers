"""The private-address guard on ``web.fetch``'s plain path.

Three properties, each asserted where it would fail:

* **classification** — every blocked address class refuses, in IPv4 and IPv6,
  spelled literally in the URL and returned by a resolver; public addresses pass;
* **every hop** — a redirect onto a private address is refused before anything
  is sent to it;
* **pinning** — the socket opens to the address that was checked, whatever the
  resolver says afterwards, while ``Host`` and SNI keep naming the host.

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
from cruxible_provider_runtime.errors import RefusalCode, RefusalError
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.provider_api import ProviderRunContext
from cruxible_provider_web.addresses import AddressGuard, Resolver, address_class
from cruxible_provider_web.fetch import WebFetch
from cruxible_provider_web.http import (
    PinnedTransport,
    RecordingClient,
    default_client_factory,
    guarded_client_factory,
)
from cruxible_provider_web.search import SearxngSearch

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


# -- classification ----------------------------------------------------------


@pytest.mark.parametrize(("address", "expected"), BLOCKED)
def test_every_blocked_class_is_classified(address: str, expected: str) -> None:
    assert address_class(ip_address(address)) == expected


@pytest.mark.parametrize("address", PUBLIC)
def test_public_addresses_pass(address: str) -> None:
    assert address_class(ip_address(address)) is None


@pytest.mark.parametrize(("address", "expected"), BLOCKED)
def test_a_literal_blocked_address_refuses_without_resolving(address: str, expected: str) -> None:
    def no_dns(host: str, port: int) -> Sequence[str]:
        raise AssertionError(f"a literal address was handed to the resolver: {host}")

    with pytest.raises(RefusalError) as exc:
        AddressGuard(no_dns).vet(f"http://{_url_host(address)}:8080/admin")

    assert exc.value.code is RefusalCode.PROVIDER_DECLINED
    assert exc.value.refusal.detail["address_class"] == expected
    assert ip_address(exc.value.refusal.detail["address"]) == ip_address(address)


@pytest.mark.parametrize(("address", "expected"), BLOCKED)
def test_a_name_resolving_to_a_blocked_address_refuses(address: str, expected: str) -> None:
    guard = AddressGuard(_resolver({"innocent.example": (address,)}))

    with pytest.raises(RefusalError) as exc:
        guard.vet("https://innocent.example/report")

    assert exc.value.code is RefusalCode.PROVIDER_DECLINED
    assert exc.value.refusal.detail["address_class"] == expected
    assert exc.value.refusal.detail["host"] == "innocent.example"


@pytest.mark.parametrize(
    "host",
    ["127.1", "2130706433", "0x7f.1", "0x7f000001", "017700000001"],
)
def test_shorthand_ipv4_spellings_are_read_as_the_address_they_are(host: str) -> None:
    with pytest.raises(RefusalError) as exc:
        AddressGuard(_resolver()).vet(f"http://{host}/")
    assert exc.value.refusal.detail["address_class"] == "loopback"


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

    with pytest.raises(RefusalError) as exc:
        guard.vet(f"http://{host}/")

    assert exc.value.refusal.detail["address_class"] == "local_name"


def test_a_scoped_link_local_literal_refuses() -> None:
    with pytest.raises(RefusalError) as exc:
        AddressGuard(_resolver()).vet("http://[fe80::1%25en0]/")
    assert exc.value.refusal.detail["address_class"] == "link_local"


def test_an_answer_mixing_public_and_private_addresses_refuses() -> None:
    """The connection could land on either, so the answer is judged as a whole."""

    guard = AddressGuard(_resolver({"split.example": (PUBLIC_V4, PUBLIC_V6, "10.0.0.1")}))

    with pytest.raises(RefusalError) as exc:
        guard.vet("https://split.example/")

    assert exc.value.refusal.detail["address"] == "10.0.0.1"


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

    with pytest.raises(RefusalError) as exc:
        AddressGuard(_resolver({"wiki.corp.example": ("10.0.0.9",)})).vet(
            "https://wiki.corp.example/page"
        )

    refusal = exc.value.refusal
    assert refusal.code is RefusalCode.PROVIDER_DECLINED
    assert refusal.message == (
        "web.fetch does not retrieve private or internal addresses; internal sources need "
        "a provider with a declared endpoint"
    )
    assert refusal.detail["reason"] == "private_or_internal_address"
    assert "declares that endpoint" in refusal.detail["remedy"]


def test_a_local_name_refusal_carries_the_same_explanation() -> None:
    with pytest.raises(RefusalError) as exc:
        AddressGuard(_resolver()).vet("http://localhost:8080/")
    assert exc.value.refusal.message.startswith("web.fetch does not retrieve private")
    assert exc.value.refusal.detail["reason"] == "private_or_internal_address"


def test_a_refusal_names_the_origin_and_never_the_path() -> None:
    with pytest.raises(RefusalError) as exc:
        AddressGuard(_resolver()).vet("http://127.0.0.1:8080/admin?token=abc")
    assert exc.value.refusal.detail["url"] == "http://127.0.0.1:8080"
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


@pytest.mark.parametrize(
    ("location", "answers"),
    [
        ("http://169.254.169.254/latest/meta-data/", {}),
        ("http://[::ffff:127.0.0.1]:6379/", {}),
        ("http://localhost:8080/", {}),
        ("https://internal.example/admin", {"internal.example": ("172.20.0.5",)}),
    ],
    ids=["literal-metadata", "mapped-loopback", "localhost", "resolved-private"],
)
def test_a_redirect_onto_a_private_address_is_refused_before_it_is_sent(
    location: str, answers: dict[str, tuple[str, ...]]
) -> None:
    origins = _Origins({"public.example": httpx.Response(302, headers={"location": location})})
    recorder = EgressRecorder()

    with (
        _guarded(origins, _resolver(answers), recorder) as client,
        pytest.raises(RefusalError) as exc,
    ):
        client.get("https://public.example/start", cap_bytes=CAP_BYTES)

    assert exc.value.code is RefusalCode.PROVIDER_DECLINED
    assert origins.hosts == ["public.example"]
    assert recorder.observed() == ["https://public.example"]


def test_a_private_first_hop_contacts_nobody() -> None:
    origins = _Origins({})
    recorder = EgressRecorder()

    resolver = _resolver({"lan.example": ("192.168.0.10",)})

    with _guarded(origins, resolver, recorder) as client, pytest.raises(RefusalError):
        client.get("https://lan.example/", cap_bytes=CAP_BYTES)

    assert origins.hosts == []
    assert recorder.observed() == []


def _context(url: str, **input_fields: Any) -> ProviderRunContext:
    return ProviderRunContext(
        run_id="run-private",
        interface_id="web.fetch",
        interface_digest="sha256:" + "aa" * 32,
        implementation_digest="sha256:" + "bb" * 32,
        input_bucket="source_kind=static_html;access=public;page_weight=light",
        input={"url": url, **input_fields},
        coordinates={},
        budgets=Budgets(wall_clock_seconds=30.0, output_bytes=1_000_000),
        declared_endpoints=("dynamic:target-from-run-input",),
        capture_contract=None,
        secrets={},
        egress=EgressRecorder(),
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/iam/",
        "http://localhost/",
        "http://2130706433/",
        "http://[::ffff:10.0.0.1]/",
    ],
)
def test_web_fetch_refuses_private_targets_with_its_production_defaults(url: str) -> None:
    """No seam is replaced: this is the client a deployed ``web.fetch`` builds."""

    result = WebFetch()(_context(url))

    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code is RefusalCode.PROVIDER_DECLINED
    assert result.refusal.detail["address_class"]


def test_a_direct_rendered_invocation_is_declined_without_contacting_anything() -> None:
    """Admission refuses ``render: true`` first; the adapter refuses it as well."""

    context = _context("https://news.example/app", render=True)
    result = WebFetch()(context)

    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code is RefusalCode.PROVIDER_DECLINED
    assert context.egress.observed() == []


def test_web_fetch_refuses_a_name_resolving_privately() -> None:
    factory = functools.partial(
        guarded_client_factory, resolver=_resolver({"wiki.corp.example": ("10.20.30.40",)})
    )

    result = WebFetch(client_factory=factory)(_context("https://wiki.corp.example/page"))

    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.detail["address_class"] == "private"


def test_search_web_is_not_guarded() -> None:
    """SearXNG is an endpoint the operator declared, and it may well be local."""

    unguarded = default_client_factory(
        EgressRecorder(), url="http://127.0.0.1:8888/search", timeout_seconds=1.0
    )
    guarded = guarded_client_factory(
        EgressRecorder(), url="http://127.0.0.1:8888/search", timeout_seconds=1.0
    )
    try:
        unguarded.vet("http://127.0.0.1:8888/search")
        with pytest.raises(RefusalError):
            guarded.vet("http://127.0.0.1:8888/search")
    finally:
        unguarded.close()
        guarded.close()
    assert SearxngSearch()._client_factory is default_client_factory
    assert WebFetch()._client_factory is guarded_client_factory


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


class _Backend(httpcore.NetworkBackend):
    """Stands in for the socket layer, recording where each connection went."""

    def __init__(self, answers: Iterable[bytes]) -> None:
        self._answers = list(answers)
        self.connected: list[tuple[str, int]] = []
        self.server_hostnames: list[str | None] = []
        self.written: list[bytes] = []

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

    with _pinned(resolver, backend, EgressRecorder()) as client, pytest.raises(RefusalError) as exc:
        client.get("https://rebind.example/doc", cap_bytes=CAP_BYTES)

    assert exc.value.refusal.detail["address_class"] == "link_local"
    assert backend.connected == [(PUBLIC_V4, 443)]


def test_the_transport_refuses_a_host_nobody_vetted() -> None:
    """Pinning is enforced in the transport, not only in the caller's habits."""

    backend = _Backend([_answer()])
    transport = PinnedTransport(AddressGuard(_resolver()), network_backend=backend)

    with httpx.Client(transport=transport) as client, pytest.raises(RefusalError):
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

    context = _context("https://news.example/article")
    result = WebFetch(client_factory=factory)(context)

    assert result.status == "ok", result
    assert backend.connected == [(PUBLIC_V4, 443)]
    assert context.egress.observed() == ["https://news.example"]
