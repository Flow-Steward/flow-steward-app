/**
 * Platform Web Component Kit — Central Registry
 *
 * Imports all platform-* components and registers them as custom elements.
 * Include via: <script type="module" src="/static/platform-kit/index.js"></script>
 */

// Layout
import { PlatformPage } from './components/platform-page.js';
import { PlatformSection } from './components/platform-section.js';
import { PlatformColumns } from './components/platform-columns.js';
import { PlatformStack } from './components/platform-stack.js';
import { PlatformActionBar } from './components/platform-action-bar.js';

// Data Display
import { PlatformStatsRow } from './components/platform-stats-row.js';
import { PlatformHealthPanel } from './components/platform-health-panel.js';
import { PlatformLogList } from './components/platform-log-list.js';
import { PlatformKvCard } from './components/platform-kv-card.js';
import { PlatformMarkdown } from './components/platform-markdown.js';
import { PlatformAlert } from './components/platform-alert.js';
import { PlatformBadge } from './components/platform-badge.js';
import { PlatformEmptyState } from './components/platform-empty-state.js';
import { PlatformStatusSummary } from './components/platform-status-summary.js';
import { PlatformExecutionWaterfall } from './components/platform-execution-waterfall.js';
import { PlatformReviewWorkspace } from './components/platform-review-workspace.js';
import { PlatformEvidenceCard } from './components/platform-evidence-card.js';
import { PlatformChart } from './components/platform-chart.js';
import { PlatformCostBreakdown } from './components/platform-cost-breakdown.js';

// Data Entry
import { PlatformForm } from './components/platform-form.js';
import { PlatformFieldset } from './components/platform-fieldset.js';
import { PlatformRepeater } from './components/platform-repeater.js';
import { PlatformKeyValue } from './components/platform-key-value.js';
import { PlatformJsonEditor } from './components/platform-json-editor.js';
import { PlatformSecretField } from './components/platform-secret-field.js';

// Interaction
import { PlatformGrid } from './components/platform-grid.js';
import { PlatformDrawer } from './components/platform-drawer.js';
import { PlatformConfirm } from './components/platform-confirm.js';
import { PlatformFilterBar } from './components/platform-filter-bar.js';

/* ── Element registry ── */
const FS_ELEMENTS = {
  'platform-page':     PlatformPage,
  'platform-section':      PlatformSection,
  'platform-columns':      PlatformColumns,
  'platform-stack':        PlatformStack,
  'platform-action-bar':   PlatformActionBar,
  'platform-stats-row':    PlatformStatsRow,
  'platform-health-panel': PlatformHealthPanel,
  'platform-log-list':     PlatformLogList,
  'platform-kv-card':      PlatformKvCard,
  'platform-markdown':     PlatformMarkdown,
  'platform-alert':     PlatformAlert,
  'platform-badge':    PlatformBadge,
  'platform-empty-state':  PlatformEmptyState,
  'platform-status-summary': PlatformStatusSummary,
  'platform-execution-waterfall': PlatformExecutionWaterfall,
  'platform-review-workspace': PlatformReviewWorkspace,
  'platform-evidence-card': PlatformEvidenceCard,
  'platform-chart': PlatformChart,
  'platform-cost-breakdown': PlatformCostBreakdown,
  'platform-form':         PlatformForm,
  'platform-fieldset':     PlatformFieldset,
  'platform-repeater':     PlatformRepeater,
  'platform-key-value':    PlatformKeyValue,
  'platform-json-editor':  PlatformJsonEditor,
  'platform-secret-field': PlatformSecretField,
  'platform-grid':         PlatformGrid,
  'platform-drawer':    PlatformDrawer,
  'platform-confirm':   PlatformConfirm,
  'platform-filter-bar':   PlatformFilterBar,
};

/** Map from v1 manifest component type to custom element tag name */
const MANIFEST_TYPE_MAP = {
  section:        'platform-section',
  columns:        'platform-columns',
  stack:          'platform-stack',
  action_bar:     'platform-action-bar',
  form:           'platform-form',
  fieldset:       'platform-fieldset',
  repeater:       'platform-repeater',
  key_value:      'platform-key-value',
  json_editor:    'platform-json-editor',
  secret_field:   'platform-secret-field',
  grid:           'platform-grid',
  stats_row:      'platform-stats-row',
  key_value_card: 'platform-kv-card',
  markdown:       'platform-markdown',
  health_panel:   'platform-health-panel',
  log_list:       'platform-log-list',
  empty_state:    'platform-empty-state',
  alert:          'platform-alert',
  status_summary: 'platform-status-summary',
  execution_waterfall: 'platform-execution-waterfall',
  review_workspace: 'platform-review-workspace',
  evidence_card: 'platform-evidence-card',
  chart: 'platform-chart',
  cost_breakdown: 'platform-cost-breakdown',
  confirm_action: 'platform-confirm',
  drawer_detail:  'platform-drawer',
  status_badge:   'platform-badge',
  filter_bar:     'platform-filter-bar',
};

/* ── Register all elements ── */
for (const [tag, cls] of Object.entries(FS_ELEMENTS)) {
  if (!customElements.get(tag)) {
    customElements.define(tag, cls);
  }
}

/* ── Exports ── */
export { PlatformElement } from './platform-base.js';
export {
  FS_ELEMENTS,
  MANIFEST_TYPE_MAP,
};
