from storage.database import Database
from config.settings import Settings

s = Settings()
db = Database(host=s.DB_HOST, port=s.DB_PORT, dbname=s.DB_NAME, user=s.DB_USER, password=s.DB_PASSWORD)
db.connect()

print("ID 1 candles:", db.fetch_one("SELECT COUNT(*) as c FROM candles_1d WHERE stock_id = 1")['c'])
print("ID 1254 candles:", db.fetch_one("SELECT COUNT(*) as c FROM candles_1d WHERE stock_id = 1254")['c'])
db.disconnect()
