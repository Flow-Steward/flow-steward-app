# Signed webhooks for workflow entrypoints

Flow Steward verifies HMAC-SHA256 signed webhook deliveries with one generic,
configuration-driven verifier. There are no provider-specific auth modes: a new
source is onboarded by filling in header names, a parser, a digest format and a
signed-payload recipe.

- Auth mode (stored value): `webhook_signature`
- Auth mode (UI label): **Signed webhook (HMAC)**
- Verifier: [`core/application/webhooks/signature_verifiers.py`](../core/application/webhooks/signature_verifiers.py)
- Editor: workflow builder → **Workflow settings → Triggers → Auth mode**

The legacy `hmac_sha256` mode is unchanged and stays byte-compatible.

## What the verifier supports

| Knob | Values |
|---|---|
| Signature source | `header` (case-insensitive lookup, 8 KiB cap) |
| Signature parser | `raw`, `prefixed`, `comma_kv` |
| Digest encoding | `hex` (64 lowercase/uppercase chars), `base64` (canonical) |
| Timestamp source | `none`, `header`, `signature_param` |
| Timestamp unit | `seconds`, `milliseconds` |
| Tolerance | 1–86400 seconds, default 300 |
| Signed payload | raw body · `timestamp + "." + raw body` · `"v0:" + timestamp + ":" + raw body` |

Every accepted signed-payload recipe contains exactly one `raw_body` part, so
the HTTP route always reads or stages the exact raw request bytes before it
authenticates. Staged (multipart) bodies stream into the HMAC in chunks.

Rules the verifier enforces, all fail-closed:

- A malformed signature header, missing signature, missing/malformed timestamp,
  expired timestamp or future timestamp is a `401`.
- A stored config that no longer normalizes is a service configuration error
  (`503`), never a `401` — it is a server defect, not a caller mistake.
- `comma_kv` parsing is deliberately simple: comma split, first `=` as the
  separator, outer whitespace trimmed. Quoted, escaped and nested values are
  rejected rather than guessed.
- Duplicate `comma_kv` keys are accepted only when *Accept any matching repeated
  signature key* is on; otherwise an ambiguous header is rejected.
- The configured signature and timestamp headers cannot be read back through a
  workflow input mapping.

Out of scope: signatures over the HTTP method or reconstructed URL, JSON
canonicalization, and RSA/ECDSA/JWKS/certificate/verify-API schemes.

## Signing secret direction

The editor makes the direction explicit, because it differs per source:

- **Generate a new signing secret** — Flow Steward creates the secret and shows
  it once after saving. Copy it into the source service. Use this when the
  receiver chooses the secret.
- **Use an existing provider signing secret** — create the endpoint in the
  provider first, then paste the signing secret it shows you. The value is sent
  once as the write-only `signing_secret` field, stored through project secret
  storage, and never echoed back, serialized or logged. The response only
  confirms `credential_configured`.

Saving non-secret signature settings never rotates or erases a stored secret.
Replacing an imported secret means saving again with a non-empty secret field;
rotating a generated secret still uses the existing rotate action.

## Example configurations

Provider names below belong to the examples only — never to verifier code.

### GitHub-style: prefixed hex over the raw body

```json
{
  "auth_mode": "webhook_signature",
  "auth_config": {
    "signature": {
      "source": "header",
      "header": "X-Hub-Signature-256",
      "parser": "prefixed",
      "prefix": "sha256=",
      "encoding": "hex"
    },
    "timestamp": { "source": "none" },
    "signed_payload": { "parts": [{ "source": "raw_body" }] }
  }
}
```

Expected request header: `X-Hub-Signature-256: sha256=<hex-digest>`

Reference: <https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries>

### Shopify-style: base64 over the raw body

```json
{
  "auth_mode": "webhook_signature",
  "auth_config": {
    "signature": {
      "source": "header",
      "header": "X-Shopify-Hmac-SHA256",
      "parser": "raw",
      "encoding": "base64"
    },
    "timestamp": { "source": "none" },
    "signed_payload": { "parts": [{ "source": "raw_body" }] }
  }
}
```

Expected request header: `X-Shopify-Hmac-SHA256: <base64-digest>`

Reference: <https://shopify.dev/docs/apps/build/webhooks/verify-deliveries>

The same shape covers Xero- and QuickBooks-style base64 body HMACs; only the
header name changes.

### Stripe-style: timestamp and signature in one comma-kv header

```json
{
  "auth_mode": "webhook_signature",
  "auth_config": {
    "signature": {
      "source": "header",
      "header": "Stripe-Signature",
      "parser": "comma_kv",
      "value_key": "v1",
      "encoding": "hex",
      "allow_multiple": true
    },
    "timestamp": {
      "source": "signature_param",
      "key": "t",
      "unit": "seconds",
      "tolerance_seconds": 300
    },
    "signed_payload": {
      "parts": [
        { "source": "timestamp" },
        { "literal": "." },
        { "source": "raw_body" }
      ]
    }
  }
}
```

Expected request header: `Stripe-Signature: t=<unix-timestamp>,v1=<hex-digest>`

`allow_multiple` accepts a delivery signed with either secret during a
provider-side rotation window.

References: <https://docs.stripe.com/webhooks> ·
<https://docs.stripe.com/webhooks/signature>

### Slack-style: separate timestamp header

```json
{
  "auth_mode": "webhook_signature",
  "auth_config": {
    "signature": {
      "source": "header",
      "header": "X-Slack-Signature",
      "parser": "prefixed",
      "prefix": "v0=",
      "encoding": "hex"
    },
    "timestamp": {
      "source": "header",
      "header": "X-Slack-Request-Timestamp",
      "unit": "seconds",
      "tolerance_seconds": 300
    },
    "signed_payload": {
      "parts": [
        { "literal": "v0:" },
        { "source": "timestamp" },
        { "literal": ":" },
        { "source": "raw_body" }
      ]
    }
  }
}
```

Expected request headers:

```text
X-Slack-Signature: v0=<hex-digest>
X-Slack-Request-Timestamp: <unix-timestamp>
```

Reference: <https://docs.slack.dev/authentication/verifying-requests-from-slack/>

## Sources this verifier cannot cover

Schemes that are not HMAC over the raw body need a different solution — for
example PayPal, AWS SNS, Google certificate/JWKS push messages, Microsoft Graph
validation, Azure Event Grid validation events, Plaid JWT/JWK verification,
Apple JWS notifications and Braintree challenge validation.
