/**
 * GET /api/bars?symbol=AAPL&timeframe=1Y
 *
 * Returns OHLCV price bars for charting.
 * Note: feed=iex — free/paper tier, ~15-min delayed.
 *
 * Timeframe shorthand → Alpaca mapping:
 *   1D  → 1Day,   limit=1
 *   1W  → 1Day,   limit=7
 *   1M  → 1Day,   limit=30
 *   3M  → 1Day,   limit=90
 *   1Y  → 1Day,   limit=365
 *   5Y  → 1Week,  limit=260
 *   MAX → 1Month, limit=120
 *
 * Secrets: read from process.env — never exposed to client.
 */

'use strict';

const { alpacaDataFetch, handleOptions, sendError } = require('./_alpaca');

const TF_MAP = {
  '1D':  { alpacaTf: '1Day',   limit: 1   },
  '1W':  { alpacaTf: '1Day',   limit: 7   },
  '1M':  { alpacaTf: '1Day',   limit: 30  },
  '3M':  { alpacaTf: '1Day',   limit: 90  },
  '1Y':  { alpacaTf: '1Day',   limit: 365 },
  '5Y':  { alpacaTf: '1Week',  limit: 260 },
  'MAX': { alpacaTf: '1Month', limit: 120 },
};

module.exports = async function handler(req, res) {
  if (handleOptions(req, res)) return;

  if (req.method !== 'GET') {
    return sendError(res, 405, 'Method not allowed');
  }

  const symbol    = (req.query.symbol    || '').trim().toUpperCase();
  const timeframe = (req.query.timeframe || '1Y').trim().toUpperCase();

  if (!symbol) {
    return sendError(res, 400, 'symbol query param is required (e.g. ?symbol=AAPL)');
  }

  const mapping = TF_MAP[timeframe];
  if (!mapping) {
    return sendError(res, 400, `Unknown timeframe "${timeframe}". Valid: ${Object.keys(TF_MAP).join(', ')}`);
  }

  const { alpacaTf, limit } = mapping;

  try {
    const path = `stocks/bars?symbols=${encodeURIComponent(symbol)}`
      + `&timeframe=${alpacaTf}&limit=${limit}&adjustment=raw&feed=iex&sort=asc`;

    const upstream = await alpacaDataFetch(path);

    if (!upstream.ok) {
      const body = await upstream.text();
      return sendError(res, upstream.status, `Alpaca data error: ${body}`);
    }

    const raw  = await upstream.json();
    const bars = (raw.bars && raw.bars[symbol]) || [];

    // Return a simplified array — truncate timestamp to date string for chart labels
    const result = bars.map(b => ({
      t: b.t ? b.t.slice(0, 10) : '',  // "2025-04-29"
      o: +(parseFloat(b.o).toFixed(4)),
      h: +(parseFloat(b.h).toFixed(4)),
      l: +(parseFloat(b.l).toFixed(4)),
      c: +(parseFloat(b.c).toFixed(4)),
      v: parseInt(b.v, 10) || 0,
    }));

    res.status(200).json(result);
  } catch (err) {
    const status = err.statusCode || 500;
    sendError(res, status, err.message || 'Internal server error');
  }
};
