/**
 * /api/orders — Alpaca paper order management
 *
 * GET    /api/orders?status=open|closed  — list orders
 * POST   /api/orders                     — place a paper order
 * DELETE /api/orders?id={orderId}        — cancel an open order
 *
 * Secrets: read from process.env — never exposed to client.
 */

'use strict';

const { alpacaFetch, handleOptions, sendError } = require('./_alpaca');

// ── Shape transformer ───────────────────────────────────────────

function shapeOrder(o) {
  return {
    id:             o.id,
    symbol:         o.symbol,
    side:           o.side,
    type:           o.type,
    qty:            o.qty,
    filledQty:      o.filled_qty          || '0',
    limitPrice:     o.limit_price         || null,
    stopPrice:      o.stop_price          || null,
    status:         o.status,
    tif:            o.time_in_force,
    submittedAt:    o.submitted_at        || null,
    filledAt:       o.filled_at           || null,
    filledAvgPrice: o.filled_avg_price    || null,
  };
}

// ── GET ─────────────────────────────────────────────────────────

async function getOrders(req, res) {
  const status = (req.query.status || 'open').toLowerCase();
  if (!['open', 'closed', 'all'].includes(status)) {
    return sendError(res, 400, 'status must be open, closed, or all');
  }

  const upstream = await alpacaFetch(
    `orders?status=${status}&limit=100&direction=desc`
  );

  if (!upstream.ok) {
    const body = await upstream.text();
    return sendError(res, upstream.status, `Alpaca error: ${body}`);
  }

  const raw     = await upstream.json();
  const orders  = (Array.isArray(raw) ? raw : []).map(shapeOrder);
  res.status(200).json(orders);
}

// ── POST ────────────────────────────────────────────────────────

async function placeOrder(req, res) {
  const body = req.body || {};

  // Validate required fields
  const symbol = (body.symbol || '').trim().toUpperCase();
  const qty    = parseInt(body.qty, 10);
  const side   = (body.side || '').toLowerCase();
  const type   = (body.type || '').toLowerCase();
  const tif    = (body.time_in_force || 'day').toLowerCase();

  if (!symbol)                         return sendError(res, 400, 'symbol is required');
  if (!qty || qty <= 0)                return sendError(res, 400, 'qty must be a positive integer');
  if (!['buy','sell'].includes(side))  return sendError(res, 400, 'side must be buy or sell');
  if (!['market','limit','stop'].includes(type))
                                       return sendError(res, 400, 'type must be market, limit, or stop');
  if (!['day','gtc'].includes(tif))    return sendError(res, 400, 'time_in_force must be day or gtc');
  if ((type === 'limit') && !body.limit_price)
                                       return sendError(res, 400, 'limit_price is required for limit orders');
  if ((type === 'stop') && !body.stop_price)
                                       return sendError(res, 400, 'stop_price is required for stop orders');

  const orderBody = {
    symbol,
    qty:            String(qty),
    side,
    type,
    time_in_force:  tif,
  };
  if (body.limit_price) orderBody.limit_price = String(parseFloat(body.limit_price).toFixed(2));
  if (body.stop_price)  orderBody.stop_price  = String(parseFloat(body.stop_price).toFixed(2));

  const upstream = await alpacaFetch('orders', {
    method:  'POST',
    body:    JSON.stringify(orderBody),
  });

  if (!upstream.ok) {
    const errBody = await upstream.text();
    return sendError(res, upstream.status, `Alpaca error: ${errBody}`);
  }

  const raw = await upstream.json();
  res.status(201).json(shapeOrder(raw));
}

// ── DELETE ──────────────────────────────────────────────────────

async function cancelOrder(req, res) {
  const id = (req.query.id || '').trim();
  if (!id) return sendError(res, 400, 'id query param is required');

  const upstream = await alpacaFetch(`orders/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  });

  // Alpaca returns 204 No Content on success
  if (upstream.status === 204 || upstream.ok) {
    return res.status(200).json({ cancelled: true, id });
  }

  const body = await upstream.text();
  sendError(res, upstream.status, `Alpaca error: ${body}`);
}

// ── Handler ─────────────────────────────────────────────────────

module.exports = async function handler(req, res) {
  if (handleOptions(req, res)) return;

  try {
    switch (req.method) {
      case 'GET':    return await getOrders(req, res);
      case 'POST':   return await placeOrder(req, res);
      case 'DELETE': return await cancelOrder(req, res);
      default:       return sendError(res, 405, 'Method not allowed');
    }
  } catch (err) {
    const status = err.statusCode || 500;
    sendError(res, status, err.message || 'Internal server error');
  }
};
