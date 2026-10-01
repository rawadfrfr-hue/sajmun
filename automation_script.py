import os
import sys
import re
import json
import time
import asyncio

# বর্তমান ডিরেক্টরিকে Python Path-এ যুক্ত করা যাতে মডিউল লোডিং এরর না হয়
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

import requests
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetBotCallbackAnswerRequest
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
# ★ ৪. ফায়ারস্টোর হেল্পার লজিক (Single-Quality & Cached Links) ★
# ==============================================================================
def normalize_quality(q: str) -> str:
    """কোয়ালিটি স্ট্রিংকে স্ট্যান্ডার্ড ফরম্যাটে কনভার্ট করা"""
    if not q:
        return "1080p"
    q_low = q.lower().strip()
    if any(k in q_low for k in ["dual", "hindi", "multi"]):
        return "dual"
    elif "1080" in q_low:
        return "1080p"
    elif "720" in q_low:
        return "720p"
    elif "480" in q_low or "300" in q_low:
        return "480p"
    return q_low

def find_cached_links_in_firestore(tmdb_id: int):
    """চেক করে ফায়ারবেসে আগে থেকেই এই মুভির কোনো ডাউনলোড লিংক আছে কি না"""
    if not db:
        return None, []
    try:
        docs = db.collection('movies').where('tmdbId', '==', int(tmdb_id)).limit(1).get()
        if docs:
            doc = docs[0]
            data = doc.to_dict()
            links = data.get('downloadLinks', [])
            return doc.id, links or []
    except Exception as e:
        print(f"⚠️ Firestore search error: {e}")
    return None, []

def find_matching_quality_link(cached_links: List[Dict[str, Any]], target_quality: str) -> Optional[Dict[str, Any]]:
    """ক্যাশড লিংকগুলোর মধ্যে কাঙ্ক্ষিত কোয়ালিটিটি (যেমন 1080p বা Dual) আছে কি না খুঁজে বের করা"""
    if not cached_links:
        return None
    norm_target = normalize_quality(target_quality)
    for l in cached_links:
        l_quality = normalize_quality(l.get("quality", ""))
        l_label = l.get("label", "").lower()
        if l_quality == norm_target or norm_target in l_label:
            return l
    return None

def save_single_quality_link_in_firestore(tmdb_id: int, movie_title: str, year: str, new_link: Dict[str, Any]) -> List[Dict[str, Any]]:
    """নতুন আনা কোয়ালিটির লিংকটি ফায়ারস্টোরের তালিকায় যুক্ত বা আপডেট করা"""
    if not db or not new_link:
        return [new_link] if new_link else []
    try:
        doc_id, existing_links = find_cached_links_in_firestore(tmdb_id)
        norm_q = normalize_quality(new_link.get("quality", ""))

        updated_links = []
        replaced = False
        for l in existing_links:
            if normalize_quality(l.get("quality", "")) == norm_q:
                updated_links.append(new_link)
                replaced = True
            else:
                updated_links.append(l)

        if not replaced:
            updated_links.append(new_link)

        if doc_id:
            db.collection('movies').document(doc_id).update({
                'downloadLinks': updated_links,
                'videoUrl': updated_links[0]['url'] if updated_links else ""
            })
            print(f"💾 Updated [{norm_q}] link for '{movie_title}' in Firestore! (Doc ID: {doc_id})")
        else:
            new_doc = {
                "title": movie_title,
                "type": "movie",
                "category": "HollyWood",
                "genre": "Action",
                "description": f"Auto-added movie {movie_title}",
                "posterUrl": "",
                "videoUrl": new_link.get('url', ''),
                "tmdbId": int(tmdb_id),
                "releaseDate": f"{year}-01-01" if year else "2024-01-01",
                "runtime": 120,
                "voteAverage": 7.5,
                "voteCount": 100,
                "director": "Unknown",
                "tagline": "",
                "topCast": [],
                "backdrops": [],
                "downloadLinks": updated_links,
                "isFeatured": False,
                "createdAt": int(time.time() * 1000)
            }
            _, new_ref = db.collection('movies').add(new_doc)
            print(f"🎉 Created new movie document in Firestore with [{norm_q}] link! (Doc ID: {new_ref.id})")
        return updated_links
    except Exception as e:
        print(f"❌ Error updating Firestore: {e}")
        return [new_link]

# ==============================================================================
# ★ ৫. নির্ভরযোগ্য ইনলাইন বাটন ক্লিক মেকানিজম (MTProto, Deep-Link, Un-cancellable Click) ★
# ==============================================================================
async def _execute_click_safely(coro, desc: str):
    """ক্লিক সিগন্যাল টেলিগ্রাম সার্ভারে পৌঁছে দেয়—বটের টোস্ট নোটিফিকেশন থাকুক বা না থাকুক"""
    try:
        await coro
        print(f"✅ [{desc}] processed by Telegram successfully!")
    except Exception as err:
        # অনেক বট আলাদা পপআপ অ্যালার্ট দেয় না, সরাসরি ফাইল ছেড়ে দেয়—তাই এটি সম্পূর্ণ স্বাভাবিক
        print(f"⚡ [{desc}] dispatched to server (status: {err})")

async def click_inline_button(client: TelegramClient, message: Any, button: Any, row_idx: int, col_idx: int) -> bool:
    """যেকোনো ইনলাইন বাটন ঠিক মানুষের আঙুলের স্পর্শের মতো ব্যাকগ্রাউন্ডে আন-ক্যান্সেলডভাবে ফায়ার করে"""
    btn_text = button.text.strip() if hasattr(button, 'text') else str(button)
    print(f"👉 Initiating un-cancellable click on: [{btn_text}] at ({row_idx}, {col_idx})")

    # ধাপ ক: মেসেজটি 'Read' হিসেবে টেলিগ্রাম সার্ভারে মার্ক করা (ঠিক যেমন মোবাইলে মেসেজ খুললে হয়)
    try:
        await client.send_read_acknowledge(PAP_KORN_BOT, message=message)
    except Exception:
        pass

    # বাটনের আসল ডাটা বা লিংক এক্সট্রাক্ট করা
    btn_data = getattr(button, 'data', None)
    if not btn_data and hasattr(button, 'button'):
        btn_data = getattr(button.button, 'data', None)

    btn_url = getattr(button, 'url', None)
    if not btn_url and hasattr(button, 'button'):
        btn_url = getattr(button.button, 'url', None)

    # ধাপ খ: যদি বাটনটি টেলিগ্রাম ডিপ-লিংক (/start ...) হয়
    if btn_url and "start=" in btn_url:
        print(f"🔗 Detected deep-link URL: {btn_url}")
        m = re.search(r'start=([a-zA-Z0-9_-]+)', btn_url)
        if m:
            payload = m.group(1)
            print(f"🚀 Sending '/start {payload}' to @{PAP_KORN_BOT}...")
            try:
                await client.send_message(PAP_KORN_BOT, f"/start {payload}")
                return True
            except Exception as e:
                print(f"⚠️ Deep-link send error: {e}")

    # ধাপ গ: সরাসরি টেলিগ্রাম কোর MTProto ও নেটিভ ক্লিক ফায়ার করা (কখনোই ক্যানসেল হবে না)
    click_dispatched = False

    # ১. সরাসরি MTProto GetBotCallbackAnswerRequest (ব্যাকগ্রাউন্ড টাস্ক)
    if btn_data:
        try:
            input_peer = await client.get_input_entity(PAP_KORN_BOT)
            print(f"📡 Dispatching MTProto GetBotCallbackAnswerRequest ({len(btn_data)} bytes)...")
            asyncio.create_task(
                _execute_click_safely(
                    client(GetBotCallbackAnswerRequest(
                        peer=input_peer,
                        msg_id=message.id,
                        data=btn_data
                    )),
                    "MTProto Raw Callback"
                )
            )
            click_dispatched = True
        except Exception as e:
            print(f"⚠️ MTProto prep error: {e}")

    # ২. টেলিথনের নেটিভ message.click(row_idx, col_idx) (ব্যাকগ্রাউন্ড টাস্ক)
    try:
        print(f"🔄 Dispatching native message.click({row_idx}, {col_idx})...")
        asyncio.create_task(
            _execute_click_safely(
                message.click(row_idx, col_idx),
                f"Native message.click({row_idx}, {col_idx})"
            )
        )
        click_dispatched = True
    except Exception as e:
        print(f"⚠️ message.click prep error: {e}")

    # ৩. বাটন অবজেক্টের সরাসরি click()
    if hasattr(button, 'click'):
        try:
            asyncio.create_task(
                _execute_click_safely(
                    button.click(),
                    "button.click()"
                )
            )
            click_dispatched = True
        except Exception:
            pass

    # ০.৫ সেকেন্ড অপেক্ষা যাতে নেটওয়ার্ক প্যাকেটগুলো সফলভাবে টেলিগ্রাম ক্লাউডে পাঠিয়ে দেওয়া যায়
    await asyncio.sleep(0.5)
    return click_dispatched

# ==============================================================================
# ★ ৬. অন-ডিমান্ড সিঙ্গেল-কোয়ালিটি টেলিগ্রাম সার্চ ও এক্সট্রাকশন ইঞ্জিন ★
# ==============================================================================
async def fetch_single_quality_from_telegram(title: str, year: str, tmdb_id: int, quality: str, request: Optional[Request] = None) -> Optional[Dict[str, Any]]:
    norm_q = normalize_quality(quality)
    async with telegram_queue_lock:
        print(f"\n=======================================================")
        print(f"⚡ ON-DEMAND SINGLE REQUEST: '{title} ({year})' -> [{norm_q.upper()}]")
        print(f"=======================================================")

        try:
            # চেক ১: ইউজার কি ইতোমধ্যে ট্যাব বন্ধ করে দিয়েছে?
            if request and await request.is_disconnected():
                print(f"🛑 User disconnected before search. Aborting.")
                return None

            # ধাপ ১: @iPapkornS2bot এ সার্চ পাঠানো
            search_query = f"{title} {year}".strip() if year else title
            print(f"🔎 Sending search query to @{PAP_KORN_BOT}: '{search_query}'")
            sent_msg = await client.send_message(PAP_KORN_BOT, search_query)

            # ধাপ ২: সার্চ রেজাল্ট বাটন আসার জন্য অপেক্ষা
            print(f"⏳ Waiting for result buttons from @{PAP_KORN_BOT}...")
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
                # যদি সাল সহ রেজাল্ট না আসে, শুধু টাইটেল দিয়ে রিট্রাই
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
                return None

            print(f"📩 Search results arrived! Total button rows: {len(results_msg.buttons)}")
            for r, row in enumerate(results_msg.buttons):
                for c, b in enumerate(row):
                    print(f"   [{r}][{c}] -> '{b.text.strip()}'")

            # ধাপ ৩: এই বাটন মেসেজটি কি "মুভি লিস্ট" (Movie Selection List) নাকি সরাসরি "কোয়ালিটি/ফাইল লিস্ট"?
            all_btn_texts = [b.text.strip() for row in results_msg.buttons for b in row]
            has_quality_files = any(any(u in txt.lower() for u in ['1080p', '720p', '480p', 'gb', 'mb']) for txt in all_btn_texts)

            if not has_quality_files:
                # এটি একটি মুভি লিস্ট (Movie Selection List)!
                print(f"🎬 Movie List detected ({len(all_btn_texts)} movie choices)! Selecting best matching movie...")
                movie_choice = None
                title_lower = title.lower().strip()

                # মুভির টাইটেল ও সালের সাথে ম্যাচিং
                for r_idx, row in enumerate(results_msg.buttons):
                    for c_idx, btn in enumerate(row):
                        b_text = btn.text.strip()
                        b_lower = b_text.lower()
                        if title_lower in b_lower:
                            movie_choice = (r_idx, c_idx, btn, b_text)
                            break
                    if movie_choice:
                        break

                # টাইটেল হুবহু না পেলে হেডার বাদ দিয়ে প্রথম মুভি বাটনটি নেওয়া
                if not movie_choice:
                    for r_idx, row in enumerate(results_msg.buttons):
                        for c_idx, btn in enumerate(row):
                            b_text = btn.text.strip()
                            if not any(bad in b_text.lower() for bad in EXCLUDE_KEYWORDS):
                                movie_choice = (r_idx, c_idx, btn, b_text)
                                break
                        if movie_choice:
                            break

                if not movie_choice:
                    movie_choice = (0, 0, results_msg.buttons[0][0], results_msg.buttons[0][0].text.strip())

                mr_idx, mc_idx, movie_btn, movie_btn_text = movie_choice
                print(f"👉 Clicking Movie List Item: [{movie_btn_text}] at ({mr_idx}, {mc_idx})...")

                last_msg_before_movie = results_msg.id
                await click_inline_button(client, results_msg, movie_btn, mr_idx, mc_idx)

                # এখন বট মুভি সিলেক্ট করার পর কোয়ালিটি বাটন পাঠাবে (মেসেজ এডিট করতে পারে অথবা নতুন মেসেজ পাঠাতে পারে)
                print("⏳ Waiting for quality/file buttons to arrive after movie selection...")
                quality_msg = None
                for _ in range(12):  # ২৪ সেকেন্ড পর্যন্ত পোলিং
                    await asyncio.sleep(2)
                    # চেক ১: বর্তমান মেসেজটি কি এডিট হয়ে কোয়ালিটি এসেছে?
                    try:
                        refreshed = await client.get_messages(PAP_KORN_BOT, ids=results_msg.id)
                        if refreshed and refreshed.buttons:
                            ref_texts = " ".join(b.text.lower() for row in refreshed.buttons for b in row)
                            if any(u in ref_texts for u in ['1080p', '720p', '480p', 'gb', 'mb']):
                                quality_msg = refreshed
                                print("✅ Quality buttons appeared in edited message!")
                                break
                    except Exception:
                        pass

                    # চেক ২: নতুন কোনো মেসেজ এসেছে কি না?
                    new_msgs = await client.get_messages(PAP_KORN_BOT, limit=3)
                    for nm in new_msgs:
                        if nm.id > last_msg_before_movie and nm.buttons:
                            nm_texts = " ".join(b.text.lower() for row in nm.buttons for b in row)
                            if any(u in nm_texts for u in ['1080p', '720p', '480p', 'gb', 'mb']):
                                quality_msg = nm
                                print("✅ Quality buttons appeared in new message!")
                                break
                    if quality_msg:
                        break

                if quality_msg:
                    results_msg = quality_msg
                    print(f"📩 Quality buttons loaded! Total rows: {len(results_msg.buttons)}")
                    for r, row in enumerate(results_msg.buttons):
                        for c, b in enumerate(row):
                            print(f"   [{r}][{c}] -> '{b.text.strip()}'")
                else:
                    print("⚠️ Quality buttons not detected after movie click. Checking available buttons...")

            # ধাপ ৪: নির্দিষ্ট কোয়ালিটির সেরা বাটনটি খুঁজে বের করা (Dual Audio, 1080p, 720p, 480p)
            target_match = None
            fallback_match = None

            for r_idx, row in enumerate(results_msg.buttons):
                for c_idx, btn in enumerate(row):
                    b_text = btn.text.strip()
                    b_lower = b_text.lower()

                    # হেডার বাটন বা ক্যাম/স্যাম্পল বাদ
                    if any(bad in b_lower for bad in EXCLUDE_KEYWORDS):
                        continue

                    # প্রথম ভ্যালিড বাটনটি ফলব্যাক হিসেবে রাখা
                    if not fallback_match:
                        fallback_match = (r_idx, c_idx, btn, b_text)

                    # কাঙ্ক্ষিত কোয়ালিটির সাথে ম্যাচিং
                    if norm_q == 'dual' and any(t in b_lower for t in ['dual', 'hindi', 'multi']):
                        target_match = (r_idx, c_idx, btn, b_text)
                        break
                    elif norm_q == '1080p' and '1080p' in b_lower:
                        target_match = (r_idx, c_idx, btn, b_text)
                        break
                    elif norm_q == '720p' and '720p' in b_lower:
                        target_match = (r_idx, c_idx, btn, b_text)
                        break
                    elif norm_q == '480p' and ('480p' in b_lower or '300mb' in b_lower or ('mb' in b_lower and any(ch.isdigit() for ch in b_lower))):
                        target_match = (r_idx, c_idx, btn, b_text)
                        break
                if target_match:
                    break

            chosen = target_match or fallback_match
            if not chosen:
                print(f"❌ No suitable button could be found for quality '{norm_q}'")
                return None

            r_idx, c_idx, target_btn, target_btn_text = chosen
            print(f"🎯 Target Quality [{norm_q}] Selected: [{target_btn_text}] at ({r_idx}, {c_idx})")

            # চেক ২: ইউজার কি এখনো আছে?
            if request and await request.is_disconnected():
                print(f"🛑 User left before button click. Aborting.")
                return None

            # বাটন ক্লিক করার আগের সর্বশেষ মেসেজ আইডি মনে রাখা
            last_bot_msgs = await client.get_messages(PAP_KORN_BOT, limit=1)
            last_papkorn_id = last_bot_msgs[0].id if last_bot_msgs else 0

            # ধাপ ৫: কোয়ালিটি বাটনে ক্লিক করা (কোর সিগন্যাল ও ফলব্যাক সহ)
            await click_inline_button(client, results_msg, target_btn, r_idx, c_idx)

            # ধাপ ৫: মুভি ফাইল আসার অপেক্ষা (সর্বোচ্চ ২৫ সেকেন্ড)
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
                print(f"⚠️ Movie file not received from @{PAP_KORN_BOT} for '{target_btn_text}'")
                return None

            file_size_bytes = movie_file_msg.file.size if movie_file_msg.file else 0
            file_size_str = format_size(file_size_bytes)
            file_name = movie_file_msg.file.name if (movie_file_msg.file and movie_file_msg.file.name) else target_btn_text
            print(f"📦 Movie file received: {file_name} ({file_size_str})")

            # চেক ৩: ইউজার কি এখনো কানেক্টেড?
            if request and await request.is_disconnected():
                print(f"🛑 User disconnected before forwarding. Aborting.")
                return None

            # ধাপ ৬: ফাইলটি @LinkFilesBot এ ফরওয়ার্ড করা
            last_link_msgs = await client.get_messages(LINK_FILES_BOT, limit=1)
            last_link_id = last_link_msgs[0].id if last_link_msgs else 0

            print(f"🚀 Forwarding to @{LINK_FILES_BOT}...")
            try:
                await client.forward_messages(LINK_FILES_BOT, movie_file_msg)
            except Exception as fwd_err:
                print(f"❌ Forward error: {fwd_err}")
                return None

            # ধাপ ৭: @LinkFilesBot থেকে ডাউনলোড লিংক আসার অপেক্ষা
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
                print(f"⚠️ No link response from @{LINK_FILES_BOT}")
                return None

            # শুধুমাত্র Download: লিংক এক্সট্রাক্ট করা
            dl_match = re.search(r'Download:\s*(https?://\S+)', link_reply_text, re.IGNORECASE)
            if dl_match:
                dl_url = dl_match.group(1).strip()
            else:
                fallback_urls = re.findall(r'https?://[^\s\n]+', link_reply_text)
                dl_url = fallback_urls[0] if fallback_urls else None

            if not dl_url:
                print(f"⚠️ Could not extract download link from:\n{link_reply_text}")
                return None

            # সুন্দর লেবেল নির্ধারণ
            label_map = {
                "dual": "Dual Audio / Hindi",
                "1080p": "1080p Full HD",
                "720p": "720p HD Quality",
                "480p": "480p SD Mobile"
            }
            clean_title = label_map.get(norm_q, f"{norm_q.upper()} Quality")
            
            link_obj = {
                "quality": norm_q,
                "label": f"{clean_title} [{file_size_str}]",
                "size": file_size_str,
                "url": dl_url
            }

            print(f"✅ Successfully extracted [{norm_q}] link: {dl_url}")

            # ধাপ ৮: ফায়ারস্টোরে এই কোয়ালিটির লিংকটি সেভ করা
            save_single_quality_link_in_firestore(tmdb_id, title, year, link_obj)

            return link_obj

        except asyncio.CancelledError:
            print(f"🛑 Cancelled because user closed the tab for '{title}'.")
            return None
        except Exception as e:
            print(f"❌ Error in single quality fetch: {e}")
            return None
        finally:
            await asyncio.sleep(2)

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
    quality: Optional[str] = "1080p"

@app.get("/")
def home():
    return {
        "status": "online",
        "service": "On-Demand Movie Link Generator",
        "mode": "Single-Quality On-Demand (Dual, 1080p, 720p, 480p)",
        "database": "Firestore (movie-box-96be3)"
    }

@app.post("/api/get-download")
async def get_download_links_post(req: DownloadRequest, request: Request):
    return await handle_download_request(request, req.tmdbId, req.title, req.year or "", req.quality or "1080p")

@app.get("/api/get-download")
async def get_download_links_get(
    request: Request,
    tmdbId: int = Query(..., description="TMDB ID"),
    title: str = Query(..., description="Movie Title"),
    year: str = Query("", description="Release Year"),
    quality: str = Query("1080p", description="Requested Quality: 'dual', '1080p', '720p', '480p'")
):
    return await handle_download_request(request, tmdbId, title, year, quality)

async def handle_download_request(request: Request, tmdb_id: int, title: str, year: str, quality: str = "1080p"):
    norm_q = normalize_quality(quality)

    # ধাপ ১: ফায়ারস্টোরে আগে থেকেই এই নির্দিষ্ট কোয়ালিটির লিংক আছে কি না চেক (Instant Cache Return - 0s)
    doc_id, cached_links = find_cached_links_in_firestore(tmdb_id)
    matched_link = find_matching_quality_link(cached_links, norm_q)
    if matched_link:
        print(f"⚡ INSTANT CACHE HIT: '{title}' [{norm_q}] link already present in Firestore! (0s delay)")
        return {
            "success": True,
            "source": "firestore_cache",
            "tmdbId": tmdb_id,
            "title": title,
            "quality": norm_q,
            "link": matched_link,
            "allLinks": cached_links
        }

    # ইউজার যদি ব্যাকএন্ডে পৌঁছানোর আগেই বন্ধ করে দেয়
    if await request.is_disconnected():
        print(f"🛑 Client disconnected before processing '{title}'.")
        return {"success": False, "detail": "User disconnected"}

    # ধাপ ২: ফায়ারবেসে না থাকলে অন-ডিমান্ড টেলিগ্রাম থেকে শুধুমাত্র এই কোয়ালিটির ফাইল এনে লিংক বানানো
    print(f"🔍 No cached link for '{title}' [{norm_q}]. Fetching single quality live from Telegram...")
    new_link = await fetch_single_quality_from_telegram(title, year, tmdb_id, norm_q, request)

    if not new_link:
        raise HTTPException(
            status_code=404, 
            detail=f"Could not generate download link for '{title}' in quality '{norm_q}' on Telegram."
        )

    # রিফ্রেশ করা অল লিংকস ফায়ারস্টোর থেকে আনা
    _, updated_links = find_cached_links_in_firestore(tmdb_id)

    return {
        "success": True,
        "source": "telegram_live_generated",
        "tmdbId": tmdb_id,
        "title": title,
        "quality": norm_q,
        "link": new_link,
        "allLinks": updated_links or [new_link]
    }

# ==============================================================================
# ★ ৭. লোকাল রান করার কোড ★
# ==============================================================================
if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get('PORT', 8000))
    # সরাসরি app অবজেক্ট দিয়ে রান করা (যাতে কোনো module import lookup failure না হয়)
    uvicorn.run(app, host="0.0.0.0", port=port)
