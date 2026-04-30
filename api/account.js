/**
 * GET /api/account
 * Returns live account summary from Alpaca paper trading.
 * Secrets: read from process.env — never exposed to client.
 */

'use strict';

const { alpacaFetch, handleOptions, sendError } = require('./_alpaca');

module.exports = async function handler(req, res) {
  if (handleOptions(req, res)) return;

  if (req.method !== 'GET') {
    return sendError(res, 405, 'Method not allowed');
  }

  try {
    const upstream = await alpacaFetch('account');

    if (!upstream.ok) {
      const body = await upstream.text();
      return sendError(res, upstream.status, `Alpaca error: ${body}`);
    }

    const raw = await upstream.json();

    // Return clean numeric shape — Alpaca returns cash/equity as strings
    res.status(200).json({
      equity:         parseFloat(raw.equity)         || 0,
      cash:           parseFloat(raw.cash)           || 0,
      buyingPower:    parseFloat(raw.buying_power)   || 0,
      portfolioValue: parseFloat(raw.portfolio_value)|| 0,
    });
  } catch (err) {
    const status = err.statusCode || 500;
    sendError(res, status, err.message || 'Internal server error');
  }
};
