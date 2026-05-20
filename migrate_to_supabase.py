#!/usr/bin/env python3
"""
Neon to Supabase Migration Script
Run this locally: python3 migrate_to_supabase.py
"""

import psycopg2
import psycopg2.extras
import sys

# Connection strings
NEON_URL = "postgresql://neondb_owner:npg_TLW2gbMpxNs8@ep-dark-base-ai5w7le0-pooler.c-4.us-east-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
SUPABASE_URL = "postgresql://postgres:u2acn2@2026@db.zfsgtrqtqxgxlodyjwtx.supabase.co:5432/postgres"

print("=" * 50)
print("Neon to Supabase Migration")
print("=" * 50)
print()

try:
    print("Step 1: Connecting to Neon...")
    neon_conn = psycopg2.connect(NEON_URL)
    neon_cursor = neon_conn.cursor()
    print("✓ Connected to Neon")
    print()

    print("Step 2: Connecting to Supabase...")
    supabase_conn = psycopg2.connect(SUPABASE_URL)
    supabase_cursor = supabase_conn.cursor()
    print("✓ Connected to Supabase")
    print()

    print("Step 3: Getting list of tables from Neon...")
    neon_cursor.execute("""
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'public'
        ORDER BY tablename
    """)
    tables = [row[0] for row in neon_cursor.fetchall()]
    print(f"✓ Found {len(tables)} tables: {', '.join(tables)}")
    print()

    if len(tables) == 0:
        print("⚠ No tables found in Neon database")
        sys.exit(1)

    print("Step 4: Copying schema and data...")
    print()

    total_rows = 0

    # Copy all tables
    for table in tables:
        print(f"  Processing table: {table}...", end=" ", flush=True)

        # Get column info
        neon_cursor.execute(f"""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
        """, (table,))
        columns = [row[0] for row in neon_cursor.fetchall()]

        if not columns:
            print("SKIP (no columns)")
            continue

        col_str = ', '.join(columns)

        # Fetch all data from Neon
        neon_cursor.execute(f"SELECT {col_str} FROM public.{table}")
        rows = neon_cursor.fetchall()

        if rows:
            # Create placeholder string
            placeholders = ','.join(['%s'] * len(columns))
            insert_sql = f"INSERT INTO public.{table} ({col_str}) VALUES ({placeholders})"

            # Insert into Supabase in batches
            psycopg2.extras.execute_batch(supabase_cursor, insert_sql, rows, page_size=100)
            supabase_conn.commit()

            total_rows += len(rows)
            print(f"✓ {len(rows)} rows")
        else:
            print("✓ 0 rows")

    print()
    print("=" * 50)
    print("Migration Complete!")
    print("=" * 50)
    print()
    print(f"Total rows migrated: {total_rows}")
    print()
    print("Next steps:")
    print("1. Update your environment variable DATABASE_URL:")
    print("   DATABASE_URL=postgresql://postgres:u2acn2@2026@db.zfsgtrqtqxgxlodyjwtx.supabase.co:5432/postgres")
    print()
    print("2. Restart your Flask application")
    print()
    print("3. Test the booking portal to verify everything works")
    print()

    neon_cursor.close()
    neon_conn.close()
    supabase_cursor.close()
    supabase_conn.close()

except psycopg2.Error as e:
    print(f"\n✗ Database error: {e}")
    sys.exit(1)
except Exception as e:
    print(f"\n✗ Error: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
