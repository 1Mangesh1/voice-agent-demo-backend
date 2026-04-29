"""Quick DB sanity check.

Usage:  python scripts/test_db.py
Reads DATABASE_URL (or SUPABASE_URL if it's a Postgres URI) from .env.
Reports auth, version, current schema, and a write/read roundtrip.
Password is never printed.
"""
import os
import re
import sys
import time
import uuid

import psycopg
from dotenv import load_dotenv

load_dotenv()

url = (os.getenv("DATABASE_URL") or "").strip()
if not url:
    su = (os.getenv("SUPABASE_URL") or "").strip()
    if su.startswith("postgres"):
        url = su
if not url:
    print("FAIL: no DATABASE_URL/SUPABASE_URL set"); sys.exit(2)

# psycopg accepts postgresql:// directly; strip SQLAlchemy prefix if present.
url = re.sub(r"^postgresql\+psycopg://", "postgresql://", url)

# Mask password for display.
masked = re.sub(r"(:)([^@]+)(@)", lambda m: m.group(1) + "***" + m.group(3), url)
print(f"→ {masked}")

t0 = time.perf_counter()
try:
    with psycopg.connect(url, connect_timeout=10) as conn:
        dt_connect = (time.perf_counter() - t0) * 1000
        print(f"✓ connected in {dt_connect:.0f} ms")
        with conn.cursor() as cur:
            cur.execute("select version()")
            ver = cur.fetchone()[0]
            print(f"  server: {ver.split(',')[0]}")

            cur.execute("select current_database(), current_user, current_schema()")
            db, usr, schema = cur.fetchone()
            print(f"  db={db}  user={usr}  schema={schema}")

            # Check our app tables exist (created by SQLModel.metadata.create_all)
            cur.execute(
                """
                select tablename from pg_tables
                where schemaname = 'public'
                  and tablename in ('user', 'appointment', 'callsession')
                order by tablename
                """
            )
            tables = [r[0] for r in cur.fetchall()]
            print(f"  tables present: {tables or 'none — first server boot will create them'}")

            # Roundtrip write/read on a throwaway temp table.
            tmp = f"_pingtest_{uuid.uuid4().hex[:8]}"
            cur.execute(f'create temp table "{tmp}" (msg text)')
            cur.execute(f'insert into "{tmp}" values (%s) returning msg', ("hello from claude",))
            got = cur.fetchone()[0]
            assert got == "hello from claude"
            print(f"✓ write/read roundtrip ok")
except psycopg.OperationalError as e:
    print(f"✗ FAIL: {e}")
    sys.exit(1)
except Exception as e:
    print(f"✗ ERROR: {type(e).__name__}: {e}")
    sys.exit(1)

print("All good.")
