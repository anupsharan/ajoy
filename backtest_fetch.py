#!/usr/bin/env python3
"""Stage 1: Fetch historical bars from Tradier and save to backtest_data.json"""
import asyncio, json, os, sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv()

from app.services.tradier import TradierClient

SYMBOLS = [
    "AAPL","AMD","AMZN","AVGO","CRM","EXE","GOOGL","HOOD",
    "INTC","META","MRVL","MSFT","MU","NOW","NVDA","ORCL",
    "PLTR","QQQ","SPY","TSLA",
]

async def main():
    client = TradierClient()
    fetch_start = (date.today() - timedelta(days=45)).strftime("%Y-%m-%d 09:30")
    fetch_end   = date.today().strftime("%Y-%m-%d 16:00")
    print(f"Fetching {len(SYMBOLS)} symbols: {fetch_start} → {fetch_end}")

    sem = asyncio.Semaphore(5)
    data = {}

    async def fetch(sym):
        async with sem:
            print(f"  {sym}...", flush=True)
            b1  = await client.get_intraday_bars(sym, "1min",  start=fetch_start, end=fetch_end)
            b15 = await client.get_intraday_bars(sym, "15min", start=fetch_start, end=fetch_end)
            data[sym] = {
                "1m":  [{"time": b.time.isoformat(), "open": b.open, "high": b.high,
                          "low": b.low, "close": b.close, "volume": b.volume} for b in b1],
                "15m": [{"time": b.time.isoformat(), "open": b.open, "high": b.high,
                          "low": b.low, "close": b.close, "volume": b.volume} for b in b15],
            }

    await asyncio.gather(*[fetch(s) for s in SYMBOLS])

    with open("backtest_data.json", "w") as f:
        json.dump(data, f)

    total_1m  = sum(len(data[s]["1m"])  for s in data)
    total_15m = sum(len(data[s]["15m"]) for s in data)
    print(f"\nSaved backtest_data.json — {total_1m} 1-min bars, {total_15m} 15-min bars")

if __name__ == "__main__":
    asyncio.run(main())
