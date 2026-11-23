import os
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

# Change "qdr:d" (past 24h) to "qdr:h" (past 1h) after your first test run
SEARCH_WINDOW = "qdr:d"

llm_client = genai.Client(api_key=GEMINI_API_KEY)

def init_db():
    conn = sqlite3.connect("seen_posts.db")
    conn.execute("CREATE TABLE IF NOT EXISTS seen (url TEXT PRIMARY KEY)")
    conn.commit()
    return conn

def is_new(conn, url):
    cur = conn.execute("SELECT 1 FROM seen WHERE url = ?", (url,))
    if cur.fetchone():
        return False
    conn.execute("INSERT INTO seen (url) VALUES (?)", (url,))
    conn.commit()
    return True

def fetch_xray_posts():
    posts = []
    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
    for query in CONFIG.get("xray_queries", []):
        payload = {"q": query, "tbs": SEARCH_WINDOW, "num": 10}
        resp = requests.post("https://google.serper.dev/search", headers=headers, json=payload, timeout=15)
        if resp.status_code == 200:
            for item in resp.json().get("organic", []):
                source = "LinkedIn" if "linkedin.com" in item["link"] else "X"
                posts.append({
                    "source": source,
                    "title": item.get("title", ""),
                    "text": item.get("snippet", ""),
                    "url": item["link"]
                })
    return posts

def fetch_reddit_posts():
    posts = []
    headers = {"User-Agent": "HiringRadarBot/1.0"}
    for sub in CONFIG.get("subreddits", []):
        url = f"https://www.reddit.com/r/{sub}/new.json?limit=10"
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

def evaluate_post(post):
    prompt = f"""
    You are a strict semiconductor job-hunting filter.
    Candidate Profile:
    - Primary Roles (High Priority): {CONFIG['profile']['primary_roles']}
    - Secondary Roles (Medium Priority): {CONFIG['profile']['secondary_roles']}
    - Bonus Domain: {CONFIG['profile']['bonus_domain']}
    - Experience: {CONFIG['profile']['experience']}
    - Target Companies: {CONFIG['profile']['target_companies']}
    - Locations: {CONFIG['profile']['locations']}

    Evaluate this post:
    Source: {post['source']}
    Title: {post['title']}
    Content: {post['text']}

    Strict Filtering Rules:
    1. REJECT if the author is a job seeker ("I am looking for...", "#opentowork") or a VLSI coaching/training institute.
    2. REJECT if the role explicitly requires 3+ years of experience (e.g., Senior, Staff, Principal, 5+ YoE).
    3. ACCEPT if it is a real opening/referral for 0-2 YoE, 2026 grads, or unspecified experience in Primary or Secondary roles.
    4. Set tag to "🔥 PRIMARY + ARCH" if it's RTL/DV + Computer Architecture, "⚡ PRIMARY" for RTL/DV, or "📌 SECONDARY" for Arch/DFT/STA.
    5. Draft a sharp, 3-line cold DM/comment tailored to the post.

    Return JSON: {{"is_match": bool, "tag": "string", "reason": "1-line summary", "draft_dm": "string"}}
    """
    resp = llm_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    return json.loads(resp.text)

def send_telegram_alert(post, evaluation):
    msg = (
        f"{evaluation.get('tag', '⚡ LEAD')} | *{post['source']}*\n\n"
        f"*{post['title']}*\n"
        f"💡 *Why:* {evaluation['reason']}\n\n"
        f"✉️ *Draft DM (Tap to copy):*\n`{evaluation['draft_dm']}`\n\n"
        f"🔗 [Open Post]({post['url']})"
    )
    requests.post(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "Markdown", "disable_web_page_preview": True},
        timeout=10
    )

def run():
    conn = init_db()
    all_posts = fetch_xray_posts() + fetch_reddit_posts() + fetch_discord_posts()
    print(f"Fetched {len(all_posts)} posts across sources.")
    for post in all_posts:
        if not is_new(conn, post["url"]):
            continue
        try:
            result = evaluate_post(post)
            if result.get("is_match"):
                send_telegram_alert(post, result)
                print(f"[ALERT SENT] {post['source']}: {post['title']}")
        except Exception as e:
            print(f"[ERROR] {post['url']} -> {e}")
    conn.close()

if __name__ == "__main__":
    run()
