import { PlatformElement } from '../platform-base.js';

const ECHARTS_SRC = '/static/vendor/echarts-6.0.0.min.js';
let echartsLoader = null;

function loadECharts() {
  if (globalThis.echarts) {
    return Promise.resolve(globalThis.echarts);
  }
  if (echartsLoader) {
    return echartsLoader;
  }
  echartsLoader = new Promise((resolve, reject) => {
    const existing = document.querySelector(`script[data-platform-echarts="true"]`);
    if (existing) {
      existing.addEventListener('load', () => resolve(globalThis.echarts), { once: true });
      existing.addEventListener('error', reject, { once: true });
      return;
    }
    const script = document.createElement('script');
    script.src = ECHARTS_SRC;
    script.async = true;
    script.dataset.platformEcharts = 'true';
    script.addEventListener('load', () => resolve(globalThis.echarts), { once: true });
    script.addEventListener('error', () => reject(new Error('Failed to load Apache ECharts.')), {
      once: true,
    });
    document.head.appendChild(script);
  });
  return echartsLoader;
}

class PlatformChart extends PlatformElement {
  static get styles() {
    return `
      :host {
        display: block;
      }
      .shell {
        border: 1px solid var(--border);
        border-radius: var(--r-lg);
        background: var(--surface);
        padding: 20px;
        display: grid;
        gap: 16px;
      }
      .header {
        display: grid;
        gap: 4px;
      }
      .title {
        margin: 0;
        font-size: var(--bk-font-lg);
        font-weight: 600;
        color: var(--text-1);
      }
      .subtitle {
        margin: 0;
        color: var(--text-3);
        font-size: var(--bk-font-sm);
      }
      .meta {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        color: var(--text-3);
        font-size: 12px;
      }
      .meta-chip {
        display: inline-flex;
        align-items: center;
        border-radius: 999px;
        background: var(--surface-2);
        padding: 4px 10px;
      }
      .chart-shell {
        min-height: 220px;
        position: relative;
      }
      .chart {
        width: 100%;
        min-height: 220px;
      }
      .empty {
        min-height: 220px;
        border: 1px dashed var(--border);
        border-radius: var(--r-md);
        background: var(--surface-2);
        display: grid;
        place-items: center;
        padding: 24px;
        text-align: center;
      }
      .empty.is-hidden {
        display: none;
      }
      .empty-title {
        display: block;
        color: var(--text-1);
        font-weight: 600;
        margin-bottom: 6px;
      }
      .empty-copy {
        color: var(--text-3);
        margin: 0;
      }
    `;
  }

  constructor() {
    super();
    this._chart = null;
    this._resizeObserver = null;
  }

  disconnectedCallback() {
    if (this._resizeObserver) {
      this._resizeObserver.disconnect();
      this._resizeObserver = null;
    }
    if (this._chart) {
      this._chart.dispose();
      this._chart = null;
    }
  }

  _hasRenderableData(config) {
    const dataset = config?.dataset || {};
    const series = Array.isArray(dataset.series) ? dataset.series : [];
    return series.some((entry) => {
      if (!entry || !Array.isArray(entry.data)) {
        return false;
      }
      return entry.data.some((value) => {
        if (value && typeof value === 'object') {
          return Number(value.value || 0) > 0;
        }
        return Number(value || 0) > 0;
      });
    });
  }

  _metaEntries(config) {
    const meta = config?.meta || {};
    return Object.entries(meta)
      .filter(([, value]) => value !== null && value !== undefined && value !== '')
      .slice(0, 4);
  }

  _buildSeries(config) {
    const dataset = config?.dataset || {};
    const series = Array.isArray(dataset.series) ? dataset.series : [];
    const chartType = String(config.chart_type || 'line').trim().toLowerCase();
    if (chartType === 'donut') {
      const first = series[0] || { data: [] };
      return [
        {
          type: 'pie',
          radius: ['45%', '72%'],
          center: ['50%', '50%'],
          avoidLabelOverlap: true,
          label: {
            formatter: '{b}: {d}%',
          },
          data: Array.isArray(first.data) ? first.data : [],
        },
      ];
    }
    return series.map((entry) => ({
      name: entry.name || '',
      type: chartType === 'line' ? 'line' : 'bar',
      stack: chartType === 'stacked_bar' ? 'total' : undefined,
      smooth: chartType === 'line',
      emphasis: { focus: 'series' },
      data: Array.isArray(entry.data) ? entry.data : [],
    }));
  }

  _buildOption(config) {
    const dataset = config?.dataset || {};
    const x = Array.isArray(dataset.x) ? dataset.x : [];
    const chartType = String(config.chart_type || 'line').trim().toLowerCase();
    const series = this._buildSeries(config);
    const color = ['#2563eb', '#0d9488', '#f59e0b', '#dc2626', '#7c3aed', '#0891b2'];

    if (chartType === 'donut') {
      return {
        color,
        tooltip: { trigger: 'item' },
        legend: {
          bottom: 0,
          left: 'center',
        },
        series,
      };
    }

    return {
      color,
      tooltip: { trigger: 'axis' },
      legend: {
        top: 0,
      },
      grid: {
        left: 16,
        right: 16,
        top: 44,
        bottom: 24,
        containLabel: true,
      },
      xAxis: {
        type: 'category',
        data: x,
        axisTick: { alignWithLabel: true },
      },
      yAxis: {
        type: 'value',
      },
      series,
    };
  }

  _ensureResize(container) {
    if (this._resizeObserver || !container || !('ResizeObserver' in globalThis)) {
      return;
    }
    const ResizeObserverCtor = globalThis.ResizeObserver;
    this._resizeObserver = new ResizeObserverCtor(() => {
      if (this._chart) {
        this._chart.resize();
      }
    });
    this._resizeObserver.observe(container);
  }

  _render() {
    void this._renderAsync();
  }

  async _renderAsync() {
    const config = this.config || {};
    const title = config.title || this.getAttribute('title') || '';
    const subtitle = config.subtitle || this.getAttribute('subtitle') || '';
    const emptyState = config.empty_state || {};
    const hasData = this._hasRenderableData(config);
    const height = Math.max(220, Number(config.height || this.getAttribute('height') || 280));
    const metaEntries = this._metaEntries(config);

    if (this._chart) {
      this._chart.dispose();
      this._chart = null;
    }

    this._shadow.innerHTML = `
      <section class="shell">
        <div class="header">
          ${title ? `<h3 class="title">${this.esc(title)}</h3>` : ''}
          ${subtitle ? `<p class="subtitle">${this.esc(subtitle)}</p>` : ''}
          ${
            metaEntries.length
              ? `<div class="meta">${metaEntries
                  .map(
                    ([key, value]) =>
                      `<span class="meta-chip">${this.esc(key)}: ${this.esc(String(value))}</span>`
                  )
                  .join('')}</div>`
              : ''
          }
        </div>
        <div class="chart-shell">
          <div class="chart" style="height:${height}px"></div>
          <div class="empty ${hasData ? 'is-hidden' : ''}">
            <div>
              <span class="empty-title">${this.esc(
                emptyState.title || 'No chart data available'
              )}</span>
              <p class="empty-copy">${this.esc(
                emptyState.message || 'This report returned summary data without a chart.'
              )}</p>
            </div>
          </div>
        </div>
      </section>
    `;

    if (!hasData) {
      return;
    }

    const container = this._shadow.querySelector('.chart');
    if (!container) {
      return;
    }

    try {
      const echarts = await loadECharts();
      if (!this.isConnected || !container?.isConnected || !echarts) {
        return;
      }
      this._chart = echarts.init(container);
      this._chart.setOption(this._buildOption(config), true);
      this._ensureResize(container);
    }
    catch {
      const empty = this._shadow.querySelector('.empty');
      if (empty) {
        empty.classList.remove('is-hidden');
      }
    }
  }
}

customElements.define('platform-chart', PlatformChart);
export { PlatformChart };
