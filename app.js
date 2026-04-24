'use strict';

const DATA_URL      = './data/portfolio.json';
const AGENT_LOG_URL = './data/agent_log.json';
const REFRESH_MS = 5 * 60 * 1000;
const FETCH_TIMEOUT_MS = 15_000;

// ── Helpers ──────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const fmt$ = v => '$' + Math.abs(+v || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtK = v => '$' + (Math.abs(+v || 0) / 1000).toFixed(1) + 'k';
const fmtPct = (v, sign = true) => { const n = Number.isFinite(+v) ? +v : 0; return (sign && n > 0 ? '+' : '') + n.toFixed(2) + '%'; };
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
    this.setupTabs();
    await this.load();
    setInterval(() => this.load(), REFRESH_MS);
  }

  setupTabs() {
    document.querySelectorAll('.tab').forEach(btn => {
      btn.addEventListener('click', () => {
        const target = btn.dataset.tab;
        document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
        btn.classList.add('active');
        const panel = document.getElementById(`panel-${target}`);
        if (panel) panel.classList.add('active');
      });
    });
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
    try {
      this.renderHeader();
      this.renderStats();
      this.renderEquityChart();
      this.renderHoldings();
      this.renderTrades();
      this.renderStrategy();
    } catch (err) {
      // Portfolio render failed — show error instead of eternal spinner
      console.error('Render error:', err);
      this.showError(err.message || 'Failed to render portfolio data');
      return;
    }
    // Agent log is non-critical — errors here don't block the portfolio tab
    try { this.renderAgentLog(); } catch (e) { console.warn('Agent log render:', e); }

    $('loadingState').hidden = true;
    $('errorState').hidden   = true;
    $('mainContent').hidden  = false;
    $('tabBar').hidden       = false;
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
    const unrealLabel = s.total_trades === 0
      ? '<span class="muted-cell">Awaiting first rebalance</span>'
      : `<span class="profit-cell">+${fmt$(s.unrealized_pnl)} unrealized</span>`;
    $('pnlBreakdown').innerHTML = unrealLabel;

    const winRateNum = Number.isFinite(s.win_rate) ? s.win_rate : 0;
    $('winRate').textContent = s.total_trades === 0 ? '—' : (winRateNum * 100).toFixed(1) + '%';
    $('winLossCount').textContent = s.total_trades === 0
      ? 'No trades yet'
      : `${s.winning_trades}W / ${s.losing_trades}L of ${s.total_trades}`;

    const sharpe = Number.isFinite(s.sharpe_ratio) ? s.sharpe_ratio : 0;
    $('sharpeRatio').textContent = s.total_trades === 0 ? '—' : sharpe.toFixed(2);

    const ddEl = $('maxDrawdown');
    ddEl.textContent = s.total_trades === 0 ? '—' : fmtPct(s.max_drawdown_pct, false);
    ddEl.className = 'stat-value loss';
    $('cashPct').textContent = `Cash: ${fmt$(s.cash)} (${(Number.isFinite(s.cash_pct) ? s.cash_pct : 0).toFixed(1)}%)`;
  }

  // ── Equity Chart ──────────────────────────────────────────
  renderEquityChart() {
    const { equity_curve, summary } = this.data;
    const ctx = $('equityChart').getContext('2d');

    const returnPct = this.data.meta.initial_capital > 0
      ? ((summary.portfolio_value - this.data.meta.initial_capital) / this.data.meta.initial_capital * 100)
      : 0;
    const chartReturn = $('equityReturn');

    if (!equity_curve || equity_curve.length < 2) {
      chartReturn.textContent = 'Awaiting data';
      chartReturn.className = 'chart-return card-meta muted-cell';
      if (this.chart) { this.chart.destroy(); this.chart = null; }
      return;
    }

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
    $('holdingsCount').textContent = holdings.length || 0;

    if (!holdings.length) {
      $('holdingsTbody').innerHTML =
        `<tr><td colspan="5" class="empty-cell">No positions yet — first rebalance runs May 1</td></tr>`;
      return;
    }

    const maxMom = Math.max(...holdings.map(h => h.momentum_6m)) || 1;

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
    $('totalTradesCount').textContent = `${summary.total_trades || 0} total`;

    if (!trades || !trades.length) {
      $('tradesTbody').innerHTML =
        `<tr><td colspan="7" class="empty-cell">No trades recorded yet</td></tr>`;
      return;
    }

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
      const ratio = f.total > 0 ? f.passing / f.total : 0;
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

    $('agentRunCount').textContent = runs.length || '0';

    if (log.last_run) {
      const TYPE_LABEL = { day_start: 'Day Start', day_end: 'Day End', monthly: 'Monthly' };
      $('agentLastRun').textContent =
        `${TYPE_LABEL[log.last_type] || log.last_type || '—'} · ${fmtDate(log.last_run)}`;
    } else {
      $('agentLastRun').textContent = 'No runs yet';
    }

    const list = $('agentRunList');

    if (!runs.length) {
      list.innerHTML = `
        <div class="agent-empty-state">
          <div class="agent-empty-icon">&#x23F1;</div>
          <div class="agent-empty-title">Awaiting first agent run</div>
          <div class="agent-empty-body">
            The Claude AI agent runs automatically:
            <ul>
              <li><strong>9:00 AM ET</strong> Mon&ndash;Fri &mdash; pre-market flag check</li>
              <li><strong>4:30 PM ET</strong> Mon&ndash;Fri &mdash; post-close sell/hold decisions</li>
              <li><strong>4:30 PM ET</strong> 1st of month &mdash; full portfolio rebalance</li>
            </ul>
            Tap <strong>&#x25B6; Run workflow</strong> above to trigger immediately.
          </div>
        </div>`;
      return;
    }

    const TYPE_LABEL = { day_start: 'Day Start', day_end: 'Day End', monthly: 'Monthly' };

    const html = runs.map(run => {
      const rawType = run.type || run.run_type || '';
      const runType = sanitize(rawType);
      const regime  = sanitize(run.regime || 'unknown');
      const conf    = Number.isFinite(run.regime_confidence)
        ? ` · ${(run.regime_confidence * 100).toFixed(0)}%` : '';
      const headline = sanitize(run.summary    || '');
      const assess   = sanitize(run.assessment || '');
      const ts       = fmtDate(run.timestamp   || '');

      const flags = (run.flags || []).map(f => {
        const text = typeof f === 'string' ? f : `${f.symbol || ''}: ${f.flag || ''}`;
        return `<span class="flag-chip">${sanitize(text)}</span>`;
      }).join('');

      const decisions = (run.decisions || []).map(d => {
        const action = sanitize(String(d.action || '').toUpperCase());
        const sym    = sanitize(d.symbol || '');
        const reason = sanitize(d.reason || '');
        const cls    = ['SELL','BUY','HOLD','WATCH'].includes(action) ? action : 'HOLD';
        return `<span class="decision-chip ${cls}" title="${reason}">${action}${sym ? ' ' + sym : ''}</span>`;
      }).join('');

      const cashHtml = run.cash_action
        ? `<span class="decision-chip WATCH">CASH ${sanitize(run.cash_action).toUpperCase()}</span>`
        : '';

      const chipsRow = flags + decisions + cashHtml;

      return `
        <div class="agent-run type-${runType}">
          <div class="agent-run-header">
            <span class="run-type-badge ${runType}">${sanitize(TYPE_LABEL[rawType] || rawType)}</span>
            <span class="run-regime ${regime}">${regime}${conf}</span>
            <span class="run-time">${ts}</span>
          </div>
          ${headline ? `<p class="run-headline">${headline}</p>` : ''}
          ${assess   ? `<p class="run-assess">${assess}</p>`    : ''}
          ${chipsRow ? `<div class="run-chips-row">${chipsRow}</div>` : ''}
        </div>`;
    }).join('');

    list.innerHTML = html;
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
