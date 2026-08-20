"""``search.web`` — ranked results from a configured SearXNG instance.

A light client, not an engine. This adapter runs no index and hosts nothing: it
submits a query to an instance somebody deployed and records what came back.

**Which instance, and how it is named.** The instance is *configuration*, and
configuration that decides who gets talked to is governed: the instance's origin
is declared in the manifest's ``declared_endpoints`` and therefore travels in the
accepted Provider artifact, where it can be reviewed and where the cloud
backend's allowlist can be built from it. The executor names the instance for a
run through ``coordinates``, and this adapter refuses
(``undeclared_egress``) if the coordinate names an origin the accepted
declaration does not carry. Pointing a deployment at a different instance is
therefore a governance act, which is the correct shape for a decision about
where a run's evidence comes from.

The instance's **credential** is a different thing and takes the other path: it
arrives by secret-ref over the executor's descriptor channel, never in the
coordinates, never in the manifest, and never in a digest preimage.

**What the output claims.** ``retrieved`` holds what the instance answered — a
record of an exchange, gradeable as observed-shaped material *about the
instance's answer*, never about the world. ``derived`` holds what this adapter
did with it: the recency filter SearXNG cannot express, and the truncation to the
depth the run asked for. The adapter mints no Capture; the CaptureContract
decides the grade.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from cruxible_provider_runtime.egress import normalize_endpoint, partition_declared
from cruxible_provider_runtime.errors import RefusalCode, RefusalError, refuse
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext

from .http import ClientFactory, default_client_factory
from .interfaces import SEARCH_INTERFACE_ID

__all__ = ["CREDENTIAL_REF", "SearxngSearch"]

CREDENTIAL_REF = "search.web.instance_credential"
"""The ref under which an instance credential is delivered, when there is one."""

RESPONSE_CAP_BYTES = 4 * 1024 * 1024
HOURS_PER_DAY = 24


class SearxngSearch:
    """Query a configured SearXNG instance and record its answer."""

    interface_id = SEARCH_INTERFACE_ID

    def __init__(self, *, client_factory: ClientFactory | None = None) -> None:
        self._client_factory = client_factory or default_client_factory

    def __call__(self, context: ProviderRunContext) -> ProviderResult:
        try:
            instance = self._instance(context)
            query, limit, max_age_hours, language = _parse_input(context)
        except RefusalError as exc:
            return ProviderResult(status="refused", refusal=exc.refusal)

        parameters = {
            "q": query,
            "format": "json",
            "language": language,
            "pageno": "1",
            "safesearch": "1",
        }
        time_range = _time_range(max_age_hours)
        if time_range is not None:
            parameters["time_range"] = time_range

        headers = {}
        credential = context.secrets.get(CREDENTIAL_REF)
        if credential is not None:
            headers["authorization"] = credential

        endpoint = f"{instance}/search"
        client = self._client_factory(
            context.egress,
            url=endpoint,
            timeout_seconds=max(1.0, context.budgets.wall_clock_seconds * 0.8),
        )
        with client:
            response = client.get(
                _with_query(endpoint, parameters), headers=headers, cap_bytes=RESPONSE_CAP_BYTES
            )

        if not 200 <= response.status_code < 300:
            return ProviderResult.failed(
                "HttpStatus",
                f"instance answered {response.status_code}",
                instance=instance,
                status_code=response.status_code,
            )
        try:
            import json

            document = json.loads(response.text)
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

        results = [
            _normalize(entry, rank)
            for rank, entry in enumerate(document.get("results") or [], start=1)
            if isinstance(entry, dict)
        ]
        as_of = _as_of(context)
        kept, dropped = _filter_by_recency(results, max_age_hours, as_of)
        truncated = kept[:limit]

        return ProviderResult.ok(
            {
                "input_bucket": context.input_bucket,
                "retrieved": {
                    "instance": instance,
                    "query_submitted": query,
                    "parameters": parameters,
                    "number_of_results": document.get("number_of_results"),
                    "result_count": len(results),
                    "results": results,
                    "source": "packaged-recording" if response.from_recording else "network",
                },
                "derived": {
                    "kind": "recency_filtered_ranking",
                    "engine": "adapter",
                    "results": truncated,
                    "dropped_by_recency": dropped,
                    "truncated_to": limit,
                    # Recorded because the filter is a function of it. A
                    # derivation whose input is an implicit wall clock cannot be
                    # checked afterwards, and "cannot be checked afterwards" is
                    # the property this whole layer exists to remove.
                    "as_of": as_of.isoformat(),
                },
            },
            metrics={"result_count": float(len(results)), "returned": float(len(truncated))},
        )

    # -- configuration -----------------------------------------------------

    def _instance(self, context: ProviderRunContext) -> str:
        instance = context.coordinates.get("instance_url")
        if not isinstance(instance, str) or not instance.strip():
            raise refuse(
                RefusalCode.PROVIDER_DECLINED,
                "search.web needs an instance_url coordinate naming the configured instance",
            )
        origin = normalize_endpoint(instance)
        declared, dynamic = partition_declared(context.declared_endpoints)
        if origin not in declared:
            raise refuse(
                RefusalCode.UNDECLARED_EGRESS,
                "the run names an instance this implementation does not declare",
                implementation_digest=context.implementation_digest,
                instance=origin,
                declared=list(declared),
                dynamic_forms=list(dynamic),
            )
        return origin


def _parse_input(context: ProviderRunContext) -> tuple[str, int, int | None, str]:
    payload = context.input
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise refuse(RefusalCode.PROVIDER_DECLINED, "search.web needs a non-empty query")
    limit = payload.get("limit", 10)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise refuse(RefusalCode.PROVIDER_DECLINED, "limit must be a positive integer")
    max_age_hours = payload.get("max_age_hours")
    if max_age_hours is not None and (
        not isinstance(max_age_hours, int) or isinstance(max_age_hours, bool) or max_age_hours <= 0
    ):
        raise refuse(RefusalCode.PROVIDER_DECLINED, "max_age_hours must be a positive integer")
    language = payload.get("language", "en")
    if not isinstance(language, str) or not language:
        raise refuse(RefusalCode.PROVIDER_DECLINED, "language must be a string")
    return query.strip(), limit, max_age_hours, language


def _time_range(max_age_hours: int | None) -> str | None:
    """Map an hour bound onto SearXNG's three-value vocabulary.

    The mapping loses precision in one direction only: SearXNG can say day,
    month, or year, so a 90-day bound is submitted as ``year`` and the surplus is
    removed by this adapter afterwards. Submitting the *narrower* range would
    lose results the run asked for, which is the failure that cannot be repaired
    downstream.
    """

    if max_age_hours is None:
        return None
    if max_age_hours <= HOURS_PER_DAY:
        return "day"
    if max_age_hours <= HOURS_PER_DAY * 30:
        return "month"
    return "year"


def _with_query(endpoint: str, parameters: dict[str, str]) -> str:
    from urllib.parse import urlencode

    return f"{endpoint}?{urlencode(parameters)}"


def _normalize(entry: dict[str, Any], rank: int) -> dict[str, Any]:
    return {
        "rank": rank,
        "url": entry.get("url"),
        "title": entry.get("title"),
        "snippet": entry.get("content"),
        "engine": entry.get("engine"),
        "published": entry.get("publishedDate"),
    }


def _as_of(context: ProviderRunContext) -> datetime:
    """The instant the recency filter is evaluated against.

    Taken from the ``as_of`` coordinate when the executor supplies one, and from
    the wall clock otherwise. The coordinate exists because a filter evaluated
    against an implicit clock produces a result nobody can reproduce from the
    receipt — including the run's own conformance fixtures, which would
    otherwise start failing on a date nobody chose.
    """

    supplied = context.coordinates.get("as_of")
    if supplied is None:
        return datetime.now(UTC)
    if not isinstance(supplied, str):
        raise refuse(RefusalCode.PROVIDER_DECLINED, "the as_of coordinate must be a string")
    try:
        parsed = datetime.fromisoformat(supplied.replace("Z", "+00:00"))
    except ValueError as exc:
        raise refuse(
            RefusalCode.PROVIDER_DECLINED,
            f"the as_of coordinate {supplied!r} is not an ISO 8601 instant",
        ) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _filter_by_recency(
    results: list[dict[str, Any]], max_age_hours: int | None, as_of: datetime
) -> tuple[list[dict[str, Any]], int]:
    """Drop results older than the bound, keeping undated ones.

    An undated result is kept, and that is a judgement worth stating: a missing
    ``publishedDate`` is the instance saying nothing, and dropping on silence
    would quietly delete most of the web from every bounded query.
    """

    if max_age_hours is None:
        return results, 0

    horizon = as_of - timedelta(hours=max_age_hours)
    kept: list[dict[str, Any]] = []
    dropped = 0
    for entry in results:
        published = entry.get("published")
        if not isinstance(published, str) or not published:
            kept.append(entry)
            continue
        try:
            when = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError:
            kept.append(entry)
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        if when >= horizon:
            kept.append(entry)
        else:
            dropped += 1
    return kept, dropped
