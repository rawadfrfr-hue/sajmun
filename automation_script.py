import os
import re
import json
import time
import asyncio
import requests
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import firebase_admin
from firebase_admin import credentials, firestore

# ==============================================================================
# ★ ১. কনফিগারেশন সেটিংস (Railway Environment Variables) ★
# ==============================================================================
try:
    raw_api_id = os.environ.get('TG_API_ID', '23447557').strip()
    TG_API_ID = int(raw_api_id) if raw_api_id else 23447557
except Exception:
    TG_API_ID = 23447557

TG_API_HASH = os.environ.get('TG_API_HASH', '29ef51e5e92055e35f175ea1fe6ca643').strip()
STRING_SESSION = os.environ.get('TELEGRAM_STRING_SESSION', '').strip()

TMDB_API_KEY = os.environ.get('TMDB_API_KEY', '40997d508f165094637f1d6f8a9ab148').strip()

FIREBASE_PROJECT_ID = os.environ.get('FIREBASE_PROJECT_ID', 'movie-box-96be3').strip()
SERVICE_ACCOUNT_KEY_PATH = 'serviceAccountKey.json'
SERVICE_ACCOUNT_JSON_ENV = os.environ.get('FIREBASE_SERVICE_ACCOUNT_JSON', '').strip()

PAP_KORN_BOT = 'iPapkornS2bot'
LINK_FILES_BOT = 'LinkFilesBot'
# শুধুমাত্র নির্দিষ্ট ৪টি কোয়ালিটি ফিল্টার করা হবে
TARGET_QUALITIES = ['dual audio', '1080p', '720p', '480p']
EXCLUDE_KEYWORDS = ['cam', 'hdcam', 'ts', 'sample', 'trailer', 'preview', 'part']

# ==============================================================================
# ★ ২. Firebase Firestore সংযোগ (ফাইল অথবা Railway এনভায়রনমেন্ট ভ্যারিয়েবল) ★
# ==============================================================================
print("🔥 Initializing Firebase Firestore...")
try:
    if SERVICE_ACCOUNT_JSON_ENV:
        # Railway Variables থেকে সরাসরি JSON লোড করা
        cred_dict = json.loads(SERVICE_ACCOUNT_JSON_ENV)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred, {'projectId': FIREBASE_PROJECT_ID})
        print("✅ Firestore connected via Railway Environment Variable!")
    elif os.path.exists(SERVICE_ACCOUNT_KEY_PATH):
        # লোকাল ফাইল থেকে লোড করা
        cred = credentials.Certificate(SERVICE_ACCOUNT_KEY_PATH)
        firebase_admin.initialize_app(cred, {'projectId': FIREBASE_PROJECT_ID})
        print("✅ Firestore connected via serviceAccountKey.json file!")
    else:
        firebase_admin.initialize_app(options={'projectId': FIREBASE_PROJECT_ID})
        print("⚠️ Firebase initialized with default credentials.")
    db = firestore.client()
except Exception as e:
    print(f"⚠️ Firebase Init Warning: {e}")
    db = None

def clean_and_fix_session(s: str) -> str:
    if not s:
        return ""
    # কোটেশন এবং অপ্রয়োজনীয় স্পেস/নিউলাইন সরানো
    s = s.strip().strip('"').strip("'").replace(" ", "").replace("\n", "").replace("\r", "")
    # Base64 প্যাডিং সমস্যা নিজে থেকেই সমাধান করা (Incorrect padding error fix)
    if len(s) > 1:
        prefix = s[0]
        b64_part = s[1:]
        missing_padding = len(b64_part) % 4
        if missing_padding != 0:
            b64_part += '=' * (4 - missing_padding)
        s = prefix + b64_part
    return s

cleaned_session_str = clean_and_fix_session(STRING_SESSION)

# ==============================================================================
# ★ ৩. Telethon টেলিগ্রাম ক্লায়েন্ট সেটআপ ★
# ==============================================================================
if cleaned_session_str:
    print("🔑 Using Telegram StringSession (Cloud Mode)")
    try:
        client = TelegramClient(StringSession(cleaned_session_str), TG_API_ID, TG_API_HASH)
    except Exception as sess_err:
        print(f"⚠️ StringSession Init Warning: {sess_err}. Falling back to empty session.")
        client = TelegramClient(StringSession(), TG_API_ID, TG_API_HASH)
else:
    print("💻 Using empty StringSession mode")
    client = TelegramClient(StringSession(), TG_API_ID, TG_API_HASH)

# অন-ডিমান্ড ট্র্যাকার ও কনকারেন্সি লক (যাতে একই সাথে একাধিক রিকোয়েস্টে বট তালগোল না পাকায়)
telegram_queue_lock = asyncio.Lock()

def format_size(bytes_size):
    if not bytes_size:
        return "Unknown"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.1f} TB"

# ==============================================================================
# ★ ৪. ফায়ারস্টোর হেল্পার লজিক (Caching & Instant Fetch) ★
# ==============================================================================
def find_cached_links_in_firestore(tmdb_id: int):
    """চেক করে ফায়ারবেসে আগে থেকেই এই মুভির ডাউনলোড লিংক আছে কি না"""
    if not db:
        return None, None
    try:
        docs = db.collection('movies').where('tmdbId', '==', int(tmdb_id)).limit(1).get()
        if docs:
            doc = docs[0]
            data = doc.to_dict()
            links = data.get('downloadLinks', [])
            if links and len(links) > 0:
                return doc.id, links
            return doc.id, []
    except Exception as e:
        print(f"⚠️ Firestore search error: {e}")
    return None, None

def update_or_save_links_in_firestore(tmdb_id: int, movie_title: str, year: str, links: List[Dict[str, Any]]):
    """ডাউনলোড লিংক ফায়ারস্টোরে আপডেট বা সেভ করে যাতে ভবিষ্যতে আর টেলিগ্রামে না যেতে হয়"""
    if not db or not links:
        return
    try:
        doc_id, existing_links = find_cached_links_in_firestore(tmdb_id)
        if doc_id:
            db.collection('movies').document(doc_id).update({
                'downloadLinks': links,
                'videoUrl': links[0]['url'] if links else ""
            })
            print(f"💾 Updated downloadLinks for '{movie_title}' in Firestore! (Doc ID: {doc_id})")
        else:
            new_doc = {
                "title": movie_title,
                "type": "movie",
                "category": "HollyWood",
                "genre": "Action",
                "description": f"Auto-added movie {movie_title}",
                "posterUrl": "",
                "videoUrl": links[0]['url'] if links else "",
                "tmdbId": int(tmdb_id),
                "releaseDate": f"{year}-01-01" if year else "2024-01-01",
                "runtime": 120,
                "voteAverage": 7.5,
                "voteCount": 100,
                "director": "Unknown",
                "tagline": "",
                "topCast": [],
                "backdrops": [],
                "downloadLinks": links,
                "isFeatured": False,
                "createdAt": int(time.time() * 1000)
            }
            _, new_ref = db.collection('movies').add(new_doc)
            print(f"🎉 Created new movie document in Firestore! (Doc ID: {new_ref.id})")
    except Exception as e:
        print(f"❌ Error updating Firestore: {e}")

# ==============================================================================
# ★ ৫. অন-ডিমান্ড টেলিগ্রাম সার্চ ও এক-এক-করে বাটন প্রসেসিং পাইপলাইন ★
# ==============================================================================
async def fetch_links_from_telegram(title: str, year: str, tmdb_id: int, request: Optional[Request] = None):
    async with telegram_queue_lock:
        print(f"\n=======================================================")
        print(f"⚡ ON-DEMAND REQUEST: Movie '{title} ({year})'")
        print(f"=======================================================")

        collected_links = []
        try:
            # চেক ১: ইউজার কি রিকোয়েস্ট পাঠানোর সাথে সাথেই ট্যাব বন্ধ করে দিয়েছে?
            if request and await request.is_disconnected():
                print(f"🛑 User left before search started for '{title}'. Aborting immediately.")
                return []

            # ধাপ ১: @iPapkornS2bot এ মুভি সার্চ পাঠানো
            search_query = f"{title} {year}".strip() if year else title
            print(f"🔎 Sending search query to @{PAP_KORN_BOT}: '{search_query}'")
            sent_msg = await client.send_message(PAP_KORN_BOT, search_query)
            
            # ধাপ ২: বট থেকে সার্চ রেজাল্ট বাটন আসার জন্য অপেক্ষা
            print(f"⏳ Waiting for search result buttons from @{PAP_KORN_BOT}...")
            results_msg = None
            for _ in range(12):  # ২৪ সেকেন্ড পর্যন্ত পোলিং
                await asyncio.sleep(2)
                recent_msgs = await client.get_messages(PAP_KORN_BOT, limit=5)
                for m in recent_msgs:
                    if m.id > sent_msg.id and (m.buttons or m.reply_markup):
                        results_msg = m
                        break
                if results_msg:
                    break

            if not results_msg or not results_msg.buttons:
                # যদি বছর সহ না পাওয়া যায়, তবে শুধু টাইটেল দিয়ে চেষ্টা
                if year:
                    print(f"🔄 Retrying with title only: '{title}'...")
                    sent_msg = await client.send_message(PAP_KORN_BOT, title)
                    for _ in range(10):
                        await asyncio.sleep(2)
                        recent_msgs = await client.get_messages(PAP_KORN_BOT, limit=5)
                        for m in recent_msgs:
                            if m.id > sent_msg.id and (m.buttons or m.reply_markup):
                                results_msg = m
                                break
                        if results_msg:
                            break

            if not results_msg or not results_msg.buttons:
                print(f"❌ No buttons received from @{PAP_KORN_BOT} for '{title}'")
                return []

            print(f"📩 Search results arrived! Total button rows: {len(results_msg.buttons)}")

            # ধাপ ৩: ৩-৪টি সেরা কোয়ালিটি বাটন ফিল্টার ও বাছাই করা (Dual Audio, 1080p, 720p, 480p)
            selected_buttons = []
            seen_categories = set()

            for r_idx, row in enumerate(results_msg.buttons):
                for c_idx, btn in enumerate(row):
                    btn_text = btn.text.strip()
                    btn_lower = btn_text.lower()

                    # হেডার বাটন বা অপ্রয়োজনীয় ক্যাম/স্যাম্পল বাদ দেওয়া
                    if any(bad in btn_lower for bad in EXCLUDE_KEYWORDS):
                        continue
                    # সাইজ বা কোয়ালিটির উল্লেখ না থাকলে বাদ (যেমন হেডার বাটন)
                    if not any(unit in btn_lower for unit in ['gb', 'mb', 'kb', '1080p', '720p', '480p']):
                        continue

                    # ক্যাটাগরি নির্ধারণ
                    category = None
                    if any(term in btn_lower for term in ['dual', 'hindi', 'multi']):
                        category = 'dual_audio'
                    elif '1080p' in btn_lower:
                        category = '1080p'
                    elif '720p' in btn_lower:
                        category = '720p'
                    elif '480p' in btn_lower or '300mb' in btn_lower or ('mb' in btn_lower and float(re.search(r'(\d+)', btn_text).group(1) if re.search(r'(\d+)', btn_text) else 999) < 600):
                        category = '480p'
                    elif len(selected_buttons) < 3:
                        category = f'other_{r_idx}_{c_idx}'

                    if category and category not in seen_categories:
                        seen_categories.add(category)
                        selected_buttons.append({
                            "row": r_idx,
                            "col": c_idx,
                            "btn": btn,
                            "text": btn_text,
                            "category": category
                        })

                    if len(selected_buttons) >= 4:
                        break
                if len(selected_buttons) >= 4:
                    break

            if not selected_buttons:
                print("⚠️ Could not match standard keywords, picking first available movie buttons.")
                for r_idx, row in enumerate(results_msg.buttons):
                    for c_idx, btn in enumerate(row):
                        b_text = btn.text.strip()
                        if any(u in b_text.lower() for u in ['gb', 'mb', '1080p', '720p']):
                            selected_buttons.append({
                                "row": r_idx, "col": c_idx, "btn": btn, "text": b_text, "category": "general"
                            })
                            if len(selected_buttons) >= 3:
                                break
                    if len(selected_buttons) >= 3:
                        break

            print(f"🎯 Selected {len(selected_buttons)} target buttons to process ONE BY ONE:")
            for idx, item in enumerate(selected_buttons):
                print(f"   [{idx+1}] {item['text']}")

            # ধাপ ৪: এক এক করে (One by One) বাটনে ক্লিক করা, ফাইল পাওয়া ও ফরওয়ার্ড করা
            for idx, item in enumerate(selected_buttons):
                # চেক ২: ইউজার কি ওয়েবসাইট বা ব্রাউজার ট্যাব বন্ধ করে বের হয়ে গেছে?
                if request and await request.is_disconnected():
                    print(f"🛑 User closed the website/tab! Aborting remaining Telegram requests for '{title}' immediately.")
                    break

                btn_text = item["text"]
                print(f"\n--- [Processing {idx+1}/{len(selected_buttons)}]: {btn_text} ---")

                # বাটন ক্লিক করার আগের সর্বশেষ মেসেজ আইডি মনে রাখা
                last_bot_msgs = await client.get_messages(PAP_KORN_BOT, limit=1)
                last_papkorn_id = last_bot_msgs[0].id if last_bot_msgs else 0

                # বাটন ক্লিক
                print(f"👉 Clicking quality button: '{btn_text}'")
                clicked_ok = False
                try:
                    await item["btn"].click()
                    clicked_ok = True
                except Exception as err1:
                    try:
                        await results_msg.click(item["row"], item["col"])
                        clicked_ok = True
                    except Exception as err2:
                        print(f"⚠️ Button click error: {err2}")

                if not clicked_ok:
                    print("⚠️ Could not click button, skipping.")
                    continue

                # মুভি ফাইল আসার অপেক্ষা (সর্বোচ্চ ২৫ সেকেন্ড)
                print(f"⏳ Waiting for movie file from @{PAP_KORN_BOT}...")
                movie_file_msg = None
                for _ in range(12):
                    await asyncio.sleep(2)
                    new_msgs = await client.get_messages(PAP_KORN_BOT, limit=3)
                    for m in new_msgs:
                        if m.id > last_papkorn_id and (m.media or m.file):
                            movie_file_msg = m
                            break
                    if movie_file_msg:
                        break

                if not movie_file_msg:
                    print(f"⚠️ No file received for '{btn_text}', moving to next.")
                    continue

                file_size_bytes = movie_file_msg.file.size if movie_file_msg.file else 0
                file_size_str = format_size(file_size_bytes)
                file_name = movie_file_msg.file.name if (movie_file_msg.file and movie_file_msg.file.name) else btn_text
                print(f"📦 Movie file received: {file_name} ({file_size_str})")

                # ধাপ ৫: ফাইলটি @LinkFilesBot এ ফরওয়ার্ড করা
                last_link_msgs = await client.get_messages(LINK_FILES_BOT, limit=1)
                last_link_id = last_link_msgs[0].id if last_link_msgs else 0

                print(f"🚀 Forwarding to @{LINK_FILES_BOT}...")
                try:
                    await client.forward_messages(LINK_FILES_BOT, movie_file_msg)
                except Exception as fwd_err:
                    print(f"❌ Forward error: {fwd_err}")
                    continue

                # ধাপ ৬: @LinkFilesBot থেকে ডাউনলোড লিংক আসার অপেক্ষা
                print(f"⏳ Waiting for download link from @{LINK_FILES_BOT}...")
                link_reply_text = None
                for _ in range(12):
                    await asyncio.sleep(2)
                    new_lmsgs = await client.get_messages(LINK_FILES_BOT, limit=3)
                    for lm in new_lmsgs:
                        if lm.id > last_link_id and lm.message and ("Download:" in lm.message or "http" in lm.message):
                            link_reply_text = lm.message
                            break
                    if link_reply_text:
                        break

                if not link_reply_text:
                    print(f"⚠️ No response from @{LINK_FILES_BOT}, moving to next.")
                    continue

                # শুধুমাত্র Download: লিংক এক্সট্রাক্ট করা (Watch লিংক বা অন্যান্য নয়)
                dl_match = re.search(r'Download:\s*(https?://\S+)', link_reply_text, re.IGNORECASE)
                if dl_match:
                    dl_url = dl_match.group(1).strip()
                else:
                    fallback_urls = re.findall(r'https?://[^\s\n]+', link_reply_text)
                    dl_url = fallback_urls[0] if fallback_urls else None

                if not dl_url:
                    print(f"⚠️ Could not parse download URL from message:\n{link_reply_text}")
                    continue

                # সুন্দর ও সুস্পষ্ট লেবেল তৈরি
                lower_text = btn_text.lower()
                is_dual = "dual" in lower_text or "hindi" in lower_text or "multi" in lower_text

                if "1080p" in lower_text:
                    label = "Dual Audio [1080p Full HD]" if is_dual else "1080p Full HD"
                elif "720p" in lower_text:
                    label = "Dual Audio [720p HD]" if is_dual else "720p HD Quality"
                elif "480p" in lower_text or "300mb" in lower_text:
                    label = "Dual Audio [480p SD]" if is_dual else "480p SD Mobile"
                elif is_dual:
                    label = f"Dual Audio [{file_size_str}]"
                else:
                    label = f"Fast Download [{file_size_str}]"

                collected_links.append({
                    "label": label,
                    "size": file_size_str,
                    "url": dl_url
                })
                print(f"✅ Successfully collected link [{len(collected_links)}]: {label} -> {dl_url}")

                # প্রতিটি ফাইলের মাঝে ৩ সেকেন্ডের বিরতি
                await asyncio.sleep(3)

            # ধাপ ৭: ফায়ারস্টোরে লিঙ্কগুলো স্থায়ীভাবে সংরক্ষণ করা
            if collected_links:
                print(f"\n🎉 Total {len(collected_links)} download link(s) collected! Saving to Firestore...")
                update_or_save_links_in_firestore(tmdb_id, title, year, collected_links)
            else:
                print("⚠️ No valid download links could be gathered.")

            return collected_links

        except asyncio.CancelledError:
            print(f"🛑 Operation cancelled because user closed the tab/browser for '{title}'.")
            return collected_links
        except Exception as overall_err:
            print(f"❌ Error during telegram link fetch: {overall_err}")
            return collected_links
        finally:
            await asyncio.sleep(3)

# ==============================================================================
# ★ ৬. FastAPI Microservice Engine ★
# ==============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # সার্ভার শুরু হওয়ার সময় টেলিগ্রাম ক্লায়েন্ট চালু হবে
    print("🚀 Connecting Telegram Client via StringSession...")
    try:
        await client.connect()
        if await client.is_user_authorized():
            print("📱 Telegram Client is AUTHORIZED & LISTENING!")
        else:
            print("⚠️ Telegram Client connected, but session needs authorization.")
    except Exception as e:
        print(f"⚠️ Telegram Connection Warning: {e}")
    yield
    # সার্ভার বন্ধ হওয়ার সময় ডিসকানেক্ট হবে
    print("🛑 Disconnecting Telegram Client...")
    try:
        await client.disconnect()
    except Exception:
        pass

app = FastAPI(
    title="Movie On-Demand Download Service",
    description="Fetches download links on-demand from Telegram bots & saves to Firestore",
    lifespan=lifespan
)

# আপনার ওয়েবসাইট থেকে সরাসরি কল করার জন্য CORS অন করা
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # viewr.indevs.in সহ যেকোনো ডোমেন এলাউড
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class DownloadRequest(BaseModel):
    tmdbId: int
    title: str
    year: Optional[str] = ""

@app.get("/")
def home():
    return {
        "status": "online",
        "service": "On-Demand Movie Link Generator",
        "database": "Firestore (movie-box-96be3)"
    }

@app.post("/api/get-download")
async def get_download_links_post(req: DownloadRequest, request: Request):
    return await handle_download_request(request, req.tmdbId, req.title, req.year or "")

@app.get("/api/get-download")
async def get_download_links_get(
    request: Request,
    tmdbId: int = Query(..., description="TMDB ID"),
    title: str = Query(..., description="Movie Title"),
    year: str = Query("", description="Release Year")
):
    return await handle_download_request(request, tmdbId, title, year)

async def handle_download_request(request: Request, tmdb_id: int, title: str, year: str):
    # ধাপ ১: ফায়ারস্টোরে আগে থেকেই লিংক আছে কি না চেক (Instant Cache Return)
    doc_id, cached_links = find_cached_links_in_firestore(tmdb_id)
    if cached_links and len(cached_links) > 0:
        print(f"⚡ INSTANT CACHE HIT: '{title}' links already present in Firestore! (0s delay)")
        return {
            "success": True,
            "source": "firestore_cache",
            "tmdbId": tmdb_id,
            "title": title,
            "links": cached_links
        }

    # ইউজার যদি ব্যাকএন্ডে পৌঁছানোর আগেই বন্ধ করে দেয়
    if await request.is_disconnected():
        print(f"🛑 Client disconnected before processing '{title}'.")
        return {"success": False, "detail": "User disconnected"}

    # ধাপ ২: ফায়ারবেসে না থাকলে অন-ডিমান্ড টেলিগ্রাম থেকে এনে দেওয়া
    print(f"🔍 No cached links found for '{title}'. Fetching live from Telegram...")
    links = await fetch_links_from_telegram(title, year, tmdb_id, request)

    if not links:
        raise HTTPException(
            status_code=404, 
            detail=f"Could not find download links for '{title}' on Telegram."
        )

    return {
        "success": True,
        "source": "telegram_live_generated",
        "tmdbId": tmdb_id,
        "title": title,
        "links": links
    }

# ==============================================================================
# ★ ৭. লোকাল রান করার কোড ★
# ==============================================================================
if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get('PORT', 8000))
    uvicorn.run("automation_script:app", host="0.0.0.0", port=port, reload=False)
