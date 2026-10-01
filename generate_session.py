"""
এই স্ক্রিপ্টটি একবার আপনার কম্পিউটারে রান করে টেলিগ্রাম সেশন স্ট্রিং (Session String) তৈরি করে নিন।
এটি Render.com এ Environment Variable (TELEGRAM_STRING_SESSION) হিসেবে দিয়ে দিলে রেন্ডার সার্ভার 
কখনই আর লগইন কোড বা ফোন নম্বর চাইবে না, ২৪ ঘণ্টা নিরবচ্ছিন্নভাবে চলবে!
"""
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID = int(input("Enter your Telegram API ID: "))
API_HASH = input("Enter your Telegram API Hash: ")

with TelegramClient(StringSession(), API_ID, API_HASH) as client:
    print("\n" + "="*50)
    print("✅ আপনার TELEGRAM_STRING_SESSION নিচে দেওয়া হলো:")
    print("="*50)
    print(client.session.save())
    print("="*50)
    print("⚠️ এই স্ট্রিংটি কাউকে দেবেন না। এটি Render-এর Environment Variable-এ যোগ করুন।\n")
