'use strict';

const DATA_URL = './data/portfolio.json';
const REFRESH_MS = 5 * 60 * 1000;

// ── Helpers ──────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const fmt$ = v => '$' + Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtK = v => '$' + (Math.abs(v) / 1000).toFixed(1) + 'k';
const fmtPct = (v, sign = true) => (sign && v > 0 ? '+' : '') + v.toFixed(2) + '%';
const fmtDate = s => {
  const d = new Date(s);
  return isNaN(d) ? s : d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: '2-digit' });
};

function colorClass(v) { return v > 0 ? 'profit-cell' : v < 0 ? 'loss-cell' : 'muted-cell'; }
function signedHtml(value, formatted) {
  const cls = colorClass(value);
  return `<span class="${cls}">${formatted}</span>`;
}

// ── App ──────────────────────────────────────────────────────
class TradeQuestApp {
  constructor() {
    this.data = null;
    this.chart = null;
  }

  async init() {
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('./sw.js').catch(() => {});
    }
    await this.load();
    setInterval(() => this.load(), REFRESH_MS);
  }

  async load() {
    try {
      const res = await fetch(`${DATA_URL}?_=${Date.now()}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      this.data = await res.json();
      this.render();
    } catch (err) {
      this.showError(err.message);
    }
  }

  render() {
    this.renderHeader();
    this.renderStats();
    this.renderEquityChart();
    this.renderHoldings();
    this.renderTrades();
    this.renderStrategy();
    $('loadingState').hidden = true;
    $('errorState').hidden = true;
    $('mainContent').hidden = false;
  }

  // ── Header ────────────────────────────────────────────────
  renderHeader() {
    const { meta, summary } = this.data;
    const updated = new Date(summary.last_updated);
    const timeStr = updated.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });
    const dateStr = updated.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    $('lastUpdated').textContent = `Updated ${dateStr} ${timeStr}`;

    const regime = meta.market_regime;
    const badge = $('regimeBadge');
    badge.className = `regime-badge ${regime}`;
    badge.querySelector('#regimeLabel').textContent =
      regime === 'bull' ? 'Bull Market' : regime === 'bear' ? 'Bear Market' : 'Sideways';

    $('nextRebalance').textContent = `Rebalance: ${fmtDate(meta.next_rebalance)}`;
  }

  // ── Stat Cards ────────────────────────────────────────────
  renderStats() {
    const s = this.data.summary;

    $('portfolioValue').textContent = fmtK(s.portfolio_value);
    $('portfolioValue').className = 'stat-value';
    $('portfolioPnlPct').innerHTML = signedHtml(s.total_pnl_pct, fmtPct(s.total_pnl_pct));

    const pnlEl = $('totalPnl');
    pnlEl.textContent = (s.total_pnl >= 0 ? '+' : '-') + fmt$(s.total_pnl);
    pnlEl.className = `stat-value ${s.total_pnl >= 0 ? 'profit' : 'loss'}`;
    $('pnlBreakdown').innerHTML =
      `<span class="profit-cell">+${fmt$(s.unrealized_pnl)} unrealized</span>`;

    $('winRate').textContent = (s.win_rate * 100).toFixed(1) + '%';
    $('winLossCount').textContent = `${s.winning_trades}W / ${s.losing_trades}L of ${s.total_trades}`;

    $('sharpeRatio').textContent = s.sharpe_ratio.toFixed(2);

    const ddEl = $('maxDrawdown');
    ddEl.textContent = fmtPct(s.max_drawdown_pct, false);
    ddEl.className = 'stat-value loss';
    $('cashPct').textContent = `Cash: ${fmt$(s.cash)} (${s.cash_pct.toFixed(1)}%)`;
  }

  // ── Equity Chart ──────────────────────────────────────────
  renderEquityChart() {
    const { equity_curve, summary } = this.data;
    const ctx = $('equityChart').getContext('2d');

    const returnPct = ((summary.portfolio_value - this.data.meta.initial_capital)
      / this.data.meta.initial_capital * 100);
    const chartReturn = $('equityReturn');
    chartReturn.textContent = fmtPct(returnPct) + ' since inception';
    chartReturn.className = `chart-return ${returnPct >= 0 ? 'profit-cell' : 'loss-cell'}`;

    if (this.chart) this.chart.destroy();

    const gradient = ctx.createLinearGradient(0, 0, 0, 220);
    gradient.addColorStop(0, 'rgba(212,175,55,0.18)');
    gradient.addColorStop(1, 'rgba(212,175,55,0)');

    this.chart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: equity_curve.map(p => p.date),
        datasets: [{
          data: equity_curve.map(p => p.value),
          borderColor: '#D4AF37',
          borderWidth: 1.8,
          backgroundColor: gradient,
          fill: true,
          tension: 0.4,
          pointRadius: 0,
          pointHoverRadius: 5,
          pointHoverBackgroundColor: '#D4AF37',
          pointHoverBorderColor: '#0B0B0B',
          pointHoverBorderWidth: 2,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: '#1A1A1A',
            borderColor: '#2A2A2A',
            borderWidth: 1,
            titleColor: '#808080',
            bodyColor: '#EAEAEA',
            padding: 10,
            callbacks: {
              label: ctx => `  ${fmt$(ctx.raw)}`,
              afterLabel: ctx => {
                const pct = ((ctx.raw - equity_curve[0].value) / equity_curve[0].value * 100);
                return `  ${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`;
              }
            }
          }
        },
        scales: {
          x: {
            grid: { color: 'rgba(255,255,255,0.03)', drawBorder: false },
            ticks: { color: '#555', maxTicksLimit: 7, font: { size: 10 } },
            border: { color: '#1E1E1E' }
          },
          y: {
            position: 'right',
            grid: { color: 'rgba(255,255,255,0.03)', drawBorder: false },
            ticks: {
              color: '#555',
              font: { size: 10 },
              callback: v => '$' + (v / 1000).toFixed(0) + 'k'
            },
            border: { color: '#1E1E1E' }
          }
        }
      }
    });
  }

  // ── Holdings ──────────────────────────────────────────────
  renderHoldings() {
    const { holdings } = this.data;
    $('holdingsCount').textContent = holdings.length;

    const maxMom = Math.max(...holdings.map(h => h.momentum_6m));

    const rows = holdings.map(h => {
      const momPct = Math.min(100, (h.momentum_6m / maxMom) * 100);
      const rankCls = h.momentum_rank <= 5 ? 'top5' : '';
      return `
        <tr>
          <td>
            <span class="sym">${h.symbol}</span>
            <span class="sector-tag">${h.sector}</span>
          </td>
          <td>${fmt$(h.market_value)}</td>
          <td class="${colorClass(h.pnl_pct)}">${fmtPct(h.pnl_pct)}</td>
          <td>
            <div class="mom-bar">
              <div class="mom-track">
                <div class="mom-fill" style="width:${momPct.toFixed(0)}%"></div>
              </div>
              <span class="muted-cell" style="font-size:0.7rem">
                ${fmtPct(h.momentum_6m * 100, false)}
              </span>
            </div>
          </td>
          <td><span class="rank-num ${rankCls}">${h.momentum_rank}</span></td>
        </tr>`;
    }).join('');

    $('holdingsTbody').innerHTML = rows;
  }

  // ── Trades ────────────────────────────────────────────────
  renderTrades() {
    const { trades, summary } = this.data;
    $('totalTradesCount').textContent = `${summary.total_trades} total`;

    const recent = trades.slice(0, 15);
    const rows = recent.map(t => {
      const pnlHtml = t.pnl !== null
        ? `<span class="${colorClass(t.pnl)}">${t.pnl >= 0 ? '+' : ''}${fmt$(t.pnl)}</span>`
        : `<span class="muted-cell">—</span>`;
      return `
        <tr>
          <td class="muted-cell">${fmtDate(t.date)}</td>
          <td><span class="action-badge action-${t.action.toLowerCase()}">${t.action}</span></td>
          <td><span class="sym">${t.symbol}</span></td>
          <td class="muted-cell">${t.shares}</td>
          <td class="muted-cell">${fmt$(t.price)}</td>
          <td>${fmt$(t.value)}</td>
          <td class="reason-cell" title="${t.reason}">${t.reason}</td>
        </tr>`;
    }).join('');

    $('tradesTbody').innerHTML = rows;
  }

  // ── Strategy ──────────────────────────────────────────────
  renderStrategy() {
    const { filter_status, meta } = this.data;

    const filters = $('strategyFilters');
    filters.innerHTML = Object.values(filter_status).map(f => {
      const ratio = f.passing / f.total;
      const statusCls = ratio === 1 ? 'pass' : ratio >= 0.8 ? 'warn' : 'fail';
      const icon = ratio === 1 ? '✓' : ratio >= 0.8 ? '!' : '✗';
      return `
        <div class="filter-row">
          <div class="filter-top">
            <div class="filter-label">
              <div class="filter-icon ${statusCls}">${icon}</div>
              ${f.label}
            </div>
            <span class="filter-count">${f.passing}/${f.total}</span>
          </div>
          <div class="filter-desc">${f.description}</div>
          <div class="filter-bar-track">
            <div class="filter-bar-fill ${statusCls}"
              style="width:${(ratio * 100).toFixed(0)}%"></div>
          </div>
        </div>`;
    }).join('');

    const ri = meta.regime_indicators;
    $('regimeDetail').innerHTML = `
      <div class="regime-grid">
        <div class="regime-stat">
          <span class="regime-stat-label">VIX</span>
          <span class="regime-stat-value">${ri.vix}</span>
        </div>
        <div class="regime-stat">
          <span class="regime-stat-label">Breadth</span>
          <span class="regime-stat-value">${(meta.regime_indicators.breadth_pct * 100).toFixed(0)}% above 200-MA</span>
        </div>
        <div class="regime-stat">
          <span class="regime-stat-label">Trend</span>
          <span class="regime-stat-value" style="text-transform:capitalize">${ri.ma200_trend}</span>
        </div>
        <div class="regime-stat">
          <span class="regime-stat-label">Equity exposure</span>
          <span class="regime-stat-value">${(meta.equity_exposure * 100).toFixed(0)}%</span>
        </div>
      </div>
      <div class="regime-desc">${ri.description}</div>`;
  }

  // ── Error ─────────────────────────────────────────────────
  showError(msg) {
    $('loadingState').hidden = true;
    $('mainContent').hidden = true;
    const el = $('errorState');
    el.hidden = false;
    el.innerHTML = `
      <div class="error-icon">⚠</div>
      <p>Failed to load portfolio data</p>
      <p style="color:var(--muted);font-size:0.72rem;margin-top:4px">${msg}</p>
      <button onclick="app.load()" style="
        margin-top:14px;padding:7px 18px;
        background:var(--surface);border:1px solid var(--border);
        color:var(--text);border-radius:var(--radius);
        cursor:pointer;font-size:0.78rem;
      ">Retry</button>`;
  }
}

const app = new TradeQuestApp();
app.init();
