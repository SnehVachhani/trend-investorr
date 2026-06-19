"""Debug: test with original Connors params (RSI-10 < 30) on Nifty 50"""
import numpy as np
from storage.database import Database
from config.settings import Settings
from strategies.backtester import compute_sma, compute_rsi

s = Settings()
db = Database(host=s.DB_HOST, port=s.DB_PORT, dbname=s.DB_NAME, user=s.DB_USER, password=s.DB_PASSWORD)
db.connect()

stock = db.fetch_one("SELECT id, symbol FROM stocks WHERE symbol ILIKE %s LIMIT 1", ("%nifty%",))
print(f"Stock: {stock['symbol']}")

rows = db.fetch_all(
    "SELECT timestamp, open, high, low, close FROM candles_1d WHERE stock_id = %s ORDER BY timestamp ASC",
    (stock["id"],),
)
closes = np.array([float(r["close"]) for r in rows])
print(f"Candles: {len(rows)} | {rows[0]['timestamp']} to {rows[-1]['timestamp']}")

sma200 = compute_sma(closes, 200)

# Compare RSI periods and thresholds
configs = [
    ("RSI-14 < 25 (current backtest)", 14, 25),
    ("RSI-14 < 30", 14, 30),
    ("RSI-10 < 30 (original Connors)", 10, 30),
    ("RSI-10 < 25", 10, 25),
    ("RSI-7  < 30", 7, 30),
    ("RSI-3  < 10 (ConnorsRSI classic)", 3, 10),
]

print("\n=== Entry signal comparison (all require close > SMA200) ===\n")
for label, period, thresh in configs:
    rsi = compute_rsi(closes, period)
    count = 0
    for i in range(200, len(closes)):
        if not np.isnan(rsi[i]) and not np.isnan(sma200[i]):
            if rsi[i] < thresh and closes[i] > sma200[i]:
                count += 1
    print(f"  {label:45s} -> {count:4d} signal days")

# Now show what happens on RELIANCE (individual stock)
stock2 = db.fetch_one("SELECT id, symbol FROM stocks WHERE symbol ILIKE %s LIMIT 1", ("%RELIANCE%",))
if stock2:
    rows2 = db.fetch_all(
        "SELECT timestamp, close FROM candles_1d WHERE stock_id = %s ORDER BY timestamp ASC",
        (stock2["id"],),
    )
    closes2 = np.array([float(r["close"]) for r in rows2])
    sma200_2 = compute_sma(closes2, 200)
    print(f"\n=== Same comparison on {stock2['symbol']} ({len(rows2)} candles) ===\n")
    for label, period, thresh in configs:
        rsi = compute_rsi(closes2, period)
        count = 0
        for i in range(200, len(closes2)):
            if not np.isnan(rsi[i]) and not np.isnan(sma200_2[i]):
                if rsi[i] < thresh and closes2[i] > sma200_2[i]:
                    count += 1
        print(f"  {label:45s} -> {count:4d} signal days")

db.disconnect()
