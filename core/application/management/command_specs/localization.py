"""Localization tooling public command specifications."""

from __future__ import annotations

from core.application.management.command_specs.contracts import (
    CommandSpec,
    FieldSpec,
    FieldType,
    InteractivePolicy,
    OperationType,
)
from core.application.tooling.contracts import ToolRunResult
from core.application.tooling.i18n.models import (
    LocalizationBootstrapCatalogRequest,
    LocalizationCompileRequest,
    LocalizationInitLocaleRequest,
    LocalizationRebuildPotRequest,
    LocalizationUpdateRequest,
    LocalizationValidateKeysRequest,
)

_TOKEN_LOCALIZATION_BOOTSTRAP_CATALOG = "localization.bootstrap-catalog"
_TOKEN_LOCALIZATION_REBUILD_POT = "localization.rebuild-pot"
_TOKEN_LOCALIZATION_INIT_LOCALE = "localization.init-locale"
_TOKEN_LOCALIZATION_UPDATE = "localization.update"
_TOKEN_LOCALIZATION_COMPILE = "localization.compile"
_TOKEN_LOCALIZATION_VALIDATE_KEYS = "localization.validate-keys"


def _timeout() -> FieldSpec:
    return FieldSpec(
        key="timeout_seconds",
        label="Timeout seconds",
        type=FieldType.INTEGER,
        default=300,
        option_name="--timeout-seconds",
        advanced=True,
    )


LOCALIZATION_COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(
        command_id=_TOKEN_LOCALIZATION_BOOTSTRAP_CATALOG,
        path=("localization", "bootstrap-catalog"),
        summary="Bootstrap UI translation catalogs.",
        description="Generate locale catalogs and metadata from the in-code UI translation registry.",
        examples=("flow-steward localization bootstrap-catalog",),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(
            FieldSpec(
                key="source",
                label="Source",
                type=FieldType.PATH,
                default="core/application/settings/ui_i18n.py",
                option_name="--source",
            ),
            _timeout(),
        ),
        input_model=LocalizationBootstrapCatalogRequest,
        result_model=ToolRunResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_LOCALIZATION_BOOTSTRAP_CATALOG,
        result_renderer_ref=_TOKEN_LOCALIZATION_BOOTSTRAP_CATALOG,
    ),
    CommandSpec(
        command_id=_TOKEN_LOCALIZATION_REBUILD_POT,
        path=("localization", "rebuild-pot"),
        summary="Rebuild the UI POT file.",
        description="Rebuild locales/ui.pot from the default locale catalog keys.",
        examples=("flow-steward localization rebuild-pot",),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(_timeout(),),
        input_model=LocalizationRebuildPotRequest,
        result_model=ToolRunResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_LOCALIZATION_REBUILD_POT,
        result_renderer_ref=_TOKEN_LOCALIZATION_REBUILD_POT,
    ),
    CommandSpec(
        command_id=_TOKEN_LOCALIZATION_INIT_LOCALE,
        path=("localization", "init-locale"),
        summary="Initialize a locale catalog.",
        description="Initialize one locale catalog and metadata entry from locales/ui.pot.",
        examples=(
            "flow-steward localization init-locale de --label German --native-label Deutsch",
        ),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(
            FieldSpec(
                key="code",
                label="Locale code",
                type=FieldType.RUNTIME_IDENTIFIER,
                required=True,
                positional=True,
            ),
            FieldSpec(key="label", label="Label", type=FieldType.TEXT, option_name="--label"),
            FieldSpec(
                key="native_label",
                label="Native label",
                type=FieldType.TEXT,
                option_name="--native-label",
            ),
            _timeout(),
        ),
        input_model=LocalizationInitLocaleRequest,
        result_model=ToolRunResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_LOCALIZATION_INIT_LOCALE,
        result_renderer_ref=_TOKEN_LOCALIZATION_INIT_LOCALE,
    ),
    CommandSpec(
        command_id=_TOKEN_LOCALIZATION_UPDATE,
        path=("localization", "update"),
        summary="Update locale catalogs.",
        description="Update locale catalogs from locales/ui.pot without fuzzy matching.",
        examples=("flow-steward localization update",),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(_timeout(),),
        input_model=LocalizationUpdateRequest,
        result_model=ToolRunResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_LOCALIZATION_UPDATE,
        result_renderer_ref=_TOKEN_LOCALIZATION_UPDATE,
    ),
    CommandSpec(
        command_id=_TOKEN_LOCALIZATION_COMPILE,
        path=("localization", "compile"),
        summary="Compile locale catalogs.",
        description="Compile locale catalogs into gettext .mo files via pybabel.",
        examples=("flow-steward localization compile --force",),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(
            FieldSpec(
                key="force",
                label="Force",
                type=FieldType.BOOLEAN,
                default=False,
                option_name="--force",
            ),
            _timeout(),
        ),
        input_model=LocalizationCompileRequest,
        result_model=ToolRunResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_LOCALIZATION_COMPILE,
        result_renderer_ref=_TOKEN_LOCALIZATION_COMPILE,
    ),
    CommandSpec(
        command_id=_TOKEN_LOCALIZATION_VALIDATE_KEYS,
        path=("localization", "validate-keys"),
        summary="Validate UI i18n keys.",
        description=(
            "Validate referenced UI keys in POT, nonblank locale translations, "
            "and matching compiled catalogs."
        ),
        examples=("flow-steward localization validate-keys",),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(_timeout(),),
        input_model=LocalizationValidateKeysRequest,
        result_model=ToolRunResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_LOCALIZATION_VALIDATE_KEYS,
        result_renderer_ref=_TOKEN_LOCALIZATION_VALIDATE_KEYS,
    ),
)
