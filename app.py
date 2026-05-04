from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import requests
import time
import psycopg2
import os
import threading
from datetime import date, timedelta


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

# GraphQL query
USER_PROFILE_QUERY = """
query getUserProfile($username: String!) {
  matchedUser(username: $username) {
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

# improved fetch with retries and better headers
def fetch_leetcode(username: str, retries=2, timeout=30):
    headers = {
        "Content-Type": "application/json",
        "Referer": "https://leetcode.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Origin": "https://leetcode.com",
    }

    payload = {"query": USER_PROFILE_QUERY, "variables": {"username": username}}

    for attempt in range(retries + 1):
        try:
            r = requests.post(LEETCODE_GRAPHQL, json=payload, headers=headers, timeout=timeout)
            if r.status_code == 403:
                app.logger.error(f"Access forbidden (403) for user {username}. Render IP might be blocked.")
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as http_err:
            status = getattr(http_err.response, "status_code", None)
            if status in (429, 499) or (status and 500 <= status < 600):
                if attempt < retries:
                    time.sleep(1 + attempt * 1)
                    continue
            raise
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            if attempt < retries:
                time.sleep(1 + attempt * 0.5)
                continue
            raise
    raise RuntimeError("Failed to fetch leetcode profile after retries")

def transform_response(data):
    matched = (data or {}).get("data", {}).get("matchedUser")
    if not matched:
        err_msg = (data or {}).get("errors")
        if err_msg:
            return {"ok": False, "error": f"GraphQL errors: {err_msg}"}
        return {"ok": False, "error": "User not found or profile is private."}

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

# ---------- DATABASE SETUP ---------- #
def get_db_info():
    url = os.environ.get("DATABASE_URL")
    if not url:
        return "sqlite", "leetcode_users.db"
    return "postgres", url

def get_db_connection():
    db_type, url = get_db_info()
    if db_type == "sqlite":
        import sqlite3
        conn = sqlite3.connect(url, check_same_thread=False)
        return conn
    else:
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        try:
            return psycopg2.connect(url, sslmode="require")
        except Exception:
            return psycopg2.connect(url)

def get_placeholder():
    db_type, _ = get_db_info()
    return "?" if db_type == "sqlite" else "%s"

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    db_type, _ = get_db_info()
    
    if db_type == "sqlite":
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS leetcode_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                ranking INTEGER,
                reputation INTEGER,
                easy INTEGER DEFAULT 0,
                medium INTEGER DEFAULT 0,
                hard INTEGER DEFAULT 0,
                total INTEGER DEFAULT 0,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_total INTEGER DEFAULT 0,
                last_active_date DATE,
                current_streak INTEGER DEFAULT 0
            )
        """)
    else:
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
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_total INTEGER DEFAULT 0,
                last_active_date DATE,
                current_streak INTEGER DEFAULT 0
            )
        """)
    conn.commit()
    cursor.close()
    conn.close()

# Initialize DB on startup
with app.app_context():
    try:
        init_db()
        app.logger.info("✅ Database initialized successfully.")
    except Exception as e:
        app.logger.error("⚠️ Database initialization failed: %s", e)

@app.route("/admin/init_db")
def admin_init_db():
    """Manual route to trigger database initialization if it fails on startup"""
    try:
        init_db()
        return jsonify({"ok": True, "message": "Database initialized successfully."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

def store_user_stats(username, stats):
    conn = get_db_connection()
    cursor = conn.cursor()
    p = get_placeholder()
    solved = stats.get("solved", {})
    new_total = solved.get("All", 0)

    # Get previous data
    cursor.execute(f"""
        SELECT last_total, last_active_date, current_streak
        FROM leetcode_users WHERE username = {p}
    """, (username,))
    row = cursor.fetchone()

    last_total = row[0] if row else 0
    last_active_date = row[1] if row else None
    current_streak = row[2] if row else 0

    # SQLite returns date strings, PostgreSQL returns date objects
    if isinstance(last_active_date, str):
        from datetime import datetime
        try:
            last_active_date = datetime.strptime(last_active_date, "%Y-%m-%d").date()
        except:
            pass

    today = date.today()

    # ---- STREAK LOGIC ----
    if new_total > (last_total or 0):
        if last_active_date == today - timedelta(days=1):
            current_streak += 1
        elif last_active_date == today:
            pass # already updated today
        else:
            current_streak = 1
        last_active_date = today
    else:
        if last_active_date and (today - last_active_date).days > 1:
            current_streak = 0
    # ----------------------

    db_type, _ = get_db_info()
    if db_type == "sqlite":
        # SQLite UPSERT
        cursor.execute(f"""
            INSERT INTO leetcode_users
            (username, ranking, reputation, easy, medium, hard, total,
             last_updated, last_total, last_active_date, current_streak)
            VALUES ({p},{p},{p},{p},{p},{p},{p},CURRENT_TIMESTAMP,{p},{p},{p})
            ON CONFLICT (username)
            DO UPDATE SET
                ranking = EXCLUDED.ranking,
                reputation = EXCLUDED.reputation,
                easy = EXCLUDED.easy,
                medium = EXCLUDED.medium,
                hard = EXCLUDED.hard,
                total = EXCLUDED.total,
                last_updated = CURRENT_TIMESTAMP,
                last_total = EXCLUDED.total,
                last_active_date = {p},
                current_streak = {p}
        """, (
            username,
            stats.get("ranking"),
            stats.get("reputation"),
            solved.get("Easy", 0),
            solved.get("Medium", 0),
            solved.get("Hard", 0),
            new_total,
            new_total,
            last_active_date,
            current_streak,
            last_active_date,
            current_streak
        ))
    else:
        # Postgres UPSERT
        cursor.execute(f"""
            INSERT INTO leetcode_users
            (username, ranking, reputation, easy, medium, hard, total,
             last_updated, last_total, last_active_date, current_streak)
            VALUES ({p},{p},{p},{p},{p},{p},{p},CURRENT_TIMESTAMP,{p},{p},{p})
            ON CONFLICT (username)
            DO UPDATE SET
                ranking = EXCLUDED.ranking,
                reputation = EXCLUDED.reputation,
                easy = EXCLUDED.easy,
                medium = EXCLUDED.medium,
                hard = EXCLUDED.hard,
                total = EXCLUDED.total,
                last_updated = CURRENT_TIMESTAMP,
                last_total = EXCLUDED.total,
                last_active_date = {p},
                current_streak = {p}
        """, (
            username,
            stats.get("ranking"),
            stats.get("reputation"),
            solved.get("Easy", 0),
            solved.get("Medium", 0),
            solved.get("Hard", 0),
            new_total,
            new_total,
            last_active_date,
            current_streak,
            last_active_date,
            current_streak
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
        status = getattr(e.response, "status_code", None) if hasattr(e, "response") else None
        text = getattr(e.response, "text", None) if hasattr(e, "response") else None
        return {"ok": False, "error": f"Network error: {e} (status={status}) body={text}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ---------- ADMIN ROUTES ---------- #
@app.route("/admin/upload", methods=["POST"])
def admin_upload():
    text = request.form.get("usernames", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "No usernames provided"}), 400
    usernames = [u.strip() for u in text.split("\n") if u.strip()]

    results = {"success": [], "errors": []}
    for username in usernames[:50]:
        stats = fetch_or_update_user(username)
        if stats.get("ok"):
            results["success"].append(username)
        else:
            results["errors"].append(f"{username}: {stats.get('error')}")
        time.sleep(0.8)
    return jsonify(results)

@app.route("/admin/delete/<username>", methods=["DELETE"])
def admin_delete(username):
    conn = get_db_connection()
    cursor = conn.cursor()
    p = get_placeholder()
    cursor.execute(f"DELETE FROM leetcode_users WHERE username = {p}", (username,))
    deleted = cursor.rowcount > 0
    conn.commit()
    cursor.close()
    conn.close()
    CACHE.pop(f"lc:{username.lower()}", None)
    if deleted:
        return jsonify({"ok": True, "message": f"User '{username}' deleted successfully."})
    else:
        return jsonify({"ok": False, "error": f"User '{username}' not found."}), 404

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
        return jsonify({"ok": True, "message": f"All users deleted successfully. {deleted_count} records removed."})
    except Exception as e:
        app.logger.error("Failed to delete all users: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500

# ---------- REFRESH LOGIC ---------- #
_refresh_lock = threading.Lock()

def refresh_all_users_once():
    """Refresh all users sequentially from DB"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM leetcode_users ORDER BY total DESC")
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    for uname in [r[0] for r in rows]:
        try:
            CACHE.pop(f"lc:{uname.lower()}", None)
            fetch_or_update_user(uname)
            time.sleep(0.5)
        except Exception as e:
            app.logger.warning("Refresh failed for %s: %s", uname, e)

@app.route("/admin/refresh_now", methods=["POST"])
def admin_refresh_now():
    """Trigger background refresh of ALL users"""
    def _run():
        with _refresh_lock:
            try:
                refresh_all_users_once()
                app.logger.info("Manual refresh completed.")
            except Exception as e:
                app.logger.error("Manual refresh failed: %s", e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Refresh started"}), 202

# ---------- API ROUTES ---------- #
@app.route("/api/users")
def api_users():
    try:
        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", 12))
        offset = (page - 1) * per_page
        refresh_live = request.args.get("live", "0").lower() in ("1", "true", "yes")

        conn = get_db_connection()
        cursor = conn.cursor()
        p = get_placeholder()
        cursor.execute("SELECT COUNT(*) FROM leetcode_users")
        total_row = cursor.fetchone()
        total = total_row[0] if total_row else 0

        if refresh_live:
            cursor.execute(f"""
    SELECT username, ranking, reputation, easy, medium, hard, total,
           last_updated, current_streak, last_active_date
    FROM leetcode_users
    WHERE ranking IS NOT NULL
    ORDER BY total DESC
    LIMIT {p} OFFSET {p}
""", (per_page, offset))

            rows = cursor.fetchall()
            for uname in [r[0] for r in rows[:10]]:
                try:
                    CACHE.pop(f"lc:{uname.lower()}", None)
                    fetch_or_update_user(uname)
                    time.sleep(0.5)
                except Exception as e:
                    app.logger.warning("Live refresh failed for %s: %s", uname, e)
            cursor.close()
            conn.close()
            conn = get_db_connection()
            cursor = conn.cursor()

        cursor.execute(f"""
    SELECT username, ranking, reputation, easy, medium, hard, total,
           last_updated, current_streak, last_active_date
    FROM leetcode_users
    WHERE ranking IS NOT NULL
    ORDER BY total DESC
    LIMIT {p} OFFSET {p}
""", (per_page, offset))


        users = []
        for row in cursor.fetchall():
            easy = row[3] or 0
            medium = row[4] or 0
            hard = row[5] or 0

            # -------- PLACEMENT SCORE LOGIC --------
            placement_score = easy * 1 + medium * 2 + hard * 3

            if placement_score < 200:
                placement_level = "Beginner"
                level_color = "red"
            elif placement_score < 600:
                placement_level = "Intermediate"
                level_color = "orange"
            else:
                placement_level = "Placement Ready"
                level_color = "green"
            # ---------------------------------------

            # -------- STRENGTH / WEAKNESS LOGIC --------
            scores_map = {
                "Easy": easy,
                "Medium": medium,
                "Hard": hard
            }

            strength = max(scores_map, key=scores_map.get)
            weakness = min(scores_map, key=scores_map.get)
            # -------------------------------------------

            # Handle timestamps and dates from SQLite/Postgres
            last_updated = row[7]
            if isinstance(last_updated, str):
                try:
                    from datetime import datetime
                    # SQLite default TIMESTAMP is CURRENT_TIMESTAMP which is 'YYYY-MM-DD HH:MM:SS'
                    # But it could also be 'YYYY-MM-DDTHH:MM:SS' if ISO format was used
                    if 'T' in last_updated:
                        last_updated = datetime.fromisoformat(last_updated)
                    else:
                        last_updated = datetime.strptime(last_updated, "%Y-%m-%d %H:%M:%S")
                except:
                    pass
            
            last_active = row[9]
            if isinstance(last_active, str):
                try:
                    from datetime import datetime
                    last_active = datetime.strptime(last_active, "%Y-%m-%d").date()
                except:
                    pass

            users.append({
    "username": row[0],
    "ranking": row[1],
    "reputation": row[2],
    "easy": easy,
    "medium": medium,
    "hard": hard,
    "total": row[6],

    # placement
    "placement_score": placement_score,
    "placement_level": placement_level,
    "placement_color": level_color,

    # analysis
    "strength": strength,
    "weakness": weakness,

    # timestamps
    "last_updated": last_updated.isoformat() if hasattr(last_updated, "isoformat") else str(last_updated),

    # ✅ NEW — streak fields (CORRECT INDEX)
    "streak": row[8] or 0,
    "last_active": last_active.isoformat() if hasattr(last_active, "isoformat") else str(last_active),
})




        cursor.close()
        conn.close()

        return jsonify({
            "users": users,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": (total + per_page - 1) // per_page,
            "live_refreshed": refresh_live
        })

    except Exception as e:
        app.logger.exception("api_users error")
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

@app.route("/login")
def login():
    return send_from_directory("static", "login.html")

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