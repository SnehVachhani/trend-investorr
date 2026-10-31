import os
import psycopg2
import psycopg2.extras

conn = psycopg2.connect(
    host='localhost', port=5432,
    dbname='trend_investorr', user='postgres', password=os.environ.get("DB_PASSWORD", "")
)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

# 1. Check candle_coverage rows
cur.execute("SELECT COUNT(*) AS total FROM candle_coverage")
print(f"=== candle_coverage: {cur.fetchone()['total']} rows ===\n")

# 2. Show sample rows with date ranges
cur.execute("""
    SELECT cc.interval, s.symbol, 
           cc.earliest_ts, cc.latest_ts, cc.candle_count, cc.source,
           cc.last_updated
    FROM candle_coverage cc
    JOIN stocks s ON s.id = cc.stock_id
    ORDER BY cc.last_updated DESC
    LIMIT 15
""")
rows = cur.fetchall()
if rows:
    print(f"{'interval':<12} {'symbol':<15} {'earliest_ts':<22} {'latest_ts':<22} {'count':>6} {'source':<12}")
    print("-" * 95)
    for r in rows:
        print(f"{r['interval']:<12} {r['symbol']:<15} {str(r['earliest_ts']):<22} {str(r['latest_ts']):<22} {r['candle_count']:>6} {r['source']:<12}")
else:
    print("  (empty)")

# 3. Check actual candle data counts per table
print("\n=== Candle table row counts ===")
for table in ['candles_1m', 'candles_5m', 'candles_15m', 'candles_1h', 'candles_1d', 'candles_1w']:
    cur.execute(f"SELECT COUNT(*) AS cnt FROM {table}")
    cnt = cur.fetchone()['cnt']
    if cnt > 0:
        cur.execute(f"SELECT MIN(timestamp) AS earliest, MAX(timestamp) AS latest FROM {table}")
        r = cur.fetchone()
        print(f"  {table:<12}: {cnt:>10,} rows | {r['earliest']} -> {r['latest']}")
    else:
        print(f"  {table:<12}: {cnt:>10,} rows")

# 4. Verify coverage matches actual data (spot check)
print("\n=== Coverage vs Actual Data (spot check) ===")
cur.execute("""
    SELECT cc.stock_id, s.symbol, cc.interval, 
           cc.earliest_ts AS cov_earliest, cc.latest_ts AS cov_latest, cc.candle_count AS cov_count
    FROM candle_coverage cc
    JOIN stocks s ON s.id = cc.stock_id
    WHERE cc.interval = 'ONE_MINUTE'
    ORDER BY cc.last_updated DESC
    LIMIT 5
""")
coverage_rows = cur.fetchall()
for cr in coverage_rows:
    cur.execute(f"""
        SELECT COUNT(*) AS actual_count, MIN(timestamp) AS actual_earliest, MAX(timestamp) AS actual_latest
        FROM candles_1m WHERE stock_id = %s
    """, (cr['stock_id'],))
    actual = cur.fetchone()
    match_earliest = str(cr['cov_earliest']) == str(actual['actual_earliest']) if actual['actual_count'] > 0 else False
    match_latest = str(cr['cov_latest']) == str(actual['actual_latest']) if actual['actual_count'] > 0 else False
    print(f"  {cr['symbol']:<15} coverage: {cr['cov_earliest']} -> {cr['cov_latest']} (count={cr['cov_count']})")
    print(f"  {'':>15} actual:   {actual['actual_earliest']} -> {actual['actual_latest']} (count={actual['actual_count']})")
    print(f"  {'':>15} earliest_match={match_earliest}  latest_match={match_latest}")
    print()

conn.close()
