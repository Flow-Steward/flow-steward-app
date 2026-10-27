/**
 * Platform Web Component Kit — App Shell Registry
 *
 * Loads only the components used across the standard app shell pages.
 * Pages that render arbitrary extension manifests should load the full
 * /static/platform-kit/index.js entrypoint instead.
 */

import { PlatformAlert } from './components/platform-alert.js';
import { PlatformBadge } from './components/platform-badge.js';
import { PlatformDrawer } from './components/platform-drawer.js';
import { PlatformEmptyState } from './components/platform-empty-state.js';
import { PlatformGrid } from './components/platform-grid.js';
import { PlatformHealthPanel } from './components/platform-health-panel.js';
import { PlatformKvCard } from './components/platform-kv-card.js';
import { PlatformLogList } from './components/platform-log-list.js';
import { PlatformStatsRow } from './components/platform-stats-row.js';

const FS_APP_ELEMENTS = {
  'platform-alert': PlatformAlert,
  'platform-badge': PlatformBadge,
  'platform-drawer': PlatformDrawer,
  'platform-empty-state': PlatformEmptyState,
  'platform-grid': PlatformGrid,
  'platform-health-panel': PlatformHealthPanel,
  'platform-kv-card': PlatformKvCard,
  'platform-log-list': PlatformLogList,
  'platform-stats-row': PlatformStatsRow,
};

for (const [tag, cls] of Object.entries(FS_APP_ELEMENTS)) {
  if (!customElements.get(tag)) {
    customElements.define(tag, cls);
  }
}

export { FS_APP_ELEMENTS };
