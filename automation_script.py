import os
import re
import json
import time
import asyncio
import requests
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query
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
TG_API_ID = int(os.environ.get('TG_API_ID', 12345678))            # আপনার Telegram API ID (সংখ্যা)
TG_API_HASH = os.environ.get('TG_API_HASH', 'your_telegram_api_hash')  # আপনার Telegram API Hash
STRING_SESSION = os.environ.get('TELEGRAM_STRING_SESSION', '')   # Railway-এর জন্য অত্যন্ত জরুরি StringSession

TMDB_API_KEY = os.environ.get('TMDB_API_KEY', 'your_tmdb_api_key')

FIREBASE_PROJECT_ID = os.environ.get('FIREBASE_PROJECT_ID', 'movie-box-96be3')
SERVICE_ACCOUNT_KEY_PATH = 'serviceAccountKey.json'
SERVICE_ACCOUNT_JSON_ENV = os.environ.get('FIREBASE_SERVICE_ACCOUNT_JSON', '')

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

# ==============================================================================
# ★ ৩. Telethon টেলিগ্রাম ক্লায়েন্ট সেটআপ ★
# ==============================================================================
if STRING_SESSION:
    print("🔑 Using Telegram StringSession (Cloud Mode)")
    client = TelegramClient(StringSession(STRING_SESSION), TG_API_ID, TG_API_HASH)
else:
    print("💻 Using local file session: movie_session.session")
    client = TelegramClient('movie_session', TG_API_ID, TG_API_HASH)

# অন-ডিমান্ড ট্র্যাকার ও কনকারেন্সি লক (যাতে একই সাথে একাধিক রিকোয়েস্টে বট তালগোল না পাকায়)
telegram_queue_lock = asyncio.Lock()
current_task = {
    "active": False,
    "tmdb_id": None,
    "title": None,
    "forwarded_files": [],
    "collected_links": [],
}

def format_size(bytes_size):
    if not bytes_size:
        return "Unknown"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.1f} TB"

# টেলিগ্রাম ইভেন্ট লিসেনার
@client.on(events.NewMessage(from_users=PAP_KORN_BOT))
async def handle_papkorn_bot_response(event):
    if not current_task["active"]:
        return

    # বাটন ক্লিক লজিক (শুধুমাত্র নির্দিষ্ট ৩-৪টি কোয়ালিটি ফিল্টার করা হবে)
    if event.reply_markup:
        print(f"📩 Buttons received from @{PAP_KORN_BOT} for '{current_task['title']}'!")
        clicked_qualities = set()
        
        for row in event.reply_markup.rows:
            for button in row.buttons:
                btn_text = button.text.strip()
                btn_lower = btn_text.lower()

                # কোনো ফেক, ক্যাম (CAM) বা ট্রেইলার বাটন থাকলে বাদ দেওয়া
                if any(bad in btn_lower for bad in EXCLUDE_KEYWORDS):
                    continue
                
                # শুধুমাত্র নির্দিষ্ট ৩-৪টি কোয়ালিটি পছন্দ করা
                for q in TARGET_QUALITIES:
                    if q in btn_lower and q not in clicked_qualities:
                        print(f"👉 Selected target quality button: [{btn_text}]")
                        clicked_qualities.add(q)
                        try:
                            await event.click(button)
                            await asyncio.sleep(2)  # টেলিগ্রাম সুরক্ষার জন্য নিরাপদ বিরতি
                        except Exception as click_err:
                            print(f"⚠️ Button click error: {click_err}")
                        break
                
                # সর্বোচ্চ ৪টি কোয়ালিটি লিংক পাওয়া গেলেই থামবে
                if len(clicked_qualities) >= 4:
                    break

@client.on(events.NewMessage(from_users=PAP_KORN_BOT))
async def handle_incoming_movie_file(event):
    if not current_task["active"]:
        return

    # ফাইল এলে সাথে সাথে ১ মিনিটের আগে @LinkFilesBot-এ ফরোয়ার্ড
    if event.media:
        file_size = 0
        file_name = "Movie File"
        if hasattr(event.media, 'document') and event.media.document:
            file_size = event.media.document.size
            for attr in event.media.document.attributes:
                if hasattr(attr, 'file_name') and attr.file_name:
                    file_name = attr.file_name
                    break

        readable_size = format_size(file_size)
        print(f"⚡ File received from @{PAP_KORN_BOT}: {file_name} ({readable_size})")
        print(f"🚀 Forwarding to @{LINK_FILES_BOT}...")

        try:
            fwd_msg = await client.forward_messages(LINK_FILES_BOT, event.message)
            current_task["forwarded_files"].append({
                "size": readable_size,
                "name": file_name,
                "fwd_id": fwd_msg.id
            })
        except Exception as fwd_err:
            print(f"❌ Forwarding error: {fwd_err}")

@client.on(events.NewMessage(from_users=LINK_FILES_BOT))
async def handle_linkfiles_bot_response(event):
    if not current_task["active"]:
        return

    text = event.message.message or ""
    urls = re.findall(r'(https?://[^\s]+)', text)
    
    if urls:
        download_url = urls[0]
        print(f"🔗 Received download link from @{LINK_FILES_BOT}: {download_url}")
        
        label = "High Speed Download"
        size = "Unknown"
        if current_task["forwarded_files"]:
            file_info = current_task["forwarded_files"].pop(0)
            size = file_info.get("size", "Unknown")
            name = file_info.get("name", "").lower()
            
            is_dual = "dual" in name or "hindi" in name or "multi" in name
            
            if "1080p" in name:
                label = "Dual Audio [1080p Full HD]" if is_dual else "1080p Full HD"
            elif "720p" in name:
                label = "Dual Audio [720p HD]" if is_dual else "720p HD Quality"
            elif "480p" in name or "300mb" in name:
                label = "Dual Audio [480p SD]" if is_dual else "480p SD (Data Saver)"
            elif is_dual:
                label = f"Dual Audio Multi [{size}]"
            else:
                label = f"Fast Download [{size}]"
        
        # ডুপ্লিকেট লিংক বা অতিরিক্ত লিংক এড়ানো (সর্বোচ্চ ৪টি)
        if len(current_task["collected_links"]) < 4:
            current_task["collected_links"].append({
                "label": label,
                "size": size,
                "url": download_url
            })

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
            # ডকুমেন্টে শুধু downloadLinks ফিল্ড আপডেট করা
            db.collection('movies').document(doc_id).update({
                'downloadLinks': links,
                'videoUrl': links[0]['url'] if links else ""
            })
            print(f"💾 Updated downloadLinks for '{movie_title}' in Firestore! (Doc ID: {doc_id})")
        else:
            # যদি ডকুমেন্ট না থাকে, তবে তৈরি করা
            new_doc = {
                "title": movie_title,
                "type": "movie",
                "category": "HollyWood",
                "genre": "Action",
                "description": f"Auto-added movie {movie_title}",
                "posterUrl": "",
                "videoUrl": links[0]['url'] if links else "",
                "tmdbId": int(tmdb_id),
                "releaseDate": f"{year}-01-01",
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
# ★ ৫. অন-ডিমান্ড টেলিগ্রাম সার্চ ফাংশন ★
# ==============================================================================
async def fetch_links_from_telegram(title: str, year: str, tmdb_id: int):
    async with telegram_queue_lock:
        print(f"\n=======================================================")
        print(f"⚡ ON-DEMAND REQUEST: User clicked download for '{title} ({year})'")
        print(f"=======================================================")

        # রিসেট স্টেট
        current_task["active"] = True
        current_task["tmdb_id"] = tmdb_id
        current_task["title"] = title
        current_task["forwarded_files"] = []
        current_task["collected_links"] = []

        try:
            # টেলিগ্রাম বটে সার্চ পাঠানো
            search_query = f"{title} {year}".strip()
            print(f"🔎 Sending search query to @{PAP_KORN_BOT}: '{search_query}'")
            await client.send_message(PAP_KORN_BOT, search_query)

            # বট রেসপন্স এবং লিংকের জন্য অপেক্ষা (সর্বোচ্চ ৬০ সেকেন্ড)
            max_wait = 60
            waited = 0
            while waited < max_wait:
                await asyncio.sleep(3)
                waited += 3
                
                # যদি লিংক পাওয়া যায় এবং কোনো পেন্ডিং ফাইল ফরোয়ার্ডে না থাকে
                if len(current_task["collected_links"]) > 0 and len(current_task["forwarded_files"]) == 0:
                    print(f"✅ Successfully collected {len(current_task['collected_links'])} link(s) in {waited}s!")
                    break

            links = list(current_task["collected_links"])
            
            # ফায়ারস্টোরে ক্যাশ করে রাখা (যাতে পরবর্তী ইউজার সাথে সাথে পায়)
            if links:
                update_or_save_links_in_firestore(tmdb_id, title, year, links)
                
            return links
        finally:
            current_task["active"] = False
            # টেলিগ্রাম সুরক্ষার জন্য পরবর্তী রিকোয়েস্টের আগে ৫ সেকেন্ড কুলডাউন
            await asyncio.sleep(5)

# ==============================================================================
# ★ ৬. FastAPI Microservice Engine ★
# ==============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # সার্ভার শুরু হওয়ার সময় টেলিগ্রাম ক্লায়েন্ট চালু হবে
    print("🚀 Starting Telegram Client...")
    await client.start()
    print("📱 Telegram Client is LIVE & LISTENING!")
    yield
    # সার্ভার বন্ধ হওয়ার সময় ডিসকানেক্ট হবে
    print("🛑 Disconnecting Telegram Client...")
    await client.disconnect()

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
async def get_download_links_post(req: DownloadRequest):
    return await handle_download_request(req.tmdbId, req.title, req.year or "")

@app.get("/api/get-download")
async def get_download_links_get(
    tmdbId: int = Query(..., description="TMDB ID"),
    title: str = Query(..., description="Movie Title"),
    year: str = Query("", description="Release Year")
):
    return await handle_download_request(tmdbId, title, year)

async def handle_download_request(tmdb_id: int, title: str, year: str):
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

    # ধাপ ২: ফায়ারবেসে না থাকলে অন-ডিমান্ড টেলিগ্রাম থেকে এনে দেওয়া
    print(f"🔍 No cached links found for '{title}'. Fetching live from Telegram...")
    links = await fetch_links_from_telegram(title, year, tmdb_id)

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
