/**
 * GET /api/quote?symbols=AAPL,TSLA
 *
 * Returns latest quotes for one or more symbols via Alpaca snapshots.
 * Note: feed=iex — free/paper tier, ~15-min delayed during market hours.
 *
 * Secrets: read from process.env — never exposed to client.
 */

'use strict';

const { alpacaDataFetch, handleOptions, sendError } = require('./_alpaca');

module.exports = async function handler(req, res) {
  if (handleOptions(req, res)) return;

  if (req.method !== 'GET') {
    return sendError(res, 405, 'Method not allowed');
  }

  const symbols = (req.query.symbols || '').trim().toUpperCase();
  if (!symbols) {
    return sendError(res, 400, 'symbols query param is required (e.g. ?symbols=AAPL,TSLA)');
  }

  try {
    const upstream = await alpacaDataFetch(
      `stocks/snapshots?symbols=${encodeURIComponent(symbols)}&feed=iex`
    );

    if (!upstream.ok) {
      const body = await upstream.text();
      return sendError(res, upstream.status, `Alpaca data error: ${body}`);
    }

    const raw = await upstream.json();

    // Transform each symbol snapshot into a clean quote shape
    const result = {};
    for (const [sym, snap] of Object.entries(raw)) {
      const lt  = snap.latestTrade   || {};
      const lq  = snap.latestQuote  || {};
      const db  = snap.dailyBar     || {};
      const pdb = snap.prevDailyBar || {};

      const price     = parseFloat(lt.p  || db.c) || 0;
      const prevClose = parseFloat(pdb.c)          || 0;
      const change    = prevClose ? +(price - prevClose).toFixed(4) : 0;
      const changePct = prevClose ? +((change / prevClose) * 100).toFixed(4) : 0;

      result[sym] = {
        price:     +price.toFixed(4),
        bid:       parseFloat(lq.bp) || 0,
        ask:       parseFloat(lq.ap) || 0,
        open:      parseFloat(db.o)  || 0,
        high:      parseFloat(db.h)  || 0,
        low:       parseFloat(db.l)  || 0,
        volume:    parseInt(db.v, 10)|| 0,
        prevClose: +prevClose.toFixed(4),
        change,
        changePct,
      };
    }

    res.status(200).json(result);
  } catch (err) {
    const status = err.statusCode || 500;
    sendError(res, status, err.message || 'Internal server error');
  }
};
