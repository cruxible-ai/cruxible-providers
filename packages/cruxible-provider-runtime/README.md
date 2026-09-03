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
    [--secret-path <path> | --secret-pipe-fd <n>] \
    [--secret-pipe-timeout <seconds>] [--] <command> [args...]
```

### The two deliveries

| Flag | What the executor provides | What the shim does |
|---|---|---|
| `--secret-path <path>` | A file on a **tmpfs/ramfs** mount inside the container | Opens it (`O_NOFOLLOW`, `O_NONBLOCK`), refuses anything that is not a regular file on a memory-backed mount with exactly one name, refuses a bundle over the cap, **unlinks the path**, then reads it bounded into an anonymous in-memory copy |
| `--secret-pipe-fd <n>` | A one-shot pipe, written and left open on descriptor *n* | Drains it under the same bounded read and re-delivers the bytes the same way, so what the child reads has an end |

Both end in the same thing: an anonymous descriptor carrying a copy of the
bundle, which is what the child inherits on the fixed number. Path mode copies
rather than passing the file's own descriptor on purpose — `fstat` measures the
file at one instant, the child's reader has no cap of its own, and a writer
still holding the unlinked inode would otherwise decide where the child's EOF
is.

Neither delivery leaves anything readable behind. The path is unlinked before
the exec, and the shim refuses rather than exec'ing unless the inode had exactly
one name before the unlink and none after — a second name is a bundle that
outlives the run, and asking the descriptor afterwards is the only question
whose answer is about the inode rather than the name. The pipe is spent.
Nothing about the material is on argv, in the environment, or on any listable
path by the time provider code starts.

Nothing the shim opens or reads can block indefinitely. The path is opened
`O_NONBLOCK`, so a FIFO planted where the bundle should be returns instead of
waiting for a writer, and every read runs against a deadline —
`SECRET_DRAIN_TIMEOUT_SECONDS`, five seconds, moved only by
`--secret-pipe-timeout <seconds>` and never by the environment. A run that hangs
is worse than one that refuses: it spends the wall clock of a run that never
started.

Before either delivery is arranged the shim closes every descriptor it holds
except the standard streams and the one it was named. That is not tidiness: a
pipe whose write end leaked into the container never reaches EOF. It reaches
copies inside this process only — against a write end held outside the
container, the deadline is the whole defence.

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
| `secret_path_unreadable` | Missing, a symlink, or not a regular file — a FIFO, directory, socket or device included |
| `secret_path_not_exclusive` | The bundle had a second name before the unlink, or still had one after it |
| `secret_bundle_too_large` | Over `MAX_SECRET_BUNDLE_BYTES` (64 KiB), the same rule `secrets` enforces, one process earlier |
| `secret_pipe_fd_invalid` | Not an open pipe, a standard stream, or a number no descriptor could have |
| `secret_pipe_timeout` | The delivery did not reach its end inside the deadline |
| `secret_delivery_failed` | The bundle could not be unlinked or re-delivered — and the code any unforeseen failure is rendered as, because no exception escapes as a traceback |
| `conflicting_secret_delivery`, `missing_option_value`, `invalid_option_value`, `no_command`, `exec_failed` | Malformed invocation |

Every one of these exits **78**, including the ones an executor reaches by
getting its own argv wrong. Exit 1 with a traceback is what a crashed child
looks like, and the executor has only the status and one line to tell the two
apart.

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
2. **Deliver on a mount private to this run, and writable by nothing else.**
   This is the obligation the shim cannot check for you. *Memory-backed is not
   private.* `fstatfs` answers "is this tmpfs or ramfs", and a `/dev/shm` shared
   through `--ipc=host` or a shared IPC namespace answers yes while another
   container reads the bundle out of it — demonstrated across two containers.
   `/dev` is devtmpfs, whose magic is tmpfs's, and it passes too. So the check
   is a **necessary condition, not a sufficient one**: it rejects overlayfs, a
   volume and the container's own non-tmpfs `/tmp`, and it cannot tell a mount
   that belongs to this run from one anybody can reach. Give the run its own
   tmpfs, mounted for this container alone, in a directory nothing else can
   write to — the second half matters because a writer in the delivery
   directory can plant a FIFO or a second name where the bundle should be, and
   the shim's answer to both is a refusal, not a run.
3. **Unlinking is the shim's job, not the executor's.** The executor must not
   delete the path itself; it has no way to know the shim has opened it, and a
   race there is a run that starts with no credentials.
4. **Write the descriptor number from `container_secret_channel`.** Never a
   literal in another repository.
5. **Close the pipe's write end.** The shim's descriptor sweep closes a copy
   that leaked into the container; the executor's own copy is outside the
   process and beyond its reach. An unclosed write end used to hang the run
   forever. It now refuses `secret_pipe_timeout` after
   `SECRET_DRAIN_TIMEOUT_SECONDS` — a bounded failure is not a working
   delivery, and the obligation stands.
6. **Keep the bundle under 64 KiB.** The same 64 KiB the executor's own channel
   writer enforces, the shim's `fstat` refuses past, and the shim's bounded
   drain stops at.


## What it does not own

Governance. The Provider artifact kind, interface registration, and bucket
registration live in core; `registry.StubRegistry` exists so that this
repository's conformance suite has something to bind against, and
`docs/core-integration-seam.md` specifies what replaces it.
