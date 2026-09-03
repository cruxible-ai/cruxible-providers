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
| `container_entry` | The image's entry shim: a memory-backed secret in, an inherited descriptor out |
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

## Secret delivery in containers

Locally the executor is the child's parent, so it opens a descriptor over the
credential bundle and hands it across with `pass_fds`; the run context names the
number and `child` reads it. A container breaks that. A fresh container is given
stdin, stdout and stderr and nothing else, an executor has no way to pass a
descriptor across the boundary, and the no-mounts law forbids bind-mounting a
secret file into the image. Something inside the image has to turn a delivery
the container runtime *can* perform into the descriptor the child expects.

`cruxible_provider_runtime.container_entry` is that something, and it is the
images' `ENTRYPOINT`. The harness stays in `CMD`; the shim execs whatever argv it
is handed, so an image started with no secret flag runs exactly the process it
ran before the shim existed.

```
python -m cruxible_provider_runtime.container_entry \
    [--secret-path <path> | --secret-pipe-fd <n>] [--] <command> [args...]
```

### The two deliveries

| Flag | What the executor provides | What the shim does |
|---|---|---|
| `--secret-path <path>` | A file on a **tmpfs/ramfs** mount inside the container | Opens it (`O_NOFOLLOW`), verifies the mount really is memory-backed, refuses a bundle over the cap, **unlinks the path**, and installs the descriptor |
| `--secret-pipe-fd <n>` | A one-shot pipe, written and left open on descriptor *n* | Drains it under the cap and re-delivers the bytes on an anonymous memory descriptor, so what the child reads has an end |

Both are memory-backed by construction, and neither leaves anything readable
behind: the path is gone before the exec, and the pipe is spent. Nothing about
the material is on argv, in the environment, or on any listable path by the time
provider code starts — the flags name a path or a descriptor number and the shim
consumes both.

Before either delivery is arranged the shim closes every descriptor it holds
except the standard streams and the one it was named. That is not tidiness: a
pipe whose write end leaked into the container never reaches EOF, and a reader
waiting for one that is not coming is a run that hangs rather than fails.

### The fixed descriptor

The bundle always lands on **`container_entry.SECRET_CHANNEL_FD`** — descriptor
3, the first above the standard streams, and out of reach of the harness's own
`reserve_stdout()`, which duplicates stdout onto the lowest *free* number and so
lands on 4 with the bundle already open on 3.

The run context has to name the descriptor the child actually sees, and the
executor building that context is outside the image with no way to observe it.
So it calls the helper rather than writing an integer:

```python
from cruxible_provider_runtime import container_secret_channel

context = RunContext(..., secret_channel=container_secret_channel(sorted(bundle)))
```

`container_secret_channel(refs)` takes `SecretRef`s or bare ref strings and
returns a `SecretChannelSpec` on the fixed descriptor, refs sorted — the same
spec `execute.invoke` builds for the local path, for the same bundle.

### Refusals

The shim runs before there is a run context, a run id, or anywhere to put a
typed refusal, so it exits **78** with one line on stderr and no traceback —
a traceback from a process holding a bundle is one more place for bytes to end
up. The line is `shim_refused: <code>`, and the codes are closed:

| Code | Meaning |
|---|---|
| `secret_path_not_memory_backed` | The path's mount is not tmpfs or ramfs |
| `secret_path_unverifiable` | The filesystem type could not be read at all (see the platform note below) |
| `secret_path_unreadable` | Missing, a symlink, or not a regular file |
| `secret_bundle_too_large` | Over `MAX_SECRET_BUNDLE_BYTES` (64 KiB), the same rule `secrets` enforces, one process earlier |
| `secret_pipe_fd_invalid` | Not an open pipe, or a standard stream |
| `secret_delivery_failed` | The bundle could not be unlinked, or could not be re-delivered |
| `conflicting_secret_delivery`, `missing_option_value`, `no_command`, `exec_failed` | Malformed invocation |

**Platform note.** The memory-backed check is `fstatfs` against the descriptor
the shim already holds — `f_type` compared to `TMPFS_MAGIC` / `RAMFS_MAGIC` —
which is a Linux interface CPython does not expose, so it is reached through
`ctypes`. Anywhere else, and on Linux if the call fails, the shim refuses
`secret_path_unverifiable` rather than guessing. `--secret-path` is therefore
**Linux-only**, which is where provider images run; `--secret-pipe-fd` is the
portable delivery.

### What the executor owes

Ruled by the maintainer, 2026-09-04:

1. **Deliver on memory, never on a mount.** A tmpfs file or a one-shot pipe. A
   bind-mounted secret file is not a delivery this shim will accept, and the
   no-mounts law rules it out before the shim ever sees it.
2. **Unlinking is the shim's job, not the executor's.** The executor must not
   delete the path itself; it has no way to know the shim has opened it, and a
   race there is a run that starts with no credentials.
3. **Write the descriptor number from `container_secret_channel`.** Never a
   literal in another repository.
4. **Close the pipe's write end.** The shim's descriptor sweep will close a copy
   that leaked into the container, but the executor's own copy is the executor's
   to close.
5. **Keep the bundle under 64 KiB.** The cap is enforced in three places now and
   it is the same 64 KiB in all of them.


## What it does not own

Governance. The Provider artifact kind, interface registration, and bucket
registration live in core; `registry.StubRegistry` exists so that this
repository's conformance suite has something to bind against, and
`docs/core-integration-seam.md` specifies what replaces it.
