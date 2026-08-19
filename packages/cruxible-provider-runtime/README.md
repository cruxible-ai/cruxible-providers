# cruxible-provider-runtime

The support library every Cruxible provider package uses. Apache-2.0.

Providers do not import most of this. It is the *executor's* half of the
contract, plus the small provider-facing surface in
`cruxible_provider_runtime.provider_api`.

## What it owns

| Module | Responsibility |
|---|---|
| `manifest` | The package-side manifest schema. Unknown fields fail closed. |
| `artifact` | The governed Provider artifact payload and its digest |
| `protocol` | The run context and result envelope, and `protocol_version` |
| `canonical` | Canonical JSON and domain-tagged digests |
| `digests` | `implementation_digest` and `materialization_digest` |
| `resolution` | Lock resolution for an explicit marker environment |
| `index` | Fetch-on-bind from pinned indexes |
| `cache` | The sealed, permission-checked, atomically-renamed materialization cache |
| `secrets` | Credential delivery over an inherited descriptor, and redaction |
| `budget` | Out-of-process wall-clock and output-size enforcement |
| `egress` | Endpoints declared versus endpoints actually contacted |
| `buckets` | The bucket vocabulary format, ids, and selectors |
| `registry` | A **stub** registry standing in for core |
| `backends` | The two backend kinds and their injected drivers |
| `binding`, `execute` | Bind and invoke |
| `child` | The provider-side process harness |
| `testing` | Fakes, so that no conformance test needs a network or a container engine |
| `errors` | The typed refusal taxonomy |

## The provider-facing surface

```python
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext


class Implementation:
    interface_id = "plane.operation"

    def __call__(self, context: ProviderRunContext) -> ProviderResult:
        return ProviderResult.ok({"result": ...})
```

Credentials arrive already resolved in `context.secrets`, keyed by ref. Endpoints
are recorded through `context.egress.record(url)`. Budgets are in
`context.budgets` so a provider can report against them, but they are enforced
around the process rather than inside it.

## What it does not own

Governance. The Provider artifact kind, interface registration, and bucket
registration live in core; `registry.StubRegistry` exists so that this
repository's conformance suite has something to bind against, and
`docs/core-integration-seam.md` specifies what replaces it.
