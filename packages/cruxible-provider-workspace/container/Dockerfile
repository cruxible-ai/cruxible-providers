# Container backend image for the workspace built-in provider.
#
# The build is NOT claimed bit-reproducible. What is authoritative is the image
# digest; what makes it trustworthy is the provenance recorded below, which the
# executor checks against the accepted Provider artifact and refuses on
# mismatch. See container/provenance.md for the label contract and the build
# arguments each label is fed from.
#
# No test in this repository requires Docker. The container path is covered at
# protocol level against a fake driver; this file is the buildable spelling of
# the same contract.

# The base image is pinned by digest, never by tag: a tag is a moving pointer,
# and a moving base defeats the point of pinning anything above it.
ARG BASE_IMAGE_DIGEST=sha256:0000000000000000000000000000000000000000000000000000000000000000
ARG UV_IMAGE_DIGEST=sha256:0000000000000000000000000000000000000000000000000000000000000000

# The installer is pinned by digest for the same reason the base image is: an
# unpinned build tool is an unpinned build.
FROM ghcr.io/astral-sh/uv@${UV_IMAGE_DIGEST} AS uv

FROM python:3.12-slim@${BASE_IMAGE_DIGEST} AS build

ARG PROVIDER_ARTIFACT_DIGEST
ARG MATERIALIZATION_DIGEST
ARG BUILDER_IDENTITY

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CONFIG=1 \
    UV_PROJECT_ENVIRONMENT=/opt/provider/.venv

WORKDIR /opt/provider

# Materialize from the committed lock and nothing else. --locked makes the lock
# authority: a resolver free to move is a resolver free to break a pin.
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --locked --no-dev

FROM python:3.12-slim@${BASE_IMAGE_DIGEST}

ARG PROVIDER_ARTIFACT_DIGEST
ARG MATERIALIZATION_DIGEST
ARG BASE_IMAGE_DIGEST
ARG BUILDER_IDENTITY

# The provenance contract. These four labels are exactly the fields
# ImageProvenance carries, and exactly what the executor compares against the
# accepted Provider artifact before it will run the image.
LABEL ai.cruxible.provider.artifact_digest="${PROVIDER_ARTIFACT_DIGEST}" \
      ai.cruxible.provider.materialization_digest="${MATERIALIZATION_DIGEST}" \
      ai.cruxible.provider.base_image_digest="${BASE_IMAGE_DIGEST}" \
      ai.cruxible.provider.builder_identity="${BUILDER_IDENTITY}" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.title="cruxible-provider-workspace"

COPY --from=build /opt/provider /opt/provider
ENV PATH="/opt/provider/.venv/bin:${PATH}"

# Run as an unprivileged user. The container backend is the only place a
# third-party provider is actually contained, so the containment has to be real.
RUN useradd --system --create-home --uid 10001 provider
USER 10001

# The image speaks the same child protocol as the local backend: a run context
# on stdin, a result envelope on stdout, credential material on an inherited
# descriptor named by the run context.
#
# ENTRYPOINT is the secret shim; the invocation itself stays in CMD. A fresh
# container is handed stdin, stdout and stderr and nothing else, so no executor
# can pass a descriptor across the boundary, and the no-mounts law rules out
# bind-mounting a secret file. The shim turns a memory-backed delivery a
# container runtime CAN perform -- a tmpfs file or a one-shot pipe -- into the
# descriptor the child reads, on the fixed number the run context names
# (cruxible_provider_runtime.container_entry.SECRET_CHANNEL_FD).
#
# The ContainerDriver contract survives this intact. What it forbids is an
# ENTRYPOINT that invokes the child itself, because the executor's argv would
# then start the harness a second time inside the first. The shim runs no
# command of its own: it execs the argv it is handed, which is CMD when the
# executor supplies none and the executor's own argv when it does. Started with
# no secret flag it is a pass-through, so an image built from this file behaves
# exactly as it did before the shim existed.
ENTRYPOINT ["python", "-m", "cruxible_provider_runtime.container_entry"]
CMD ["python", "-m", "cruxible_provider_runtime.child"]
