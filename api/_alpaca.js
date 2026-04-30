/**
 * Shared Alpaca API helpers for TradeQuest-AI serverless functions.
 *
 * SECURITY: Secrets are read from process.env only — they never appear in
 * client-side code, localStorage, or the repository.
 */

'use strict';

/**
 * Abort with 403 if ALPACA_BASE_URL is not a paper-trading URL.
 * This is a hard guard that prevents accidental live-money trades.
 */
function requirePaper() {
  const url = process.env.ALPACA_BASE_URL || '';
  if (!url.includes('paper')) {
    const err = new Error('NOT_PAPER_URL');
    err.statusCode = 403;
    throw err;
  }
}

/**
 * Build the Alpaca auth headers from environment variables.
 * Called on every request so secrets are never cached in module scope.
 */
function authHeaders() {
  return {
    'APCA-API-KEY-ID':     process.env.ALPACA_API_KEY    || '',
    'APCA-API-SECRET-KEY': process.env.ALPACA_SECRET_KEY || '',
    'Content-Type':        'application/json',
  };
}

/**
 * Fetch from the Alpaca brokerage API (account, orders, assets…).
 * Base: process.env.ALPACA_BASE_URL/v2/{path}
 */
async function alpacaFetch(path, opts = {}) {
  requirePaper();
  const base = (process.env.ALPACA_BASE_URL || '').replace(/\/$/, '');
  const url  = `${base}/v2/${path}`;
  return fetch(url, {
    ...opts,
    headers: { ...authHeaders(), ...(opts.headers || {}) },
  });
}

/**
 * Fetch from the Alpaca Market Data API (quotes, bars, snapshots…).
 * Base: https://data.alpaca.markets/v2/{path}
 */
async function alpacaDataFetch(path, opts = {}) {
  requirePaper();
  const url = `https://data.alpaca.markets/v2/${path}`;
  return fetch(url, {
    ...opts,
    headers: { ...authHeaders(), ...(opts.headers || {}) },
  });
}

/**
 * Handle OPTIONS preflight — must be called at the top of every handler.
 * Returns true if the request was handled (caller should return immediately).
 */
function handleOptions(req, res) {
  if (req.method === 'OPTIONS') {
    res.status(200).end();
    return true;
  }
  return false;
}

/**
 * Send a JSON error response.
 */
function sendError(res, status, message) {
  res.status(status).json({ error: message, code: status });
}

module.exports = { requirePaper, alpacaFetch, alpacaDataFetch, handleOptions, sendError };
