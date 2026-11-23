import os
import time
import json
import sqlite3
import yaml
import requests
from google import genai
from google.genai import types

with open("config.yaml", "r") as f:
    CONFIG = yaml.safe_load(f)

SERPER_API_KEY = os.environ["SERPER_API_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TG_BOT_TOKEN   = os.environ["TG_BOT_TOKEN"]
TG_CHAT_ID     = os.environ["TG_CHAT_ID"]
DISCORD_TOKEN  = os.environ.get("DISCORD_TOKEN", "")

# Change "qdr:d" (past 24h) to "qdr:h" (past 1h) after you see clean [EVAL] logs
SEARCH_WINDOW = "qdr:d"

llm_client = genai.Client(api_key=GEMINI_API_KEY)

def init_db():
    conn = sqlite3.connect("seen_posts.db")
    conn.execute("CREATE TABLE IF NOT EXISTS seen (url TEXT PRIMARY KEY)")
    conn.commit()
    return conn

def is_seen(conn, url):
    cur = conn.execute("SELECT 1 FROM seen WHERE url = ?", (url,))
    return cur.fetchone() is not None

def mark_seen(conn, url):
    conn.execute("INSERT OR IGNORE INTO seen (url) VALUES (?)", (url,))
    conn.commit()

def fetch_xray_posts():
    posts = []
    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
    for query in CONFIG.get("xray_queries", []):
        payload = {"q": query, "tbs": SEARCH_WINDOW, "num": 10}
        resp = requests.post("https://google.serper.dev/search", headers=headers, json=payload, timeout=15)
        if resp.status_code == 200:
            for item in resp.json().get("organic", []):
                link = item.get("link", "")
                if "google.com/goto" in link or link.endswith(("/all", "/with_replies")):
                    continue
                if "x.com" in link and "/status/" not in link:
                    continue
                if "linkedin.com" in link:
                    source = "LinkedIn"
                elif "reddit.com" in link:
                    source = "Reddit"
                else:
                    source = "X"
                posts.append({
                    "source": source,
                    "title": item.get("title", ""),
                    "text": item.get("snippet", ""),
                    "url": link
                })
    return posts

def fetch_reddit_posts():
    posts = []
    headers = {"User-Agent": "HiringRadarBot/1.0"}
    for sub in CONFIG.get("subreddits", []):
        url = f"https://www.reddit.com/r/{sub}/new.json?limit=10"
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code == 200:
                for child in resp.json()["data"]["children"]:
                    data = child["data"]
                    posts.append({
                        "source": f"Reddit (r/{sub})",
                        "title": data.get("title", ""),
                        "text": data.get("selftext", "")[:600],
                        "url": f"https://reddit.com{data['permalink']}"
                    })
        except Exception:
            continue
    return posts

def fetch_discord_posts():
    posts = []
    if not DISCORD_TOKEN:
        return posts
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}
    for channel_id in CONFIG.get("discord_channels", []):
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages?limit=10"
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            for msg in resp.json():
                posts.append({
                    "source": "Discord",
                    "title": f"Message by {msg['author']['username']}",
                    "text": msg.get("content", "")[:600],
                    "url": f"https://discord.com/channels/@me/{channel_id}/{msg['id']}"
                })
    return posts

def evaluate_posts_batch(posts):
    """Evaluates all new posts in ONE Gemini API call to stay under the 5 RPM free limit."""
    formatted_posts = [
        {"id": i, "source": p["source"], "title": p["title"], "text": p["text"]}
        for i, p in enumerate(posts)
    ]

    prompt = f"""
    You are a strict semiconductor job-hunting filter.
    Candidate Profile:
    - Primary Roles (High Priority): {CONFIG['profile']['primary_roles']}
    - Secondary Roles (Medium Priority): {CONFIG['profile']['secondary_roles']}
    - Bonus Domain: {CONFIG['profile']['bonus_domain']}
    - Experience: {CONFIG['profile']['experience']}
    - Target Companies: {CONFIG['profile']['target_companies']}
    - Locations: {CONFIG['profile']['locations']}

    Evaluate this JSON list of posts:
    {json.dumps(formatted_posts)}

    Strict Filtering Rules:
    1. REJECT if the author is a job seeker ("I am looking for...", "#opentowork") or a VLSI coaching/training institute.
    2. REJECT if the role explicitly requires 3+ years of experience (e.g., Senior, Staff, Principal, 5+ YoE).
    3. ACCEPT if it is a real opening/referral for 0-2 YoE, 2026 grads, or unspecified experience in Primary or Secondary roles.
    4. Set tag to "🔥 PRIMARY + ARCH" if it's RTL/DV + Computer Architecture, "⚡ PRIMARY" for RTL/DV, or "📌 SECONDARY" for Arch/DFT/STA.
    5. Draft a sharp, 3-line cold DM/comment tailored to the post (leave empty string if is_match is false).

    Return a JSON array with one object per post:
    [{{"id": int, "is_match": bool, "tag": "string", "reason": "1-line summary", "draft_dm": "string"}}]
    """
    for attempt in range(3):
        try:
            resp = llm_client.models.generate_content(
                model="gemini-3.8-flash",
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            return json.loads(resp.text)
        except Exception as e:
            if ("503" in str(e) or "429" in str(e)) and attempt < 2:
                wait = 25 * (attempt + 1)
                print(f"[RETRY] Hit temporary limit, waiting {wait}s...")
                time.sleep(wait)
                continue
            raise

def send_telegram_alert(post, evaluation):
    msg = (
        f"{evaluation.get('tag', '⚡ LEAD')} | {post['source']}\n\n"
        f"{post['title']}\n"
        f"Why: {evaluation['reason']}\n\n"
        f"Draft DM:\n{evaluation['draft_dm']}\n\n"
        f"Link: {post['url']}"
    )
    resp = requests.post(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT_ID, "text": msg, "disable_web_page_preview": True},
        timeout=10
    )
    print(f"[TELEGRAM] Status: {resp.status_code} | Response: {resp.text}")

def run():
    conn = init_db()
    all_posts = fetch_xray_posts() + fetch_reddit_posts() + fetch_discord_posts()
    new_posts = [p for p in all_posts if not is_seen(conn, p["url"])]
    print(f"Fetched {len(all_posts)} clean posts ({len(new_posts)} new).")

    if not new_posts:
        conn.close()
        return

    try:
        results = evaluate_posts_batch(new_posts)
        for res in results:
            idx = res.get("id")
            if idx is None or idx >= len(new_posts):
                continue
            post = new_posts[idx]
            mark_seen(conn, post["url"])
            print(f"[EVAL] match={res.get('is_match')} | {post['source']} | {post['title'][:60]}")
            if res.get("is_match"):
                send_telegram_alert(post, res)
    except Exception as e:
        print(f"[ERROR] Batch evaluation failed -> {e}")

    conn.close()

if __name__ == "__main__":
    run()
