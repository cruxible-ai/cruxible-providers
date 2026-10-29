"""The web plane's operations and the refusal codes they declare.

``registration.json`` is the declaration; these constants are how the
implementations spell the same names, and the registration-stability test holds
the two together. Each code is the operation's own vocabulary, not the
runtime's: the runtime's closed codes (an undelivered credential, undeclared
egress, a missing engine) are raised as runtime refusals and never declared
here.
"""

from __future__ import annotations

__all__ = [
    "CROSS_ORIGIN_CREDENTIALED_REDIRECT",
    "FETCH_OPERATION",
    "PROVIDER_DECLINED",
    "REDIRECT_LIMIT",
    "SEARCH_OPERATION",
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
