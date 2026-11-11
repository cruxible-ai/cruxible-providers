"""``search.web`` — the answer of a configured SearXNG instance to one query.

A light client, not an engine. This adapter runs no index and hosts nothing: it
submits a query to an instance somebody deployed and records what came back.

**Which instance, and how it is named.** The instance is *configuration*, and
configuration that decides who gets talked to is governed: the instance's origin
is declared in the manifest's ``declared_endpoints`` and therefore travels in the
accepted Provider artifact, where it can be reviewed and where the cloud
backend's allowlist can be built from it. The executor names the instance for a
run through ``coordinates``, and this adapter refuses
(``undeclared_instance``, declared) if the coordinate names an origin the accepted
declaration does not carry. Pointing a deployment at a different instance is
therefore a governance act, which is the correct shape for a decision about
where a run's evidence comes from.

The instance's **credential** is a different thing and takes the other path: it
arrives by secret-ref over the executor's descriptor channel, never in the
coordinates, never in the manifest, and never in a digest preimage.

**What the output is.** The Capture's bytes are the instance's response body,
verbatim: what the instance answered, a fact about the instance at a moment
rather than about the world. How it answered (the request URL with the
parameters actually submitted, the final URL, the status, the media type, the
request digest) is the typed coordinate (:mod:`.coordinate`). Ranking the
answer — by recency, by depth — would be a derived reading and is not done
here; the instance's own ``time_range`` parameter is the only filter, and it is
part of the request. The adapter mints no Capture; the CaptureKind governs it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import urlencode

from cruxible_provider_runtime.acquisition import external_capture_result
from cruxible_provider_runtime.egress import (
    DYNAMIC_TARGET_FROM_CONFIGURATION,
    normalize_endpoint,
    partition_declared,
)
from cruxible_provider_runtime.errors import Declined, decline
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext

from .coordinate import COORDINATE_TYPE, SELECTOR_TYPE, http_coordinate
from .http import ClientFactory, default_client_factory
from .operations import PROVIDER_DECLINED, SEARCH_OPERATION, UNDECLARED_INSTANCE

__all__ = ["CREDENTIAL_REF", "TIME_RANGES", "SearxngSearch"]

CREDENTIAL_REF = "search.web.instance_credential"
"""The ref under which an instance credential is delivered, when there is one."""

RESPONSE_CAP_BYTES = 4 * 1024 * 1024
TIME_RANGES = ("day", "month", "year")
"""SearXNG's own recency vocabulary, submitted as its ``time_range`` parameter."""


class SearxngSearch:
    """Query a configured SearXNG instance and return its answer as the Capture."""

    operations = (SEARCH_OPERATION,)

    def __init__(self, *, client_factory: ClientFactory | None = None) -> None:
        self._client_factory = client_factory or default_client_factory

    def __call__(self, context: ProviderRunContext) -> ProviderResult:
        try:
            # The input first: what this adapter declines for the input alone is
            # declined whatever instance a deployment configures.
            query, language, time_range = _parse_input(context)
            instance = self._instance(context)
        except Declined as exc:
            return ProviderResult(status="refused", refusal=exc.refusal)

        parameters = {
            "q": query,
            "format": "json",
            "language": language,
            "pageno": "1",
            "safesearch": "1",
        }
        if time_range is not None:
            parameters["time_range"] = time_range

        headers = {}
        credential = context.secrets.get(CREDENTIAL_REF)
        if credential is not None:
            headers["authorization"] = credential

        request_url = f"{instance}/search?{urlencode(parameters)}"
        client = self._client_factory(
            context.egress,
            url=request_url,
            timeout_seconds=max(1.0, context.budgets.wall_clock_seconds * 0.8),
        )
        with client:
            response = client.get(request_url, headers=headers, cap_bytes=RESPONSE_CAP_BYTES)

        if not 200 <= response.status_code < 300:
            return ProviderResult.failed(
                "HttpStatus",
                f"instance answered {response.status_code}",
                instance=instance,
                status_code=response.status_code,
            )
        try:
            document = json.loads(response.body)
        except ValueError:
            # The overwhelmingly common cause, and worth naming: a SearXNG
            # instance with the json format disabled answers 200 with HTML.
            return ProviderResult.failed(
                "MalformedInstanceResponse",
                "the instance did not answer JSON; a SearXNG instance must enable the "
                "json format for this adapter to read it",
                instance=instance,
                content_type=response.content_type,
            )
        if not isinstance(document, dict):
            return ProviderResult.failed(
                "MalformedInstanceResponse",
                "the instance answered JSON that is not an object",
                instance=instance,
            )
        results = document.get("results")
        return ProviderResult.ok(
            external_capture_result(
                response.body,
                source_identity="search.response",
                coordinate_type=COORDINATE_TYPE,
                selector_type=SELECTOR_TYPE,
                coordinate=http_coordinate(
                    url=request_url,
                    final_url=response.final_url,
                    status=response.status_code,
                    content_type=response.content_type,
                    response_headers=response.headers,
                    from_recording=response.from_recording is not None,
                ),
                observed_at=datetime.now(UTC),
            ),
            metrics={"result_count": float(len(results) if isinstance(results, list) else 0)},
        )

    # -- configuration -----------------------------------------------------

    def _instance(self, context: ProviderRunContext) -> str:
        instance = context.coordinates.get("instance_url")
        if not isinstance(instance, str) or not instance.strip():
            raise decline(
                PROVIDER_DECLINED,
                "search.web needs an instance_url coordinate naming the configured instance",
            )
        origin = normalize_endpoint(instance)
        declared, dynamic = partition_declared(context.declared_endpoints)
        if origin not in declared and DYNAMIC_TARGET_FROM_CONFIGURATION not in dynamic:
            raise decline(
                UNDECLARED_INSTANCE,
                "the run names an instance this implementation does not declare",
                implementation_digest=context.implementation_digest,
                instance=origin,
                declared=list(declared),
                dynamic_forms=list(dynamic),
            )
        return origin


def _parse_input(context: ProviderRunContext) -> tuple[str, str, str | None]:
    payload = context.input
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise decline(PROVIDER_DECLINED, "search.web needs a non-empty query")
    language = payload.get("language", "en")
    if not isinstance(language, str) or not language:
        raise decline(PROVIDER_DECLINED, "language must be a string")
    time_range = payload.get("time_range")
    if time_range is not None and time_range not in TIME_RANGES:
        raise decline(PROVIDER_DECLINED, "time_range must be day, month or year")
    return query.strip(), language, time_range
