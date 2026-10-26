"""Make a throwaway copy of a database with a known admin login, for testing.

    python3 qa_db.py SOURCE.db DEST.db        # copy + ensure user "qa" (admin, password from QA_PASSWORD)

SOURCE may be any app database (a UAT copy, or a copy of a real one); data/scraper.db itself is only ever read.
The schema is migrated on the copy exactly as the app does at startup, so old copies work too.
"""
import os
import shutil
import sys
from pathlib import Path

src, dest = Path(sys.argv[1]), Path(sys.argv[2])
if dest.resolve() == src.resolve():
    sys.exit("DEST must be a different file from SOURCE")
dest.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(src, dest)

os.environ["DB_PATH"] = str(dest)
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))  # the SinarGalaksiERP folder
from backend import database as db  # noqa: E402  (DB_PATH must be set first)
from backend.auth import hash_password  # noqa: E402

db.init_db()
password = os.environ.get("QA_PASSWORD", "qa-test-1234")
with db._conn() as con:
    row = con.execute("SELECT id FROM users WHERE username_normalized='qa'").fetchone()
if row is None:
    db.create_user("qa", hash_password(password), role="admin")
else:
    with db._conn() as con:
        con.execute("UPDATE users SET password_hash=?, role='admin', is_active=1 WHERE id=?", (hash_password(password), row["id"]))
print(f"{dest}  login: qa / {password}")
