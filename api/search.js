/**
 * GET /api/search?q=AAPL
 *
 * Symbol search via Alpaca assets endpoint.
 * Returns top 10 active, tradable US equity results.
 *
 * Secrets: read from process.env — never exposed to client.
 */

'use strict';

const { alpacaFetch, handleOptions, sendError } = require('./_alpaca');

module.exports = async function handler(req, res) {
  if (handleOptions(req, res)) return;

  if (req.method !== 'GET') {
    return sendError(res, 405, 'Method not allowed');
  }

  const q = (req.query.q || '').trim();
  if (!q) {
    return sendError(res, 400, 'q query param is required (e.g. ?q=AAPL)');
  }

  try {
    const upstream = await alpacaFetch(
      `assets?search=${encodeURIComponent(q)}&asset_class=us_equity&status=active`
    );

    if (!upstream.ok) {
      const body = await upstream.text();
      return sendError(res, upstream.status, `Alpaca error: ${body}`);
    }

    const raw = await upstream.json();

    // Filter to tradable only and cap at 10 results
    const results = (Array.isArray(raw) ? raw : [])
      .filter(a => a.tradable)
      .slice(0, 10)
      .map(a => ({
        symbol:       a.symbol,
        name:         a.name         || '',
        exchange:     a.exchange     || '',
        fractionable: a.fractionable || false,
      }));

    res.status(200).json(results);
  } catch (err) {
    const status = err.statusCode || 500;
    sendError(res, status, err.message || 'Internal server error');
  }
};
