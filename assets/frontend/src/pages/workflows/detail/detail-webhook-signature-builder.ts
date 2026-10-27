import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
/**
 * Deterministic, dependency-free translation between the guided "Signed webhook
 * (HMAC)" controls and the generic `webhook_signature` auth config the backend
 * stores.
 *
 * Provider names appear here only inside the example presets. Nothing in the
 * conversion, summary or preview logic branches on a provider.
 */

export const SIGNED_WEBHOOK_AUTH_MODE = "webhook_signature";

/** Flow Steward generates the signing secret and reveals it once. */
export const SIGNED_WEBHOOK_SECRET_GENERATED = "generate"; // pragma: allowlist secret
/** The operator pastes a signing secret the source service already issued. */
export const SIGNED_WEBHOOK_SECRET_IMPORTED = "import"; // pragma: allowlist secret

export type SignedWebhookValueMode = "raw" | "prefixed" | "comma_kv";
export type SignedWebhookDigestFormat = "hex" | "base64";
export type SignedWebhookTimestampMode = "none" | "header" | "signature_param";
export type SignedWebhookTimestampUnit = "seconds" | "milliseconds";
export type SignedWebhookPayloadRecipe =
  | "raw_body"
  | "timestamp_dot_body"
  | "v0_timestamp_body";

export type SignedWebhookBuilderDraft = {
  signatureHeader: string;
  valueMode: SignedWebhookValueMode;
  prefix: string;
  signatureKey: string;
  allowMultiple: boolean;
  digestFormat: SignedWebhookDigestFormat;
  timestampMode: SignedWebhookTimestampMode;
  timestampHeader: string;
  timestampKey: string;
  timestampUnit: SignedWebhookTimestampUnit;
  toleranceSeconds: string;
  payloadRecipe: SignedWebhookPayloadRecipe;
};

// pragma: allowlist secret - direction of the signing secret, not a secret value
export type SignedWebhookSecretDirection =
  | typeof SIGNED_WEBHOOK_SECRET_GENERATED
  | typeof SIGNED_WEBHOOK_SECRET_IMPORTED;

const DEFAULT_TOLERANCE_SECONDS = "300";
export const SIGNED_WEBHOOK_MAX_TOLERANCE_SECONDS = 86400;

/**
 * Never truncate the typed tolerance: `Number.parseInt("1.5")` would silently
 * narrow the operator's freshness window to one second and leave the backend
 * nothing to reject. A value that is not a whole number of seconds is forwarded
 * verbatim so the server fails it closed with an accurate message.
 */
function toleranceSecondsValue(raw: string): number | string {
  const value = raw.trim();
  if (!value) {
    return Number(DEFAULT_TOLERANCE_SECONDS);
  }
  return /^\d+$/.test(value) ? Number(value) : value;
}

/** Inline validation message for the Tolerance field, or "" when it is usable. */
export function signedWebhookToleranceError(draft: SignedWebhookBuilderDraft, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (draft.timestampMode === "none") {
    return "";
  }
  const value = draft.toleranceSeconds.trim();
  if (!value) {
    return "";
  }
  if (!/^\d+$/.test(value)) {
    return t("react.workflows.tolerance_must_be_a_whole_number_of_seconds", "Tolerance must be a whole number of seconds.");
  }
  const seconds = Number(value);
  if (seconds < 1 || seconds > SIGNED_WEBHOOK_MAX_TOLERANCE_SECONDS) {
    return t("react.workflows.display_tolerance_range", "Tolerance must be between 1 and {max} seconds.", { max: SIGNED_WEBHOOK_MAX_TOLERANCE_SECONDS });
  }
  return "";
}

const PAYLOAD_PARTS: Record<
  SignedWebhookPayloadRecipe,
  Array<Record<string, string>>
> = {
  raw_body: [{ source: "raw_body" }],
  timestamp_dot_body: [
    { source: "timestamp" },
    { literal: "." },
    { source: "raw_body" },
  ],
  v0_timestamp_body: [
    { literal: "v0:" },
    { source: "timestamp" },
    { literal: ":" },
    { source: "raw_body" },
  ],
};

export const SIGNED_WEBHOOK_PAYLOAD_RECIPE_LABELS: Record<
  SignedWebhookPayloadRecipe,
  string
> = {
  raw_body: "Raw body",
  timestamp_dot_body: 'Timestamp + "." + raw body',
  v0_timestamp_body: '"v0:" + timestamp + ":" + raw body',
};

export function signedWebhookPayloadRecipeLabel(recipe: SignedWebhookPayloadRecipe, t: I18nContextValue["t"] = fallbackContext.t): string {
  switch (recipe) {
    case "raw_body": return t("react.workflows.display_raw_body", "Raw body");
    case "timestamp_dot_body": return t("react.workflows.display_timestamp_dot_body", 'Timestamp + "." + raw body');
    case "v0_timestamp_body": return t("react.workflows.display_v0_timestamp_body", '"v0:" + timestamp + ":" + raw body');
  }
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function defaultSignedWebhookBuilderDraft(): SignedWebhookBuilderDraft {
  return {
    signatureHeader: "",
    valueMode: "raw",
    prefix: "",
    signatureKey: "",
    allowMultiple: false,
    digestFormat: "hex",
    timestampMode: "none",
    timestampHeader: "",
    timestampKey: "",
    timestampUnit: "seconds",
    toleranceSeconds: DEFAULT_TOLERANCE_SECONDS,
    payloadRecipe: "raw_body",
  };
}

/**
 * Keep the draft internally consistent: the backend accepts a timestamp only
 * when the signed payload actually includes one, and a `signature_param`
 * timestamp only for comma-separated key/value headers.
 */
export function normalizeSignedWebhookBuilderDraft(
  draft: SignedWebhookBuilderDraft,
): SignedWebhookBuilderDraft {
  const next: SignedWebhookBuilderDraft = { ...draft };
  if (next.valueMode !== "prefixed") {
    next.prefix = "";
  }
  if (next.valueMode !== "comma_kv") {
    next.signatureKey = "";
    next.allowMultiple = false;
    if (next.timestampMode === "signature_param") {
      next.timestampMode = "header";
    }
  }
  if (next.payloadRecipe === "raw_body") {
    next.timestampMode = "none";
  } else if (next.timestampMode === "none") {
    next.timestampMode = next.valueMode === "comma_kv" ? "signature_param" : "header";
  }
  if (next.timestampMode === "none") {
    next.timestampHeader = "";
    next.timestampKey = "";
  }
  if (next.timestampMode === "header") {
    next.timestampKey = "";
  }
  if (next.timestampMode === "signature_param") {
    next.timestampHeader = "";
  }
  if (!text(next.toleranceSeconds)) {
    next.toleranceSeconds = DEFAULT_TOLERANCE_SECONDS;
  }
  return next;
}

export function signedWebhookAuthConfigFromDraft(
  draft: SignedWebhookBuilderDraft,
): Record<string, unknown> {
  const normalized = normalizeSignedWebhookBuilderDraft(draft);
  const signature: Record<string, unknown> = {
    source: "header",
    header: text(normalized.signatureHeader),
    parser: normalized.valueMode,
    encoding: normalized.digestFormat,
  };
  if (normalized.valueMode === "prefixed") {
    signature.prefix = text(normalized.prefix);
  }
  if (normalized.valueMode === "comma_kv") {
    signature.value_key = text(normalized.signatureKey);
    signature.allow_multiple = Boolean(normalized.allowMultiple);
  }
  const timestamp: Record<string, unknown> = { source: normalized.timestampMode };
  if (normalized.timestampMode !== "none") {
    timestamp.unit = normalized.timestampUnit;
    timestamp.tolerance_seconds = toleranceSecondsValue(normalized.toleranceSeconds);
    if (normalized.timestampMode === "header") {
      timestamp.header = text(normalized.timestampHeader);
    } else {
      timestamp.key = text(normalized.timestampKey);
    }
  }
  return {
    signature,
    timestamp,
    signed_payload: {
      parts: PAYLOAD_PARTS[normalized.payloadRecipe].map((part) => ({ ...part })),
    },
  };
}

function payloadRecipeFromParts(parts: unknown): SignedWebhookPayloadRecipe | null {
  if (!Array.isArray(parts)) {
    return null;
  }
  const key = JSON.stringify(
    parts.map((part) => {
      const record = asRecord(part);
      return "literal" in record
        ? ["literal", text(record.literal)]
        : ["source", text(record.source)];
    }),
  );
  const match = (
    Object.keys(PAYLOAD_PARTS) as SignedWebhookPayloadRecipe[]
  ).find(
    (recipe) =>
      JSON.stringify(
        PAYLOAD_PARTS[recipe].map((part) =>
          "literal" in part ? ["literal", part.literal] : ["source", part.source],
        ),
      ) === key,
  );
  return match ?? null;
}

/** Matches the backend's tolerance rule: a whole number of seconds in 1..86400. */
function isWholeSecondsWindow(value: unknown): boolean {
  if (value === undefined || value === null) {
    return true;
  }
  let seconds = Number.NaN;
  if (typeof value === "number") {
    seconds = value;
  } else if (typeof value === "string" && /^\d+$/.test(value.trim())) {
    seconds = Number(value.trim());
  }
  return (
    Number.isInteger(seconds) && seconds > 0 && seconds <= SIGNED_WEBHOOK_MAX_TOLERANCE_SECONDS
  );
}

/**
 * Rehydrate a saved config back into the guided controls. `supported` is false
 * when the stored shape cannot be represented by the builder, in which case the
 * editor falls back to a read-only advanced view.
 */
export function signedWebhookDraftFromAuthConfig(config: unknown): {
  draft: SignedWebhookBuilderDraft;
  supported: boolean;
} {
  const source = asRecord(config);
  const signature = asRecord(source.signature);
  const timestamp = asRecord(source.timestamp);
  const draft = defaultSignedWebhookBuilderDraft();
  const recipe = payloadRecipeFromParts(asRecord(source.signed_payload).parts);
  const valueMode = text(signature.parser) || "raw";
  const digestFormat = text(signature.encoding) || "hex";
  const timestampMode = text(timestamp.source) || "none";
  const timestampUnit = text(timestamp.unit) || "seconds";
  const recipeUsesTimestamp = recipe !== null && recipe !== "raw_body";
  // Mirror the backend normalizer's *required* fields, not just the enums: a
  // stored `prefixed` config with no prefix (or a `signature_param` timestamp on
  // a non-comma-kv header) is unrepresentable here and must stay read-only,
  // otherwise the builder would silently rewrite it into a different rule.
  const supported =
    text(signature.source) === "header" &&
    Boolean(text(signature.header)) &&
    (valueMode === "raw" || valueMode === "prefixed" || valueMode === "comma_kv") &&
    (valueMode !== "prefixed" || Boolean(text(signature.prefix))) &&
    (valueMode !== "comma_kv" || Boolean(text(signature.value_key))) &&
    (digestFormat === "hex" || digestFormat === "base64") &&
    (timestampMode === "none" ||
      timestampMode === "header" ||
      timestampMode === "signature_param") &&
    (timestampMode !== "header" || Boolean(text(timestamp.header))) &&
    (timestampMode !== "signature_param" ||
      (Boolean(text(timestamp.key)) && valueMode === "comma_kv")) &&
    (timestampUnit === "seconds" || timestampUnit === "milliseconds") &&
    (timestampMode === "none" || isWholeSecondsWindow(timestamp.tolerance_seconds)) &&
    recipe !== null &&
    recipeUsesTimestamp === (timestampMode !== "none");
  if (!supported) {
    return { draft, supported: false };
  }
  draft.signatureHeader = text(signature.header);
  draft.valueMode = valueMode as SignedWebhookValueMode;
  draft.prefix = text(signature.prefix);
  draft.signatureKey = text(signature.value_key);
  draft.allowMultiple = Boolean(signature.allow_multiple);
  draft.digestFormat = digestFormat as SignedWebhookDigestFormat;
  draft.timestampMode = timestampMode as SignedWebhookTimestampMode;
  draft.timestampHeader = text(timestamp.header);
  draft.timestampKey = text(timestamp.key);
  draft.timestampUnit = timestampUnit as SignedWebhookTimestampUnit;
  const toleranceSeconds = timestamp.tolerance_seconds;
  draft.toleranceSeconds =
    typeof toleranceSeconds === "number" || typeof toleranceSeconds === "string"
      ? String(toleranceSeconds)
      : String(DEFAULT_TOLERANCE_SECONDS);
  draft.payloadRecipe = recipe;
  return { draft: normalizeSignedWebhookBuilderDraft(draft), supported: true };
}

function digestPlaceholder(draft: SignedWebhookBuilderDraft): string {
  return draft.digestFormat === "base64" ? "<base64-digest>" : "<hex-digest>";
}

/**
 * Placeholder request headers the source service has to send, derived from the
 * generic config rather than from any provider preset.
 */
export function signedWebhookPlaceholderHeaders(
  draft: SignedWebhookBuilderDraft,
): Array<[string, string]> {
  const normalized = normalizeSignedWebhookBuilderDraft(draft);
  const headerName = text(normalized.signatureHeader) || "X-Signature";
  const digest = digestPlaceholder(normalized);
  let value = digest;
  if (normalized.valueMode === "prefixed") {
    value = `${text(normalized.prefix)}${digest}`;
  } else if (normalized.valueMode === "comma_kv") {
    const signatureKey = text(normalized.signatureKey) || "v1";
    value =
      normalized.timestampMode === "signature_param"
        ? `${text(normalized.timestampKey) || "t"}=<unix-timestamp>,${signatureKey}=${digest}`
        : `${signatureKey}=${digest}`;
  }
  const headers: Array<[string, string]> = [[headerName, value]];
  if (normalized.timestampMode === "header") {
    headers.push([
      text(normalized.timestampHeader) || "X-Request-Timestamp",
      "<unix-timestamp>",
    ]);
  }
  return headers;
}

/** One-line, read-only description of the rule that will be saved. */
export function signedWebhookSummary(draft: SignedWebhookBuilderDraft, t: I18nContextValue["t"] = fallbackContext.t): string {
  const normalized = normalizeSignedWebhookBuilderDraft(draft);
  const headerName = text(normalized.signatureHeader);
  if (!headerName) {
    return t("react.workflows.enter_the_signature_header_the_source_service_sends", "Enter the signature header the source service sends.");
  }
  let read = t("react.workflows.display_read_header", "Read {header}", { header: headerName });
  if (normalized.valueMode === "prefixed") {
    read = t("react.workflows.display_read_header_after_prefix", 'Read {header} after "{prefix}"', { header: headerName, prefix: text(normalized.prefix) });
  } else if (normalized.valueMode === "comma_kv") {
    read = t("react.workflows.display_read_signature_from_header", "Read {key} from {header}", { key: text(normalized.signatureKey) || "v1", header: headerName });
  }
  const payload =
    signedWebhookPayloadRecipeLabel(normalized.payloadRecipe, t).toLowerCase();
  return t("react.workflows.value0_as_value1_sign_value2", "{value0} as {value1}; sign {value2}", { value0: read, value1: normalized.digestFormat, value2: payload });
}

export type SignedWebhookExample = {
  id: string;
  label: string;
  draft: SignedWebhookBuilderDraft;
};

/**
 * Data-only shortcuts. They populate the generic controls and nothing else — no
 * provider id, profile or catalog entry is created or sent to the backend.
 */
export function localizedSIGNED_WEBHOOK_EXAMPLES(t: I18nContextValue["t"] = fallbackContext.t): SignedWebhookExample[] {
  return [
  {
    id: "github_style",
    label: t("react.workflows.github_style", "GitHub-style"),
    draft: {
      ...defaultSignedWebhookBuilderDraft(),
      signatureHeader: "X-Hub-Signature-256",
      valueMode: "prefixed",
      prefix: "sha256=",
      digestFormat: "hex",
      payloadRecipe: "raw_body",
    },
  },
  {
    id: "shopify_style",
    label: t("react.workflows.shopify_style", "Shopify-style"),
    draft: {
      ...defaultSignedWebhookBuilderDraft(),
      signatureHeader: "X-Shopify-Hmac-SHA256",
      valueMode: "raw",
      digestFormat: "base64",
      payloadRecipe: "raw_body",
    },
  },
  {
    id: "stripe_style",
    label: t("react.workflows.stripe_style", "Stripe-style"),
    draft: {
      ...defaultSignedWebhookBuilderDraft(),
      signatureHeader: "Stripe-Signature",
      valueMode: "comma_kv",
      signatureKey: "v1",
      allowMultiple: true,
      digestFormat: "hex",
      timestampMode: "signature_param",
      timestampKey: "t",
      payloadRecipe: "timestamp_dot_body",
    },
  },
  {
    id: "slack_style",
    label: t("react.workflows.slack_style", "Slack-style"),
    draft: {
      ...defaultSignedWebhookBuilderDraft(),
      signatureHeader: "X-Slack-Signature",
      valueMode: "prefixed",
      prefix: "v0=",
      digestFormat: "hex",
      timestampMode: "header",
      timestampHeader: "X-Slack-Request-Timestamp",
      payloadRecipe: "v0_timestamp_body",
    },
  },
];
}

export const SIGNED_WEBHOOK_EXAMPLES: SignedWebhookExample[] = localizedSIGNED_WEBHOOK_EXAMPLES();
