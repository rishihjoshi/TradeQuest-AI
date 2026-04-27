'use strict';

// ── Constants ─────────────────────────────────────────────────
const DATA_URL           = './data/portfolio.json';
const AGENT_LOG_URL      = './data/agent_log.json';
const NEWS_URL           = './data/news.json';
const ENRICHMENT_URL     = './data/enrichment.json';
const MARKET_REFRESH_MS  = 30_000;        // 30 s during market hours
const DEFAULT_REFRESH_MS = 5 * 60_000;    // 5 min otherwise
const FETCH_TIMEOUT_MS   = 15_000;
const NEWS_TTL_MS        = 5 * 60_000;    // 5-min memory cache for news tab
const NEWS_PAGE_SIZE     = 25;
const LS_AGENT_KEY       = 'tq_agent_runs';
const MAX_AGENT_HISTORY  = 10;

// ── Helpers ───────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const fmt$ = v => '$' + Math.abs(+v || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtK = v => '$' + (Math.abs(+v || 0) / 1000).toFixed(1) + 'k';
const fmtPct = (v, sign = true) => {
  const n = Number.isFinite(+v) ? +v : 0;
  return (sign && n > 0 ? '+' : '') + n.toFixed(2) + '%';
};
const fmtDate = s => {
  const d = new Date(s);
  return isNaN(d) ? sanitize(String(s))
    : d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: '2-digit' });
};

// Escapes untrusted strings before inserting via innerHTML
function sanitize(str) {
  return String(str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#x27;');
}

function colorClass(v) { return v > 0 ? 'profit-cell' : v < 0 ? 'loss-cell' : 'muted-cell'; }
function signedHtml(value, formatted) {
  return `<span class="${colorClass(value)}">${formatted}</span>`;
}

// ── Market hours detection (America/New_York) ─────────────────
// Used only for adaptive refresh interval — no API credentials involved
function isMarketHours() {
  try {
    const et   = new Date(new Date().toLocaleString('en-US', { timeZone: 'America/New_York' }));
    const day  = et.getDay(); // 0=Sun 6=Sat
    if (day === 0 || day === 6) return false;
    const mins = et.getHours() * 60 + et.getMinutes();
    return mins >= 570 && mins <= 960; // 9:30 – 16:00 ET
  } catch { return false; }
}

// ── Agent history — non-sensitive metadata only ───────────────
// Stores only: id, timestamp, type, regime, confidence, flagCount,
// decisionCount, summary (≤200 chars). No API keys, no credentials.
const AgentHistory = {
  load() {
    try { return JSON.parse(localStorage.getItem(LS_AGENT_KEY) || '[]'); }
    catch { return []; }
  },
  save(run) {
    try {
      const hist  = this.load();
      const entry = {
        id:            String(run.id || '').slice(0, 40),
        timestamp:     String(run.timestamp || ''),
        type:          String(run.type || '').slice(0, 20),
        regime:        String(run.regime || 'unknown').slice(0, 20),
        confidence:    Number.isFinite(run.regime_confidence) ? run.regime_confidence : 0,
        flagCount:     Array.isArray(run.flags)     ? run.flags.length     : 0,
        decisionCount: Array.isArray(run.decisions) ? run.decisions.length : 0,
        // Truncated summary — not the full assessment, no sensitive data
        summary: String(run.summary || '').slice(0, 200),
      };
      const deduped = hist.filter(h => h.id !== entry.id);
      deduped.unshift(entry);
      localStorage.setItem(LS_AGENT_KEY, JSON.stringify(deduped.slice(0, MAX_AGENT_HISTORY)));
    } catch { /* localStorage unavailable (private mode / quota) */ }
  },
  clear() {
    try { localStorage.removeItem(LS_AGENT_KEY); } catch {}
  },
};

// ── Recover a parse-failed agent run ─────────────────────────
// When agent.py hits max_tokens the Python parser catches the
// truncated JSON and stores the raw text in run.assessment.
// This function tries to extract usable fields from that raw text.
function tryRecoverRun(run) {
  if (!String(run.summary || '').includes('parse failed')) return run;
  const raw = String(run.assessment || '');
  let inner = null;
  // Pass 1: direct JSON.parse
  try { inner = JSON.parse(raw); } catch {}
  // Pass 2: extract first {...} block via regex
  if (!inner) {
    try {
      const m = raw.match(/\{[\s\S]*\}/);
      if (m) inner = JSON.parse(m[0]);
    } catch {}
  }
  if (inner && (inner.assessment || inner.regime)) {
    return { ...run, ...inner, _recovered: true };
  }
  return { ...run, _parseFailed: true };
}

// ── Classify flag severity ────────────────────────────────────
function classifyFlag(text) {
  const t = text.toLowerCase();
  if (t.includes('sell') || t.includes('exceeds') || t.includes('below ma') || t.includes('triggered'))
    return 'critical';
  if (t.includes('approaching') || t.includes('boundary') || t.includes('watch') || t.includes('monitor'))
    return 'warning';
  return 'info';
}

// ── Canvas sparkline (no library) ────────────────────────────
// Generates a deterministic 8-point price path from momentum data.
// Deterministic seed means it won't change on re-render (good UX).
// Real per-symbol price history will replace this when the bot
// starts writing it to portfolio.json.
function drawSparkline(canvas, holding) {
  if (!canvas || !holding) return;
  const dpr = window.devicePixelRatio || 1;
  const w   = Math.max(canvas.offsetWidth,  72);
  const h   = Math.max(canvas.offsetHeight, 28);
  canvas.width  = w * dpr;
  canvas.height = h * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);

  // Build deterministic 8-point sparkline from 6M momentum
  const cur     = holding.current_price || holding.avg_cost || 100;
  const mom     = holding.momentum_6m   || 0;
  const sym     = holding.symbol || 'X';
  const seed    = sym.charCodeAt(0) * 137 + (sym.charCodeAt(sym.length - 1) || 65) * 31;
  const dailyMom = Math.pow(1 + mom, 1 / 126); // 6M → daily factor
  const n = 8;
  const pts = Array.from({ length: n }, (_, i) => {
    const noise = Math.sin(seed + i * 2.718) * cur * 0.007;
    return cur / Math.pow(dailyMom, n - 1 - i) + noise;
  });
  pts[n - 1] = cur; // always end exactly at current price

  const min = Math.min(...pts), max = Math.max(...pts), range = max - min || 1;
  const xs  = pts.map((_, i) => (i / (n - 1)) * w);
  const ys  = pts.map(v    => h - 2 - ((v - min) / range) * (h - 6));
  const up  = pts[n - 1] >= pts[0];
  const clr = up ? '#22FF88' : '#FF4D4D';

  // Gradient fill
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, up ? 'rgba(34,255,136,0.18)' : 'rgba(255,77,77,0.18)');
  grad.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.beginPath();
  xs.forEach((x, i) => (i === 0 ? ctx.moveTo(x, ys[i]) : ctx.lineTo(x, ys[i])));
  ctx.lineTo(xs[n - 1], h);
  ctx.lineTo(xs[0], h);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // Line
  ctx.beginPath();
  xs.forEach((x, i) => (i === 0 ? ctx.moveTo(x, ys[i]) : ctx.lineTo(x, ys[i])));
  ctx.strokeStyle = clr;
  ctx.lineWidth   = 1.5;
  ctx.stroke();
}

// ── SVG sector donut (no library) ────────────────────────────
const SECTOR_COLORS = {
  'Technology':       '#60A5FA',
  'Communication':    '#A78BFA',
  'Healthcare':       '#34D399',
  'Consumer Disc.':   '#F59E0B',
  'Consumer Staples': '#6EE7B7',
  'Industrials':      '#FCA5A5',
  'Financials':       '#93C5FD',
  'Energy':           '#FCD34D',
};
function buildSectorDonut(holdings) {
  if (!holdings?.length) {
    return `<svg width="52" height="52" viewBox="0 0 52 52">
      <circle cx="26" cy="26" r="18" fill="none" stroke="#222" stroke-width="8"/>
    </svg>`;
  }
  const totals = {};
  holdings.forEach(h => {
    totals[h.sector] = (totals[h.sector] || 0) + (h.market_value || 0);
  });
  const total = Object.values(totals).reduce((a, b) => a + b, 0) || 1;
  const R = 18, C = 2 * Math.PI * R;
  // Start at 12-o'clock: stroke-dashoffset = C/4 shifts start forward by 90°
  let offset = C / 4;
  const segs = Object.entries(totals).map(([s, v]) => {
    const dash  = (v / total) * C;
    const color = SECTOR_COLORS[s] || '#555';
    const el    = `<circle cx="26" cy="26" r="${R}" fill="none" stroke="${color}"
      stroke-width="8"
      stroke-dasharray="${dash.toFixed(2)} ${(C - dash).toFixed(2)}"
      stroke-dashoffset="${offset.toFixed(2)}"/>`;
    offset -= dash;
    return el;
  }).join('');
  return `<svg width="52" height="52" viewBox="0 0 52 52">${segs}</svg>`;
}

// ── Time-ago helper ───────────────────────────────────────────
// Returns "just now", "5m ago", "2h ago", "3d ago" from an ISO string.
function timeAgo(isoStr) {
  if (!isoStr) return '—';
  const diff = Math.floor((Date.now() - new Date(isoStr).getTime()) / 1000);
  if (diff < 60)        return 'just now';
  if (diff < 3600)      return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400)     return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

// ── App ───────────────────────────────────────────────────────
class TradeQuestApp {
  constructor() {
    this.data              = null;
    this.agentLog          = null;
    this.newsData          = null;
    this.newsFetchedAt     = 0;
    this.newsFilter        = 'ALL';   // 'ALL' | 'bull' | 'bear' | 'neutral'
    this.newsTickerFilter  = null;    // null = no ticker filter
    this.newsDisplayLimit  = NEWS_PAGE_SIZE;
    this.chart             = null;
    this.tradeFilter       = 'ALL';   // 'ALL' | 'BUY' | 'SELL'
    this.tradeSort         = 'date';  // 'date' | 'pnl'
    this.refreshTimer      = null;
    this.timeAgoTimer      = null;
  }

  async init() {
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('./sw.js').catch(() => {});
    }
    this.setupTabs();
    this.setupTradeControls();
    this.setupAgentHistoryUI();
    this.setupNewsControls();
    await this.load();
    this.scheduleRefresh();
    // Update time-ago strings every 60 s
    this.timeAgoTimer = setInterval(() => this.refreshTimeAgoLabels(), 60_000);
  }

  // ── Adaptive refresh (market-hours aware) ─────────────────
  // Security note: refresh only polls local static JSON files.
  // No Alpaca API calls from browser — credentials stay in GitHub Actions.
  scheduleRefresh() {
    clearTimeout(this.refreshTimer);
    const live = isMarketHours();
    this.updateLiveIndicator(live);
    const ms = live ? MARKET_REFRESH_MS : DEFAULT_REFRESH_MS;
    this.refreshTimer = setTimeout(async () => {
      await this.load();
      this.scheduleRefresh(); // re-check market hours each cycle
    }, ms);
  }

  updateLiveIndicator(isLive) {
    const dot   = $('liveIndicator');
    const label = $('liveLabel');
    if (dot) {
      dot.className = `live-dot${isLive ? ' active' : ''}`;
      dot.setAttribute('title', isLive
        ? 'Market open — refreshing every 30 s'
        : 'Market closed — refreshing every 5 min');
    }
    if (label) {
      label.textContent = isLive ? 'LIVE' : 'DELAYED';
      label.className   = `live-label${isLive ? ' active' : ''}`;
    }
  }

  setupTabs() {
    document.querySelectorAll('.tab').forEach(btn => {
      btn.addEventListener('click', () => {
        const target = btn.dataset.tab;
        document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
        btn.classList.add('active');
        const panel = $(`panel-${target}`);
        if (panel) panel.classList.add('active');
        // Lazy-load news on first focus, then respect TTL
        if (target === 'news') this.loadNewsIfStale();
      });
    });
  }

  setupTradeControls() {
    document.querySelectorAll('.trade-filter-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.trade-filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        this.tradeFilter = btn.dataset.filter;
        if (this.data) this.renderTrades();
      });
    });
    document.querySelectorAll('.trade-sort-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.trade-sort-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        this.tradeSort = btn.dataset.sort;
        if (this.data) this.renderTrades();
      });
    });
  }

  setupNewsControls() {
    // Sentiment filter buttons
    document.querySelectorAll('.news-filter-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.news-filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        this.newsFilter       = btn.dataset.sentiment;
        this.newsDisplayLimit = NEWS_PAGE_SIZE;
        this.renderNews();
      });
    });
    // Load More button
    const lmBtn = $('newsLoadMoreBtn');
    if (lmBtn) {
      lmBtn.addEventListener('click', () => {
        this.newsDisplayLimit += NEWS_PAGE_SIZE;
        this.renderNews();
      });
    }
  }

  setupAgentHistoryUI() {
    const btn = $('clearAgentHistory');
    if (btn) {
      btn.addEventListener('click', () => {
        if (confirm('Clear agent run history stored in this browser?\n(The agent_log.json on GitHub is not affected.)')) {
          AgentHistory.clear();
          this.renderAgentTimeline();
        }
      });
    }
  }

  async load() {
    const ts = Date.now();
    const fetchWithTimeout = url => {
      const ctrl = new AbortController();
      const t    = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
      // Log endpoint path only — never full URL with any params
      console.warn(`[TQ] GET ${url.split('/').pop()}`);
      return fetch(`${url}?_=${ts}`, { signal: ctrl.signal }).finally(() => clearTimeout(t));
    };
    try {
      const [portfolioRes, agentRes, enrichRes] = await Promise.all([
        fetchWithTimeout(DATA_URL),
        fetchWithTimeout(AGENT_LOG_URL),
        fetchWithTimeout(ENRICHMENT_URL),
      ]);
      if (!portfolioRes.ok) throw new Error(`Portfolio HTTP ${portfolioRes.status}`);
      this.data       = await portfolioRes.json();
      this.agentLog   = agentRes.ok  ? await agentRes.json()  : { runs: [] };
      this.enrichment = enrichRes.ok ? await enrichRes.json() : null;
      this.render();
    } catch (err) {
      this.showError(err.name === 'AbortError' ? 'Request timed out' : err.message);
    }
  }

  render() {
    try {
      this.renderHeader();
      this.renderStats();
      this.renderCatalysts();
      this.renderEquityChart();
      this.renderHoldings();
      this.renderTrades();
      this.renderStrategy();
    } catch (err) {
      console.error('Render error:', err);
      this.showError(err.message || 'Failed to render portfolio data');
      return;
    }
    try {
      this.renderAgentLog();
      this.renderAgentTimeline();
      this.renderMomentumHeatmap();
    } catch (e) { console.warn('Agent render:', e); }

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
    const badge  = $('regimeBadge');
    badge.className = `regime-badge ${regime}`;
    badge.querySelector('#regimeLabel').textContent =
      regime === 'bull' ? 'Bull Market' : regime === 'bear' ? 'Bear Market' : 'Sideways';

    $('nextRebalance').textContent = `Rebalance: ${fmtDate(meta.next_rebalance)}`;
  }

  // ── Stat Cards ────────────────────────────────────────────
  renderStats() {
    const s = this.data.summary;

    $('portfolioValue').textContent = fmtK(s.portfolio_value);
    $('portfolioValue').className   = 'stat-value';
    $('portfolioPnlPct').innerHTML  = signedHtml(s.total_pnl_pct, fmtPct(s.total_pnl_pct));

    const pnlEl = $('totalPnl');
    pnlEl.textContent = (s.total_pnl >= 0 ? '+' : '-') + fmt$(s.total_pnl);
    pnlEl.className   = `stat-value ${s.total_pnl >= 0 ? 'profit' : 'loss'}`;

    const unrealLabel = s.total_trades === 0
      ? '<span class="muted-cell">Awaiting first rebalance</span>'
      : `<span class="${colorClass(s.unrealized_pnl)}">${s.unrealized_pnl >= 0 ? '+' : ''}${fmt$(s.unrealized_pnl)} unrealized</span>`;
    $('pnlBreakdown').innerHTML = unrealLabel;

    const winRateNum = Number.isFinite(s.win_rate) ? s.win_rate : 0;
    $('winRate').textContent    = s.total_trades === 0 ? '—' : (winRateNum * 100).toFixed(1) + '%';
    $('winLossCount').textContent = s.total_trades === 0
      ? 'No trades yet'
      : `${s.winning_trades}W / ${s.losing_trades}L of ${s.total_trades}`;

    const sharpe = Number.isFinite(s.sharpe_ratio) ? s.sharpe_ratio : 0;
    $('sharpeRatio').textContent = s.total_trades === 0 ? '—' : sharpe.toFixed(2);

    const ddEl = $('maxDrawdown');
    ddEl.textContent = s.total_trades === 0 ? '—' : fmtPct(s.max_drawdown_pct, false);
    ddEl.className   = 'stat-value loss';

    $('cashPct').textContent =
      `Cash: ${fmt$(s.cash)} (${(Number.isFinite(s.cash_pct) ? s.cash_pct : 0).toFixed(1)}%)`;
  }

  // ── Upcoming Catalysts Banner ─────────────────────────────
  renderCatalysts() {
    const el = $('catalystsBanner');
    if (!el) return;

    const e = this.enrichment;
    if (!e) { el.hidden = true; return; }

    const earnings = (e.earnings_this_week || []);
    const macro    = (e.macro_events_14d   || []);
    const breadth  = e.market_breadth;

    if (!earnings.length && !macro.length && !breadth) { el.hidden = true; return; }

    const chips = [];

    earnings.forEach(ev => {
      const label = sanitize(`${ev.symbol} earnings ${ev.date} (${ev.timing === 'Before Market Open' ? 'BMO' : ev.timing === 'After Market Close' ? 'AMC' : 'TAS'})`);
      chips.push(`<span class="catalyst-chip earnings">${label}</span>`);
    });

    macro.forEach(ev => {
      const label = sanitize(`${ev.date} ${ev.event}`);
      chips.push(`<span class="catalyst-chip macro">${label}</span>`);
    });

    if (breadth) {
      const isHealthy = parseFloat(breadth.breadth_raw) > 0.60;
      const isWeak    = parseFloat(breadth.breadth_raw) < 0.40;
      const cls       = isHealthy ? 'breadth-healthy' : isWeak ? 'breadth-weak' : 'breadth-neutral';
      const label     = sanitize(`Breadth ${breadth.pct_above_200ma} above 200MA`);
      chips.push(`<span class="catalyst-chip ${cls}">${label}</span>`);
    }

    el.innerHTML = `<span class="catalysts-label">Catalysts</span>${chips.join('')}`;
    el.hidden = false;
  }

  // ── Equity Chart ──────────────────────────────────────────
  renderEquityChart() {
    const { equity_curve, summary } = this.data;
    const ctx         = $('equityChart').getContext('2d');
    const returnPct   = this.data.meta.initial_capital > 0
      ? ((summary.portfolio_value - this.data.meta.initial_capital) / this.data.meta.initial_capital * 100)
      : 0;
    const chartReturn = $('equityReturn');

    if (!equity_curve || equity_curve.length < 2) {
      chartReturn.textContent = 'Awaiting data';
      chartReturn.className   = 'chart-return card-meta muted-cell';
      if (this.chart) { this.chart.destroy(); this.chart = null; }
      return;
    }

    chartReturn.textContent = fmtPct(returnPct) + ' since inception';
    chartReturn.className   = `chart-return ${returnPct >= 0 ? 'profit-cell' : 'loss-cell'}`;

    if (this.chart) this.chart.destroy();

    const gradient = ctx.createLinearGradient(0, 0, 0, 220);
    gradient.addColorStop(0, 'rgba(212,175,55,0.18)');
    gradient.addColorStop(1, 'rgba(212,175,55,0)');

    this.chart = new Chart(ctx, {
      type: 'line',
      data: {
        labels:   equity_curve.map(p => p.date),
        datasets: [{
          data:                    equity_curve.map(p => p.value),
          borderColor:             '#D4AF37',
          borderWidth:             1.8,
          backgroundColor:         gradient,
          fill:                    true,
          tension:                 0.4,
          pointRadius:             0,
          pointHoverRadius:        5,
          pointHoverBackgroundColor: '#D4AF37',
          pointHoverBorderColor:   '#0B0B0B',
          pointHoverBorderWidth:   2,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: '#1A1A1A', borderColor: '#2A2A2A', borderWidth: 1,
            titleColor: '#808080', bodyColor: '#EAEAEA', padding: 10,
            callbacks: {
              label:      ctx => `  ${fmt$(ctx.raw)}`,
              afterLabel: ctx => {
                const pct = (ctx.raw - equity_curve[0].value) / equity_curve[0].value * 100;
                return `  ${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`;
              },
            },
          },
        },
        scales: {
          x: {
            grid:   { color: 'rgba(255,255,255,0.03)', drawBorder: false },
            ticks:  { color: '#555', maxTicksLimit: 7, font: { size: 10 } },
            border: { color: '#1E1E1E' },
          },
          y: {
            position: 'right',
            grid:   { color: 'rgba(255,255,255,0.03)', drawBorder: false },
            ticks:  { color: '#555', font: { size: 10 }, callback: v => '$' + (v / 1000).toFixed(0) + 'k' },
            border: { color: '#1E1E1E' },
          },
        },
      },
    });
  }

  // ── Holdings — Robinhood-style position cards ─────────────
  renderHoldings() {
    const { holdings } = this.data;
    $('holdingsCount').textContent = holdings.length || 0;
    const grid = $('positionsGrid');
    if (!grid) return;

    if (!holdings.length) {
      grid.innerHTML = `<div class="pos-empty">No positions yet — first rebalance runs May 1</div>`;
      return;
    }

    grid.innerHTML = holdings.map((h, idx) => {
      const sym     = sanitize(h.symbol);
      const name    = sanitize(h.name || h.sector || '');
      const sector  = sanitize(h.sector || '');
      const shares  = Number.isFinite(h.shares) ? h.shares : 0;
      const rank    = Number.isFinite(h.momentum_rank) ? h.momentum_rank : '—';
      const rankCls = h.momentum_rank <= 5 ? 'top5' : h.momentum_rank <= 10 ? 'mid' : '';
      const pnl     = h.pnl || 0;
      const pnlCls  = colorClass(pnl);
      const pnlSign = pnl >= 0 ? '+' : '−';
      const pnlAbs  = fmt$(Math.abs(pnl));
      const maCls   = h.status === 'above_ma' ? 'profit-cell' : 'loss-cell';
      const maText  = h.status === 'above_ma' ? '▲ Above MA50' : '▼ Below MA50';
      const shareLabel = `${shares} ${shares === 1 ? 'share' : 'shares'} · avg ${fmt$(h.avg_cost)}`;
      return `
        <div class="pos-card" data-idx="${idx}" role="button" tabindex="0" aria-expanded="false">
          <div class="pos-main">
            <div class="pos-identity">
              <span class="pos-sym">${sym}</span>
              <span class="pos-meta">${shareLabel}</span>
            </div>
            <canvas class="pos-sparkline" data-idx="${idx}" width="64" height="32"></canvas>
            <div class="pos-values">
              <span class="pos-equity">${fmt$(h.market_value)}</span>
              <span class="pos-return ${pnlCls}">${pnlSign}${pnlAbs} <span class="pos-return-pct">(${fmtPct(h.pnl_pct)})</span></span>
            </div>
          </div>
          <div class="pos-detail" id="pos-detail-${idx}" hidden>
            <div class="pos-detail-grid">
              <div class="pos-stat"><span class="pos-stat-label">Current Price</span><span>${fmt$(h.current_price || h.avg_cost)}</span></div>
              <div class="pos-stat"><span class="pos-stat-label">Avg Cost</span><span>${fmt$(h.avg_cost)}</span></div>
              <div class="pos-stat"><span class="pos-stat-label">MA50 Status</span><span class="${maCls}" style="font-size:0.7rem">${maText}</span></div>
              <div class="pos-stat"><span class="pos-stat-label">Rank</span><span class="rank-num ${rankCls}" style="font-size:0.72rem;width:auto;height:auto;padding:1px 7px;border-radius:3px">${rank}</span></div>
              <div class="pos-stat"><span class="pos-stat-label">6M Momentum</span><span>${fmtPct((h.momentum_6m || 0) * 100, false)}</span></div>
              <div class="pos-stat"><span class="pos-stat-label">EPS Growth</span><span>${h.eps_growth != null ? h.eps_growth + '%' : '—'}</span></div>
              <div class="pos-stat"><span class="pos-stat-label">Fwd P/E</span><span>${h.forward_pe != null ? Number(h.forward_pe).toFixed(1) : '—'}</span></div>
              <div class="pos-stat"><span class="pos-stat-label">30d Vol</span><span>${h.volatility_30d != null ? (h.volatility_30d * 100).toFixed(0) + '%' : '—'}</span></div>
            </div>
            <div class="pos-detail-footer">
              <span class="muted-cell" style="font-size:0.7rem">Entry ${fmtDate(h.entry_date || '')}</span>
              <span class="pos-sector-chip">${sector}</span>
              <button class="pos-trade-btn" onclick="event.stopPropagation();app.showOrderModal(${idx})">Paper Trade</button>
            </div>
          </div>
        </div>`;
    }).join('');

    // Expand / collapse on click
    grid.querySelectorAll('.pos-card').forEach(card => {
      card.addEventListener('click', () => {
        const idx    = card.dataset.idx;
        const detail = $(`pos-detail-${idx}`);
        const open   = !detail.hidden;
        detail.hidden = open;
        card.setAttribute('aria-expanded', String(!open));
        card.classList.toggle('expanded', !open);
      });
      card.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); card.click(); }
      });
    });

    // Draw sparklines after layout is stable
    requestAnimationFrame(() => {
      grid.querySelectorAll('.pos-sparkline').forEach(canvas => {
        const idx = parseInt(canvas.dataset.idx, 10);
        if (holdings[idx]) drawSparkline(canvas, holdings[idx]);
      });
    });
  }

  // ── Trades (with filter + sort) ───────────────────────────
  renderTrades() {
    const { trades = [], summary } = this.data;
    $('totalTradesCount').textContent = `${summary.total_trades || 0} total`;

    // Apply filter
    let list = this.tradeFilter === 'ALL'
      ? trades
      : trades.filter(t => (t.action || '').toUpperCase() === this.tradeFilter);

    // Apply sort
    if (this.tradeSort === 'pnl') {
      list = [...list].sort((a, b) => (b.pnl || 0) - (a.pnl || 0));
    }

    const recent = list.slice(0, 20);

    if (!recent.length) {
      $('tradesTbody').innerHTML =
        `<tr><td colspan="7" class="empty-cell">${trades.length ? 'No matching trades' : 'No trades recorded yet'}</td></tr>`;
      return;
    }

    $('tradesTbody').innerHTML = recent.map(t => {
      const pnlHtml  = t.pnl != null
        ? `<span class="${colorClass(t.pnl)}">${t.pnl >= 0 ? '+' : ''}${fmt$(t.pnl)}</span>`
        : `<span class="muted-cell">—</span>`;
      const action   = sanitize(t.action).toUpperCase();
      const actionCls = action === 'BUY' ? 'buy' : action === 'SELL' ? 'sell' : 'buy';
      const sym      = sanitize(t.symbol);
      const reason   = sanitize(t.reason);
      const shares   = Number.isFinite(t.shares) ? t.shares : '—';
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
  }

  // ── Strategy ──────────────────────────────────────────────
  renderStrategy() {
    const { filter_status, meta } = this.data;

    $('strategyFilters').innerHTML = Object.values(filter_status).map(f => {
      const ratio     = f.total > 0 ? f.passing / f.total : 0;
      const statusCls = ratio === 1 ? 'pass' : ratio >= 0.8 ? 'warn' : 'fail';
      const icon      = ratio === 1 ? '✓' : ratio >= 0.8 ? '!' : '✗';
      const label     = sanitize(f.label);
      const desc      = sanitize(f.description);
      const pNum      = Number.isFinite(f.passing) ? f.passing : 0;
      const tNum      = Number.isFinite(f.total)   ? f.total   : 0;
      return `
        <div class="filter-row">
          <div class="filter-top">
            <div class="filter-label"><div class="filter-icon ${statusCls}">${icon}</div>${label}</div>
            <span class="filter-count">${pNum}/${tNum}</span>
          </div>
          <div class="filter-desc">${desc}</div>
          <div class="filter-bar-track">
            <div class="filter-bar-fill ${statusCls}" style="width:${(ratio * 100).toFixed(0)}%"></div>
          </div>
        </div>`;
    }).join('');

    const ri          = meta.regime_indicators;
    const regimeDesc  = sanitize(ri.description);
    const trendLabel  = sanitize(ri.ma200_trend);
    $('regimeDetail').innerHTML = `
      <div class="regime-grid">
        <div class="regime-stat"><span class="regime-stat-label">VIX</span><span class="regime-stat-value">${Number(ri.vix).toFixed(1)}</span></div>
        <div class="regime-stat"><span class="regime-stat-label">Breadth</span><span class="regime-stat-value">${(Number(ri.breadth_pct) * 100).toFixed(0)}% above 200-MA</span></div>
        <div class="regime-stat"><span class="regime-stat-label">Trend</span><span class="regime-stat-value" style="text-transform:capitalize">${trendLabel}</span></div>
        <div class="regime-stat"><span class="regime-stat-label">Equity exposure</span><span class="regime-stat-value">${(Number(meta.equity_exposure) * 100).toFixed(0)}%</span></div>
      </div>
      <div class="regime-desc">${regimeDesc}</div>`;
  }

  // ── Momentum Heatmap (Task 5) ─────────────────────────────
  renderMomentumHeatmap() {
    const el = $('momentumHeatmap');
    if (!el) return;
    const { holdings } = this.data;

    if (!holdings?.length) {
      el.innerHTML = `<div class="heat-empty">No positions — heatmap available after first rebalance</div>`;
      return;
    }

    const sorted = [...holdings].sort((a, b) => (a.momentum_rank || 99) - (b.momentum_rank || 99));
    el.innerHTML = sorted.map(h => {
      const rank  = h.momentum_rank || 99;
      const tier  = rank <= 6 ? 'green' : rank <= 11 ? 'yellow' : 'red';
      const sym   = sanitize(h.symbol);
      const mom   = h.momentum_6m != null ? fmtPct(h.momentum_6m * 100, false) : '—';
      return `
        <div class="heat-cell ${tier}" data-symbol="${sym}"
             title="${sym} · Rank #${rank} · 6M ${mom}"
             role="button" tabindex="0">
          <span class="heat-sym">${sym}</span>
          <span class="heat-rank">#${rank}</span>
          <span class="heat-mom">${mom}</span>
        </div>`;
    }).join('');

    // Tap cell → switch to Portfolio tab and highlight the position card
    el.querySelectorAll('.heat-cell').forEach(cell => {
      const jump = () => {
        const sym = cell.dataset.symbol;
        const tab = document.querySelector('.tab[data-tab="portfolio"]');
        if (tab) tab.click();
        setTimeout(() => {
          document.querySelectorAll('.pos-card').forEach(card => {
            if (card.querySelector('.pos-sym')?.textContent === sym) {
              card.scrollIntoView({ behavior: 'smooth', block: 'center' });
              card.classList.add('highlight');
              setTimeout(() => card.classList.remove('highlight'), 1500);
            }
          });
        }, 150);
      };
      cell.addEventListener('click', jump);
      cell.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); jump(); } });
    });
  }

  // ── Agent Log (Task 1 — parse fix + beautiful UI) ─────────
  renderAgentLog() {
    const log     = this.agentLog || { runs: [] };
    const rawRuns = (log.runs || []).slice().reverse(); // newest first

    // Save non-sensitive metadata to localStorage before recovery
    rawRuns.slice(0, 5).forEach(run => AgentHistory.save(run));

    // Attempt to recover parse-failed runs
    const runs = rawRuns.map(tryRecoverRun);

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

    list.innerHTML = runs.map(run => {
      const rawType  = run.type || run.run_type || '';
      const runType  = sanitize(rawType);
      const regime   = sanitize(run.regime || 'unknown');
      const conf     = Number.isFinite(run.regime_confidence)
        ? ` · ${(run.regime_confidence * 100).toFixed(0)}%` : '';
      const headline = sanitize(run.summary    || '');
      const assess   = sanitize(run.assessment || '');
      const ts       = fmtDate(run.timestamp   || '');

      // Complete parse failure — styled error card with copy button
      if (run._parseFailed) {
        const rawContent = String(run.assessment || '');
        return `
          <div class="agent-run type-${runType} run-parse-error">
            <div class="agent-run-header">
              <span class="run-type-badge ${runType}">${sanitize(TYPE_LABEL[rawType] || rawType)}</span>
              <span class="run-time">${ts}</span>
              <span class="parse-fail-badge">Parse Error</span>
            </div>
            <p class="run-assess">Claude&apos;s response could not be parsed. The raw text is stored in agent_log.json. This is likely a token-length issue — the agent.py max_tokens has been increased to 4000 to prevent recurrence.</p>
            <button class="copy-raw-btn" data-run-id="${sanitize(run.id || ts)}">Copy raw response</button>
          </div>`;
      }

      // Flag chips with severity classification
      const flagsHtml = (run.flags || []).map(f => {
        const text = typeof f === 'string' ? f : `${f.symbol || ''}: ${f.flag || ''}`;
        const tier = classifyFlag(text);
        const disp = sanitize(text.length > 70 ? text.slice(0, 70) + '…' : text);
        return `<span class="flag-chip ${tier}" title="${sanitize(text)}">${disp}</span>`;
      }).join('');

      // Decision chips
      const decisionsHtml = (run.decisions || []).map(d => {
        const action  = sanitize(String(d.action || '').toUpperCase());
        const sym     = sanitize(d.symbol || '');
        const reason  = sanitize(d.reason || '');
        const urgency = sanitize(d.urgency || '');
        const cls     = ['SELL', 'BUY', 'HOLD', 'WATCH'].includes(action) ? action : 'HOLD';
        const tip     = reason + (urgency ? ' · ' + urgency : '');
        return `<span class="decision-chip ${cls}" title="${tip}">${action}${sym ? ' ' + sym : ''}</span>`;
      }).join('');

      const cashHtml = run.cash_action
        ? `<span class="decision-chip WATCH">CASH ${sanitize(run.cash_action).toUpperCase()}</span>` : '';

      const chipsRow = flagsHtml + decisionsHtml + cashHtml;

      const recoveredBadge = run._recovered
        ? `<span class="recovered-badge" title="JSON recovered from raw stored text">✓ recovered</span>` : '';

      const tokenMeta = run.usage?.input_tokens
        ? `<span class="run-token-meta">${run.usage.input_tokens}in / ${run.usage.output_tokens}out${run.usage.cache_read_tokens ? ' · ' + run.usage.cache_read_tokens + ' cached' : ''}</span>`
        : '';

      return `
        <div class="agent-run type-${runType}">
          <div class="agent-run-header">
            <span class="run-type-badge ${runType}">${sanitize(TYPE_LABEL[rawType] || rawType)}</span>
            <span class="run-regime ${regime}">${regime}${conf}</span>
            <span class="run-time">${ts}</span>
            ${recoveredBadge}
          </div>
          ${headline ? `<p class="run-headline">${headline}</p>` : ''}
          ${assess   ? `<p class="run-assess">${assess}</p>`    : ''}
          ${chipsRow ? `<div class="run-chips-row">${chipsRow}</div>` : ''}
          ${run.cash_rationale ? `<p class="run-cash-note muted-cell">${sanitize(run.cash_rationale)}</p>` : ''}
          ${tokenMeta ? `<div class="run-meta">${tokenMeta}</div>` : ''}
        </div>`;
    }).join('');

    // Wire copy buttons (avoids inline onclick in template)
    list.querySelectorAll('.copy-raw-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const runId = btn.dataset.runId;
        const run   = rawRuns.find(r => r.id === runId || fmtDate(r.timestamp) === runId);
        const text  = run?.assessment || 'No raw content available';
        if (navigator.clipboard) {
          navigator.clipboard.writeText(text)
            .then(() => { btn.textContent = 'Copied!'; setTimeout(() => { btn.textContent = 'Copy raw response'; }, 2000); })
            .catch(() => { btn.textContent = 'Open DevTools → Network'; });
        } else {
          btn.textContent = 'Clipboard unavailable';
        }
      });
    });
  }

  // ── Agent Run History Timeline (Task 4 — localStorage) ────
  renderAgentTimeline() {
    const el = $('agentTimeline');
    if (!el) return;
    const hist = AgentHistory.load();

    if (!hist.length) {
      el.innerHTML = `<p class="muted-cell" style="font-size:0.75rem;padding:16px 18px">No history in this browser yet — runs will appear here after the agent fires.</p>`;
      return;
    }

    const TYPE_LABEL = { day_start: 'Day Start', day_end: 'Day End', monthly: 'Monthly' };
    el.innerHTML = hist.map(h => {
      const regime = sanitize(h.regime || 'unknown');
      const type   = sanitize(h.type   || '');
      const conf   = Number.isFinite(h.confidence) ? `${(h.confidence * 100).toFixed(0)}%` : '';
      return `
        <div class="timeline-entry">
          <div class="timeline-dot ${regime}"></div>
          <div class="timeline-body">
            <div class="timeline-header">
              <span class="run-type-badge ${type}">${sanitize(TYPE_LABEL[h.type] || h.type || '—')}</span>
              <span class="run-regime ${regime}">${regime}${conf ? ' · ' + conf : ''}</span>
              <span class="muted-cell" style="font-size:0.65rem">${fmtDate(h.timestamp)}</span>
            </div>
            <p class="timeline-summary">${sanitize(h.summary)}</p>
            <div class="timeline-counts">${h.flagCount} flags · ${h.decisionCount} decisions</div>
          </div>
        </div>`;
    }).join('');
  }

  // ── Order Modal (Task 2C — paper trade with validation) ───
  // Security: validation only. Submit opens GitHub Actions page —
  // NO direct Alpaca API call from browser (credentials stay server-side).
  showOrderModal(idx) {
    const holding = this.data?.holdings?.[idx];
    if (!holding) return;

    const cash    = this.data?.summary?.cash || 0;
    const price   = holding.current_price || holding.avg_cost || 1;
    const maxQty  = Math.floor(cash / price);
    const sym     = sanitize(holding.symbol);
    const modal   = $('orderModal');
    if (!modal) return;

    modal.innerHTML = `
      <div class="modal-backdrop" role="presentation"></div>
      <div class="modal-panel" role="dialog" aria-modal="true" aria-label="Paper Trade ${sym}">
        <div class="modal-header">
          <div>
            <h2 class="modal-title">Paper Trade</h2>
            <p class="modal-subtitle">${sym} · ${sanitize(holding.name || holding.sector || '')}</p>
          </div>
          <button class="modal-close" aria-label="Close">&#x2715;</button>
        </div>
        <div class="modal-body">
          <div class="modal-stats-row">
            <div class="modal-stat-item">
              <span class="modal-stat-label">Current Price</span>
              <span class="modal-stat-val">${fmt$(price)}</span>
            </div>
            <div class="modal-stat-item">
              <span class="modal-stat-label">Available Cash</span>
              <span class="modal-stat-val">${fmt$(cash)}</span>
            </div>
            <div class="modal-stat-item">
              <span class="modal-stat-label">Max Shares</span>
              <span class="modal-stat-val">${maxQty > 0 ? maxQty : '—'}</span>
            </div>
          </div>
          <div class="modal-field">
            <label class="modal-label" for="orderQty">Number of shares</label>
            <input type="number" id="orderQty" class="modal-input"
                   min="1" max="${maxQty}" step="1" placeholder="0"
                   autocomplete="off">
            <div class="modal-order-total" id="orderTotal">Enter a quantity above</div>
          </div>
          <div id="orderValidation" class="modal-validation" hidden></div>
          <div class="modal-paper-info">
            <span class="modal-info-icon">ℹ</span>
            <span>Paper trading only — no real money. Orders are queued for the next GitHub Actions workflow run and executed via the Alpaca paper API.</span>
          </div>
        </div>
        <div class="modal-footer">
          <button class="modal-cancel-btn" id="modalCancelBtn">Cancel</button>
          <a class="modal-submit-btn"
             href="https://github.com/rishihjoshi/TradeQuest-AI/actions/workflows/update.yml"
             target="_blank" rel="noopener noreferrer">
            Open Workflow ↗
          </a>
        </div>
      </div>`;

    // Live validation
    const qtyInput  = $('orderQty');
    const totalEl   = $('orderTotal');
    const validEl   = $('orderValidation');

    const validate = () => {
      const raw = qtyInput.value.trim();
      const qty = parseInt(raw, 10);
      validEl.hidden = true;

      if (!raw || isNaN(qty)) { totalEl.textContent = 'Enter a quantity above'; return; }

      // Validation rules: integer > 0, ≤ floor(cash / price), price bounds auto-satisfied (market order)
      if (qty <= 0 || !Number.isInteger(qty)) {
        validEl.hidden = false; validEl.textContent = 'Must be a positive whole number'; return;
      }
      if (qty > maxQty) {
        validEl.hidden = false;
        validEl.textContent = `Exceeds available cash — max ${maxQty} shares at ${fmt$(price)}`;
        return;
      }
      const total = qty * price;
      totalEl.textContent = `≈ ${fmt$(total)} (${((total / (cash || 1)) * 100).toFixed(1)}% of cash)`;
    };

    qtyInput.addEventListener('input', validate);

    // Close handlers
    const close = () => { modal.hidden = true; modal.innerHTML = ''; };
    modal.querySelector('.modal-backdrop').addEventListener('click', close);
    modal.querySelector('.modal-close').addEventListener('click', close);
    $('modalCancelBtn').addEventListener('click', close);
    document.addEventListener('keydown', function esc(e) {
      if (e.key === 'Escape') { close(); document.removeEventListener('keydown', esc); }
    });

    modal.hidden = false;
    requestAnimationFrame(() => qtyInput?.focus());
  }

  // ── News — lazy fetch with 5-min memory TTL ───────────────
  async loadNewsIfStale() {
    const age = Date.now() - this.newsFetchedAt;
    if (this.newsData && age < NEWS_TTL_MS) {
      this.renderNews();
      return;
    }
    this.renderNewsSkeletons();
    try {
      const ctrl = new AbortController();
      const t    = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
      const res  = await fetch(`${NEWS_URL}?_=${Date.now()}`, { signal: ctrl.signal })
                     .finally(() => clearTimeout(t));
      if (!res.ok) throw new Error(`News HTTP ${res.status}`);
      this.newsData      = await res.json();
      this.newsFetchedAt = Date.now();
    } catch (err) {
      $('newsArticleFeed').innerHTML =
        `<div class="news-error">Could not load news — ${sanitize(err.message)}</div>`;
      return;
    }
    this.renderNews();
  }

  renderNewsSkeletons(n = 5) {
    const feed = $('newsArticleFeed');
    if (!feed) return;
    feed.innerHTML = Array.from({ length: n }, () => `
      <div class="news-article-card skeleton">
        <div class="sk-line sk-badge"></div>
        <div class="sk-line sk-headline"></div>
        <div class="sk-line sk-body"></div>
        <div class="sk-line sk-meta"></div>
      </div>`).join('');
  }

  renderNews() {
    const feed = $('newsArticleFeed');
    if (!feed) return;

    const data     = this.newsData;
    const articles = data?.articles ?? [];

    // Update sentiment summary bar
    const bull    = articles.filter(a => a.sentiment === 'bull').length;
    const bear    = articles.filter(a => a.sentiment === 'bear').length;
    const neutral = articles.filter(a => a.sentiment === 'neutral').length;
    const genAt   = data?.generated_at ? timeAgo(data.generated_at) : '—';
    if ($('newsBullCount'))     $('newsBullCount').textContent    = bull;
    if ($('newsBearCount'))     $('newsBearCount').textContent    = bear;
    if ($('newsNeutralCount'))  $('newsNeutralCount').textContent = neutral;
    if ($('newsGeneratedAt'))   $('newsGeneratedAt').textContent  = `Updated ${genAt}`;

    // Build ticker chip list from articles (symbols present in filtered view)
    const allSymbols = [...new Set(articles.flatMap(a => a.symbols || []))].sort();
    const chipsEl    = $('newsTickerChips');
    if (chipsEl) {
      chipsEl.innerHTML = allSymbols.map(sym => {
        const s     = sanitize(sym);
        const active = this.newsTickerFilter === sym ? ' active' : '';
        return `<button class="ticker-chip${active}" data-sym="${s}">${s}</button>`;
      }).join('');
      chipsEl.querySelectorAll('.ticker-chip').forEach(chip => {
        chip.addEventListener('click', () => {
          const sym = chip.dataset.sym;
          this.newsTickerFilter = this.newsTickerFilter === sym ? null : sym;
          this.newsDisplayLimit = NEWS_PAGE_SIZE;
          this.renderNews();
        });
      });
    }

    // Filter: sentiment + ticker
    let filtered = articles;
    if (this.newsFilter !== 'ALL') {
      filtered = filtered.filter(a => a.sentiment === this.newsFilter);
    }
    if (this.newsTickerFilter) {
      filtered = filtered.filter(a => (a.symbols || []).includes(this.newsTickerFilter));
    }

    if (!filtered.length) {
      feed.innerHTML = `<div class="news-empty">No articles match the current filter.</div>`;
      $('newsLoadMore').hidden = true;
      return;
    }

    const page     = filtered.slice(0, this.newsDisplayLimit);
    const hasMore  = filtered.length > this.newsDisplayLimit;

    feed.innerHTML = page.map(a => {
      const sent    = a.sentiment || 'neutral';
      const sentLbl = sent === 'bull' ? 'Bullish' : sent === 'bear' ? 'Bearish' : 'Neutral';
      const conf    = Number.isFinite(a.confidence) ? ` ${Math.round(a.confidence * 100)}%` : '';
      const tickers = (a.symbols || []).slice(0, 5).map(s => `<span class="article-ticker">${sanitize(s)}</span>`).join('');
      const url     = a.url ? `href="${sanitize(a.url)}" target="_blank" rel="noopener noreferrer"` : '';
      const ago     = timeAgo(a.created_at);
      const src     = a.source ? sanitize(a.source) : '';
      const author  = a.author ? ` · ${sanitize(a.author)}` : '';
      return `
        <div class="news-article-card" data-ts="${sanitize(a.created_at || '')}">
          <div class="article-header">
            <span class="sentiment-badge ${sent}">${sentLbl}${conf}</span>
            ${tickers}
            <span class="article-meta muted-cell">${src}${author}</span>
          </div>
          <a class="article-headline" ${url}>${sanitize(a.headline)}</a>
          ${a.summary ? `<p class="article-summary muted-cell">${sanitize(a.summary)}</p>` : ''}
          ${a.reason  ? `<p class="article-reason">${sanitize(a.reason)}</p>` : ''}
          <div class="article-footer">
            <span class="article-time muted-cell" data-ts="${sanitize(a.created_at || '')}">${ago}</span>
            ${url ? `<a class="article-link" ${url}>Read ↗</a>` : ''}
          </div>
        </div>`;
    }).join('');

    const lm = $('newsLoadMore');
    if (lm) lm.hidden = !hasMore;
  }

  // Refresh only the time-ago labels without a full re-render
  refreshTimeAgoLabels() {
    document.querySelectorAll('.article-time[data-ts]').forEach(el => {
      if (el.dataset.ts) el.textContent = timeAgo(el.dataset.ts);
    });
    if ($('newsGeneratedAt') && this.newsData?.generated_at) {
      $('newsGeneratedAt').textContent = `Updated ${timeAgo(this.newsData.generated_at)}`;
    }
  }

  // ── Error ─────────────────────────────────────────────────
  showError(msg) {
    $('loadingState').hidden = true;
    $('mainContent').hidden  = true;
    const el = $('errorState');
    el.hidden = false;
    el.innerHTML = `
      <div class="error-icon">⚠</div>
      <p>Failed to load portfolio data</p>
      <p style="color:var(--muted);font-size:0.72rem;margin-top:4px">${sanitize(msg)}</p>
      <button onclick="app.load()" style="margin-top:14px;padding:7px 18px;background:var(--surface);border:1px solid var(--border);color:var(--text);border-radius:var(--radius);cursor:pointer;font-size:0.78rem;">Retry</button>`;
  }
}

const app = new TradeQuestApp();
app.init();
