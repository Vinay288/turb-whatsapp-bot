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
        print(f"❌ Sheet Connection Error: {e}")
        db = None

connect_to_sheet()
user_sessions = {}

# --- HELPER FUNCTIONS ---

def get_user_from_db(phone):
    """Checks if user exists in sheet and returns their name."""
    if db is None: return None
    try:
        # Column B is Phone, Column F is Name
        records = db.get_all_records()
        for r in reversed(records): # Search newest first
            if str(r.get('Phone')) == str(phone) and r.get('Name'):
                return r.get('Name')
    except:
        return None
    return None

def get_slots_info(date_str):
    if db is None: return ([], TOTAL_CAPACITY_PER_DAY)
    try:
        records = db.get_all_records()
        booked = [r.get('Time') for r in records if str(r.get('Date')) == date_str and r.get('Status') == 'Confirmed']
        left = max(0, TOTAL_CAPACITY_PER_DAY - len(booked))
        return (booked, left)
    except:
        return ([], TOTAL_CAPACITY_PER_DAY)

def is_slot_still_free(date_str, time_str):
    booked, _ = get_slots_info(date_str)
    return time_str not in booked

def sync_by_session(session_id, phone, name=None, date=None, time_val=None, status="Enquiry", query=None):
    if db is None: return
    try:
        sessions_col = db.col_values(7) 
        if session_id in sessions_col:
            row = sessions_col.index(session_id) + 1
            if name: db.update_cell(row, 6, name)
            if date: db.update_cell(row, 3, date)
            if time_val: db.update_cell(row, 4, time_val)
            if query: db.update_cell(row, 8, query)
            db.update_cell(row, 5, status)
        else:
            db.append_row([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), str(phone), date or "", time_val or "", status, name or "", session_id, query or ""])
    except Exception as e:
        print(f"❌ Sheet Sync Error: {e}")

def send_wa(to, payload):
    url = f"https://graph.facebook.com/v21.0/{PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    return requests.post(url, json=payload, headers=headers)

# --- WEBHOOKS ---

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
        
        # --- 1. HANDLE INTERACTIVE REPLIES ---
        if msg.get("type") == "interactive":
            interact = msg.get("interactive", {})
            reply = interact.get("list_reply") or interact.get("button_reply")
            if not reply: return {"ok": True}
            sid = reply.get("id")

            # DATE SELECTED
            if sid.startswith("date_"):
                date_val = sid.split("_")[1]
                user_sessions[phone] = user_sessions.get(phone, {"session_id": f"{phone}_{int(time.time())}"})
                user_sessions[phone].update({"date": date_val, "state": "SELECTING_TIME"})
                send_wa(phone, {
                    "messaging_product": "whatsapp", "to": phone, "type": "interactive",
                    "interactive": {
                        "type": "button",
                        "body": {"text": f"📅 *Selected Date:* {date_val}\n\nWhen would you like to play?"},
                        "action": {"buttons": [
                            {"type": "reply", "reply": {"id": f"brk_{date_val}_AM", "title": "🌅 Morning"}},
                            {"type": "reply", "reply": {"id": f"brk_{date_val}_PM", "title": "☀️ Afternoon"}}
                        ]}
                    }
                })

            # BRACKET SELECTED
            elif sid.startswith("brk_"):
                _, d_v, brk = sid.split("_")
                booked, _ = get_slots_info(d_v)
                slots = ["06:00 AM", "07:00 AM", "08:00 AM", "09:00 AM", "10:00 AM", "11:00 AM"] if brk == "AM" else ["12:00 PM", "01:00 PM", "02:00 PM", "03:00 PM", "04:00 PM", "05:00 PM"]
                rows = [{"id": f"time_{d_v}_{t}", "title": t} for t in slots if t not in booked]
                send_wa(phone, {
                    "messaging_product": "whatsapp", "to": phone, "type": "interactive",
                    "interactive": {
                        "type": "list",
                        "header": {"type": "text", "text": "Pick Slot"},
                        "body": {"text": f"Available slots for *{d_v}*:"},
                        "action": {"button": "🕒 Select Time", "sections": [{"title": f"{brk} Slots", "rows": rows}]}}
                })

            # FINAL CONFIRMATION
            elif sid.startswith("time_"):
                _, d_v, t_v = sid.split("_", 2)
                if not is_slot_still_free(d_v, t_v):
                    send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": "⚠️ *Slot Taken!* This slot was just booked. Please pick another."}})
                    return {"ok": True}

                sess = user_sessions.get(phone, {})
                sync_by_session(sess.get("session_id", f"{phone}_lost"), phone, date=d_v, time_val=t_v, status="Confirmed")
                
                receipt = (f"✅ *BOOKING CONFIRMED!*\n━━━━━━━━━━━━━━\n🏟️ *The GSS Turf*\n👤 *Name:* {sess.get('name', 'Guest')}\n📅 *Date:* {d_v}\n⏰ *Time:* {t_v}\n💰 *Price:* ₹900\n━━━━━━━━━━━━━━\nSee you on the field! ⚽🏏")
                send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": receipt}})
                user_sessions.pop(phone, None)

            elif sid == "book":
                user_sessions[phone] = {"session_id": f"{phone}_{int(time.time())}", "state": "NAME"}
                send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": "⚽ *GSS Turf*\nPlease type your *Full Name*:"}})

        # --- 2. HANDLE ALL OTHER INPUTS (TEXT, IMAGES, ETC.) ---
        else:
            # We treat every non-interactive message as a potential start or a name input
            body = msg.get("text", {}).get("body", "").strip()
            sess = user_sessions.get(phone)

            # A. If we are waiting for a name
            if sess and sess.get("state") == "NAME":
                user_sessions[phone].update({"name": body, "state": "DATE"})
                sync_by_session(sess["session_id"], phone, name=body, status="Enquiry")
                
                today = datetime.now()
                rows = [{"id": f"date_{(today+timedelta(days=i)).strftime('%Y-%m-%d')}", "title": (today+timedelta(days=i)).strftime('%Y-%m-%d'), "description": f"{(today+timedelta(days=i)).strftime('%A')}"} for i in range(7)]
                send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "interactive", "interactive": {
                    "type": "list", "header": {"type": "text", "text": "Step 2"}, "body": {"text": f"Hi {body}, pick a date:"}, "action": {"button": "📅 Select Date", "sections": [{"title": "Availability", "rows": rows}]}}})

            # B. If user sends any message during an active booking flow (Enquiry)
            elif sess and sess.get("state") in ["DATE", "SELECTING_TIME"]:
                sync_by_session(sess["session_id"], phone, query=body or "Media Input", status="Query Received")
                send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": "📝 *Got it!* I've noted your question. An admin will check it.\n\nYou can continue your booking above."}})

            # C. Catch-All / Welcome Back Logic
            else:
                existing_name = get_user_from_db(phone)
                if existing_name:
                    welcome_text = f"⚽ *Welcome back, {existing_name}!* 🔥\n\nReady for another match at *The GSS Turf*?\n\n💰 *₹900/hour*"
                    user_sessions[phone] = {"name": existing_name, "session_id": f"{phone}_{int(time.time())}"}
                else:
                    welcome_text = "⚽ *The GSS Turf*\n\nWelcome! Book your slot, gather your squad, and let’s play!\n\n💰 *₹900/hour*"
                
                send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "interactive", "interactive": {
                    "type": "button", "body": {"text": welcome_text}, "action": {"buttons": [{"type": "reply", "reply": {"id": "book", "title": "⚡ Book Now"}}]}}})

    except Exception as e:
        print(f"❌ Exception: {e}")
    
    return {"ok": True}
