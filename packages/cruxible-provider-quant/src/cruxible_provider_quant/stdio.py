"""Keep an engine's chatter off the protocol's stdout.

The child harness writes the result envelope to file descriptor 1 and nothing
else may. That is fine for a provider that computes in silence; it is not fine
for real analytical engines, several of which log progress to stdout by default.
splink prints ``Blocking time: 0.00 seconds`` on every prediction, and a single
such line ahead of the envelope turns a successful run into
``provider_protocol_violation``.

So every engine call in this package runs inside :func:`stdout_to_stderr`. The
redirect is at the **file-descriptor** level, not at ``sys.stdout``, because the
writers that matter are not all Python: duckdb, BLAS, and numba all write
through the C runtime, and ``contextlib.redirect_stdout`` never sees them. The
original descriptor is duplicated aside and restored on the way out, including
on an exception, so the harness still owns fd 1 when it writes the envelope.

Diverted output is not discarded. It goes to stderr, which the executor already
captures, redacts, and carries into exhaust — a diagnostic that vanishes is
worse than one in the wrong stream.

This is a workaround for a runtime seam, and it is worth naming as one: the
child harness could take this responsibility for every provider by moving its
own writing end to a private duplicate of fd 1 for the duration of the provider
call. Until it does, each plane package that binds a chatty engine has to
remember, and the failure mode of forgetting is a corrupted envelope rather than
a warning.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager

__all__ = ["stdout_to_stderr"]


@contextmanager
def stdout_to_stderr() -> Iterator[None]:
    """Point fd 1 at fd 2 for the duration of the block, then put it back."""

    sys.stdout.flush()
    saved = os.dup(1)
    try:
        os.dup2(2, 1)
        yield
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)
