# Container provenance specification

The container backend's identity is the **image digest**. The build is not
claimed bit-reproducible, and nothing here pretends otherwise: two builds of
this Dockerfile from the same source will differ. What makes a given image
trustworthy is that it *records* what produced it, and that the executor refuses
an image whose record does not match the accepted Provider artifact.

## The four provenance fields

| Field | Label | Source |
|---|---|---|
| Provider artifact digest | `ai.cruxible.provider.artifact_digest` | `artifact_digest(payload)` over the accepted `providers/<provider-id>.yaml` payload with `status` removed |
| Materialization digest | `ai.cruxible.provider.materialization_digest` | the local-backend materialization digest for the marker environment the image was built for |
| Base image digest | `ai.cruxible.provider.base_image_digest` | the `FROM ...@sha256:` pin |
| Builder identity | `ai.cruxible.provider.builder_identity` | the build system's own identity string (workflow ref plus runner identity) |

The same four fields are also emitted as a build-provenance attestation
alongside the image. Labels are convenient; the attestation is what a verifier
that does not trust the registry's metadata reads.

## Why the materialization digest is on the image

A backend switch must not split track record. The **implementation digest** —
interface id, interface digest, entrypoint object path, distribution sha256 — is
identical whether the provider runs from a local environment or from this image,
and it is the track-record key. The **materialization digest** differs by
backend: locally it is the hashed resolution for a marker environment, in cloud
it is the image digest itself. Recording the local-equivalent materialization on
the image is what lets a reviewer ask "is this image built from the same
resolved dependency set the local backend would have materialized?" and get an
answer, rather than a shrug.

## What the executor checks

Before running an image, `ContainerBackend.verify_image` calls the driver's
`inspect` and compares all four fields against the accepted artifact. Any
mismatch is a typed `image_provenance_mismatch` refusal. There is no partial
acceptance and no warning mode.

## Build arguments

```sh
docker build \
  --build-arg BASE_IMAGE_DIGEST="sha256:<base>" \
  --build-arg UV_IMAGE_DIGEST="sha256:<uv>" \
  --build-arg PROVIDER_ARTIFACT_DIGEST="sha256:<artifact>" \
  --build-arg MATERIALIZATION_DIGEST="sha256:<materialization>" \
  --build-arg BUILDER_IDENTITY="<workflow-ref>@<runner>" \
  -f container/Dockerfile .
```

Both the base image and the `uv` installer image are pinned by digest. An
unpinned build tool is an unpinned build, and the provenance labels would be
recording the identity of something nobody fixed.

## Entrypoint and argv

`ENTRYPOINT` is the secret shim, `cruxible_provider_runtime.container_entry`, and
the child-harness invocation stays in `CMD`. The shim runs no command of its own:
it consumes its own leading flags, arranges the secret channel they name, and
`execv`s the rest — `CMD` when the executor supplies no argv, the executor's argv
when it does. What the `ContainerDriver` contract forbids is an `ENTRYPOINT` that
starts the harness itself, because the supplied argv would then run it a second
time inside the first and the inner process would read an already-consumed stdin.

The shim exists because a fresh container is handed stdin, stdout and stderr and
nothing else: an executor cannot pass a file descriptor across the container
boundary, and the no-mounts law rules out bind-mounting a secret file. It accepts
one memory-backed delivery — `--secret-path <tmpfs file>` or
`--secret-pipe-fd <n>` — and installs it on the fixed descriptor
`cruxible_provider_runtime.container_entry.SECRET_CHANNEL_FD`, which is the number
the run context must name. Started with no secret flag it is a pass-through, so an
image carrying it runs exactly as it did before it did. See "Secret delivery in
containers" in the `cruxible-provider-runtime` README.

## What is not claimed

- Not bit-reproducible.
- The labels are not self-authenticating: a registry that serves a different
  image under the same digest has broken content addressing, and no label can
  repair that.
- Nothing in this specification is a substitute for the cloud backend's
  structural egress enforcement (default-deny plus allowlist). Provenance says
  what the image is; the network policy says what it may reach.
