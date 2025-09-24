from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import requests
import time
import psycopg2
import os
import threading  # For simple timeout handling

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

# GraphQL query (cleaned up - no spaces/typos)
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

# Improved fetch with fewer retries for speed
def fetch_leetcode(username: str, retries=1, timeout=10):  # Reduced timeout/retries for faster failure
    headers = {
        "Content-Type": "application/json",
        "Referer": "https://leetcode.com",
        "User -Agent": "Mozilla/5.0 (compatible; LeetStats/1.0; +https://your-site.example)",
        "Accept": "application/json",
    }

    payload = {"query": USER_PROFILE_QUERY, "variables": {"username": username}}

    for attempt in range(retries + 1):
        try:
            r = requests.post(LEETCODE_GRAPHQL, json=payload, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as http_err:
            status = getattr(http_err.response, "status_code", None)
            if status in (429, 499) or (status and 500 <= status < 600):
                if attempt < retries:
                    time.sleep(0.5 + attempt * 0.5)  # Shorter backoff
                    continue
            raise
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            if attempt < retries:
                time.sleep(0.5 + attempt * 0.25)  # Shorter
                continue
            raise
    raise RuntimeError("Failed to fetch LeetCode profile after retries")

def transform_response(data):
    matched = (data or {}).get("data", {}).get("matchedUser ")
    if not matched:
        err_msg = (data or {}).get("errors")
        if err_msg:
            return {"ok": False, "error": f"GraphQL errors: {err_msg}"}
        return {"ok": False, "error": "User  not found or profile is private."}

    profile = matched.get("profile") or {}
    ac_list = matched.get("submitStats", {}).get("acSubmissionNum") or []
    solved = {item.get("difficulty"): item.get("count", 0) for item in ac_list if "difficulty" in item}

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
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set in environment")

    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    try:
        return psycopg2.connect(url, sslmode="require")
    except Exception:
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

def ensure_db():
    try:
        init_db()
        app.logger.info("Database initialized successfully.")
    except Exception as e:
        app.logger.error("Failed to initialize DB: %s", e)

ensure_db()

def store_user_stats(username, stats):
    conn = get_db_connection()
    cursor = conn.cursor()
    solved = stats.get("solved", {})
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
def fetch_or_update_user(username, force=False):
    key = f"lc:{username.lower()}"
    if not force:
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
        status = getattr(e.response, "status_code", None) if hasattr(e, "response") else None
        text = getattr(e.response, "text", None) if hasattr(e, "response") else None
        return {"ok": False, "error": f"Network error: {e} (status={status})"}
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
        stats = fetch_or_update_user(username)  # No force for bulk efficiency
        if stats.get("ok"):
            results["success"].append(username)
        else:
            results["errors"].append(f"{username}: {stats.get('error')}")
        time.sleep(0.5)  # Slightly longer for bulk to avoid limits
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

@app.route("/admin/delete_all", methods=["DELETE"])
def admin_delete_all():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM leetcode_users")
        deleted_count = cursor.rowcount if cursor.rowcount is not None else 0
        conn.commit()
        cursor.close()
        conn.close()

        for key in list(CACHE.keys()):
            if key.startswith("lc:"):
                CACHE.pop(key, None)

        return jsonify({
            "ok": True,
            "message": f"All users deleted successfully. {deleted_count} records removed."
        })
    except Exception as e:
        app.logger.error("Failed to delete all users: %s", e)
        return jsonify({"ok": False, "error": f"Failed to delete all users: {str(e)}"}), 500

@app.route("/api/users")
def api_users():
    try:
        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", 12))
        offset = (page - 1) * per_page

        # Step 1: Quick DB query for paginated users (using existing data)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM leetcode_users WHERE ranking IS NOT NULL")
        total = cursor.fetchone()[0]

        cursor.execute("""
            SELECT username, ranking, reputation, easy, medium, hard, total, last_updated
            FROM leetcode_users
            WHERE ranking IS NOT NULL
            ORDER BY total DESC
            LIMIT %s OFFSET %s
        """, (per_page, offset))

        db_users = cursor.fetchall()
        usernames_to_refresh = [row[0] for row in db_users]  # Only these users (e.g., 12 max)
        cursor.close()
        conn.close()

        # Step 2: Refresh ONLY the users on this page (real-time for visible ones)
        # Use a simple timeout wrapper to prevent hanging >10s total
        def refresh_users_thread():
            for username in usernames_to_refresh:
                try:
                    fetch_or_update_user(username, force=True)
                except Exception as e:
                    app.logger.warning(f"Failed to refresh {username}: {e}")  # Log but don't crash
                time.sleep(0.1)  # Short sleep for rate limiting

        # Run refresh in background thread with timeout (simple way to avoid blocking forever)
        thread = threading.Thread(target=refresh_users_thread)
        thread.start()
        thread.join(timeout=10)  # Wait max 10s; if timeout, continue with partial updates
        if thread.is_alive():
            app.logger.warning("Refresh thread timed out; serving partial updates")

        # Step 3: Re-query the SAME paginated slice (now with fresh data for this page)
        conn2 = get_db_connection()
        cursor2 = conn2.cursor()
        cursor2.execute("""
            SELECT username, ranking, reputation, easy, medium, hard, total, last_updated
            FROM leetcode_users
            WHERE ranking IS NOT NULL
            ORDER BY total DESC
            LIMIT %s OFFSET %s
        """, (per_page, offset))  # No COUNT again; use previous total

        users = []
        for row in cursor2.fetchall():
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

        cursor2.close()
        conn2.close()

        return jsonify({
            "users": users,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": (total + per_page - 1) // per_page,
        })
    except Exception as e:
        app.logger.error("Error in /api/users: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500

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

@app.errorhandler(Exception)
def handle_exception(e):
    return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    try:
        init_db()
        print("✅ Database initialized")
    except Exception as e:
        print("⚠️ init_db() failed:", e)
    print("🚀 Server running at http://127.0.0.1:5000")
    app.run(debug=True)
