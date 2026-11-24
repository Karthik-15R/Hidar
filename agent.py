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

SEARCH_WINDOW = "qdr:h"

GEMINI_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-4-hydrogen",
    "gemini-3.8-flash",
]

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
    formatted_posts = [
        {"id": i, "source": p["source"], "title": p["title"], "text": p["text"]}
        for i, p in enumerate(posts)
    ]

    prompt = f"""
    You are a strict front-end semiconductor job-hunting filter for India.
    Candidate Profile:
    - Target Roles ONLY: {CONFIG['profile']['primary_roles']}
    - Experience: {CONFIG['profile']['experience']}
    - Top-Priority MNCs: {CONFIG['profile']['priority_mncs']}
    - Target Locations: {CONFIG['profile']['locations']}

    Evaluate this JSON list of posts:
    {json.dumps(formatted_posts)}

    Strict Filtering Rules:
    1. ACCEPT ONLY front-end RTL Design, Digital Design, ASIC Design, Design Verification (DV), Verification, or SoC Design roles.
    2. REJECT Physical Design (PD), STA, DFT, Layout, Analog, Embedded Software, or IT roles.
    3. REJECT if the author is a job seeker ("I am looking for...", "#opentowork") or a VLSI coaching/training institute (e.g., Maven Silicon, VLSI Guru, ChipEdge, Mosart, Takshila).
    4. REJECT if the role explicitly requires 3+ years of experience (Senior, Staff, Principal, Lead, 4+ YoE).
    5. ACCEPT 0-2 YoE, fresher, 2026 grad, or unspecified experience openings across BOTH top MNCs and other semiconductor companies/startups in India.
    6. Tagging:
       - Use "🔥 TOP MNC" if the company is a major semiconductor MNC ({CONFIG['profile']['priority_mncs']}).
       - Use "⚡ OTHER CO" if it is a startup, mid-size product firm, or service company.
    7. Draft a sharp, 3-line cold DM/comment tailored to the post (leave empty string if is_match is false).

    Return a JSON array with one object per post:
    [{{"id": int, "is_match": bool, "tag": "string", "reason": "1-line summary including company & role", "draft_dm": "string"}}]
    """

    last_error = None
    for model_name in GEMINI_MODELS:
        try:
            print(f"[LLM] Trying {model_name}...")
            resp = llm_client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            print(f"[LLM] Success with {model_name}")
            return json.loads(resp.text)
        except Exception as e:
            last_error = e
            print(f"[LLM] {model_name} failed: {e}")
            time.sleep(3)
            continue

    raise last_error

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
