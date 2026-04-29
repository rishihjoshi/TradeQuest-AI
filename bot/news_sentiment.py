#!/usr/bin/env python3
"""TradeQuest AI — News Sentiment Analyzer.

Fetches recent news for current holdings from Alpaca Markets News API,
runs Claude sentiment analysis on each article, writes data/news.json.

Security: ALPACA_API_KEY + ALPACA_SECRET_KEY consumed ONLY via env vars
          injected by GitHub Actions secrets. Never stored in repo.
          ANTHROPIC_API_KEY same. news.json contains zero credential fields.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import requests

REPO_ROOT  = Path(__file__).resolve().parent.parent
DATA_FILE  = REPO_ROOT / "data" / "portfolio.json"
NEWS_FILE  = REPO_ROOT / "data" / "news.json"

MODEL         = "claude-haiku-4-5-20251001"  # fast + cheap for 120-token per-article tasks
MAX_ARTICLES  = 30
MAX_TOKENS    = 120
SLEEP_BETWEEN = 0.5  # seconds between Claude calls to avoid burst throttling

ALPACA_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"

SENTIMENT_PROMPT = """Classify the sentiment of this financial news article for the mentioned stock(s).

Headline: {headline}
Summary: {summary}

Reply ONLY with valid JSON, no other text:
{{"sentiment": "bull|bear|neutral", "confidence": 0.0, "reason": "one sentence max 120 chars"}}"""


def _safe(text: str | None, max_len: int = 400) -> str:
    """Sanitise external news text before prompt injection — strips control and Markdown chars."""
    cleaned = (str(text) if text is not None else "")[:max_len]
    cleaned = cleaned.replace("\n", " ").replace("\r", " ")
    for ch in ("#", "*", "`", "\\"):
        cleaned = cleaned.replace(ch, "")
    return cleaned


def get_holdings() -> list[str]:
    if not DATA_FILE.exists():
        return []
    with open(DATA_FILE, encoding="utf-8") as f:
        portfolio = json.load(f)
    return [h["symbol"] for h in portfolio.get("holdings", []) if h.get("symbol")]


def fetch_alpaca_news(symbols: list[str]) -> list[dict]:
    api_key    = os.environ.get("ALPACA_API_KEY", "")
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
    if not api_key or not secret_key:
        raise RuntimeError(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set via GitHub Actions secrets."
        )

    params: dict = {"limit": MAX_ARTICLES, "sort": "desc"}
    if symbols:
        params["symbols"] = ",".join(symbols[:20])  # Alpaca API max symbols per call

    resp = requests.get(
        ALPACA_NEWS_URL,
        headers={
            "APCA-API-KEY-ID":     api_key,
            "APCA-API-SECRET-KEY": secret_key,
        },
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("news", [])


def analyze_sentiment(client: anthropic.Anthropic, headline: str, summary: str) -> dict:
    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{
                "role": "user",
                "content": SENTIMENT_PROMPT.format(
                    headline=_safe(headline, 200),
                    summary=_safe(summary, 400),
                ),
            }],
        )
        raw   = msg.content[0].text.strip()
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start < 0 or end <= start:
            raise ValueError("No JSON object found in response")
        result    = json.loads(raw[start:end])
        sentiment = result.get("sentiment", "neutral")
        if sentiment not in ("bull", "bear", "neutral"):
            sentiment = "neutral"
        return {
            "sentiment":  sentiment,
            "confidence": min(1.0, max(0.0, float(result.get("confidence", 0.5)))),
            "reason":     str(result.get("reason", ""))[:200],
        }
    except Exception as e:
        print(f"  Sentiment error: {e}", file=sys.stderr)
        return {"sentiment": "neutral", "confidence": 0.0, "reason": "Analysis unavailable"}


def main():
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        raise RuntimeError("ANTHROPIC_API_KEY must be set via GitHub Actions secrets.")

    client  = anthropic.Anthropic(api_key=anthropic_key)
    symbols = get_holdings()
    print(f"Holdings: {symbols or ['(market-wide — no holdings yet)']}")

    print("Fetching news from Alpaca Markets...")
    articles_raw = fetch_alpaca_news(symbols)
    print(f"Fetched {len(articles_raw)} articles — analyzing up to {MAX_ARTICLES}")

    articles: list[dict] = []
    for i, art in enumerate(articles_raw[:MAX_ARTICLES]):
        headline = art.get("headline", "")
        summary  = art.get("summary",  "")
        syms_raw = art.get("symbols",  [])

        print(f"[{i + 1}/{min(len(articles_raw), MAX_ARTICLES)}] {headline[:70]}...")
        sentiment_result = analyze_sentiment(client, headline, summary)

        if i < min(len(articles_raw), MAX_ARTICLES) - 1:
            time.sleep(SLEEP_BETWEEN)

        articles.append({
            "id":         str(art.get("id", i)),
            "headline":   _safe(headline, 200),
            "summary":    _safe(summary, 500),
            "url":        art.get("url",        ""),
            "author":     art.get("author",     ""),
            "source":     art.get("source",     ""),
            "symbols":    syms_raw,
            "created_at": art.get("created_at", ""),
            "sentiment":  sentiment_result["sentiment"],
            "confidence": sentiment_result["confidence"],
            "reason":     sentiment_result["reason"],
        })

    output = {
        "generated_at":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "article_count": len(articles),
        "articles":      articles,
    }

    NEWS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(NEWS_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    bull    = sum(1 for a in articles if a["sentiment"] == "bull")
    bear    = sum(1 for a in articles if a["sentiment"] == "bear")
    neutral = sum(1 for a in articles if a["sentiment"] == "neutral")

    print(f"\nSentiment: {bull} bull · {bear} bear · {neutral} neutral")
    print(f"Written  : {NEWS_FILE}")


if __name__ == "__main__":
    main()
