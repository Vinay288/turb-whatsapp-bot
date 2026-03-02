import os, json, requests, gspread, time, re
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Response, Query
from oauth2client.service_account import ServiceAccountCredentials

app = FastAPI()

# --- CONFIG ---
WA_TOKEN = os.getenv("WA_TOKEN")
PHONE_ID = os.getenv("PHONE_ID")
VERIFY_TOKEN = "MY_TURF_TOKEN_123"

# 6 AM to 6 PM = 12 total hourly slots
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
        print("✅ Connected to Sheet: turf")
    except Exception as e:
        print(f"❌ Connection Error: {e}")
        db = None

connect_to_sheet()
user_sessions = {}

# --- SLOT LOGIC ---

def get_slots_info(date_str):
    if db is None: return ([], TOTAL_CAPACITY_PER_DAY)
    try:
        records = db.get_all_records()
        booked = [r.get('Time') for r in records if str(r.get('Date')) == date_str and r.get('Status') == 'Confirmed']
        left = max(0, TOTAL_CAPACITY_PER_DAY - len(booked))
        return (booked, left)
    except:
        return ([], TOTAL_CAPACITY_PER_DAY)

def sync_by_session(session_id, phone, name=None, date=None, time_val=None, status="Enquiry"):
    if db is None: return
    try:
        sessions_col = db.col_values(7) # Column G
        if session_id in sessions_col:
            row = sessions_col.index(session_id) + 1
            if name: db.update_cell(row, 6, name)
            if date: db.update_cell(row, 3, date)
            if time_val: db.update_cell(row, 4, time_val)
            db.update_cell(row, 5, status)
            db.update_cell(row, 1, str(datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        else:
            db.append_row([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), str(phone), date or "", time_val or "", status, name or "", session_id])
    except Exception as e:
        print(f"Sync Error: {e}")

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
        entry = data['entry'][0]['changes'][0]['value']
        if "messages" not in entry: return {"ok": True}
        msg = entry['messages'][0]
        phone = msg['from']
        if db is None: connect_to_sheet()
        
        text = msg.get("text", {}).get("body", "").lower().strip()
        is_greeting = re.search(r"\b(hi|hello|hey|start|book|turf|match|play|slots)\b", text)

        # 1. WELCOME MESSAGE
        if is_greeting:
            if phone not in user_sessions:
                new_sess = f"{phone}_{int(time.time())}"
                user_sessions[phone] = {"session_id": new_sess, "state": "START"}
                sync_by_session(new_sess, phone, status="Started Enquiry")
            
            welcome_msg = (
                "Welcome to *The GSS Turf* ⚽🏏🔥\n\n"
                "Ready to play?\n"
                "Book your slot, gather your squad, and let’s kick off and hit it out of the park! 🥅🏏\n\n"
                "🏟️ *Premium Football & Cricket Turf*\n\n"
                "💰 *Only ₹900 per hour*\n\n"
                "📍 *Located at:* Bengaluru Central / [Maps Link]\n\n"
                "👇 Tap below to secure your slot now."
            )
            
            send_wa(phone, {
                "messaging_product": "whatsapp", "to": phone, "type": "interactive",
                "interactive": {
                    "type": "button", 
                    "body": {"text": welcome_msg},
                    "action": {"buttons": [{"type": "reply", "reply": {"id": "book", "title": "⚡ Book Now"}}]}}
            })

        # 2. NAME
        elif msg.get("type") == "interactive" and msg["interactive"].get("button_reply", {}).get("id") == "book":
            user_sessions[phone]["state"] = "NAME"
            send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": "Great! Please type your *Full Name*:"}})

        # 3. DATE LIST (7 DAYS)
        elif phone in user_sessions and user_sessions[phone]["state"] == "NAME":
            name = msg["text"]["body"]
            user_sessions[phone].update({"state": "DATE", "name": name})
            sync_by_session(user_sessions[phone]["session_id"], phone, name=name, status="Entered Name")
            
            today = datetime.now()
            rows = []
            for i in range(7):
                d_obj = today + timedelta(days=i)
                d_str = d_obj.strftime('%Y-%m-%d')
                _, left = get_slots_info(d_str)
                rows.append({
                    "id": f"date_{d_str}", 
                    "title": d_str, 
                    "description": f"{d_obj.strftime('%A')} | {left} slots left"
                })

            send_wa(phone, {
                "messaging_product": "whatsapp", "to": phone, "type": "interactive",
                "interactive": {
                    "type": "list", 
                    "header": {"type": "text", "text": "Step 2: Pick Date"},
                    "body": {"text": f"Hi {name}, choose a day for your match:"},
                    "action": {"button": "📅 Select Date", "sections": [{"title": "Availability", "rows": rows}]}}
            })

        # 4. TIME SLOT SELECTION (6 AM TO 6 PM)
        elif msg.get("type") == "interactive" and msg["interactive"].get("list_reply"):
            lid = msg["interactive"]["list_reply"]["id"]
            
            if lid.startswith("date_"):
                date_val = lid.split("_")[1]
                booked, left = get_slots_info(date_val)

                if left == 0:
                    send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": f"❌ No slots available for {date_val}. Please select another day!"}})
                    # Re-trigger date selection automatically
                    return {"ok": True}

                user_sessions[phone].update({"state": "TIME", "date": date_val})
                
                # FULL LIST 6 AM TO 6 PM
                all_times = [
                    "06:00 AM", "07:00 AM", "08:00 AM", "09:00 AM", "10:00 AM", "11:00 AM",
                    "12:00 PM", "01:00 PM", "02:00 PM", "03:00 PM", "04:00 PM", "05:00 PM"
                ]
                available = [t for t in all_times if t not in booked]
                
                time_rows = [{"id": f"time_{date_val}_{s}", "title": s} for s in available]
                send_wa(phone, {
                    "messaging_product": "whatsapp", "to": phone, "type": "interactive",
                    "interactive": {
                        "type": "list", 
                        "header": {"type": "text", "text": "Step 3: Select Time"},
                        "body": {"text": f"Showing available times for *{date_val}*:"},
                        "action": {"button": "🕒 Pick Time", "sections": [{"title": "Morning & Afternoon", "rows": time_rows}]}}
                })

            # 5. FINAL CONFIRM
            elif lid.startswith("time_"):
                _, d_v, t_v = lid.split("_", 2)
                sess = user_sessions[phone]
                sync_by_session(sess["session_id"], phone, date=d_v, time_val=t_v, status="Confirmed")
                
                confirm_body = (
                    "🎉 *BOOKING SUCCESSFUL!*\n\n"
                    f"👤 *Player:* {sess.get('name')}\n"
                    f"📅 *Date:* {d_v}\n"
                    f"⏰ *Time:* {t_v}\n\n"
                    "Ready to play? Let's kick off! ⚽🏏"
                )
                send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": confirm_body}})
                user_sessions.pop(phone, None)

    except Exception as e: print(f"Bot Error: {e}")
    return {"status": "ok"}
