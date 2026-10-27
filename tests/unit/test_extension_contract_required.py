"""Every extension execution boundary requires an explicit current host contract."""

from types import SimpleNamespace

import pytest

from core.application.extensions.command_install_runtime import _release_incompatibility
from core.infrastructure.db.remote_extension_runtime import _runtime_availability_error
from core.infrastructure.extension_runtime.extension_runtime_invocation import (
    extension_runtime_availability_failure,
)


@pytest.mark.parametrize("declared", [None, "", "   ", "extension_host_v1"])
def test_install_rejects_unconfirmed_host_contract(declared):
    release = SimpleNamespace(contract_version=declared)
    assert _release_incompatibility(release, {})


@pytest.mark.parametrize("declared", [None, "", "   ", "extension_host_v1"])
def test_local_invocation_rejects_unconfirmed_contract_before_external_effect(declared):
    failure = extension_runtime_availability_failure(
        {"enabled": True, "runtime": {"contract_version": declared}}
    )
    assert failure is not None
    assert failure["detail"] == "extension_contract_incompatible"
    assert failure["response"]["definitely_no_external_effect"] is True


@pytest.mark.parametrize("declared", [None, "", "   ", "extension_host_v1"])
def test_remote_invocation_rejects_unconfirmed_host_contract(declared):
    assert (
        _runtime_availability_error(
            {"runtime": {"contract_version": declared}, "runtime_status": "ready"}
        )
        == "capability_mismatch"
    )


@pytest.mark.parametrize("container", ["runtime", "compiled_manifest", "manifest"])
def test_current_contract_remains_usable_for_local_invocation(container):
    runtime = {"contract_version": "extension_host_v2"}
    extension = {container: runtime if container == "runtime" else {"runtime": runtime}}
    assert extension_runtime_availability_failure(extension) is None


def test_current_contract_remains_usable_for_install_and_remote_invocation():
    assert not _release_incompatibility(SimpleNamespace(contract_version="extension_host_v2"), {})
    assert not _runtime_availability_error({"runtime": {"contract_version": "extension_host_v2"}})


@pytest.mark.parametrize("declared", [None, "", "   ", "extension_host_v1"])
def test_sdk_rejects_unconfirmed_envelope_contract(declared):
    from core.infrastructure.extension_sdk.public.flowsteward_extension_sdk import (
        runtime_context_from_payload,
    )

    with pytest.raises(ValueError, match="contract"):
        runtime_context_from_payload({"contract_version": declared, "runtime_context": {}})
