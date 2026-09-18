"""Resource readiness is package-defined and reports availability honestly."""

from types import SimpleNamespace

import pytest
from cruxible_provider_runtime import package_probe
from cruxible_provider_runtime.registration import RuntimeRequirement


@pytest.mark.parametrize("available", [True, False])
def test_resource_probe_preserves_boolean_result(monkeypatch, available):
    monkeypatch.setattr(
        package_probe, "import_module", lambda name: SimpleNamespace(check=lambda: available)
    )
    result = package_probe.ResourceProbe()(
        SimpleNamespace(input={"entrypoint": "example.resources:check"})
    )
    assert result.status == "ok"
    assert result.output == {"available": available}


def test_resource_probe_refuses_truthy_non_boolean(monkeypatch):
    monkeypatch.setattr(
        package_probe, "import_module", lambda name: SimpleNamespace(check=lambda: "ready")
    )
    with pytest.raises(ValueError, match="Boolean"):
        package_probe.ResourceProbe()(
            SimpleNamespace(input={"entrypoint": "example.resources:check"})
        )


def test_resource_probe_refuses_invalid_entrypoint_before_import(monkeypatch):
    monkeypatch.setattr(package_probe, "import_module", lambda name: pytest.fail("imported"))
    with pytest.raises(ValueError):
        package_probe.ResourceProbe()(SimpleNamespace(input={"entrypoint": "../../run"}))


def test_resource_requirement_needs_a_probe_but_python_extra_does_not():
    fields = {
        "interface_id": "web.browser",
        "name": "chromium",
        "description": "Browser executable",
    }
    with pytest.raises(ValueError, match="probe"):
        RuntimeRequirement(kind="runtime_resource", **fields)
    assert RuntimeRequirement(kind="python_extra", **fields).probe_entrypoint is None
