"""
One-shot script: remove PLTR trade #133 from Ajoy's DB so the bot
won't auto-close it when it restarts.

Run this ONCE with the bot stopped:
    python remove_pltr_trade.py

After running, add this to your .env before starting the bot:
    ORPHAN_STOP_EXCLUDED_SYMBOLS=PLTR

Remove the .env line after July 10 once the option expires.
"""
import sqlite3, sys

DB_PATH = "ajoy.db"   # run from the ajoy/ directory

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Confirm the trade exists and is what we expect
cur.execute("SELECT id, symbol, option_symbol, direction, quantity, entry_price, status FROM trades WHERE id=133")
row = cur.fetchone()

if row is None:
    print("Trade #133 not found — already removed or ID mismatch.")
    conn.close()
    sys.exit(0)

print("Found trade to remove:")
print(f"  ID            : {row['id']}")
print(f"  Symbol        : {row['symbol']}")
print(f"  Option symbol : {row['option_symbol']}")
print(f"  Direction     : {row['direction']}")
print(f"  Quantity      : {row['quantity']}")
print(f"  Entry price   : ${row['entry_price']}")
print(f"  Status        : {row['status']}")
print()

confirm = input("Delete this trade from Ajoy? (yes/no): ").strip().lower()
if confirm != "yes":
    print("Aborted — no changes made.")
    conn.close()
    sys.exit(0)

cur.execute("DELETE FROM trades WHERE id=133")
conn.commit()

# Verify
cur.execute("SELECT id FROM trades WHERE id=133")
if cur.fetchone() is None:
    print("✓ Trade #133 deleted. PLTR260710C00139000 is no longer managed by Ajoy.")
else:
    print("ERROR: Delete failed — trade still present.")

conn.close()
print()
print("Next steps:")
print("  1. Add to .env:  ORPHAN_STOP_EXCLUDED_SYMBOLS=PLTR")
print("  2. Start the bot — it will NOT touch the PLTR position.")
print("  3. After July 10 (expiry), remove the .env line.")
