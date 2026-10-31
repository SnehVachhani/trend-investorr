from storage.database import Database
from config.settings import Settings

s = Settings()
db = Database(host=s.DB_HOST, port=s.DB_PORT, dbname=s.DB_NAME, user=s.DB_USER, password=s.DB_PASSWORD)
db.connect()

# Delete the empty duplicate (ID 1254)
db.execute("DELETE FROM stocks WHERE id = 1254")

# Rename the real one (ID 1) to the standard symbol "NIFTY"
db.execute("UPDATE stocks SET symbol = 'NIFTY' WHERE id = 1")

print("Cleanup complete!")
db.disconnect()
