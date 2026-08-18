# Evidence — Timeline (Apr 24 – Aug 10, 2026)

Source: `../data/portfolio.json` → `equity_curve[]` and `spy_curve[]`.

Both series start at $9,864 (inception). `gap` = portfolio − SPY.

```
date      portfolio      spy       gap    phase
Apr 24       9,864     9,864        +0    inception
May 19       9,717    10,138      -421    1. over-trading trough
Jun 22      10,699    10,311      +388    2. PEAK — ahead of SPY
Jul 1       10,389    10,330       +59    3. quarterly rebalance begins
Jul 10      10,525    10,458       +67       rebalance still grinding
Jul 23       9,937    10,225      -288       61% cash + JBL short
Aug 3       10,006    10,495      -489    4. frozen — ratchet + churn
Aug 10       9,534    10,724    -1,190       LAST DATA — outage begins
```

Peak **$10,699** (Jun 22) → last **$9,534** (Aug 10) = **-10.9%** from peak, while SPY rose $10,311 → $10,724.

## The four phases

**1. Over-trading (Apr 24 – May 19) — −$421.** 50 trades in 6 weeks against a ~35/yr target.
TSLA reached 39% of the book against an 8% cap. Churn without conviction.

**2. The signal works (May 20 – Jun 22) — +$388 ahead.** Positions ranked 1–10 averaged
+11.8%; ranks 11–18 averaged −4.2%. Stock selection was never the problem, at any point.

**3. The July blunder (Jul 1 – Jul 23) — −$288.** The quarterly rebalance needed ~15 legs but
`MAX_ORDERS_PER_RUN = 5` stretched it over 12 trading days, parking 44%→61% in cash through a
rising market. Simultaneously JBL trend-broke and was re-sold every run — `max(1, int(-22))`
returned 1, so Alpaca extended a naked short by a share a day to −22. Sector cap only warned,
so Financials reached 49%. Sizing was price-driven: corr(price, weight) = +0.68.

**4. Frozen (Jul 24 – Aug 17) — −$1,190 (−11.1pp).** v3.1 stopped the short growing but made
it unclosable. Net exposure fell to 20.6% (long $9,539 − short $7,571) while the broker
reported 79.4% cash. 100 SELL decisions vs 2 BUY across 90 runs. Then on Aug 10 the Anthropic
credit balance ran out, agent.py exited 1 as step 1 of market-open.yml, and the entire
pipeline stopped for 7 days with nobody alerted.
