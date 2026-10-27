import {
  SIGNED_WEBHOOK_EXAMPLES,
  defaultSignedWebhookBuilderDraft,
  normalizeSignedWebhookBuilderDraft,
  signedWebhookAuthConfigFromDraft,
  signedWebhookDraftFromAuthConfig,
  signedWebhookPlaceholderHeaders,
  signedWebhookSummary,
  signedWebhookToleranceError,
  type SignedWebhookBuilderDraft,
} from "./detail-webhook-signature-builder";

function exampleDraft(id: string): SignedWebhookBuilderDraft {
  const example = SIGNED_WEBHOOK_EXAMPLES.find((entry) => entry.id === id);
  if (!example) {
    throw new Error(`missing example ${id}`);
  }
  return { ...example.draft };
}

describe("signed webhook builder helper", () => {
  it("builds a raw base64 body config from the Shopify-style example", () => {
    expect(signedWebhookAuthConfigFromDraft(exampleDraft("shopify_style"))).toEqual({
      signature: {
        source: "header",
        header: "X-Shopify-Hmac-SHA256",
        parser: "raw",
        encoding: "base64",
      },
      timestamp: { source: "none" },
      signed_payload: { parts: [{ source: "raw_body" }] },
    });
  });

  it("builds a prefixed hex body config from the GitHub-style example", () => {
    expect(signedWebhookAuthConfigFromDraft(exampleDraft("github_style"))).toEqual({
      signature: {
        source: "header",
        header: "X-Hub-Signature-256",
        parser: "prefixed",
        encoding: "hex",
        prefix: "sha256=",
      },
      timestamp: { source: "none" },
      signed_payload: { parts: [{ source: "raw_body" }] },
    });
  });

  it("builds a comma-kv timestamped config from the Stripe-style example", () => {
    expect(signedWebhookAuthConfigFromDraft(exampleDraft("stripe_style"))).toEqual({
      signature: {
        source: "header",
        header: "Stripe-Signature",
        parser: "comma_kv",
        encoding: "hex",
        value_key: "v1",
        allow_multiple: true,
      },
      timestamp: {
        source: "signature_param",
        unit: "seconds",
        tolerance_seconds: 300,
        key: "t",
      },
      signed_payload: {
        parts: [{ source: "timestamp" }, { literal: "." }, { source: "raw_body" }],
      },
    });
  });

  it("builds a separate-timestamp-header config from the Slack-style example", () => {
    expect(signedWebhookAuthConfigFromDraft(exampleDraft("slack_style"))).toEqual({
      signature: {
        source: "header",
        header: "X-Slack-Signature",
        parser: "prefixed",
        encoding: "hex",
        prefix: "v0=",
      },
      timestamp: {
        source: "header",
        unit: "seconds",
        tolerance_seconds: 300,
        header: "X-Slack-Request-Timestamp",
      },
      signed_payload: {
        parts: [
          { literal: "v0:" },
          { source: "timestamp" },
          { literal: ":" },
          { source: "raw_body" },
        ],
      },
    });
  });

  it("round-trips every example through the saved config shape", () => {
    for (const example of SIGNED_WEBHOOK_EXAMPLES) {
      const config = signedWebhookAuthConfigFromDraft(example.draft);
      const { draft, supported } = signedWebhookDraftFromAuthConfig(config);

      expect(supported).toBe(true);
      expect(draft).toEqual(normalizeSignedWebhookBuilderDraft(example.draft));
    }
  });

  it("drops a timestamp when the signed payload is raw body only", () => {
    const draft = normalizeSignedWebhookBuilderDraft({
      ...exampleDraft("slack_style"),
      payloadRecipe: "raw_body",
    });

    expect(draft.timestampMode).toBe("none");
    expect(signedWebhookAuthConfigFromDraft(draft).timestamp).toEqual({ source: "none" });
  });

  it("clears prefix and comma-kv fields that do not apply to the chosen value mode", () => {
    const draft = normalizeSignedWebhookBuilderDraft({
      ...exampleDraft("stripe_style"),
      valueMode: "prefixed",
      prefix: "v1=",
    });

    expect(draft.signatureKey).toBe("");
    expect(draft.allowMultiple).toBe(false);
    expect(draft.timestampMode).toBe("header");
  });

  it.each([
    [
      "a prefixed parser with no prefix",
      { source: "header", header: "X-Sig", parser: "prefixed", encoding: "hex" },
      { source: "none" },
      [{ source: "raw_body" }],
    ],
    [
      "a comma-kv parser with no value key",
      { source: "header", header: "X-Sig", parser: "comma_kv", encoding: "hex" },
      { source: "none" },
      [{ source: "raw_body" }],
    ],
    [
      "a header timestamp with no header name",
      { source: "header", header: "X-Sig", parser: "prefixed", prefix: "v0=", encoding: "hex" },
      { source: "header", unit: "seconds", tolerance_seconds: 300 },
      [
        { literal: "v0:" },
        { source: "timestamp" },
        { literal: ":" },
        { source: "raw_body" },
      ],
    ],
    [
      "a signature_param timestamp on a non-comma-kv header",
      { source: "header", header: "X-Sig", parser: "prefixed", prefix: "v0=", encoding: "hex" },
      { source: "signature_param", key: "t", unit: "seconds", tolerance_seconds: 300 },
      [{ source: "timestamp" }, { literal: "." }, { source: "raw_body" }],
    ],
    [
      "a timestamped recipe with no timestamp configured",
      { source: "header", header: "X-Sig", parser: "raw", encoding: "hex" },
      { source: "none" },
      [{ source: "timestamp" }, { literal: "." }, { source: "raw_body" }],
    ],
    [
      "a fractional tolerance window",
      {
        source: "header",
        header: "X-Sig",
        parser: "comma_kv",
        value_key: "v1",
        encoding: "hex",
      },
      { source: "signature_param", key: "t", unit: "seconds", tolerance_seconds: 1.5 },
      [{ source: "timestamp" }, { literal: "." }, { source: "raw_body" }],
    ],
  ])(
    "keeps %s read-only instead of rewriting it into a different rule",
    (_label, signature, timestamp, parts) => {
      const { supported, draft } = signedWebhookDraftFromAuthConfig({
        signature,
        timestamp,
        signed_payload: { parts },
      });

      expect(supported).toBe(false);
      expect(draft).toEqual(defaultSignedWebhookBuilderDraft());
    },
  );

  it.each(["1.5", "0.5", " 12s", "1e3", "-30"])(
    "forwards the non-whole tolerance %s verbatim instead of truncating it",
    (typed) => {
      const config = signedWebhookAuthConfigFromDraft({
        ...exampleDraft("stripe_style"),
        toleranceSeconds: typed,
      });

      expect(
        (config.timestamp as Record<string, unknown>).tolerance_seconds,
      ).toBe(typed.trim());
    },
  );

  it.each([
    ["300", 300],
    [" 120 ", 120],
    ["86400", 86400],
  ])("sends the whole tolerance %s as a number", (typed, expected) => {
    const config = signedWebhookAuthConfigFromDraft({
      ...exampleDraft("stripe_style"),
      toleranceSeconds: typed,
    });

    expect((config.timestamp as Record<string, unknown>).tolerance_seconds).toBe(expected);
  });

  it.each([
    ["1.5", "Tolerance must be a whole number of seconds."],
    ["abc", "Tolerance must be a whole number of seconds."],
    ["0", "Tolerance must be between 1 and 86400 seconds."],
    ["86401", "Tolerance must be between 1 and 86400 seconds."],
  ])("flags the unusable tolerance %s in the editor", (typed, message) => {
    expect(
      signedWebhookToleranceError({ ...exampleDraft("stripe_style"), toleranceSeconds: typed }),
    ).toBe(message);
  });

  it("does not flag a tolerance on a recipe that carries no timestamp", () => {
    expect(
      signedWebhookToleranceError({
        ...exampleDraft("shopify_style"),
        toleranceSeconds: "1.5",
      }),
    ).toBe("");
  });

  it("reports an unrepresentable saved config as unsupported", () => {
    const { supported, draft } = signedWebhookDraftFromAuthConfig({
      signature: { source: "header", header: "X-Signature", parser: "raw", encoding: "hex" },
      timestamp: { source: "none" },
      signed_payload: {
        parts: [{ source: "method" }, { source: "raw_body" }],
      },
    });

    expect(supported).toBe(false);
    expect(draft).toEqual(defaultSignedWebhookBuilderDraft());
  });

  it.each([
    ["github_style", ["X-Hub-Signature-256: sha256=<hex-digest>"]],
    ["shopify_style", ["X-Shopify-Hmac-SHA256: <base64-digest>"]],
    ["stripe_style", ["Stripe-Signature: t=<unix-timestamp>,v1=<hex-digest>"]],
    [
      "slack_style",
      [
        "X-Slack-Signature: v0=<hex-digest>",
        "X-Slack-Request-Timestamp: <unix-timestamp>",
      ],
    ],
  ])("renders placeholder headers for %s", (id, expected) => {
    expect(
      signedWebhookPlaceholderHeaders(exampleDraft(id as string)).map(
        ([name, value]) => `${name}: ${value}`,
      ),
    ).toEqual(expected);
  });

  it.each([
    ["shopify_style", "Read X-Shopify-Hmac-SHA256 as base64; sign raw body"],
    [
      "github_style",
      'Read X-Hub-Signature-256 after "sha256=" as hex; sign raw body',
    ],
    [
      "stripe_style",
      'Read v1 from Stripe-Signature as hex; sign timestamp + "." + raw body',
    ],
    [
      "slack_style",
      'Read X-Slack-Signature after "v0=" as hex; sign "v0:" + timestamp + ":" + raw body',
    ],
  ])("summarises %s as a one-line rule", (id, expected) => {
    expect(signedWebhookSummary(exampleDraft(id as string))).toBe(expected);
  });

  it("prompts for the signature header before one is entered", () => {
    expect(signedWebhookSummary(defaultSignedWebhookBuilderDraft())).toBe(
      "Enter the signature header the source service sends.",
    );
  });

  it("keeps example presets free of provider identity fields", () => {
    for (const example of SIGNED_WEBHOOK_EXAMPLES) {
      const config = signedWebhookAuthConfigFromDraft(example.draft);

      expect(JSON.stringify(config)).not.toContain(example.id);
      expect(Object.keys(config).sort()).toEqual([
        "signature",
        "signed_payload",
        "timestamp",
      ]);
    }
  });
});
