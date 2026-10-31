from storage.database import Database
from config.settings import Settings

s = Settings()
db = Database(host=s.DB_HOST, port=s.DB_PORT, dbname=s.DB_NAME, user=s.DB_USER, password=s.DB_PASSWORD)
db.connect()

rows = db.fetch_all("SELECT id, symbol, name, (SELECT COUNT(*) FROM candles_1d WHERE stock_id = stocks.id) as candles FROM stocks WHERE symbol ILIKE %s LIMIT 15", ("%nifty%",))
for r in rows:
    print(f"{r['symbol']} | {r['name']} | Candles: {r['candles']}")
db.disconnect()
