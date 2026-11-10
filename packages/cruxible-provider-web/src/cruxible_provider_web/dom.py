"""``web.render.dom`` -- a fetch: the DOM a browser assembles for one URL.

Some pages carry nothing until a browser runs them: the body an origin sends is
an empty root and a script. ``web.render.dom`` is the fetch for those pages
(D-R37): a browser navigates to the URL the run names, runs the page's scripts,
pulls the subresources the markup names, and the serialized DOM it assembles is
the Capture's bytes (``text/html``), verbatim. It is an acquisition, not a
derive: it reads the live web (``effect: external_read``), so it takes a URL,
never a Capture, and two runs may differ.

Its coordinate is an HTTP coordinate like ``web.fetch``'s: the URL asked for,
the URL the main frame settled on, its 2xx status, the kept freshness headers of
the main-frame response, the request digest, and ``renderer`` -- the engine that
assembled the page, so a reader never takes a DOM for the body an origin sent.

**Egress.** A browser opens its own sockets, so nothing it does passes through
the instrumented HTTP client. :func:`.browser.drive_page` hooks the page's
request, response and websocket events instead, and every endpoint the page
contacts -- the main frame, a CDN, the API a script queries, a ws/wss stream it
opens -- reaches the run's egress recorder.

**Private targets.** The URL the run names is vetted before the browser goes
there, and the address the browser reports for the main-frame response is
judged before the page is captured; either one on a private, loopback,
link-local or otherwise non-public address refuses the run
(``private_or_internal_address``). What the browser contacts in between --
redirect hops, subresources, script fetches, WebSockets -- is recorded but not
vetted: see :mod:`.browser`.

A URL on the reserved ``fixture.invalid`` host is answered from a packaged
recording's post-assembly DOM rather than a browser; the coordinate says
``source: packaged-recording`` and the renderer ``recorded:<id>``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from cruxible_provider_runtime.acquisition import external_capture_result
from cruxible_provider_runtime.egress import EgressRecorder
from cruxible_provider_runtime.errors import Declined, decline
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext

from .browser import PageRenderer, PlaywrightRenderer, RenderedPage
from .coordinate import COORDINATE_TYPE, SELECTOR_TYPE, http_coordinate
from .fetch import DEFAULT_MAX_BYTES, MAX_RESPONSE_BYTES, page_weight_class
from .operations import PROVIDER_DECLINED, RENDER_DOM_OPERATION
from .recordings import is_fixture_url, recording_for

__all__ = ["DOM_MEDIA", "WebRenderDom"]

DOM_MEDIA = "text/html; charset=utf-8"


class _RecordedRenderer:
    """Replays the post-assembly DOM a packaged recording captured.

    A recorded render is not a render, and the label says so: the engine name it
    reports is ``recorded:<id>``, never ``playwright``. It serves only the
    reserved fixture host, and only a recording that carries a rendered
    document. The request is recorded like any other, as ``web.fetch`` records a
    replayed exchange.
    """

    name = "recorded"

    def render(self, url: str, *, timeout_seconds: float, recorder: EgressRecorder) -> RenderedPage:
        del timeout_seconds
        recording = recording_for(url)
        if recording is None or recording.rendered_body is None:
            raise decline(
                PROVIDER_DECLINED,
                f"no packaged recording carries a rendered document for {url!r}",
                url=url,
            )
        recorder.record(url)
        return RenderedPage(
            engine=f"recorded:{recording.id}",
            html=recording.rendered_body,
            final_url=url,
            status_code=recording.response.status_code,
            headers={key.lower(): value for key, value in recording.response.headers.items()},
        )


class WebRenderDom:
    """Assemble the page a run names in a browser; its DOM is the Capture."""

    operations = (RENDER_DOM_OPERATION,)

    def __init__(self, *, renderer: PageRenderer | None = None) -> None:
        # The default is the production spelling; the seam lets a test drive the
        # production wiring over a page double.
        self._renderer = renderer

    def __call__(self, context: ProviderRunContext) -> ProviderResult:
        try:
            url, logical_source, max_bytes = _parse(context)
            renderer = self._renderer or (
                _RecordedRenderer() if is_fixture_url(url) else PlaywrightRenderer()
            )
            page = renderer.render(
                url,
                timeout_seconds=max(1.0, context.budgets.wall_clock_seconds * 0.8),
                recorder=context.egress,
            )
        except Declined as exc:
            return ProviderResult(status="refused", refusal=exc.refusal)

        if not 200 <= page.status_code < 300:
            # As for web.fetch: a failed attempt at an answer, not a declined one.
            return ProviderResult.failed(
                "HttpStatus",
                f"origin answered {page.status_code}",
                url=url,
                final_url=page.final_url,
                status_code=page.status_code,
            )
        assembled = page.html.encode("utf-8")
        if len(assembled) > max_bytes:
            return ProviderResult.refused(
                PROVIDER_DECLINED,
                "the assembled document is heavier than the max_bytes this run declared",
                url=url,
                cap_bytes=max_bytes,
                observed_weight=page_weight_class(len(assembled)),
            )
        recorded = page.engine.startswith("recorded:")
        return ProviderResult.ok(
            external_capture_result(
                assembled,
                source_identity=logical_source,
                coordinate_type=COORDINATE_TYPE,
                selector_type=SELECTOR_TYPE,
                coordinate=http_coordinate(
                    url=url,
                    final_url=page.final_url,
                    status=page.status_code,
                    content_type=DOM_MEDIA,
                    # The main frame's length is not the assembled DOM's; only
                    # its freshness validators describe these bytes.
                    response_headers={
                        name: value
                        for name, value in page.headers.items()
                        if name != "content-length"
                    },
                    from_recording=recorded,
                    renderer=page.engine,
                ),
                observed_at=datetime.now(UTC),
            ),
            metrics={"byte_count": float(len(assembled))},
            events=[{"kind": "recorded_render", "url": url}] if recorded else [],
        )


def _parse(context: ProviderRunContext) -> tuple[str, str, int]:
    payload = context.input
    url = payload.get("url")
    if not isinstance(url, str) or urlsplit(url).scheme.lower() not in {"http", "https"}:
        raise decline(
            PROVIDER_DECLINED,
            "web.render.dom needs an http or https url",
            **({"url": url} if isinstance(url, str) else {}),
        )
    logical_source = payload.get("logical_source", "web.rendered")
    if not isinstance(logical_source, str) or not re.fullmatch(
        r"[a-z][a-z0-9_.-]{0,255}", logical_source
    ):
        raise decline(PROVIDER_DECLINED, "logical_source must be a logical name", url=url)
    max_bytes = payload.get("max_bytes", DEFAULT_MAX_BYTES)
    if (
        not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)
        or not 0 < max_bytes <= MAX_RESPONSE_BYTES
    ):
        raise decline(PROVIDER_DECLINED, "max_bytes must be between 1 and 33554432", url=url)
    return url, logical_source, max_bytes
