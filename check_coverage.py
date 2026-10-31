from storage.database import Database
from config.settings import Settings

s = Settings()
db = Database(host=s.db_host, port=s.db_port, dbname=s.db_name, user=s.db_user, password=s.db_password)
db.connect()

# Update ConnorsRSI URL
db.execute(
    "UPDATE algorithms SET reference_url = %s WHERE name = %s",
    ("https://www.youtube.com/watch?v=W8ENIXvcGlQ", "connors_rsi_pullback")
)

# Verify
rows = db.fetch_all("SELECT id, name, display_name, reference_url FROM algorithms ORDER BY id")
for r in rows:
    url = r['reference_url'] or '-'
    print(f"  {r['id']} | {r['name']:25s} | {url}")
