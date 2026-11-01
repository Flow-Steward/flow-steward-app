# B2B catalog patterns and operation qualification

Use these public examples to choose migration candidates and identify operation
requirements. They are a curated starting set, not a popularity ranking and not
claims of successful import or execution. A catalog description establishes the
use case; fetch the actual export before conversion. Current target connection
and extension availability must be established by live capability discovery.

| Source example | Source operations to inspect | Flow Steward composition and remaining qualification |
| --- | --- | --- |
| [n8n lead generation/qualification/follow-up](https://n8n.io/workflows/6941-lead-generation-and-qualification-with-gpt-4o-google-workspace-and-automated-follow-up/) | Form intake, company enrichment, classification, Sheets log, email/calendar follow-up | Use explicit trigger mapping, verified model operation, If/Switch and actual Workspace operations. Native provider-operation availability and OAuth are unverified. Inspect item expansion, follow-up timing and AI dependencies. |
| [n8n qualify/route multichannel leads](https://n8n.io/workflows/9739-qualify-and-route-leads-across-channels-with-gpt-4o-slack-and-crm-integration/) | Intake, extraction/scoring, CRM writes, Slack notification, error logging | Classify each CRM/Slack/Sheets operation separately; prefer existing native/MCP contracts, then verified HTTP children. Model and trigger readiness cannot be inferred from the template. |
| [n8n invoice intake from Outlook](https://n8n.io/workflows/3396-parse-incoming-invoices-from-outlook-using-ai-document-understanding/) | Outlook polling, attachment download, document classification/extraction, conditional routing | Artifact-mode HTTP and routing are existing primitives. Outlook auth, binary-file mapping, extraction contracts and polling checkpoints need qualification. In-progress document/model integrations are not a ready operation. |
| [Make update CRM leads via webhook and filters](https://www.make.com/templates/13208-update-leads-in-crm-using-advanced-filters-and-custom-webhooks) | Inbound webhook, filters, Close CRM update | If routing and an outbound HTTP request are available primitives. Verify inbound trigger/auth/signature handling and CRM update request/response. An outbound HTTP request cannot substitute for the inbound trigger. Blueprint JSON still required. |
| [Make template catalog](https://www.make.com/templates) email-to-Sheets example | New email trigger, extract selected fields, append a row | Inspect email checkpoint/deduplication, attachment handling and actual Sheets append operation. Credentials/setup are separate from missing operations; description-only assessment until a blueprint is obtained. |
| [Activepieces AI workflow collection](https://www.activepieces.com/blog/10-ai-powered-workflows-you-can-implement-today-templates): lead nurturing and invoice reminders | Trigger, CRM/Sheets lookup, model generation, timed email | Map the exported versioned piece/action tree. Verify delays, per-record iteration, provider actions and setup; the article is not an executable export. |
| [Activepieces Template Library](https://www.activepieces.com/docs/flows/template-library): sales and support categories | Form-to-CRM or customer-message classification, used pieces, setup | Inspect the actual included flows and piece versions. Use source-to-target operation discovery; no deterministic Activepieces parser is currently qualified. |

## Cross-template reusable children

SerpAPI, Tavily and Exa research/search calls are candidates for reusable HTTP
child workflows. Verify the endpoint, secret input contract, response shape,
rate-limit/error routing and pagination before reuse. A shipped HTTP primitive
makes a new extension optional for a simple call; it does not establish provider
compatibility. Keep each provider's distinct response schema rather than
pretending all search results are interchangeable.

## Gap report fields

For every selected source node record the source workflow URL/version, node
index, resource/operation, required auth, trigger, request/response cardinality,
selected target operation/child and its contract evidence. Classify the status
as native verified, MCP verified, existing child verified, simple HTTP verified,
setup needed, contract needed, parser/semantic adaptation needed, genuine capability
gap or availability unverified. Search ready MCP connectors before direct HTTP;
a catalog-listed connector and an authorized project action are distinct evidence.

Use the user's current deployment inventory as context for live discovery.
A shipped WhatsApp integration, for example, should be checked for the required
operation before being called missing. Partial Notion coverage and an absent
Typeform trigger are operation-specific questions, not permanent product
claims. Model/document integrations in progress need final live contracts;
source template names and planned releases do not establish readiness.

Count missing **operations** only after a live search confirms the absence of a
usable native/HTTP/child solution. Track source-parser limitations separately:
Code adaptation, n8n item linking, Make aggregators and Activepieces loops do
not automatically mean a new provider extension is needed.

Airtable example: an available Airtable MCP connector is the preferred route for
creating records. Inspect its actual create-record tool schema, field mapping,
per-item behavior and permitted connection. Missing target IDs or credentials are
setup/contract gaps; an unsupported n8n Airtable parser is adapter work. Neither
justifies replacing an available MCP action with HTTP or proposing an extension.
The [official MCP Tools reference](https://airtable.com/developers/agents/mcp/tools#record)
documents `create_records_for_table`; discover the installed version's schema
before claiming its argument mapping verified; explicitly deferred setup can use
the [full unbound draft recipe](full-yaml-assembly.md).


## Qualify candidates for a catalog campaign

When a batch is requested, freeze the candidate list before conversion. Record
catalog filters/sort, selection date, template IDs and the evidence for popularity;
if no usage/rank metric is exposed, say so instead of calling search results a
Top 10. For a later random check, sample without replacement from that same
relevant B2B pool and record the seed and exclusions. Do not select only easy
HTTP templates or silently replace failed candidates.

For each candidate retain the full operation count/mapping, draft status, fresh
inventory evidence, setup/contract blockers, genuine extension candidates and
separate structural/synthetic/live results. Report complete workflow coverage
separately from individual translated nodes. Catalog publication is a later
readiness decision, not a consequence of a high conversion percentage.
