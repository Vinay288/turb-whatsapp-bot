import os, json, requests, gspread, razorpay, smtplib, asyncio
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Response, Query, BackgroundTasks
from email.mime.text import MIMEText
from oauth2client.service_account import ServiceAccountCredentials

app = FastAPI()

# --- CONFIGURATION (Reads from Render Environment Variables) ---
WA_TOKEN = os.getenv("WA_TOKEN")
PHONE_ID = os.getenv("PHONE_ID")
RZP_KEY = os.getenv("RZP_KEY")
RZP_SECRET = os.getenv("RZP_SECRET")
OWNER_EMAIL = os.getenv("OWNER_EMAIL")
EMAIL_PASS = os.getenv("EMAIL_PASS")
VERIFY_TOKEN = "MY_TURF_TOKEN_123"

# --- CLIENT INITIALIZATION ---
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# Handles credentials from a Render Env Var (SHEET_JSON)
if os.getenv("SHEET_JSON"):
    creds_dict = json.loads(os.getenv("SHEET_JSON"))
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
else:
    # Local testing fallback
    creds = ServiceAccountCredentials.from_json_keyfile_name("service_account.json", scope)

db = gspread.authorize(creds).open("turf").sheet1
rzp = razorpay.Client(auth=(RZP_KEY, RZP_SECRET))

# In-memory session tracking
user_sessions = {}

# --- HELPERS ---

def send_wa(to, payload):
    url = f"https://graph.facebook.com/v21.0/{PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    return requests.post(url, json=payload, headers=headers)

def send_owner_alert(details):
    msg = MIMEText(f"New Booking Confirmed!\n\nName: {details['name']}\nPhone: {details['phone']}\nDate: {details['date']}\nTime: {details['time']}")
    msg['Subject'] = f"⚽ New Booking: {details['name']}"
    msg['From'] = OWNER_EMAIL
    msg['To'] = OWNER_EMAIL
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(OWNER_EMAIL, EMAIL_PASS)
            server.send_message(msg)
    except Exception as e: print(f"Mail Error: {e}")

async def schedule_reminder(phone, date_str, time_str):
    try:
        # Expected format: "2026-03-02 07:00 PM"
        book_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %I:%M %p")
        reminder_time = book_dt - timedelta(minutes=30)
        wait_seconds = (reminder_time - datetime.now()).total_seconds()
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)
            send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": "🔔 *Reminder:* Your turf slot starts in 30 minutes!"}})
    except: pass

def get_availability(date_v):
    records = db.get_all_records()
    all_slots = ["06:00 PM", "07:00 PM", "08:00 PM", "09:00 PM", "10:00 PM"]
    # Only block slots that are actually PAID
    booked = [r['Time'] for r in records if r['Date'] == date_v and r['Status'] == 'PAID']
    return [s for s in all_slots if s not in booked]

# --- ENDPOINTS ---
@app.get("/")
async def root_verify(mode: str = Query(None, alias="hub.mode"), 
                      token: str = Query(None, alias="hub.verify_token"), 
                      challenge: str = Query(None, alias="hub.challenge")):
    # This handles the GET request if Meta hits the base URL
    if mode == "subscribe" and token == "MY_TURF_TOKEN_123":
        return Response(content=challenge, media_type="text/plain")
    return {"message": "Turf Bot is Running!"}
                          
@app.get("/webhook")
async def verify(mode: str = Query(None, alias="hub.mode"), token: str = Query(None, alias="hub.verify_token"), challenge: str = Query(None, alias="hub.challenge")):
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain")
    return {"status": "ok"}

@app.post("/webhook")
async def handle_whatsapp(request: Request):
    data = await request.json()
    try:
        entry = data['entry'][0]['changes'][0]['value']
        if "messages" not in entry: return {"ok": True}
        msg = entry['messages'][0]
        phone = msg['from']
        text = msg.get("text", {}).get("body", "").lower()

        # Step 1: Welcome
        if text in ["hi", "hello", "menu"]:
            user_sessions[phone] = {"state": "START"}
            body = "🏟️ Welcome to *GSS Turf*\n📍 Bagalkot\n💰 ₹800 (6AM to 6PM) | ₹1000 (6PM to 11PM)\n\nReady to play?"
            send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "interactive", "interactive": {
                "type": "button", "body": {"text": body}, "action": {"buttons": [{"type": "reply", "reply": {"id": "book", "title": "⚽ Book Now"}}]}}})

        # Step 2: Ask Name
        elif msg.get("type") == "interactive" and msg["interactive"].get("button_reply", {}).get("id") == "book":
            user_sessions[phone] = {"state": "NAME"}
            send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": "Great! Please enter your Full Name:"}})

        # Step 3: Name Received -> Show Dates
        elif user_sessions.get(phone, {}).get("state") == "NAME":
            name = msg["text"]["body"]
            user_sessions[phone].update({"name": name, "state": "DATE"})
            today = datetime.now()
            rows = [{"id": f"date_{(today + timedelta(days=i)).strftime('%Y-%m-%d')}", "title": (today + timedelta(days=i)).strftime('%Y-%m-%d')} for i in range(4)]
            send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "interactive", "interactive": {
                "type": "list", "header": {"type": "text", "text": "Pick a Date"}, "body": {"text": f"Hi {name}, select a day:"},
                "action": {"button": "Select Date", "sections": [{"title": "Dates", "rows": rows}]}}})

        # Step 4: Handle Selections
        elif msg.get("type") == "interactive" and msg["interactive"].get("list_reply"):
            sid = msg["interactive"]["list_reply"]["id"]
            
            if sid.startswith("date_"):
                date_v = sid.split("_")[1]
                user_sessions[phone].update({"date": date_v})
                avail = get_availability(date_v)
                rows = [{"id": f"time|{date_v}|{s}", "title": s} for s in avail]
                send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "interactive", "interactive": {
                    "type": "list", "header": {"type": "text", "text": "Pick Time"}, "body": {"text": f"Slots for {date_v}:"},
                    "action": {"button": "Select Time", "sections": [{"title": "Slots", "rows": rows}]}}})
            
            elif sid.startswith("time|"):
                _, dv, tv = sid.split("|")
                # Double-check availability (Race Condition)
                if tv not in get_availability(dv):
                    send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": "❌ Slot just taken! Please pick another."}})
                    return {"ok": True}
                
                u_name = user_sessions[phone].get("name", "Customer")
                # Create Razorpay Link (Amount in Paise, so 50000 = Rs 500)
                pay_link = rzp.payment_link.create({
                    "amount": 50000, "currency": "INR", "description": f"Turf Booking {dv}",
                    "customer": {"name": u_name, "contact": phone},
                    "notes": {"phone": phone, "date": dv, "time": tv, "name": u_name}
                })['short_url']
                
                db.append_row([str(datetime.now()), phone, dv, tv, "Pending", u_name])
                send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": f"Confirming {tv} on {dv}.\n\n💳 *Pay here to lock:* {pay_link}"}})

    except Exception as e: print(f"Error: {e}")
    return {"ok": True}

@app.post("/razorpay-webhook")
async def rzp_webhook(request: Request, bg_tasks: BackgroundTasks):
    data = await request.json()
    if data['event'] == "payment_link.paid":
        notes = data['payload']['payment_link']['entity']['notes']
        # Update GSheet Status
        cell = db.find(notes['phone'])
        db.update_cell(cell.row, 5, "PAID")
        # Trigger Background Notification & Reminder
        bg_tasks.add_task(send_owner_alert, notes)
        bg_tasks.add_task(schedule_reminder, notes['phone'], notes['date'], notes['time'])
        send_wa(notes['phone'], {"messaging_product": "whatsapp", "to": notes['phone'], "type": "text", "text": {"body": "✅ *Payment Successful!* Your turf is reserved. See you soon!"}})
    return {"status": "ok"}
