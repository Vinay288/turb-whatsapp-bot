import os, json, requests, gspread, time, re
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Response, Query
from oauth2client.service_account import ServiceAccountCredentials

app = FastAPI()

# --- CONFIG ---
WA_TOKEN = os.getenv("WA_TOKEN")
PHONE_ID = os.getenv("PHONE_ID")
VERIFY_TOKEN = "MY_TURF_TOKEN_123"
TOTAL_CAPACITY_PER_DAY = 12 

# --- GOOGLE SHEETS SETUP ---
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
db = None

def connect_to_sheet():
    global db
    try:
        creds_dict = json.loads(os.getenv("SHEET_JSON"))
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        gc = gspread.authorize(creds)
        db = gc.open("turf").sheet1
    except Exception as e:
        print(f"❌ Sheet Error: {e}")
        db = None

connect_to_sheet()
user_sessions = {}

# --- CORE LOGIC ---

def get_user_name_if_exists(phone):
    """Checks the sheet for this phone number and returns the most recent name."""
    if db is None: return None
    try:
        records = db.get_all_records()
        for r in reversed(records):
            if str(r.get('Phone')).strip() == str(phone).strip() and r.get('Name'):
                return str(r.get('Name')).strip()
    except: return None
    return None

def get_slots_info(date_str):
    if db is None: return ([], TOTAL_CAPACITY_PER_DAY)
    try:
        print(date_str)
        records = db.get_all_records()
        booked = [str(r.get('Time')).strip() for r in records if str(r.get('Date')).strip() == date_str and str(r.get('Status')).strip().lower() == 'confirmed']
        print(booked)
        left = max(0, TOTAL_CAPACITY_PER_DAY - len(booked))
        return (booked, left)
    except: return ([], TOTAL_CAPACITY_PER_DAY)

def sync_by_session(session_id, phone, name=None, date=None, time_val=None, status="Enquiry"):
    if db is None: return
    try:
        sessions_col = db.col_values(7) 
        if session_id in sessions_col:
            row = sessions_col.index(session_id) + 1
            if name: db.update_cell(row, 6, name)
            if date: db.update_cell(row, 3, date)
            if time_val: db.update_cell(row, 4, time_val)
            db.update_cell(row, 5, status)
        else:
            db.append_row([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), str(phone), date or "", time_val or "", status, name or "", session_id])
    except: pass

def send_wa(to, payload):
    url = f"https://graph.facebook.com/v21.0/{PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    return requests.post(url, json=payload, headers=headers)

def send_date_menu(phone, user_name):
    """Reusable function to show the Date Menu with slots info"""
    today = datetime.now()
    rows = []
    for i in range(7):
        d_str = (today + timedelta(days=i)).strftime('%Y-%m-%d')
        _, left = get_slots_info(d_str)
        rows.append({
            "id": f"date_{d_str}", 
            "title": d_str, 
            "description": f"{(today+timedelta(days=i)).strftime('%A')} | {left} slots left"
        })
    
    send_wa(phone, {
        "messaging_product": "whatsapp", "to": phone, "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": "Step 2: Pick Date"},
            "body": {"text": f"Hi {user_name}, pick your match date below:"},
            "action": {"button": "📅 Select Date", "sections": [{"title": "Availability", "rows": rows}]}}
    })

# --- WEBHOOK ---

@app.get("/webhook")
async def verify(mode: str = Query(None, alias="hub.mode"), token: str = Query(None, alias="hub.verify_token"), challenge: str = Query(None, alias="hub.challenge")):
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain")
    return {"status": "online"}

@app.post("/webhook")
async def handle_whatsapp(request: Request):
    data = await request.json()
    try:
        entry = data.get('entry', [{}])[0].get('changes', [{}])[0].get('value', {})
        if "messages" not in entry: return {"ok": True}
        msg = entry['messages'][0]
        phone = msg['from']
        
        # 1. INTERACTIVE ACTIONS
        if msg.get("type") == "interactive":
            sid = (msg["interactive"].get("list_reply") or msg["interactive"].get("button_reply", {})).get("id")
            
            if sid.startswith("date_"):
                date_val = sid.split("_")[1]
                user_sessions[phone] = user_sessions.get(phone, {"session_id": f"{phone}_{int(time.time())}"})
                user_sessions[phone].update({"date": date_val})
                send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "interactive", "interactive": {
                    "type": "button", "body": {"text": f"📅 *Date:* {date_val}\n\nPick a time bracket:"},
                    "action": {"buttons": [{"type": "reply", "reply": {"id": f"brk_{date_val}_AM", "title": "🌅 Morning"}}, {"type": "reply", "reply": {"id": f"brk_{date_val}_PM", "title": "☀️ Afternoon"}}]}}})

            elif sid.startswith("brk_"):
                _, d_v, brk = sid.split("_")
                booked, _ = get_slots_info(d_v)
                slots = ["06:00 AM", "07:00 AM", "08:00 AM", "09:00 AM", "10:00 AM", "11:00 AM"] if brk == "AM" else ["12:00 PM", "01:00 PM", "02:00 PM", "03:00 PM", "04:00 PM", "05:00 PM"]
                rows = [{"id": f"time_{d_v}_{t}", "title": t} for t in slots if t not in booked]
                send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "interactive", "interactive": {
                    "type": "list", "header": {"type": "text", "text": "Step 3"}, "body": {"text": f"Available {brk} slots for *{d_v}*:"},
                    "action": {"button": "🕒 Pick Time", "sections": [{"title": "Slots", "rows": rows}]}}})

            elif sid.startswith("time_"):
                _, d_v, t_v = sid.split("_", 2)
                booked_now, _ = get_slots_info(d_v)
                if t_v in booked_now:
                    send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": "⚠️ *Slot Taken!* Please pick another time."}})
                else:
                    sess = user_sessions.get(phone, {})
                    sync_by_session(sess.get("session_id", f"{phone}_lost"), phone, date=d_v, time_val=t_v, status="Confirmed")
                    receipt = (f"✅ *BOOKING CONFIRMED!*\n━━━━━━━━━━━━━━\n🏟️ *GSS Turf*\n👤 *Name:* {sess.get('name', 'Guest')}\n📅 *Date:* {d_v}\n⏰ *Time:* {t_v}\n💰 *Price:* ₹900\n━━━━━━━━━━━━━━\nReady to play! ⚽🏏")
                    send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": receipt}})
                    user_sessions.pop(phone, None)

            elif sid == "book":
                known_name = get_user_name_if_exists(phone)
                if known_name:
                    user_sessions[phone] = {"name": known_name, "session_id": f"{phone}_{int(time.time())}"}
                    send_date_menu(phone, known_name)
                else:
                    user_sessions[phone] = {"session_id": f"{phone}_{int(time.time())}", "state": "NAME"}
                    send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": "⚽ *Welcome!*\nPlease type your *Full Name*:"}})

        # 2. TEXT/CATCH-ALL
        else:
            body = msg.get("text", {}).get("body", "").strip()
            sess = user_sessions.get(phone)

            if sess and sess.get("state") == "NAME":
                user_sessions[phone].update({"name": body, "state": "DATE"})
                sync_by_session(sess["session_id"], phone, name=body, status="Enquiry")
                send_date_menu(phone, body)
            else:
                known_name = get_user_name_if_exists(phone)
                welcome = f"⚽ *Welcome back, {known_name}!* \n" if known_name else ""
                welcome_msg = (
                "Welcome to *The GSS Turf* ⚽🏏🔥\n\n"
                "Ready to play?\n"
                "Book your slot, gather your squad, and let’s kick off and hit it out of the park 🥅🏏\n\n"
                "💰 *Only ₹900 per hour*\n\n"
                "👇 Tap below to secure your slot now."
            )
                send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "interactive", "interactive": {
                    "type": "button", "body": {"text": f"{welcome}{welcome_msg}"},
                    "action": {"buttons": [{"type": "reply", "reply": {"id": "book", "title": "⚡ Book Now"}}]}}})

    except Exception as e: print(f"❌ Error: {e}")
    return {"ok": True}
