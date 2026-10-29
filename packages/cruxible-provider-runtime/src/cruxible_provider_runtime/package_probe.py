"""Package-owned install-time probes executed by the ordinary supervised child.

Core runs these at installation, outside any Procedure run: it passes the probe
entrypoint a package's registration declares, and the child calls it. This
module is never imported into the daemon to execute provider code.
"""

from importlib import import_module

from .provider_api import ProviderResult, ProviderRunContext
from .registration import callable_path

RESOURCE_PROBE_OPERATION = "cruxible.package.resource"


class ResourceProbe:
    """Check a package-declared runtime resource without installing it implicitly."""

    operations = (RESOURCE_PROBE_OPERATION,)

    def __call__(self, context: ProviderRunContext) -> ProviderResult:
        entrypoint = callable_path(context.input["entrypoint"])
        module, _, member = entrypoint.partition(":")
        available = getattr(import_module(module), member)()
        if not isinstance(available, bool):
            raise ValueError("resource probe must return a Boolean")
        return ProviderResult.ok({"available": available})
