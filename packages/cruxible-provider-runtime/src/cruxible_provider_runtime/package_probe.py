"""Package-owned classifier calls executed by the ordinary supervised child.

Core passes an entry point from the accepted registration and its vocabulary.
This module is never imported into the daemon to execute provider code.
"""

from importlib import import_module

from .buckets import BucketVocabulary
from .provider_api import ProviderResult, ProviderRunContext


class ClassifierProbe:
    interface_id = "cruxible.package.classifier"

    def __call__(self, context: ProviderRunContext) -> ProviderResult:
        entrypoint = context.input["entrypoint"]
        module, separator, member = entrypoint.partition(":")
        if (
            not separator
            or not member.isidentifier()
            or not all(part.isidentifier() for part in module.split("."))
        ):
            raise ValueError("invalid classifier entry point")
        classifier = getattr(import_module(module), member)
        vocabulary = BucketVocabulary.model_validate(context.input["vocabulary"])
        assignment = classifier(context.input["value"])
        if assignment is None:
            raise ValueError("classifier declined the input")
        return ProviderResult.ok({"bucket": vocabulary.bucket_id(assignment)})


class ResourceProbe:
    """Check a package-declared runtime resource without installing it implicitly."""

    interface_id = "cruxible.package.resource"

    def __call__(self, context: ProviderRunContext) -> ProviderResult:
        from .registration import ClassifierExport

        entrypoint = ClassifierExport.callable_path(context.input["entrypoint"])
        module, _, member = entrypoint.partition(":")
        available = getattr(import_module(module), member)()
        if not isinstance(available, bool):
            raise ValueError("resource probe must return a Boolean")
        return ProviderResult.ok({"available": available})
