"""Tools the Analyst agent can call.

Two flavors:
1. **Market data** via yfinance — quote, company info, financials, news.
   yfinance is free and key-less, which makes the PoC frictionless.
   Swap for a paid feed (Polygon, FMP, Alpha Vantage) when you outgrow it.
2. **Thesis lookup** against the local JSON store.

Each tool is a `@tool`-decorated function with a Pydantic-typed argument
schema, so LangGraph's tool node can dispatch to it and the LLM gets a
clean schema in its system message.
"""

from __future__ import annotations

import logging
from typing import Any

import yfinance as yf
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from agent import plan_store

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Argument schemas
# --------------------------------------------------------------------------- #

class TickerArg(BaseModel):
    ticker: str = Field(description="Stock ticker symbol, e.g. 'GOOG' or 'MSFT'.")


class NewsArg(BaseModel):
    ticker: str = Field(description="Stock ticker symbol.")
    limit: int = Field(default=5, ge=1, le=20, description="Max headlines to return.")


class PriceHistoryArg(BaseModel):
    ticker: str = Field(description="Stock ticker symbol.")
    period: str = Field(
        default="1y",
        description="History window: '1mo'|'3mo'|'6mo'|'1y'|'2y'|'5y'|'max'.",
    )


# --------------------------------------------------------------------------- #
# Market data tools
# --------------------------------------------------------------------------- #

@tool("get_stock_quote", args_schema=TickerArg)
def get_stock_quote(ticker: str) -> dict[str, Any]:
    """Get the latest price and basic quote for a ticker.

    Returns last price, day change %, day range, 52-week range, and
    market cap. Use this whenever the user asks 'what's TICKER doing'.
    """
    try:
        t = yf.Ticker(ticker)
        info = t.fast_info
        # fast_info is the cheap, reliable accessor.
        return {
            "ticker": ticker.upper(),
            "last_price": float(info.last_price) if info.last_price else None,
            "previous_close": float(info.previous_close) if info.previous_close else None,
            "day_high": float(info.day_high) if info.day_high else None,
            "day_low": float(info.day_low) if info.day_low else None,
            "year_high": float(info.year_high) if info.year_high else None,
            "year_low": float(info.year_low) if info.year_low else None,
            "market_cap": float(info.market_cap) if info.market_cap else None,
            "currency": info.currency,
        }
    except Exception as e:
        log.exception("get_stock_quote failed")
        return {"error": f"Could not fetch quote for {ticker}: {e}"}


@tool("get_company_info", args_schema=TickerArg)
def get_company_info(ticker: str) -> dict[str, Any]:
    """Get company overview: name, sector, industry, business summary.

    Useful when the user asks "what does TICKER do" or you need sector
    context for comparison.
    """
    try:
        info = yf.Ticker(ticker).info or {}
        return {
            "ticker": ticker.upper(),
            "name": info.get("longName") or info.get("shortName"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "country": info.get("country"),
            "website": info.get("website"),
            "summary": info.get("longBusinessSummary"),
            "employees": info.get("fullTimeEmployees"),
        }
    except Exception as e:
        log.exception("get_company_info failed")
        return {"error": f"Could not fetch company info for {ticker}: {e}"}


@tool("get_financials_snapshot", args_schema=TickerArg)
def get_financials_snapshot(ticker: str) -> dict[str, Any]:
    """Get a snapshot of the most recent annual income statement and
    a few key valuation ratios.

    Returns revenue, gross profit, operating income, net income, EPS,
    plus PE / forward PE / profit margin / debt-to-equity if available.
    Use when the user asks about fundamentals.
    """
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
        # Income statement: take the most recent column.
        income = t.financials  # DataFrame: rows = lines, cols = years
        latest_col = income.columns[0] if not income.empty else None

        def _row(name: str) -> float | None:
            if latest_col is None or name not in income.index:
                return None
            v = income.at[name, latest_col]
            return float(v) if v is not None and v == v else None  # NaN check

        return {
            "ticker": ticker.upper(),
            "as_of": str(latest_col.date()) if latest_col is not None else None,
            "income_statement": {
                "total_revenue": _row("Total Revenue"),
                "gross_profit": _row("Gross Profit"),
                "operating_income": _row("Operating Income"),
                "net_income": _row("Net Income"),
            },
            "ratios": {
                "trailing_pe": info.get("trailingPE"),
                "forward_pe": info.get("forwardPE"),
                "profit_margin": info.get("profitMargins"),
                "debt_to_equity": info.get("debtToEquity"),
                "return_on_equity": info.get("returnOnEquity"),
            },
        }
    except Exception as e:
        log.exception("get_financials_snapshot failed")
        return {"error": f"Could not fetch financials for {ticker}: {e}"}


@tool("get_price_history", args_schema=PriceHistoryArg)
def get_price_history(ticker: str, period: str = "1y") -> dict[str, Any]:
    """Daily closes over a window, with summary stats.

    Returns: {ticker, period, start, end, closes:[{date,close}], summary:
    {start_price, end_price, return_pct, max, min, max_drawdown_pct,
    max_runup_pct}}. Use for back-testing or retrospective analysis.
    The closes list is downsampled to ~80 points so the payload stays small.
    """
    try:
        t = yf.Ticker(ticker)
        df = t.history(period=period)
        if df is None or df.empty:
            return {"error": f"No history for {ticker} ({period})"}

        # Build full closes list, then downsample to ~80 points for compact payloads.
        closes_full = [
            {"date": idx.strftime("%Y-%m-%d"), "close": float(row["Close"])}
            for idx, row in df.iterrows()
            if row["Close"] == row["Close"]  # NaN guard
        ]
        if len(closes_full) > 80:
            step = max(1, len(closes_full) // 80)
            closes = closes_full[::step]
            # Always include the most recent point.
            if closes[-1]["date"] != closes_full[-1]["date"]:
                closes.append(closes_full[-1])
        else:
            closes = closes_full

        prices = [p["close"] for p in closes_full]
        start_price = prices[0]
        end_price = prices[-1]

        # Max drawdown: largest peak-to-trough drop along the path.
        peak = prices[0]
        max_dd = 0.0
        for p in prices:
            if p > peak:
                peak = p
            dd = (p - peak) / peak * 100 if peak else 0.0
            if dd < max_dd:
                max_dd = dd

        # Max runup: largest trough-to-peak gain.
        trough = prices[0]
        max_ru = 0.0
        for p in prices:
            if p < trough:
                trough = p
            ru = (p - trough) / trough * 100 if trough else 0.0
            if ru > max_ru:
                max_ru = ru

        return {
            "ticker": ticker.upper(),
            "period": period,
            "start": closes_full[0]["date"],
            "end": closes_full[-1]["date"],
            "closes": closes,
            "summary": {
                "start_price": round(start_price, 2),
                "end_price": round(end_price, 2),
                "return_pct": round((end_price - start_price) / start_price * 100, 2)
                    if start_price else None,
                "max": round(max(prices), 2),
                "min": round(min(prices), 2),
                "max_drawdown_pct": round(max_dd, 2),
                "max_runup_pct": round(max_ru, 2),
            },
        }
    except Exception as e:
        log.exception("get_price_history failed")
        return {"error": f"Could not fetch history for {ticker}: {e}"}


@tool("get_recent_news", args_schema=NewsArg)
def get_recent_news(ticker: str, limit: int = 5) -> list[dict[str, Any]]:
    """Get recent news headlines for a ticker.

    Returns a list of {title, publisher, link, published_at}. Use this
    to surface what's been moving sentiment recently.
    """
    try:
        items = yf.Ticker(ticker).news or []
        out = []
        for item in items[:limit]:
            # yfinance recently changed shape; handle both old + new.
            content = item.get("content") or item
            out.append(
                {
                    "title": content.get("title"),
                    "publisher": (
                        content.get("provider", {}).get("displayName")
                        if isinstance(content.get("provider"), dict)
                        else content.get("publisher")
                    ),
                    "link": (
                        content.get("canonicalUrl", {}).get("url")
                        if isinstance(content.get("canonicalUrl"), dict)
                        else content.get("link")
                    ),
                    "published_at": content.get("pubDate") or content.get("providerPublishTime"),
                }
            )
        return out
    except Exception as e:
        log.exception("get_recent_news failed")
        return [{"error": f"Could not fetch news for {ticker}: {e}"}]


# --------------------------------------------------------------------------- #
# Plan tools
# --------------------------------------------------------------------------- #

@tool("list_plans")
def list_plans_tool() -> list[dict[str, Any]]:
    """List every plan the user has on file (one summary line each).

    Use this when the user asks "what positions am I tracking" or before
    you discuss a ticker, so you know whether they have a plan on it.
    """
    return [
        {
            "ticker": p.ticker,
            "one_liner": p.one_liner,
            "confidence": p.confidence,
            "time_horizon": p.time_horizon,
            "reason_count": len(p.reasons),
            "any_at_risk": any(r.status in ("at_risk", "off_track") for r in p.reasons),
        }
        for p in plan_store.list_plans()
    ]


@tool("get_plan", args_schema=TickerArg)
def get_plan_tool(ticker: str) -> dict[str, Any] | None:
    """Get the full plan for a ticker, including all reasons and their
    walk-away signals. Use this whenever the user asks about a ticker
    they have a plan on.
    """
    p = plan_store.get_plan(ticker)
    return p.model_dump() if p else {"error": f"No plan on file for {ticker}."}


# --------------------------------------------------------------------------- #
# Public list, used by the graph to bind tools to the LLM.
# --------------------------------------------------------------------------- #

ALL_TOOLS = [
    get_stock_quote,
    get_company_info,
    get_financials_snapshot,
    get_recent_news,
    list_plans_tool,
    get_plan_tool,
]
