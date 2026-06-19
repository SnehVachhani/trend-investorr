from storage.database import Database
from config.settings import Settings

s = Settings()
db = Database(host=s.DB_HOST, port=s.DB_PORT, dbname=s.DB_NAME, user=s.DB_USER, password=s.DB_PASSWORD)
db.connect()

stock = db.fetch_one("SELECT id FROM stocks WHERE symbol = 'NIFTY' AND exchange = 'NSE'")
print('ID:', stock['id'])
for table in ['candles_1m', 'candles_5m', 'candles_15m', 'candles_1h', 'candles_1d', 'candles_1w']:
    c = db.fetch_one(f"SELECT COUNT(*) as cnt FROM {table} WHERE stock_id = %s", (stock['id'],))
    print(f"{table}: {c['cnt']}")
db.disconnect()
