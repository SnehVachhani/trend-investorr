from storage.database import Database
from config.settings import Settings

s = Settings()
db = Database(host=s.DB_HOST, port=s.DB_PORT, dbname=s.DB_NAME, user=s.DB_USER, password=s.DB_PASSWORD)
db.connect()

rows = db.fetch_all("""
    SELECT symbol, name 
    FROM stocks 
    WHERE symbol IN ('SJS-EQ', 'SUZLON-EQ', 'GENUSPOWER-EQ', 'NETWEB-EQ', 'PREMIERENE-EQ',
                     'SJS', 'SUZLON', 'GENUSPOWER', 'NETWEB', 'PREMIERENE')
    ORDER BY symbol
""")

print(f"{'Symbol':<20} {'Name'}")
print("-" * 60)
for r in rows:
    print(f"{r['symbol']:<20} {r['name'] or '(NULL)'}")

# Also check a random sample
print("\n\nRandom 10 stocks:")
rows2 = db.fetch_all("SELECT symbol, name FROM stocks WHERE is_active = TRUE ORDER BY RANDOM() LIMIT 10")
for r in rows2:
    print(f"  {r['symbol']:<20} {r['name'] or '(NULL)'}")

db.disconnect()
