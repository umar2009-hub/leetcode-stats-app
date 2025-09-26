# app_local.py
from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import requests
import time
import sqlite3
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

def fetch_leetcode(username: str, retries=2, timeout=30):
    headers = {
        "Content-Type": "application/json",
        "Referer": "https://leetcode.com",
        "User-Agent": "Mozilla/5.0 (compatible; LeetStats/1.0; +https://your-site.example)",
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

# ---------- LOCAL SQLITE DB ----------
SQLITE_FILE = os.environ.get("SQLITE_FILE", "leetcode_users.db")

def get_db_connection():
    conn = sqlite3.connect(SQLITE_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def exec_commit(conn, query, params=()):
    cur = conn.cursor()
    cur.execute(query, params)
    conn.commit()
    rc = cur.rowcount
    cur.close()
    return rc

def exec_fetchall(conn, query, params=()):
    cur = conn.cursor()
    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()
    return rows

def exec_fetchone(conn, query, params=()):
    cur = conn.cursor()
    cur.execute(query, params)
    row = cur.fetchone()
    cur.close()
    return row

def init_db():
    conn = get_db_connection()
    create_sql = """
    CREATE TABLE IF NOT EXISTS leetcode_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        ranking INTEGER,
        reputation INTEGER,
        easy INTEGER DEFAULT 0,
        medium INTEGER DEFAULT 0,
        hard INTEGER DEFAULT 0,
        total INTEGER DEFAULT 0,
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """
    exec_commit(conn, create_sql, ())
    conn.close()

init_db()

def store_user_stats(username, stats):
    conn = get_db_connection()
    solved = stats.get("solved", {})
    sql = """
    INSERT INTO leetcode_users (username, ranking, reputation, easy, medium, hard, total, last_updated)
    VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(username) DO UPDATE SET
      ranking=excluded.ranking,
      reputation=excluded.reputation,
      easy=excluded.easy,
      medium=excluded.medium,
      hard=excluded.hard,
      total=excluded.total,
      last_updated=CURRENT_TIMESTAMP
    """
    params = (
        username,
        stats.get("ranking"),
        stats.get("reputation"),
        solved.get("Easy", 0),
        solved.get("Medium", 0),
        solved.get("Hard", 0),
        solved.get("All", 0),
    )
    exec_commit(conn, sql, params)
    conn.close()

# ---------- CORE LOGIC ----------
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
            try:
                store_user_stats(username, payload)
            except Exception:
                # don't break if DB store fails
                pass
        return payload
    except requests.Timeout:
        return {"ok": False, "error": "LeetCode API timed out."}
    except requests.RequestException as e:
        status = getattr(e.response, "status_code", None) if hasattr(e, "response") else None
        text = getattr(e.response, "text", None) if hasattr(e, "response") else None
        return {"ok": False, "error": f"Network error: {e} (status={status}) body={text}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ---------- ROUTES ----------
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
        time.sleep(0.8)
    return jsonify(results)

@app.route("/admin/delete/<username>", methods=["DELETE"])
def admin_delete(username):
    conn = get_db_connection()
    rc = exec_commit(conn, "DELETE FROM leetcode_users WHERE username = ?", (username,))
    conn.close()
    CACHE.pop(f"lc:{username.lower()}", None)
    if rc and rc > 0:
        return jsonify({"ok": True, "message": f"User '{username}' deleted successfully."})
    else:
        return jsonify({"ok": False, "error": f"User '{username}' not found."}), 404

@app.route("/admin/delete_all", methods=["DELETE"])
def admin_delete_all():
    try:
        conn = get_db_connection()
        rc = exec_commit(conn, "DELETE FROM leetcode_users", ())
        conn.close()
        for key in list(CACHE.keys()):
            if key.startswith("lc:"):
                CACHE.pop(key, None)
        return jsonify({"ok": True, "message": f"All users deleted successfully. {rc} records removed."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/users")
def api_users():
    try:
        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", 12))
        offset = (page - 1) * per_page
        refresh_live = request.args.get("live", "0").lower() in ("1", "true", "yes")

        sort_by = (request.args.get("sort") or "").lower()
        order = (request.args.get("order") or "").lower()
        allowed_cols = {"total": "total", "easy": "easy", "medium": "medium", "hard": "hard", "ranking": "ranking", "username": "username"}
        sort_col = allowed_cols.get(sort_by, "total")
        sort_dir = "ASC" if order == "asc" else "DESC"

        conn = get_db_connection()
        row = exec_fetchone(conn, "SELECT COUNT(*) as cnt FROM leetcode_users", ())
        total = int(row["cnt"]) if row else 0

        if refresh_live:
            rows = exec_fetchall(conn, "SELECT username FROM leetcode_users WHERE ranking IS NOT NULL ORDER BY total DESC LIMIT ? OFFSET ?", (per_page, offset))
            usernames_to_refresh = [r["username"] for r in rows]
            MAX_REFRESH = 40
            delay_seconds = 0.6
            for uname in usernames_to_refresh[:MAX_REFRESH]:
                try:
                    CACHE.pop(f"lc:{uname.lower()}", None)
                    fetch_or_update_user(uname)
                    time.sleep(delay_seconds)
                except Exception:
                    pass
            conn.close()
            conn = get_db_connection()

        sql = f"""
            SELECT username, ranking, reputation, easy, medium, hard, total, last_updated
            FROM leetcode_users
            WHERE ranking IS NOT NULL
            ORDER BY {sort_col} {sort_dir}
            LIMIT ? OFFSET ?
        """
        rows = exec_fetchall(conn, sql, (per_page, offset))
        users = []
        for r in rows:
            users.append({
                "username": r["username"],
                "ranking": r["ranking"],
                "reputation": r["reputation"],
                "easy": r["easy"],
                "medium": r["medium"],
                "hard": r["hard"],
                "total": r["total"],
                "last_updated": r["last_updated"]
            })
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
        row = exec_fetchone(conn, "SELECT 1 as v", ())
        val = row["v"] if row else None
        conn.close()
        return jsonify({"ok": True, "msg": "Connected to database", "test": val}), 200
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
    print(f"Using local SQLite DB: {SQLITE_FILE}")
    init_db()
    app.run(debug=True)
