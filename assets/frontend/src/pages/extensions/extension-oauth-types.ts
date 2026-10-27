export type OAuthCredentialFieldDeclaration = {
  field_id: string;
  value_type: string;
  required: boolean;
  secret: boolean;
  label: string;
  help_text: string;
  placeholder?: string;
  autocomplete?: string;
  validation?: {
    min_length?: number;
    max_length?: number;
    type?: "https_origin";
  };
};

export type OAuthIntentInputDeclaration = {
  input_id: string;
  validation?: {
    type?: string;
    max_length?: number;
  };
};

export type OAuthAuthorizationIntentDeclaration = {
  intent_id: string;
  user_inputs?: readonly OAuthIntentInputDeclaration[];
};

export type OAuthProviderDeclaration = {
  provider_id: string;
  display_name: string;
  description?: string;
  setup_page_id: string;
  help_page_id?: string;
  error_help_anchors?: Readonly<Record<string, string>>;
  credential_fields: readonly OAuthCredentialFieldDeclaration[];
  authorization_intents: readonly OAuthAuthorizationIntentDeclaration[];
};

export type OAuthConnectionTypeDeclaration = {
  connection_type_id: string;
  display_name?: string;
  description?: string;
  auth: {
    type: string;
    provider_id: string;
    scopes?: {
      required?: readonly string[];
      optional?: readonly string[];
    };
  };
};

function object(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function rows(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter((row): row is Record<string, unknown> => Boolean(row) && typeof row === "object" && !Array.isArray(row))
    : [];
}

function string(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

export function oauthProvidersFromManifest(value: unknown): OAuthProviderDeclaration[] {
  return rows(object(value).oauth_providers)
    .filter((provider) => {
      const credentialFields = rows(provider.credential_fields);
      const intents = rows(provider.authorization_intents);
      return Boolean(
        string(provider.provider_id)
        && string(provider.display_name)
        && string(provider.setup_page_id)
        && credentialFields.length
        && intents.length
        && credentialFields.every((field) =>
          string(field.field_id)
          && string(field.value_type)
          && typeof field.required === "boolean"
          && typeof field.secret === "boolean"
          && string(field.label)
          && string(field.help_text),
        )
        && intents.every((intent) => string(intent.intent_id)),
      );
    }) as OAuthProviderDeclaration[];
}

export function oauthConnectionTypesFromManifest(value: unknown): OAuthConnectionTypeDeclaration[] {
  return rows(object(value).connection_types)
    .filter((connectionType) => {
      const auth = object(connectionType.auth);
      return Boolean(
        string(connectionType.connection_type_id)
        && string(auth.type).toLowerCase() === "oauth2"
        && string(auth.provider_id),
      );
    }) as OAuthConnectionTypeDeclaration[];
}
