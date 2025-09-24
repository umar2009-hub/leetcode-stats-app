from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import requests
import time
import psycopg2  # CHANGED: use psycopg2 for PostgreSQL
import os

app = Flask(__name__, static_folder="static")
CORS(app)

LEETCODE_GRAPHQL = "https://leetcode.com/graphql"

# Simple cache (in-memory)
CACHE = {}
TTL_SECONDS = 600  # 10 min

def cache_get(key):
    item = CACHE.get(key)
    if not item:
        return None
    exp, data = item
    if time.time() > exp:
        CACHE.pop(key, None)
        return None
    return data

def cache_set(key, data, ttl=TTL_SECONDS):
    CACHE[key] = (time.time() + ttl, data)

USER_PROFILE_QUERY = """
query getUser Profile($username: String!) {
  matchedUser (username: $username) {
    username
    profile {
      ranking
      reputation
    }
    submitStats {
      acSubmissionNum {
        difficulty
        count
      }
    }
  }
}
"""

def fetch_leetcode(username: str):
    headers = {
        "Content-Type": "application/json",
        "Referer": "https://leetcode.com",
        "User -Agent": "Mozilla/5.0",
    }
    r = requests.post(
        LEETCODE_GRAPHQL,
        json={"query": USER_PROFILE_QUERY, "variables": {"username": username}},
        headers=headers,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()

def transform_response(data):
    matched = (data or {}).get("data", {}).get("matchedUser ")
    if not matched:
        return {"ok": False, "error": "User  not found or profile is private."}

    profile = matched.get("profile") or {}
    ac_list = matched.get("submitStats", {}).get("acSubmissionNum") or []
    solved = {item["difficulty"]: item.get("count", 0) for item in ac_list if "difficulty" in item}

    return {
        "ok": True,
        "username": matched.get("username"),
        "ranking": profile.get("ranking"),
        "reputation": profile.get("reputation"),
        "solved": {
            "All": solved.get("All", 0),
            "Easy": solved.get("Easy", 0),
            "Medium": solved.get("Medium", 0),
            "Hard": solved.get("Hard", 0),
        },
    }

# ---------- DATABASE SETUP (PostgreSQL) ---------- #

def get_db_connection():
    """Connect to Postgres using DATABASE_URL. Convert deprecated scheme if needed.
       Try SSL first (for managed providers), then retry without ssl if that fails (helpful for local)."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set in environment")

    # Replace old scheme (some providers give postgres://)
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    # Try with sslmode=require first (typical for managed DBs), fallback to no ssl
    try:
        return psycopg2.connect(url, sslmode="require")
    except Exception:
        # Fallback for local dev where SSL isn't enabled
        return psycopg2.connect(url)

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS leetcode_users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            ranking INTEGER,
            reputation INTEGER,
            easy INTEGER DEFAULT 0,
            medium INTEGER DEFAULT 0,
            hard INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()

# Ensure DB is initialized on import/startup (works with gunicorn and renders)
def ensure_db():
    try:
        init_db()
        app.logger.info("Database initialized successfully.")
    except Exception as e:
        # Log the error but do not crash the import: the error will still show in logs.
        # If you want the deploy to fail on init-db error, re-raise the exception instead.
        app.logger.error("Failed to initialize DB: %s", e)

# Run it immediately so the DB exists before handling requests (works with Gunicorn)
ensure_db()


def store_user_stats(username, stats):
    conn = get_db_connection()
    cursor = conn.cursor()
    solved = stats["solved"]

    cursor.execute("""
        INSERT INTO leetcode_users (username, ranking, reputation, easy, medium, hard, total, last_updated)
        VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (username)
        DO UPDATE SET 
            ranking = EXCLUDED.ranking,
            reputation = EXCLUDED.reputation,
            easy = EXCLUDED.easy,
            medium = EXCLUDED.medium,
            hard = EXCLUDED.hard,
            total = EXCLUDED.total,
            last_updated = CURRENT_TIMESTAMP
    """, (
        username,
        stats.get("ranking"),
        stats.get("reputation"),
        solved.get("Easy", 0),
        solved.get("Medium", 0),
        solved.get("Hard", 0),
        solved.get("All", 0),
    ))

    conn.commit()
    cursor.close()
    conn.close()

# ---------- CORE LOGIC ---------- #

def fetch_or_update_user(username):
    key = f"lc:{username.lower()}"
    cached = cache_get(key)
    if cached and cached.get("ok"):
        return cached

    try:
        data = fetch_leetcode(username)
        payload = transform_response(data)
        if payload.get("ok"):
            cache_set(key, payload)
            store_user_stats(username, payload)
        return payload
    except requests.Timeout:
        return {"ok": False, "error": "LeetCode API timed out."}
    except requests.RequestException as e:
        return {"ok": False, "error": f"Network error: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ---------- ROUTES ---------- #

@app.route("/admin/upload", methods=["POST"])
def admin_upload():
    text = request.form.get("usernames", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "No usernames provided"}), 400
    usernames = [u.strip() for u in text.split("\n") if u.strip()]

    results = {"success": [], "errors": []}
    for username in usernames[:50]:
        if not username:
            continue
        stats = fetch_or_update_user(username)
        if stats.get("ok"):
            results["success"].append(username)
        else:
            results["errors"].append(f"{username}: {stats.get('error')}")

    return jsonify(results)

@app.route("/admin/delete/<username>", methods=["DELETE"])
def admin_delete(username):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM leetcode_users WHERE username = %s", (username,))
    deleted = cursor.rowcount > 0
    conn.commit()
    cursor.close()
    conn.close()

    key = f"lc:{username.lower()}"
    CACHE.pop(key, None)

    if deleted:
        return jsonify({"ok": True, "message": f"User  '{username}' deleted successfully."})
    else:
        return jsonify({"ok": False, "error": f"User  '{username}' not found."}), 404

# NEW: Route to delete all users (admin only, use DELETE method for consistency)
@app.route("/admin/delete_all", methods=["DELETE"])
def admin_delete_all():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM leetcode_users")
        deleted_count = cursor.rowcount
        conn.commit()
        cursor.close()
        conn.close()

        # Clear all user-related cache entries
        user_cache_keys = [k for k in CACHE.keys() if k.startswith("lc:")]
        for key in user_cache_keys:
            CACHE.pop(key, None)

        return jsonify({
            "ok": True,
            "message": f"All users deleted successfully. {deleted_count} records removed."
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"Failed to delete all users: {str(e)}"}), 500

@app.route("/api/users")
def api_users():
    try:
        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", 12))
        offset = (page - 1) * per_page

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM leetcode_users")
        total = cursor.fetchone()[0]

        cursor.execute("""
            SELECT username, ranking, reputation, easy, medium, hard, total, last_updated
            FROM leetcode_users
            WHERE ranking IS NOT NULL
            ORDER BY total DESC
            LIMIT %s OFFSET %s
        """, (per_page, offset))

        users = []
        for row in cursor.fetchall():
            users.append({
                "username": row[0],
                "ranking": row[1],
                "reputation": row[2],
                "easy": row[3],
                "medium": row[4],
                "hard": row[5],
                "total": row[6],
                "last_updated": row[7].isoformat() if row[7] else None,
            })

        cursor.close()
        conn.close()

        return jsonify({
            "users": users,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": (total + per_page - 1) // per_page,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# Simple debug route to check DB connectivity quickly
@app.route("/debug/db")
def debug_db():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        ok = cursor.fetchone()
        cursor.close()
        conn.close()
        return jsonify({"ok": True, "msg": "Connected to database", "test": ok[0] if ok else None}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/admin")
def admin():
    return send_from_directory("static", "admin.html")

# Global exception handler that returns JSON (prevents HTML error pages)
@app.errorhandler(Exception)
def handle_exception(e):
    return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    # For local dev: try to init DB (won't be used on Render where Gunicorn is the entrypoint)
    try:
        init_db()
        print("✅ Database initialized")
    except Exception as e:
        print("⚠️ init_db() failed:", e)
    print("🚀 Server running at http://127.0.0.1:5000")
    app.run(debug=True)
