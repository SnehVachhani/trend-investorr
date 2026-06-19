from storage.database import Database
from config.settings import Settings

s = Settings()
db = Database(host=s.DB_HOST, port=s.DB_PORT, dbname=s.DB_NAME, user=s.DB_USER, password=s.DB_PASSWORD)
db.connect()

print(db.fetch_one("SELECT id, symbol, name, exchange FROM stocks WHERE id = 1"))
print(db.fetch_one("SELECT id, symbol, name, exchange FROM stocks WHERE id = 1254"))
db.disconnect()
