'use strict';

const DATA_URL      = './data/portfolio.json';
const AGENT_LOG_URL = './data/agent_log.json';
const REFRESH_MS = 5 * 60 * 1000;
const FETCH_TIMEOUT_MS = 15_000;

// ── Helpers ──────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const fmt$ = v => '$' + Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtK = v => '$' + (Math.abs(v) / 1000).toFixed(1) + 'k';
const fmtPct = (v, sign = true) => (sign && v > 0 ? '+' : '') + v.toFixed(2) + '%';
const fmtDate = s => {
  const d = new Date(s);
  // isNaN check protects against non-date strings being passed through unsanitized
  return isNaN(d) ? sanitize(String(s)) : d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: '2-digit' });
};

// Escapes untrusted strings before inserting via innerHTML
function sanitize(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#x27;');
}

function colorClass(v) { return v > 0 ? 'profit-cell' : v < 0 ? 'loss-cell' : 'muted-cell'; }
function signedHtml(value, formatted) {
  const cls = colorClass(value);
  // formatted is produced by fmt*/fmtPct which only emit digits, $, +, -, %, . — no sanitization needed
  return `<span class="${cls}">${formatted}</span>`;
}

// ── App ──────────────────────────────────────────────────────
class TradeQuestApp {
  constructor() {
    this.data = null;
    this.agentLog = null;
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
    const ts = Date.now();
    const fetchWithTimeout = url => {
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
      return fetch(`${url}?_=${ts}`, { signal: ctrl.signal }).finally(() => clearTimeout(t));
    };

    try {
      const [portfolioRes, agentRes] = await Promise.all([
        fetchWithTimeout(DATA_URL),
        fetchWithTimeout(AGENT_LOG_URL),
      ]);
      if (!portfolioRes.ok) throw new Error(`Portfolio HTTP ${portfolioRes.status}`);
      this.data = await portfolioRes.json();
      // Agent log is optional — degrade gracefully if missing
      this.agentLog = agentRes.ok ? await agentRes.json() : { runs: [] };
      this.render();
    } catch (err) {
      this.showError(err.name === 'AbortError' ? 'Request timed out' : err.message);
    }
  }

  render() {
    this.renderHeader();
    this.renderStats();
    this.renderEquityChart();
    this.renderHoldings();
    this.renderTrades();
    this.renderStrategy();
    this.renderAgentLog();
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
      const sym    = sanitize(h.symbol);
      const sector = sanitize(h.sector);
      const rank   = Number.isFinite(h.momentum_rank) ? h.momentum_rank : '—';
      return `
        <tr>
          <td>
            <span class="sym">${sym}</span>
            <span class="sector-tag">${sector}</span>
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
          <td><span class="rank-num ${rankCls}">${rank}</span></td>
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
      // action is always BUY/SELL from our own bot — sanitize defensively
      const action = sanitize(t.action).toUpperCase();
      const actionCls = action === 'BUY' ? 'buy' : action === 'SELL' ? 'sell' : 'buy';
      const sym    = sanitize(t.symbol);
      const reason = sanitize(t.reason);
      const shares = Number.isFinite(t.shares) ? t.shares : '—';
      return `
        <tr>
          <td class="muted-cell">${fmtDate(t.date)}</td>
          <td><span class="action-badge action-${actionCls}">${action}</span></td>
          <td><span class="sym">${sym}</span></td>
          <td class="muted-cell">${shares}</td>
          <td class="muted-cell">${fmt$(t.price)}</td>
          <td>${fmt$(t.value)}</td>
          <td class="reason-cell" title="${reason}">${reason}</td>
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
      // statusCls is derived from a computed ratio — not from JSON text
      const statusCls = ratio === 1 ? 'pass' : ratio >= 0.8 ? 'warn' : 'fail';
      const icon = ratio === 1 ? '✓' : ratio >= 0.8 ? '!' : '✗';
      const label = sanitize(f.label);
      const desc  = sanitize(f.description);
      const pNum  = Number.isFinite(f.passing) ? f.passing : 0;
      const tNum  = Number.isFinite(f.total)   ? f.total   : 0;
      return `
        <div class="filter-row">
          <div class="filter-top">
            <div class="filter-label">
              <div class="filter-icon ${statusCls}">${icon}</div>
              ${label}
            </div>
            <span class="filter-count">${pNum}/${tNum}</span>
          </div>
          <div class="filter-desc">${desc}</div>
          <div class="filter-bar-track">
            <div class="filter-bar-fill ${statusCls}"
              style="width:${(ratio * 100).toFixed(0)}%"></div>
          </div>
        </div>`;
    }).join('');

    const ri = meta.regime_indicators;
    // Numeric values coerced with toFixed — no sanitization needed
    // Text fields (description, ma200_trend) are sanitized
    const regimeDesc  = sanitize(ri.description);
    const trendLabel  = sanitize(ri.ma200_trend);
    $('regimeDetail').innerHTML = `
      <div class="regime-grid">
        <div class="regime-stat">
          <span class="regime-stat-label">VIX</span>
          <span class="regime-stat-value">${Number(ri.vix).toFixed(1)}</span>
        </div>
        <div class="regime-stat">
          <span class="regime-stat-label">Breadth</span>
          <span class="regime-stat-value">${(Number(ri.breadth_pct) * 100).toFixed(0)}% above 200-MA</span>
        </div>
        <div class="regime-stat">
          <span class="regime-stat-label">Trend</span>
          <span class="regime-stat-value" style="text-transform:capitalize">${trendLabel}</span>
        </div>
        <div class="regime-stat">
          <span class="regime-stat-label">Equity exposure</span>
          <span class="regime-stat-value">${(Number(meta.equity_exposure) * 100).toFixed(0)}%</span>
        </div>
      </div>
      <div class="regime-desc">${regimeDesc}</div>`;
  }

  // ── Agent Log ─────────────────────────────────────────────
  renderAgentLog() {
    const log = this.agentLog || { runs: [] };
    const runs = (log.runs || []).slice().reverse(); // newest first

    $('agentRunCount').textContent = runs.length;

    if (log.last_run) {
      const d = new Date(log.last_run);
      const typeLabel = { day_start: 'Day Start', day_end: 'Day End', monthly: 'Monthly' };
      $('agentLastRun').textContent =
        `Last: ${typeLabel[log.last_type] || log.last_type} · ${fmtDate(log.last_run)}`;
    } else {
      $('agentLastRun').textContent = 'No runs yet';
    }

    if (!runs.length) return;

    const ACTION_COLORS = { SELL: 'SELL', BUY: 'BUY', HOLD: 'HOLD', WATCH: 'WATCH' };

    const html = runs.map(run => {
      const typeLabel = { day_start: 'Day Start', day_end: 'Day End', monthly: 'Monthly' };
      const runType   = sanitize(run.run_type || '');
      const regime    = sanitize(run.regime   || 'unknown');
      const conf      = Number.isFinite(run.regime_confidence)
        ? `${(run.regime_confidence * 100).toFixed(0)}% conf` : '';
      const summary   = sanitize(run.summary  || '');
      const ts        = fmtDate(run.timestamp || '');

      // Flags
      const flags = (run.flags || []).slice(0, 6).map(f =>
        `<span class="flag-chip">${sanitize(f.symbol || '')}: ${sanitize(f.flag || '')}</span>`
      ).join('');

      // Decisions
      const decisions = (run.decisions || []).slice(0, 8).map(d => {
        const action = sanitize(String(d.action || '').toUpperCase());
        const cls    = ACTION_COLORS[action] || 'HOLD';
        const sym    = sanitize(d.symbol || '');
        const reason = sanitize(d.reason || '');
        return `<span class="decision-chip ${cls}" title="${reason}">${action} ${sym}</span>`;
      }).join('');

      // Cash action
      const cashHtml = run.cash_action
        ? `<span class="decision-chip ${sanitize(run.cash_action)}" style="font-size:0.65rem">
             CASH: ${sanitize(run.cash_action)}</span>` : '';

      return `
        <div class="agent-run">
          <div class="agent-run-header">
            <span class="run-type-badge ${runType}">${sanitize(typeLabel[run.run_type] || runType)}</span>
            <span class="run-regime ${regime}">${regime}${conf ? ` · ${conf}` : ''}</span>
            <span class="run-time">${ts}</span>
          </div>
          ${summary ? `<div class="run-summary">${summary}</div>` : ''}
          ${flags    ? `<div class="run-flags">${flags}</div>`    : ''}
          ${(decisions || cashHtml) ? `<div class="run-decisions">${decisions}${cashHtml}</div>` : ''}
        </div>`;
    }).join('');

    $('agentRunList').innerHTML = html;
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
      <p style="color:var(--muted);font-size:0.72rem;margin-top:4px">${sanitize(msg)}</p>
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
