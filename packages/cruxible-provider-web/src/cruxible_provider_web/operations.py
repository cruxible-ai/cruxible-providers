"""The web plane's operations and the refusal codes they declare.

``registration.json`` is the declaration; these constants are how the
implementations spell the same names, and the registration-stability test holds
the two together. Each code is the operation's own vocabulary, not the
runtime's: runtime codes are produced by the runtime's machinery only, so the
conditions only this adapter can detect -- a missing browser, a credential the
run names but did not deliver, an instance outside the declaration -- are
declared here as the operations' own.
"""

from __future__ import annotations

__all__ = [
    "CREDENTIAL_UNAVAILABLE",
    "CROSS_ORIGIN_CREDENTIALED_REDIRECT",
    "ENGINE_UNAVAILABLE",
    "FETCH_OPERATION",
    "PROVIDER_DECLINED",
    "REDIRECT_LIMIT",
    "SEARCH_OPERATION",
    "UNDECLARED_INSTANCE",
    "UNSUPPORTED_REDIRECT_SCHEME",
]

FETCH_OPERATION = "web.fetch"
SEARCH_OPERATION = "search.web"

PROVIDER_DECLINED = "provider_declined"
"""The input names something this adapter will not retrieve, or retrieve that way."""

REDIRECT_LIMIT = "redirect_limit"
UNSUPPORTED_REDIRECT_SCHEME = "unsupported_redirect_scheme"
CROSS_ORIGIN_CREDENTIALED_REDIRECT = "cross_origin_credentialed_redirect"
"""The three ways an origin's redirect chain is refused. Both operations follow redirects."""

ENGINE_UNAVAILABLE = "engine_unavailable"
"""web.fetch: a rendered fetch needs the browser extra and the environment lacks it."""

CREDENTIAL_UNAVAILABLE = "credential_unavailable"
"""web.fetch: the run names a credential that was not delivered."""

UNDECLARED_INSTANCE = "undeclared_instance"
"""search.web: the run names a search instance the declaration does not cover."""
