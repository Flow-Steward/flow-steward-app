import { useI18n } from "@/lib/i18n/useI18n";
import { OptionalLabel, OptionalMarker } from "@/components/shared/design-system";
import { type RefObject, type Dispatch, type SetStateAction } from "react";
import { Link } from "react-router";
import { TOKEN_SCOPES } from "@/pages/settings/shared/types";
import {
  type ServiceAccountFieldErrors,
  toggleStringSelection,
  type RestrictionOption,
  type AccountDrawerMode,
} from "./service-account-support";
import { CheckboxRow } from "./checkbox-row";

function ServiceAccountScopesField({
  errors,
  firstScopeRef,
  scopes,
  setErrors,
  setScopes,
}: Readonly<{
  errors: ServiceAccountFieldErrors;
  firstScopeRef: RefObject<HTMLInputElement | null>;
  scopes: string[];
  setErrors: Dispatch<SetStateAction<ServiceAccountFieldErrors>>;
  setScopes: Dispatch<SetStateAction<string[]>>;
}>) {
  const { t } = useI18n();
  return (
    <fieldset
      aria-describedby={errors.scopes ? "service-account-scopes-error" : "service-account-scopes-help"}
      aria-invalid={Boolean(errors.scopes)}
      aria-required="true"
      className="fs-field fs-settings-span"
    >
      <legend className="fs-field-label">{t("react.settings.maximum_authority", "Maximum authority *")}</legend>
      <span className="fs-field-helper" id="service-account-scopes-help">
        {t("react.settings.choose_the_maximum_authority_for_this_identity_deliberately", "Choose the maximum authority for this identity deliberately.")} </span>
      <div className="fs-scope-grid">
        {TOKEN_SCOPES.map((scope, index) => (
          <CheckboxRow
            checked={scopes.includes(scope)}
            id={`service-account-scope-${scope.replaceAll(".", "-")}`}
            inputRef={index === 0 ? firstScopeRef : undefined}
            key={scope}
            name="service_account_scopes"
            onChange={() =>
              setScopes((current) => {
                setErrors((currentErrors) => ({ ...currentErrors, scopes: "" }));
                return toggleStringSelection(current, scope);
              })
            }
            value={scope}
          >
            {scope}
          </CheckboxRow>
        ))}
      </div>
      {errors.scopes ? (
        <span className="fs-field-helper error" id="service-account-scopes-error" role="alert">
          {errors.scopes}
        </span>
      ) : null}
    </fieldset>
  );
}

function RestrictionSelector({
  kind,
  onSearchChange,
  onToggle,
  options,
  search,
  selectedIds,
}: Readonly<{
  kind: "project" | "workflow";
  onSearchChange: (value: string) => void;
  onToggle: (id: string) => void;
  options: RestrictionOption[];
  search: string;
  selectedIds: string[];
}>) {
  const { t } = useI18n();
  return (
    <fieldset className="fs-field">
      <legend className="fs-field-label">{kind === "project" ? t("react.settings.allowed_projects", "Allowed projects") : t("react.settings.allowed_workflows", "Allowed workflows")} <OptionalMarker /></legend>
      <span className={selectedIds.length ? "fs-field-helper" : "fs-field-helper fs-ink-attention"}>
        {selectedIds.length
          ? (kind === "project"
            ? t("react.settings.restricted_projects_total", "Restricted to {count} project(s).", { count: selectedIds.length })
            : t("react.settings.restricted_workflows_total", "Restricted to {count} workflow(s).", { count: selectedIds.length }))
          : (kind === "project"
            ? t("react.settings.unrestricted_projects_help", "Nothing selected means ALL projects. Select entries to restrict this account.")
            : t("react.settings.unrestricted_workflows_help", "Nothing selected means ALL workflows. Select entries to restrict this account."))}
      </span>
      <input
        className="fs-input"
        id={`service-account-${kind}-search`}
        name={`service_account_${kind}_search`}
        onChange={(event) => onSearchChange(event.target.value)}
        placeholder={kind === "project" ? t("react.settings.search_projects", "Search projects") : t("react.settings.search_workflows", "Search workflows")}
        value={search}
      />
      <div className="fs-settings-restriction-list">
        {options.map((option) => (
          <div key={option.id}>
            <CheckboxRow
              ariaLabel={[option.label, option.description].filter(Boolean).join(" ")}
              checked={selectedIds.includes(option.id)}
              id={`service-account-${kind}-${option.id}`}
              name={`service_account_${kind}_ids`}
              onChange={() => onToggle(option.id)}
              value={option.id}
            >
              <span className="min-w-0 [overflow-wrap:anywhere]">
                <span>{option.label}</span>
                {option.description ? <span className="block text-xs text-slate-400">{option.description}</span> : null}
              </span>
            </CheckboxRow>
            <Link
              className="ml-6 text-xs underline"
              to={kind === "workflow" ? `/registry/workflows/${encodeURIComponent(option.id)}` : `/projects/${encodeURIComponent(option.id)}`}
              rel="noopener noreferrer"
              target="_blank"
            >
              {t("react.settings.inspect", "Inspect")} {kind}: {option.label}
            </Link>
          </div>
        ))}
      </div>
    </fieldset>
  );
}

type ServiceAccountFormProps = {
  accountActiveInput: boolean;
  accountDescriptionInput: string;
  accountDisplayNameInput: string;
  accountDrawerMode: AccountDrawerMode;
  accountFieldErrors: ServiceAccountFieldErrors;
  accountKeyInput: string;
  accountKeyRef: RefObject<HTMLInputElement | null>;
  accountProjectIdsInput: string[];
  accountProjectOptions: RestrictionOption[];
  accountProjectSearch: string;
  accountScopesInput: string[];
  accountWorkflowIdsInput: string[];
  accountWorkflowOptions: RestrictionOption[];
  accountWorkflowSearch: string;
  firstAccountScopeRef: RefObject<HTMLInputElement | null>;
  onCreate: () => void;
  onUpdate: () => void;
  setAccountActiveInput: (value: boolean) => void;
  setAccountDescriptionInput: (value: string) => void;
  setAccountDisplayNameInput: (value: string) => void;
  setAccountFieldErrors: Dispatch<SetStateAction<ServiceAccountFieldErrors>>;
  setAccountKeyInput: (value: string) => void;
  setAccountProjectIdsInput: Dispatch<SetStateAction<string[]>>;
  setAccountProjectSearch: (value: string) => void;
  setAccountScopesInput: Dispatch<SetStateAction<string[]>>;
  setAccountWorkflowIdsInput: Dispatch<SetStateAction<string[]>>;
  setAccountWorkflowSearch: (value: string) => void;
};

export function ServiceAccountForm({
  accountActiveInput,
  accountDescriptionInput,
  accountDisplayNameInput,
  accountDrawerMode,
  accountFieldErrors,
  accountKeyInput,
  accountKeyRef,
  accountProjectIdsInput,
  accountProjectOptions,
  accountProjectSearch,
  accountScopesInput,
  accountWorkflowIdsInput,
  accountWorkflowOptions,
  accountWorkflowSearch,
  firstAccountScopeRef,
  onCreate,
  onUpdate,
  setAccountActiveInput,
  setAccountDescriptionInput,
  setAccountDisplayNameInput,
  setAccountFieldErrors,
  setAccountKeyInput,
  setAccountProjectIdsInput,
  setAccountProjectSearch,
  setAccountScopesInput,
  setAccountWorkflowIdsInput,
  setAccountWorkflowSearch,
}: Readonly<ServiceAccountFormProps>) {
  const { t } = useI18n();
  const submit = () => {
    if (accountDrawerMode === "create") {
      onCreate();
      return;
    }
    onUpdate();
  };
  return (
    <form
      className="fs-form-grid"
      id="service-account-form"
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <label className="fs-field">
        <span className="fs-field-label">{t("react.settings.account_key", "Account key *")}</span>
        <input
          aria-describedby={accountFieldErrors.key ? "service-account-key-error" : undefined}
          aria-invalid={Boolean(accountFieldErrors.key)}
          aria-label={t("react.settings.account_key_2", "Account key")}
          className="fs-input"
          disabled={accountDrawerMode === "detail"}
          id="service-account-key"
          name="service_account_key"
          onChange={(event) => {
            setAccountKeyInput(event.target.value);
            setAccountFieldErrors((current) => ({ ...current, key: "" }));
          }}
          ref={accountKeyRef}
          required
          value={accountKeyInput}
        />
        {accountFieldErrors.key ? (
          <span className="fs-field-helper error" id="service-account-key-error" role="alert">
            {accountFieldErrors.key}
          </span>
        ) : null}
      </label>
      <label className="fs-field">
        <OptionalLabel id="service-account-display-name-label">{t("react.settings.display_name", "Display name")}</OptionalLabel>
        <input
          aria-labelledby="service-account-display-name-label"
          className="fs-input"
          id="service-account-display-name"
          name="service_account_display_name"
          onChange={(event) => setAccountDisplayNameInput(event.target.value)}
          value={accountDisplayNameInput}
        />
      </label>
      <label className="fs-field fs-settings-span">
        <OptionalLabel id="service-account-description-label">{t("react.settings.description", "Description")}</OptionalLabel>
        <input
          aria-labelledby="service-account-description-label"
          className="fs-input"
          id="service-account-description"
          name="service_account_description"
          onChange={(event) => setAccountDescriptionInput(event.target.value)}
          value={accountDescriptionInput}
        />
      </label>
      <ServiceAccountScopesField
        errors={accountFieldErrors}
        firstScopeRef={firstAccountScopeRef}
        scopes={accountScopesInput}
        setErrors={setAccountFieldErrors}
        setScopes={setAccountScopesInput}
      />
      <RestrictionSelector
        kind="project"
        onSearchChange={setAccountProjectSearch}
        onToggle={(id) => setAccountProjectIdsInput((current) => toggleStringSelection(current, id))}
        options={accountProjectOptions}
        search={accountProjectSearch}
        selectedIds={accountProjectIdsInput}
      />
      <RestrictionSelector
        kind="workflow"
        onSearchChange={setAccountWorkflowSearch}
        onToggle={(id) => setAccountWorkflowIdsInput((current) => toggleStringSelection(current, id))}
        options={accountWorkflowOptions}
        search={accountWorkflowSearch}
        selectedIds={accountWorkflowIdsInput}
      />
      <div className="fs-field fs-settings-span">
        <CheckboxRow
          checked={accountActiveInput}
          id="service-account-active"
          name="service_account_active"
          onChange={setAccountActiveInput}
        >
          {t("react.settings.account_active", "Account active")} </CheckboxRow>
      </div>
    </form>
  );
}
