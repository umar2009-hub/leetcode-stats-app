from flask import Flask, jsonify, send_from_directory, request, render_template_string
from flask_cors import CORS
import requests
import time
import sqlite3
import csv
import io
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

# Your existing GraphQL query
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

def fetch_leetcode(username: str):
    headers = {
        "Content-Type": "application/json",
        "Referer": "https://leetcode.com",
        "User-Agent": "Mozilla/5.0",
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
    matched = (data or {}).get("data", {}).get("matchedUser")
    if not matched:
        return {"ok": False, "error": "User not found or profile is private."}

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

# Database setup
DB_PATH = "leetcode_users.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS leetcode_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            ranking INTEGER DEFAULT NULL,
            reputation INTEGER DEFAULT NULL,
            easy INTEGER DEFAULT 0,
            medium INTEGER DEFAULT 0,
            hard INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def store_user_stats(username, stats):
    conn = get_db_connection()
    cursor = conn.cursor()
    solved = stats["solved"]
    cursor.execute("""
        INSERT OR REPLACE INTO leetcode_users 
        (username, ranking, reputation, easy, medium, hard, total, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, (username, stats["ranking"], stats["reputation"], 
          solved["Easy"], solved["Medium"], solved["Hard"], solved["All"]))
    conn.commit()
    conn.close()

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

# Admin upload endpoint
@app.route("/admin/upload", methods=["POST"])
def admin_upload():
    # Only handle textarea input
    text = request.form.get("usernames", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "No usernames provided"}), 400
    usernames = [u.strip() for u in text.split("\n") if u.strip()]

    results = {"success": [], "errors": []}
    for username in usernames[:50]:  # Limit to 50 to avoid rate limits
        if not username:
            continue
        stats = fetch_or_update_user(username)
        if stats.get("ok"):
            results["success"].append(username)
        else:
            results["errors"].append(f"{username}: {stats.get('error')}")

    return jsonify(results)

# New delete endpoint
@app.route("/admin/delete/<username>", methods=["DELETE"])
def admin_delete(username):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM leetcode_users WHERE username = ?", (username,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    if deleted:
        # Invalidate cache if exists
        key = f"lc:{username.lower()}"
        CACHE.pop(key, None)
        return jsonify({"ok": True, "message": f"User  '{username}' deleted successfully."})
    else:
        return jsonify({"ok": False, "error": f"User  '{username}' not found."}), 404

# Paginated API for users
@app.route("/api/users")
def api_users():
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 12))
    offset = (page - 1) * per_page

    conn = get_db_connection()
    cursor = conn.cursor()

    # Get total count
    cursor.execute("SELECT COUNT(*) FROM leetcode_users")
    total = cursor.fetchone()[0]

    # Get paginated data (only users with data)
    cursor.execute("""
        SELECT * FROM leetcode_users 
        WHERE ranking IS NOT NULL 
        ORDER BY total DESC 
        LIMIT ? OFFSET ?
    """, (per_page, offset))
    users = [dict(row) for row in cursor.fetchall()]

    conn.close()

    return jsonify({
        "users": users,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": (total + per_page - 1) // per_page
    })

# Serve frontend
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/admin")
def admin():
    return send_from_directory("static", "admin.html")

if __name__ == "__main__":
    init_db()  # Initialize DB on start
    print("🚀 Server running at http://127.0.0.1:5000")
    print("📁 Database: leetcode_users.db")
    print("👨‍💼 Admin: http://127.0.0.1:5000/admin")
    app.run(debug=True)
