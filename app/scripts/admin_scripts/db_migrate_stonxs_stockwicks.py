import psycopg2
from psycopg2.extras import RealDictCursor

# Shared connection details
COMMON = {
    'user': 'postgres',
    'password': 'Capri4412',
    'host': 'localhost',
    'port': 5432
}

# DB-specific
STONXS_DB = {**COMMON, 'dbname': 'stonxs'}
STOCKWICKS_DB = {**COMMON, 'dbname': 'stockwicks'}

def migrate_users():
    print("🔁 Starting user migration...")

    # Connect to both databases
    prod_conn = psycopg2.connect(**STONXS_DB)
    stag_conn = psycopg2.connect(**STOCKWICKS_DB)
    prod_cur = prod_conn.cursor(cursor_factory=RealDictCursor)
    stag_cur = stag_conn.cursor()

    # Fetch users from production
    prod_cur.execute("""
        SELECT id, first_name, last_name, email, username, password,
               agreement_accepted, email_verified, options_pick_count, last_options_pick_date
        FROM users
    """)
    users = prod_cur.fetchall()
    print(f"📥 Fetched {len(users)} users from production.")

    inserted_count = 0
    for user in users:
        try:
            stag_cur.execute("""
                INSERT INTO users (
                    id, first_name, last_name, email, username,
                    password, agreement_accepted, email_verified,
                    options_pick_count, last_options_pick_date, verification_token
                ) VALUES (
                    %(id)s, %(first_name)s, %(last_name)s, %(email)s, %(username)s,
                    %(password)s, %(agreement_accepted)s, %(email_verified)s,
                    %(options_pick_count)s, %(last_options_pick_date)s, NULL
                )
                ON CONFLICT (email) DO NOTHING;
            """, user)
            inserted_count += stag_cur.rowcount
        except Exception as e:
            print(f"⚠️  Could not insert user {user['email']}: {e}")

    # Reset sequence on staging
    stag_cur.execute("SELECT setval('users_id_seq', (SELECT MAX(id) FROM users))")

    # Finalize
    stag_conn.commit()
    print(f"✅ Migration complete: {inserted_count} new users inserted.")

    # Close
    prod_cur.close()
    stag_cur.close()
    prod_conn.close()
    stag_conn.close()

if __name__ == "__main__":
    migrate_users()
