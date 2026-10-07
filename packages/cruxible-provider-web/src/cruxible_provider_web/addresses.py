"""The private-address guard ``web.fetch`` applies to every target it contacts.

``web.fetch`` retrieves a resource the run input names, which makes it a
server-side request forgery primitive unless something stands between the input
and the operator's own network. This module is that something. It refuses, with
``provider_declined``, any target whose addresses include:

* loopback (``127.0.0.0/8``, ``::1``),
* private networks (RFC 1918, IPv6 unique-local ``fc00::/7``, site-local),
* link-local (``169.254.0.0/16`` — where cloud metadata services live — and
  ``fe80::/10``),
* unspecified (``0.0.0.0/8``, ``::``),
* multicast,
* reserved and documentation ranges, and anything else that is not globally
  routable,
* carrier-grade NAT (``100.64.0.0/10``),
* any of the above embedded in an IPv6 address: IPv4-mapped (``::ffff:a.b.c.d``),
  IPv4-compatible (``::a.b.c.d``), NAT64 (``64:ff9b::/96``), 6to4 and Teredo.

A literal-IP host is judged on its own address, including the shorthand forms an
IPv4 parser accepts (``127.1``, ``2130706433``, ``0x7f.1``). A ``localhost``-style
name, or a name under a suffix reserved for local networks, is refused by name
before anything resolves it. Every other name is resolved, and **every** address
in the answer must pass: an answer mixing a public address with a private one is
refused, because the connection could land on either.

**Checking an address is only half of it.** A name that resolves to a public
address when it is checked and to a private one when the connection is opened —
DNS rebinding — defeats any guard that resolves twice. :meth:`AddressGuard.vet`
therefore *pins* what it checked, and the network backend in
:mod:`cruxible_provider_web.http` connects only to pinned addresses: the socket
opens to the address that was vetted, while the request's ``Host`` header, TLS
SNI and certificate verification keep using the name.

The guard applies to ``web.fetch`` only. ``search.web`` talks to a SearXNG
instance the operator declared, which may legitimately be local.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Sequence
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address
from typing import Final

import httpx
from cruxible_provider_runtime.egress import normalize_endpoint
from cruxible_provider_runtime.errors import RefusalCode, RefusalError, refuse

__all__ = [
    "LOCAL_NAMES",
    "LOCAL_SUFFIXES",
    "AddressGuard",
    "Resolver",
    "address_class",
    "literal_address",
    "system_resolver",
]

IPAddress = IPv4Address | IPv6Address

Resolver = Callable[[str, int], Sequence[str]]
"""``(host, port) -> addresses``. Injected so tests never touch real DNS."""

_DEFAULT_PORTS: Final = {"http": 80, "https": 443}

LOCAL_NAMES: Final = frozenset(
    {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
)
"""Names that mean "this machine" whatever a resolver says about them."""

LOCAL_SUFFIXES: Final = (".localhost", ".local", ".internal", ".home.arpa")
"""Suffixes reserved for local networks (RFC 6761, RFC 6762, RFC 8375, ICANN)."""

_IPV4_CLASSES: Final[tuple[tuple[str, tuple[IPv4Network, ...]], ...]] = (
    ("unspecified", (IPv4Network("0.0.0.0/8"),)),
    ("loopback", (IPv4Network("127.0.0.0/8"),)),
    ("link_local", (IPv4Network("169.254.0.0/16"),)),
    (
        "private",
        (IPv4Network("10.0.0.0/8"), IPv4Network("172.16.0.0/12"), IPv4Network("192.168.0.0/16")),
    ),
    ("carrier_grade_nat", (IPv4Network("100.64.0.0/10"),)),
    ("multicast", (IPv4Network("224.0.0.0/4"),)),
    (
        "reserved",
        (
            IPv4Network("192.0.0.0/24"),
            IPv4Network("192.0.2.0/24"),
            IPv4Network("192.88.99.0/24"),
            IPv4Network("198.18.0.0/15"),
            IPv4Network("198.51.100.0/24"),
            IPv4Network("203.0.113.0/24"),
            IPv4Network("240.0.0.0/4"),
        ),
    ),
)

_IPV6_CLASSES: Final[tuple[tuple[str, tuple[IPv6Network, ...]], ...]] = (
    ("unspecified", (IPv6Network("::/128"),)),
    ("loopback", (IPv6Network("::1/128"),)),
    ("link_local", (IPv6Network("fe80::/10"),)),
    (
        "private",
        (IPv6Network("fc00::/7"), IPv6Network("fec0::/10"), IPv6Network("64:ff9b:1::/48")),
    ),
    ("multicast", (IPv6Network("ff00::/8"),)),
    ("reserved", (IPv6Network("100::/64"), IPv6Network("2001:db8::/32"))),
)

_NAT64: Final = IPv6Network("64:ff9b::/96")


def system_resolver(host: str, port: int) -> list[str]:
    """Every address the system resolver gives for ``host``, in its order."""

    addresses: list[str] = []
    for *_, sockaddr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        # A scoped IPv6 answer carries its zone ("fe80::1%en0"); the address is
        # what gets judged, and a zone only ever accompanies link-local anyway.
        address = str(sockaddr[0]).split("%", 1)[0]
        if address not in addresses:
            addresses.append(address)
    return addresses


def _embedded_ipv4(address: IPv6Address) -> tuple[IPv4Address, bool] | None:
    """The IPv4 address an IPv6 form carries, and whether it is a pure translation.

    A translation (mapped, compatible, NAT64) *is* that IPv4 host, so the IPv4
    verdict is the whole verdict. A tunnel (6to4, Teredo) is judged on both.
    """

    if address.ipv4_mapped is not None:
        return address.ipv4_mapped, True
    if address.packed[:12] == bytes(12) and int(address) > 1:
        return IPv4Address(int(address) & 0xFFFFFFFF), True
    if address in _NAT64:
        return IPv4Address(int(address) & 0xFFFFFFFF), True
    if address.sixtofour is not None:
        return address.sixtofour, False
    if address.teredo is not None:
        return address.teredo[1], False
    return None


def address_class(address: IPAddress) -> str | None:
    """The blocked class ``address`` falls in, or ``None`` when it may be contacted."""

    if isinstance(address, IPv6Address):
        embedded = _embedded_ipv4(address)
        if embedded is not None:
            inner, translation = embedded
            verdict = address_class(inner)
            if verdict is not None:
                return f"ipv4_embedded_{verdict}"
            if translation:
                return None
        for name, networks6 in _IPV6_CLASSES:
            if any(address in network for network in networks6):
                return name
    else:
        for name, networks4 in _IPV4_CLASSES:
            if any(address in network for network in networks4):
                return name
    # The explicit tables above are the policy; this is the backstop for any
    # special-purpose range they do not name, so that a registry addition is
    # refused by default rather than admitted by omission.
    if not address.is_global or address.is_multicast:
        return "reserved"
    return None


def literal_address(host: str) -> IPAddress | None:
    """``host`` as an IP address, when it is one in any spelling a parser accepts."""

    candidate = host.split("%", 1)[0]
    try:
        return ip_address(candidate)
    except ValueError:
        pass
    # The shorthand IPv4 forms ("127.1", "2130706433", "0x7f.1", "0177.0.0.1"):
    # the platform resolver reads them as addresses, so this reads them the
    # same way rather than handing them to a resolver to be judged later.
    if candidate and all(char in "0123456789abcdefxABCDEFX." for char in candidate):
        try:
            return IPv4Address(socket.inet_aton(candidate))
        except OSError:
            return None
    return None


def _is_local_name(host: str) -> bool:
    name = host.rstrip(".")
    return name in LOCAL_NAMES or name.endswith(LOCAL_SUFFIXES)


class AddressGuard:
    """Vets targets before anything is sent, and remembers what it vetted.

    One guard serves one run. :meth:`vet` runs before every hop — the first
    request, each redirect, each request a rendered page makes — and replaces
    the pin for that host, so a later hop to the same name is judged on the
    answer that later hop will actually use.
    """

    def __init__(self, resolver: Resolver | None = None) -> None:
        self._resolver = resolver or system_resolver
        self._pins: dict[str, tuple[str, ...]] = {}

    def vet(self, url: str) -> tuple[str, ...]:
        """Refuse ``url`` if it reaches a blocked address; pin and return its addresses."""

        parsed = httpx.URL(url)
        host = parsed.raw_host.decode("ascii").lower()
        if not host:
            raise refuse(RefusalCode.PROVIDER_DECLINED, "web.fetch needs a url with a host")
        literal = literal_address(host)
        if literal is not None:
            self._judge(url, host, literal)
            addresses: tuple[str, ...] = (str(literal),)
        elif _is_local_name(host):
            raise self._refusal(url, host, address=None, verdict="local_name")
        else:
            port = parsed.port or _DEFAULT_PORTS.get(parsed.scheme, 443)
            try:
                answer = self._resolver(host, port)
            except OSError as exc:
                raise httpx.ConnectError(f"could not resolve {host!r}: {exc}") from exc
            if not answer:
                raise httpx.ConnectError(f"could not resolve {host!r}: empty answer")
            judged: list[str] = []
            for entry in answer:
                address = literal_address(entry)
                if address is None:
                    raise httpx.ConnectError(f"the resolver answered {entry!r} for {host!r}")
                self._judge(url, host, address)
                judged.append(str(address))
            addresses = tuple(judged)
        self._pins[host] = addresses
        return addresses

    def pinned(self, host: str) -> tuple[str, ...]:
        """The vetted addresses for ``host``; refuses a host :meth:`vet` never saw."""

        addresses = self._pins.get(host.lower())
        if addresses is None:
            raise refuse(
                RefusalCode.PROVIDER_DECLINED,
                "web.fetch refused a connection to a host it had not vetted",
                host=host,
            )
        return addresses

    def _judge(self, url: str, host: str, address: IPAddress) -> None:
        verdict = address_class(address)
        if verdict is not None:
            raise self._refusal(url, host, address=address, verdict=verdict)

    @staticmethod
    def _refusal(url: str, host: str, *, address: IPAddress | None, verdict: str) -> RefusalError:
        return refuse(
            RefusalCode.PROVIDER_DECLINED,
            "web.fetch does not retrieve from loopback, private-network, link-local or "
            "otherwise non-public addresses",
            url=normalize_endpoint(url),
            host=host,
            address=None if address is None else str(address),
            address_class=verdict,
        )
