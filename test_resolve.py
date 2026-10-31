import sys
from storage.database import Database
from config.settings import Settings
from api.server import _resolve_stock_id

s = Settings()
db = Database(host=s.DB_HOST, port=s.DB_PORT, dbname=s.DB_NAME, user=s.DB_USER, password=s.DB_PASSWORD)
db.connect()

for sym in ["NIFTY", "NIFTY 50", "NIFTY50"]:
    stock = _resolve_stock_id(db, sym)
    if stock:
        print(f"Searched '{sym}' -> Found ID={stock['id']}, Symbol='{stock['symbol']}', Name='{stock['name']}', Exchange='{stock['exchange']}'")
    else:
        print(f"Searched '{sym}' -> Not found")

db.disconnect()
