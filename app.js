'use strict';

// ── Constants ─────────────────────────────────────────────────
const DATA_URL           = './data/portfolio.json';
const AGENT_LOG_URL      = './data/agent_log.json';
const NEWS_URL           = './data/news.json';
const ENRICHMENT_URL     = './data/enrichment.json';
const TIMING_ABBREV      = { 'Before Market Open': 'BMO', 'After Market Close': 'AMC', 'During Hours': 'TAS', 'BMO': 'BMO', 'AMC': 'AMC', 'TAS': 'TAS' };
const MARKET_REFRESH_MS  = 30_000;        // 30 s during market hours
const DEFAULT_REFRESH_MS = 5 * 60_000;    // 5 min otherwise
const FETCH_TIMEOUT_MS   = 15_000;
const NEWS_TTL_MS        = 5 * 60_000;    // 5-min memory cache for news tab
const NEWS_PAGE_SIZE     = 25;
const LS_AGENT_KEY       = 'tq_agent_runs';
const MAX_AGENT_HISTORY  = 10;
const SEARCH_DEBOUNCE_MS = 300;

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
    this.symbolChart       = null;
    this.tradeFilter       = 'ALL';   // 'ALL' | 'BUY' | 'SELL'
    this.tradeSort         = 'date';  // 'date' | 'pnl'
    this.refreshTimer      = null;
    this.timeAgoTimer      = null;
    this.searchDebounce    = null;
    this.symbolScreenSym   = null;
    // Trade ticket state
    this.tradeState = {
      symbol: '', name: '', side: 'buy', type: 'market',
      qty: 0, limitPrice: null, stopPrice: null, tif: 'day',
      currentPrice: 0, buyingPower: 0,
    };
    // Orders tab state
    this.ordersData      = { open: null, closed: null };
    this.ordersFilter    = 'open';
    this.ordersSymFilter = '';
  }

  async init() {
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('./sw.js').catch(() => {});
    }
    this.setupTabs();
    this.setupTradeControls();
    this.setupAgentHistoryUI();
    this.setupNewsControls();
    this.setupSearch();
    this.setupTradeTicketListeners();
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
        if (target === 'news')   this.loadNewsIfStale();
        if (target === 'orders') this.loadOrdersIfStale();
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
      // Enrichment changes once daily — skip refetch if we already have today's data
      const today         = new Date().toISOString().slice(0, 10);
      const needsEnrich   = !this.enrichment || this.enrichment.generated_at !== today;
      const fetches       = [fetchWithTimeout(DATA_URL), fetchWithTimeout(AGENT_LOG_URL)];
      if (needsEnrich) fetches.push(fetchWithTimeout(ENRICHMENT_URL));

      const [portfolioRes, agentRes, enrichRes] = await Promise.all(fetches);
      if (!portfolioRes.ok) throw new Error(`Portfolio HTTP ${portfolioRes.status}`);
      this.data     = await portfolioRes.json();
      this.agentLog = agentRes.ok ? await agentRes.json() : { runs: [] };
      if (needsEnrich) this.enrichment = enrichRes?.ok ? await enrichRes.json() : null;
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

    const regime = ['bull', 'bear', 'sideways'].includes(meta.market_regime) ? meta.market_regime : 'sideways';
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
      const abbrev = TIMING_ABBREV[ev.timing] || ev.timing;
      const label  = sanitize(`${ev.symbol} earnings ${ev.date} (${abbrev})`);
      chips.push(`<span class="catalyst-chip earnings">${label}</span>`);
    });

    macro.forEach(ev => {
      chips.push(`<span class="catalyst-chip macro">${sanitize(`${ev.date} ${ev.event}`)}</span>`);
    });

    if (breadth) {
      const raw = breadth.breadth_raw;
      const cls = raw > 0.60 ? 'breadth-healthy' : raw < 0.40 ? 'breadth-weak' : 'breadth-neutral';
      chips.push(`<span class="catalyst-chip ${cls}">${sanitize(`Breadth ${breadth.pct_above_200ma} above 200MA`)}</span>`);
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
              <button class="pos-trade-btn" onclick="event.stopPropagation();app.showTradeTicket({symbol:'${sym}',name:'${sanitize(h.name||h.sector||'')}',side:'buy',currentPrice:${h.current_price||h.avg_cost||0}})">Trade</button>
              <button class="pos-close-btn" onclick="event.stopPropagation();app.showTradeTicket({symbol:'${sym}',name:'${sanitize(h.name||h.sector||'')}',side:'sell',qty:${shares},currentPrice:${h.current_price||h.avg_cost||0}})">Close Position</button>
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
    const rawRuns = (log.runs || []).slice(); // newest first — agent.py prepends with insert(0, entry)

    rawRuns.slice(0, 5).forEach(run => AgentHistory.save(run));

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
              <li><strong>5:30 PM ET / 4:30 PM CT</strong> Mon&ndash;Fri &mdash; post-close decisions</li>
              <li><strong>5:30 PM ET / 4:30 PM CT</strong> 1st of month &mdash; full rebalance</li>
            </ul>
            Tap <strong>&#x25B6; Run workflow</strong> above to trigger immediately.
          </div>
        </div>`;
      return;
    }

    const TYPE_LABEL = { day_start: 'Day Start', day_end: 'Day End', monthly: 'Monthly' };
    const tok = n => Number.isFinite(+n) ? String(+n) : '?';

    list.innerHTML = runs.map((run, idx) => {
      const rawType  = run.type || run.run_type || '';
      const runType  = sanitize(rawType);
      const regimeRaw = ['bull', 'bear', 'sideways'].includes(run.regime) ? run.regime : 'sideways';
      const regime   = sanitize(regimeRaw);
      const conf     = Number.isFinite(run.regime_confidence)
        ? ` · ${(run.regime_confidence * 100).toFixed(0)}%` : '';
      const headline = sanitize(run.summary    || '');
      const assess   = sanitize(run.assessment || '');
      const ts       = fmtDate(run.timestamp   || '');
      const runId    = sanitize(run.id || ts);
      const isExpanded = idx === 0;

      if (run._parseFailed) {
        return `
          <div class="agent-run type-${runType} run-parse-error${isExpanded ? ' expanded' : ''}" data-run-id="${runId}">
            <div class="agent-run-header" role="button" tabindex="0" aria-expanded="${isExpanded}">
              <span class="run-type-badge ${runType}">${sanitize(TYPE_LABEL[rawType] || rawType)}</span>
              <span class="run-time">${ts}</span>
              <span class="parse-fail-badge">Parse Error</span>
              <span class="run-expand-icon" aria-hidden="true">&#9660;</span>
            </div>
            <div class="run-body">
              <p class="run-assess">Claude&apos;s response could not be parsed. The raw text is stored in agent_log.json.</p>
              <button class="copy-raw-btn" data-run-id="${runId}">Copy raw response</button>
            </div>
          </div>`;
      }

      // Header preview: first 3 decisions (hidden when expanded)
      const previewDecisions = (run.decisions || []).slice(0, 3).map(d => {
        const action = sanitize(String(d.action || '').toUpperCase());
        const sym    = sanitize(d.symbol || '');
        const cls    = ['SELL', 'BUY', 'HOLD', 'WATCH'].includes(action) ? action : 'HOLD';
        return `<span class="decision-chip ${cls} chip-sm">${action}${sym ? ' ' + sym : ''}</span>`;
      }).join('');
      const moreCount = Math.max(0, (run.decisions || []).length - 3);
      const moreChip  = moreCount > 0 ? `<span class="chip-more">+${moreCount} more</span>` : '';

      // Body: full flags
      const flagsHtml = (run.flags || []).map(f => {
        const text = typeof f === 'string' ? f : `${f.symbol || ''}: ${f.flag || ''}`;
        const tier = classifyFlag(text);
        const disp = sanitize(text.length > 80 ? text.slice(0, 80) + '…' : text);
        return `<span class="flag-chip ${tier}" title="${sanitize(text)}">${disp}</span>`;
      }).join('');

      // Body: full decisions
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
        ? `<span class="run-token-meta">${tok(run.usage.input_tokens)}in / ${tok(run.usage.output_tokens)}out${run.usage.cache_read_tokens ? ' · ' + tok(run.usage.cache_read_tokens) + ' cached' : ''}</span>`
        : '';

      return `
        <div class="agent-run type-${runType}${isExpanded ? ' expanded' : ''}" data-run-id="${runId}">
          <div class="agent-run-header" role="button" tabindex="0" aria-expanded="${isExpanded}">
            <span class="run-type-badge ${runType}">${sanitize(TYPE_LABEL[rawType] || rawType)}</span>
            <span class="run-regime ${regime}">${regime}${conf}</span>
            <div class="run-preview-chips">${previewDecisions}${moreChip}</div>
            <span class="run-time">${ts}</span>
            ${recoveredBadge}
            <span class="run-expand-icon" aria-hidden="true">&#9660;</span>
          </div>
          <div class="run-body">
            ${headline ? `<p class="run-headline">${headline}</p>` : ''}
            ${assess   ? `<p class="run-assess">${assess}</p>`    : ''}
            ${chipsRow ? `<div class="run-chips-row">${chipsRow}</div>` : ''}
            ${run.cash_rationale ? `<p class="run-cash-note muted-cell">${sanitize(run.cash_rationale)}</p>` : ''}
            ${tokenMeta ? `<div class="run-meta">${tokenMeta}</div>` : ''}
          </div>
        </div>`;
    }).join('');

    // Expand / collapse on header click or keyboard
    list.querySelectorAll('.agent-run-header').forEach(header => {
      const toggle = () => {
        const card = header.closest('.agent-run');
        const expanded = card.classList.toggle('expanded');
        header.setAttribute('aria-expanded', expanded);
      };
      header.addEventListener('click', toggle);
      header.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
      });
    });

    // Copy-raw buttons on parse-error cards (stop propagation so header toggle doesn't fire)
    list.querySelectorAll('.copy-raw-btn').forEach(btn => {
      btn.addEventListener('click', e => {
        e.stopPropagation();
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

  // ── Search ────────────────────────────────────────────────
  setupSearch() {
    $('searchBtn')?.addEventListener('click',  () => this.openSearch());
    $('searchClose')?.addEventListener('click', () => this.closeSearch());
    $('searchBackdrop')?.addEventListener('click', () => this.closeSearch());

    const input = $('searchInput');
    if (input) {
      input.addEventListener('input', () => {
        clearTimeout(this.searchDebounce);
        this.searchDebounce = setTimeout(() => this.runSearch(input.value.trim()), SEARCH_DEBOUNCE_MS);
      });
    }

    document.addEventListener('keydown', e => {
      if (e.key === 'Escape') {
        if (!$('searchOverlay')?.hidden) { this.closeSearch(); return; }
        if (!$('symbolScreen')?.hidden)  { this.closeSymbolScreen(); return; }
        if (!$('tradeModal')?.hidden)    { this.closeTradeTicket(); }
      }
    });
  }

  openSearch() {
    const overlay = $('searchOverlay');
    if (!overlay) return;
    overlay.hidden = false;
    $('searchResults').innerHTML = '';
    requestAnimationFrame(() => $('searchInput')?.focus());
  }

  closeSearch() {
    const overlay = $('searchOverlay');
    if (!overlay) return;
    overlay.hidden = true;
    if ($('searchInput')) $('searchInput').value = '';
  }

  async runSearch(q) {
    const results = $('searchResults');
    if (!results) return;

    if (!q) {
      results.innerHTML = '<p class="search-hint">Type a symbol or company name…</p>';
      return;
    }

    results.innerHTML = Array.from({ length: 4 }, () =>
      '<div class="search-result-skeleton"><div class="sk-block sk-sym-sm"></div><div class="sk-block sk-name-sm"></div></div>'
    ).join('');

    try {
      const res  = await fetch(`${API_BASE}/api/search?q=${encodeURIComponent(q)}`);
      const data = await res.json();

      if (!res.ok) throw new Error(data.error || 'Search failed');
      if (!data.length) {
        results.innerHTML = `<p class="search-empty">No results for "${sanitize(q)}"</p>`;
        return;
      }
      this.renderSearchResults(data);
    } catch (e) {
      results.innerHTML = `<p class="search-error">Search unavailable: ${sanitize(e.message)}</p>`;
    }
  }

  renderSearchResults(items) {
    const results = $('searchResults');
    if (!results) return;
    results.innerHTML = items.map(item => `
      <button class="search-result-item" data-symbol="${sanitize(item.symbol)}">
        <span class="search-result-sym">${sanitize(item.symbol)}</span>
        <span class="search-result-name">${sanitize(item.name)}</span>
        <span class="search-result-exch muted-cell">${sanitize(item.exchange)}</span>
      </button>`).join('');

    results.querySelectorAll('.search-result-item').forEach(btn => {
      btn.addEventListener('click', () => {
        const sym  = btn.dataset.symbol;
        const name = btn.querySelector('.search-result-name')?.textContent || '';
        this.closeSearch();
        this.openSymbolScreen(sym, name);
      });
    });
  }

  // ── Symbol Screen ─────────────────────────────────────────
  openSymbolScreen(sym, name = '') {
    this.symbolScreenSym = sym;
    $('symbolScreenSym').textContent  = sym;
    $('symbolScreenName').textContent = name || sym;
    $('symbolScreen').hidden          = false;
    $('mainContent').hidden           = true;
    $('tabBar').hidden                = true;

    // Reset price block to skeleton state
    $('symbolPriceSkeleton').hidden = false;
    $('symbolPriceBlock').hidden    = true;
    $('symbolBidAsk').textContent   = '';
    $('symbolMarketStatus').textContent = '';
    ['statOpen','statHigh','statLow','statVolume','statBid','statAsk']
      .forEach(id => { const el = $(id); if (el) el.textContent = '—'; });

    // Show chart skeleton
    $('symbolChartSkeleton').hidden = false;
    const canvas = $('symbolChart');
    if (canvas) canvas.hidden = true;

    // Wire back button and trade button
    $('symbolBackBtn').onclick  = () => this.closeSymbolScreen();
    $('symbolTradeBtn').onclick = () => this.showTradeTicket({
      symbol: sym, name: $('symbolScreenName').textContent,
      side: 'buy', currentPrice: this.tradeState.currentPrice || 0,
    });

    // Wire timeframe buttons
    this.setupTimeframeBtns(sym);

    // Load quote + 1Y bars in parallel
    Promise.all([
      this.loadSymbolQuote(sym),
      this.loadSymbolBars(sym, '1Y'),
    ]).catch(() => {});
  }

  closeSymbolScreen() {
    $('symbolScreen').hidden = true;
    $('mainContent').hidden  = false;
    $('tabBar').hidden       = false;
    this.symbolScreenSym     = null;
    if (this.symbolChart) { this.symbolChart.destroy(); this.symbolChart = null; }
  }

  setupTimeframeBtns(sym) {
    document.querySelectorAll('.tf-btn').forEach(btn => {
      btn.onclick = () => {
        document.querySelectorAll('.tf-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        this.loadSymbolBars(sym, btn.dataset.tf);
      };
    });
  }

  async loadSymbolQuote(sym) {
    try {
      const res  = await fetch(`${API_BASE}/api/quote?symbols=${encodeURIComponent(sym)}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Quote failed');

      const q = data[sym];
      if (!q) return;

      // Cache current price for trade ticket pre-fill
      this.tradeState.currentPrice = q.price;

      // Populate price hero
      $('symbolPriceSkeleton').hidden = true;
      $('symbolPriceBlock').hidden    = false;

      const lastEl = $('symbolLastPrice');
      if (lastEl) lastEl.textContent = fmt$(q.price);

      const chgEl = $('symbolChange');
      if (chgEl) {
        const sign = q.change >= 0 ? '+' : '';
        chgEl.textContent = `${sign}${fmt$(q.change)} (${sign}${fmtPct(q.changePct)})`;
        chgEl.className   = `symbol-change-badge ${q.change >= 0 ? 'profit' : 'loss'}`;
      }

      const baEl = $('symbolBidAsk');
      if (baEl && q.bid && q.ask) {
        baEl.textContent = `Bid ${fmt$(q.bid)}  ·  Ask ${fmt$(q.ask)}`;
      }

      const msEl = $('symbolMarketStatus');
      if (msEl) {
        const live = isMarketHours();
        msEl.textContent = live ? '● Market Open · 15-min delayed (IEX)' : '○ Market Closed';
        msEl.className   = `symbol-market-status ${live ? 'open' : 'closed'}`;
      }

      // Populate stat grid
      const setS = (id, val) => { const el = $(id); if (el) el.textContent = val; };
      setS('statOpen',   fmt$(q.open));
      setS('statHigh',   fmt$(q.high));
      setS('statLow',    fmt$(q.low));
      setS('statVolume', q.volume ? q.volume.toLocaleString() : '—');
      setS('statBid',    q.bid ? fmt$(q.bid) : '—');
      setS('statAsk',    q.ask ? fmt$(q.ask) : '—');

      // Update symbol screen name if not already set from search
      if ($('symbolScreenName').textContent === sym) {
        $('symbolScreenName').textContent = sym;
      }
    } catch (e) {
      console.warn('[TQ] Quote error:', e.message);
    }
  }

  async loadSymbolBars(sym, timeframe) {
    const skelEl  = $('symbolChartSkeleton');
    const canvas  = $('symbolChart');
    if (skelEl) skelEl.hidden = false;
    if (canvas) canvas.hidden = true;

    try {
      const res  = await fetch(`${API_BASE}/api/bars?symbol=${encodeURIComponent(sym)}&timeframe=${timeframe}`);
      const bars = await res.json();
      if (!res.ok) throw new Error(bars.error || 'Bars failed');

      if (this.symbolChart) { this.symbolChart.destroy(); this.symbolChart = null; }

      if (skelEl) skelEl.hidden = true;
      if (canvas) canvas.hidden = false;

      const ctx      = canvas.getContext('2d');
      const gradient = ctx.createLinearGradient(0, 0, 0, 220);
      gradient.addColorStop(0, 'rgba(212,175,55,0.18)');
      gradient.addColorStop(1, 'rgba(212,175,55,0)');

      const labels = bars.map(b => {
        const d = new Date(b.t);
        if (timeframe === '1D' || timeframe === '1W' || timeframe === '1M') {
          return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
        }
        return d.toLocaleDateString('en-US', { month: 'short', year: '2-digit' });
      });

      this.symbolChart = new Chart(ctx, {
        type: 'line',
        data: {
          labels,
          datasets: [{
            data:              bars.map(b => b.c),
            borderColor:       '#D4AF37',
            borderWidth:       1.8,
            backgroundColor:   gradient,
            fill:              true,
            tension:           0.3,
            pointRadius:       0,
            pointHoverRadius:  4,
            pointHoverBackgroundColor: '#D4AF37',
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
              callbacks: { label: c => `  ${fmt$(c.raw)}` },
            },
          },
          scales: {
            x: {
              grid:   { color: 'rgba(255,255,255,0.03)' },
              ticks:  { color: '#555', maxTicksLimit: 7, font: { size: 10 } },
              border: { color: '#1E1E1E' },
            },
            y: {
              position: 'right',
              grid:   { color: 'rgba(255,255,255,0.03)' },
              ticks:  { color: '#555', font: { size: 10 }, callback: v => '$' + v.toFixed(0) },
              border: { color: '#1E1E1E' },
            },
          },
        },
      });
    } catch (e) {
      if (skelEl) skelEl.hidden = true;
      if (canvas) { canvas.hidden = false; }
      console.warn('[TQ] Bars error:', e.message);
    }
  }

  // ── Trade Ticket ──────────────────────────────────────────
  setupTradeTicketListeners() {
    // Side toggle
    $('tradeSideBuy')?.addEventListener('click',  () => this._setTradeSide('buy'));
    $('tradeSideSell')?.addEventListener('click', () => this._setTradeSide('sell'));

    // Order type
    document.querySelectorAll('.type-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.type-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        this.tradeState.type = btn.dataset.type;
        $('tradeFieldLimitPrice').hidden = this.tradeState.type !== 'limit';
        $('tradeFieldStopPrice').hidden  = this.tradeState.type !== 'stop';
        this.updateTradePreview();
      });
    });

    // TIF
    document.querySelectorAll('.tif-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.tif-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        this.tradeState.tif = btn.dataset.tif;
      });
    });

    // Inputs
    $('tradeQty')?.addEventListener('input', () => this.updateTradePreview());
    $('tradeLimitPrice')?.addEventListener('input', () => this.updateTradePreview());
    $('tradeStopPrice')?.addEventListener('input', () => this.updateTradePreview());

    // Navigation
    $('tradeReviewBtn')?.addEventListener('click',  () => this.reviewOrder());
    $('tradeSubmitBtn')?.addEventListener('click',  () => this.submitOrder());
    $('tradeBackBtn')?.addEventListener('click',    () => this._showTradeStep(1));

    // Close buttons
    ['tradeClose','tradeClose2','tradeCancelBtn','tradeReviewCancelBtn'].forEach(id => {
      $(id)?.addEventListener('click', () => this.closeTradeTicket());
    });
    $('tradeModalBackdrop')?.addEventListener('click', () => this.closeTradeTicket());
  }

  _setTradeSide(side) {
    this.tradeState.side = side;
    $('tradeSideBuy').classList.toggle('active',  side === 'buy');
    $('tradeSideSell').classList.toggle('active', side === 'sell');
    this.updateTradePreview();
  }

  _showTradeStep(n) {
    $('tradeStep1').hidden = n !== 1;
    $('tradeStep2').hidden = n !== 2;
  }

  async showTradeTicket({ symbol, name = '', side = 'buy', qty = null, currentPrice = null }) {
    this.tradeState.symbol       = symbol;
    this.tradeState.name         = name;
    this.tradeState.side         = side;
    this.tradeState.type         = 'market';
    this.tradeState.tif          = 'day';
    this.tradeState.limitPrice   = null;
    this.tradeState.stopPrice    = null;
    this.tradeState.currentPrice = currentPrice || 0;
    this.tradeState.buyingPower  = 0;

    // Reset form UI
    $('tradeSym').textContent  = symbol;
    $('tradeName').textContent = name || symbol;
    $('tradeQty').value        = qty !== null ? qty : '';
    $('tradeLimitPrice').value = '';
    $('tradeStopPrice').value  = '';
    $('tradeFieldLimitPrice').hidden = true;
    $('tradeFieldStopPrice').hidden  = true;
    $('tradeValidation').hidden      = true;
    $('tradeSubmitResult').hidden    = true;

    document.querySelectorAll('.type-btn').forEach(b =>
      b.classList.toggle('active', b.dataset.type === 'market'));
    document.querySelectorAll('.tif-btn').forEach(b =>
      b.classList.toggle('active', b.dataset.tif === 'day'));

    this._setTradeSide(side);
    this._showTradeStep(1);
    $('tradeModal').hidden = false;

    // Fetch live price + buying power in parallel (non-blocking)
    const fetchPrice = currentPrice
      ? Promise.resolve()
      : fetch(`${API_BASE}/api/quote?symbols=${encodeURIComponent(symbol)}`)
          .then(r => r.json())
          .then(d => { if (d[symbol]) this.tradeState.currentPrice = d[symbol].price; })
          .catch(() => {});

    const fetchAccount = fetch(`${API_BASE}/api/account`)
      .then(r => r.json())
      .then(d => { this.tradeState.buyingPower = d.buyingPower || 0; })
      .catch(() => { this.tradeState.buyingPower = this.data?.summary?.cash || 0; });

    await Promise.all([fetchPrice, fetchAccount]);
    this.updateTradePreview();
    requestAnimationFrame(() => $('tradeQty')?.focus());
  }

  updateTradePreview() {
    const ts     = this.tradeState;
    const qty    = parseInt($('tradeQty')?.value || '0', 10);
    const lp     = parseFloat($('tradeLimitPrice')?.value || '0');
    const sp     = parseFloat($('tradeStopPrice')?.value || '0');
    const price  = ts.type === 'limit' ? lp : ts.type === 'stop' ? sp : ts.currentPrice;
    const bp     = ts.buyingPower;
    const validEl = $('tradeValidation');
    const revBtn  = $('tradeReviewBtn');

    let err = '';
    if (!qty || qty <= 0 || !Number.isInteger(qty))   err = 'Shares must be a positive whole number.';
    else if (ts.type === 'limit' && (!lp || lp <= 0)) err = 'Limit price is required.';
    else if (ts.type === 'stop'  && (!sp || sp <= 0)) err = 'Stop price is required.';
    else if (ts.side === 'buy' && bp > 0 && qty * price > bp)
      err = `Exceeds buying power. Max ≈ ${Math.floor(bp / (price || 1))} shares.`;

    if (validEl) { validEl.textContent = err; validEl.hidden = !err; }
    if (revBtn)  revBtn.disabled = !!err;

    // Update preview rows
    const estCost = qty && price ? qty * price : 0;
    const bpAfter = ts.side === 'buy' ? bp - estCost : bp + estCost;

    const setP = (id, val) => { const el = $(id); if (el) el.textContent = val; };
    setP('previewCost',    estCost ? `${ts.side === 'sell' ? '+' : '-'}${fmt$(estCost)}` : '—');
    setP('previewBP',      bp  ? fmt$(bp)     : '—');
    setP('previewBPAfter', bp  ? fmt$(bpAfter) : '—');
  }

  reviewOrder() {
    const ts      = this.tradeState;
    const qty     = parseInt($('tradeQty')?.value || '0', 10);
    const lp      = parseFloat($('tradeLimitPrice')?.value || '0');
    const sp      = parseFloat($('tradeStopPrice')?.value || '0');
    const price   = ts.type === 'limit' ? lp : ts.type === 'stop' ? sp : ts.currentPrice;
    const estCost = qty * price;

    const typeLabel = ts.type === 'market' ? 'Market'
                    : ts.type === 'limit'  ? `Limit @ ${fmt$(lp)}`
                    : `Stop @ ${fmt$(sp)}`;

    $('tradeReviewSummary').innerHTML = `
      <div class="review-line">
        <strong>${ts.side === 'buy' ? 'BUY' : 'SELL'}</strong>
        ${sanitize(String(qty))} shares of <strong>${sanitize(ts.symbol)}</strong>
      </div>
      <div class="review-detail-grid">
        <span class="muted-cell">Order type</span><span>${sanitize(typeLabel)}</span>
        <span class="muted-cell">Time in force</span><span>${ts.tif.toUpperCase()}</span>
        <span class="muted-cell">${ts.side === 'buy' ? 'Est. cost' : 'Est. proceeds'}</span>
        <span>${estCost ? fmt$(estCost) : 'Market price'}</span>
      </div>`;

    this._showTradeStep(2);
    $('tradeSubmitResult').hidden = true;
  }

  async submitOrder() {
    const ts     = this.tradeState;
    const qty    = parseInt($('tradeQty')?.value || '0', 10);
    const lp     = parseFloat($('tradeLimitPrice')?.value || '0');
    const sp     = parseFloat($('tradeStopPrice')?.value || '0');
    const btn    = $('tradeSubmitBtn');
    const result = $('tradeSubmitResult');

    if (btn) { btn.disabled = true; btn.textContent = 'Submitting…'; }

    const body = {
      symbol:         ts.symbol,
      qty,
      side:           ts.side,
      type:           ts.type,
      time_in_force:  ts.tif,
    };
    if (ts.type === 'limit') body.limit_price = lp;
    if (ts.type === 'stop')  body.stop_price  = sp;

    try {
      const res  = await fetch(`${API_BASE}/api/orders`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(body),
      });
      const data = await res.json();

      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);

      if (result) {
        result.hidden = false;
        result.innerHTML = `<span class="submit-success">✓ Order submitted — ID ${sanitize(data.id || '')}</span>`;
      }
      if (btn) { btn.textContent = 'Done'; btn.onclick = () => { this.closeTradeTicket(); this.loadOrdersIfStale(); }; }
    } catch (e) {
      if (result) {
        result.hidden = false;
        result.innerHTML = `<span class="submit-error">✗ ${sanitize(e.message)}</span>`;
      }
      if (btn) { btn.disabled = false; btn.textContent = 'Submit Order'; }
    }
  }

  closeTradeTicket() {
    const modal = $('tradeModal');
    if (modal) modal.hidden = true;
    this._showTradeStep(1);
    $('tradeSubmitResult').hidden = true;
    const btn = $('tradeSubmitBtn');
    if (btn) { btn.disabled = false; btn.textContent = 'Submit Order'; btn.onclick = null; }
  }

  // ── Orders Tab ────────────────────────────────────────────
  async loadOrdersIfStale() {
    this.renderOrderSkeletons('openOrdersList', 5);
    this.renderOrderSkeletons('closedOrdersList', 3);

    try {
      const [openRes, closedRes] = await Promise.all([
        fetch(`${API_BASE}/api/orders?status=open`),
        fetch(`${API_BASE}/api/orders?status=closed`),
      ]);
      const [openData, closedData] = await Promise.all([
        openRes.ok   ? openRes.json()   : [],
        closedRes.ok ? closedRes.json() : [],
      ]);
      this.ordersData = { open: openData, closed: closedData };
    } catch (e) {
      this.ordersData = { open: [], closed: [] };
    }

    this.renderOrders();

    // Wire filter buttons
    document.querySelectorAll('.orders-filter-btn').forEach(btn => {
      btn.onclick = () => {
        document.querySelectorAll('.orders-filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        this.ordersFilter = btn.dataset.status;
        this.renderOrders();
      };
    });

    // Wire symbol filter
    const symInput = $('ordersSymbolInput');
    if (symInput) {
      symInput.addEventListener('input', () => {
        this.ordersSymFilter = symInput.value.trim().toUpperCase();
        this.renderOrders();
      });
    }
  }

  renderOrders() {
    const { open, closed } = this.ordersData;

    const openSec   = $('openOrdersSection');
    const closedSec = $('closedOrdersSection');

    if (this.ordersFilter === 'open') {
      if (openSec)   openSec.hidden   = false;
      if (closedSec) closedSec.hidden = true;
    } else {
      if (openSec)   openSec.hidden   = true;
      if (closedSec) closedSec.hidden = false;
    }

    const sym = this.ordersSymFilter;

    const filterFn = o => !sym || (o.symbol || '').toUpperCase().includes(sym);

    const openFiltered   = (open   || []).filter(filterFn);
    const closedFiltered = (closed || []).filter(filterFn);

    const countEl = $('openOrdersCount');
    if (countEl) countEl.textContent = openFiltered.length || '';

    this._renderOrderList('openOrdersList',   openFiltered,   true);
    this._renderOrderList('closedOrdersList',  closedFiltered, false);

    // Activity feed — merged timeline
    const all = [...(open || []), ...(closed || [])]
      .filter(filterFn)
      .sort((a, b) => new Date(b.submittedAt) - new Date(a.submittedAt));
    this._renderActivityFeed(all);
  }

  _renderOrderList(containerId, orders, showCancel) {
    const el = $(containerId);
    if (!el) return;

    if (!orders.length) {
      el.innerHTML = `<div class="orders-empty muted-cell">${showCancel ? 'No open orders' : 'No order history'}</div>`;
      return;
    }

    el.innerHTML = orders.map(o => {
      const sym      = sanitize(o.symbol || '');
      const side     = sanitize(o.side   || '');
      const type     = sanitize(o.type   || '');
      const qty      = sanitize(o.qty    || '');
      const filled   = sanitize(o.filledQty || '0');
      const status   = sanitize(o.status || '');
      const lp       = o.limitPrice ? fmt$(o.limitPrice) : '';
      const sp       = o.stopPrice  ? fmt$(o.stopPrice)  : '';
      const priceStr = lp ? `@ ${lp}` : sp ? `stop ${sp}` : 'market';
      const when     = timeAgo(o.submittedAt);
      const fillInfo = o.filledAvgPrice
        ? `<span class="muted-cell">Filled ${fmt$(o.filledAvgPrice)} × ${filled}</span>` : '';
      const cancelBtn = showCancel
        ? `<button class="order-cancel-btn" data-cancel-order="${sanitize(o.id)}">Cancel</button>` : '';

      return `
        <div class="order-card" data-order-id="${sanitize(o.id)}">
          <div class="order-card-main">
            <span class="order-side-badge ${side}">${side.toUpperCase()}</span>
            <span class="order-sym">${sym}</span>
            <span class="order-qty">${qty} sh</span>
            <span class="order-type muted-cell">${type} ${priceStr}</span>
          </div>
          <div class="order-card-meta">
            <span class="order-status-badge status-${status}">${status}</span>
            ${fillInfo}
            <span class="muted-cell order-when">${when}</span>
            ${cancelBtn}
          </div>
        </div>`;
    }).join('');

    // Wire cancel buttons via event delegation
    el.querySelectorAll('[data-cancel-order]').forEach(btn => {
      btn.addEventListener('click', () => this.cancelOrder(btn.dataset.cancelOrder, btn));
    });
  }

  _renderActivityFeed(orders) {
    const feed = $('activityFeed');
    if (!feed) return;

    if (!orders.length) {
      feed.innerHTML = `<div class="muted-cell activity-empty">No activity yet.</div>`;
      return;
    }

    const dotColor = s => {
      if (s === 'filled')   return 'dot-green';
      if (['canceled','cancelled','rejected','expired'].includes(s)) return 'dot-red';
      return 'dot-gold';
    };

    feed.innerHTML = orders.map(o => {
      const s   = sanitize(o.status || '');
      const sym = sanitize(o.symbol || '');
      const side = sanitize(o.side  || '');
      const qty  = sanitize(o.qty   || '');
      return `
        <div class="activity-entry">
          <div class="activity-dot ${dotColor(o.status)}"></div>
          <div class="activity-body">
            <span class="order-side-badge ${side} small">${side.toUpperCase()}</span>
            <span class="activity-sym">${sym}</span>
            <span class="muted-cell activity-qty">${qty} sh</span>
            <span class="order-status-badge status-${s}">${s}</span>
            <span class="muted-cell activity-when">${timeAgo(o.submittedAt)}</span>
          </div>
        </div>`;
    }).join('');
  }

  async cancelOrder(orderId, btn) {
    if (!orderId) return;
    if (btn) { btn.disabled = true; btn.textContent = 'Cancelling…'; }

    try {
      const res  = await fetch(`${API_BASE}/api/orders?id=${encodeURIComponent(orderId)}`, { method: 'DELETE' });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Cancel failed');

      // Remove from DOM immediately, then refresh
      const card = document.querySelector(`.order-card[data-order-id="${CSS.escape(orderId)}"]`);
      if (card) card.remove();

      const countEl = $('openOrdersCount');
      if (countEl) {
        const current = parseInt(countEl.textContent || '0', 10);
        if (current > 1) countEl.textContent = current - 1; else countEl.textContent = '';
      }

      // Refresh orders list after short delay
      setTimeout(() => this.loadOrdersIfStale(), 800);
    } catch (e) {
      if (btn) { btn.disabled = false; btn.textContent = 'Cancel'; }
      console.warn('[TQ] Cancel order error:', e.message);
    }
  }

  renderOrderSkeletons(containerId, n = 5) {
    const el = $(containerId);
    if (!el) return;
    el.innerHTML = Array.from({ length: n }, () => `
      <div class="order-card order-card-skeleton">
        <div class="order-card-main">
          <div class="sk-block sk-badge-sm"></div>
          <div class="sk-block sk-sym-sm"></div>
          <div class="sk-block sk-name-sm"></div>
        </div>
      </div>`).join('');
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

    // Chip list: when a sentiment filter is active, only show tickers in that filtered set
    const chipSource = this.newsFilter !== 'ALL'
      ? articles.filter(a => a.sentiment === this.newsFilter)
      : articles;

    // Compute dominant sentiment per ticker from chipSource (matches the active filter view)
    const sentBySymbol = {};
    chipSource.forEach(a => {
      (a.symbols || []).forEach(sym => {
        if (!sentBySymbol[sym]) sentBySymbol[sym] = { bull: 0, bear: 0, neutral: 0 };
        sentBySymbol[sym][a.sentiment] = (sentBySymbol[sym][a.sentiment] || 0) + 1;
      });
    });
    const dominantSent = sym => {
      const c = sentBySymbol[sym] || {};
      if ((c.bull || 0) >= (c.bear || 0) && (c.bull || 0) >= (c.neutral || 0)) return 'bull';
      if ((c.bear || 0) >= (c.neutral || 0)) return 'bear';
      return 'neutral';
    };
    const allSymbols = [...new Set(chipSource.flatMap(a => a.symbols || []))].sort();
    const chipsEl    = $('newsTickerChips');
    if (chipsEl) {
      chipsEl.innerHTML = allSymbols.map(sym => {
        const s      = sanitize(sym);
        const sent   = dominantSent(sym);
        const active = this.newsTickerFilter === sym ? ' active' : '';
        return `<button class="ticker-chip ${sent}${active}" data-sym="${s}">${s}</button>`;
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
      const safeUrl = a.url && /^https?:\/\//i.test(a.url) ? sanitize(a.url) : '';
      const url     = safeUrl ? `href="${safeUrl}" target="_blank" rel="noopener noreferrer"` : '';
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
