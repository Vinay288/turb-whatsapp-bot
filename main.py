import os, json, requests, gspread, time
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Response, Query
from oauth2client.service_account import ServiceAccountCredentials

app = FastAPI()

# --- CONFIG ---
WA_TOKEN = os.getenv("WA_TOKEN")
PHONE_ID = os.getenv("PHONE_ID")
VERIFY_TOKEN = "MY_TURF_TOKEN_123"

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

# --- LOGIC HELPERS ---

def get_slots_left(date_str):
    """Calculates availability by counting 'Confirmed' rows for a date"""
    if db is None: return 10
    try:
        records = db.get_all_records()
        max_slots = 10
        booked = [r for r in records if str(r.get('Date')) == date_str and r.get('Status') == 'Confirmed']
        return max(0, max_slots - len(booked))
    except:
        return 10

def sync_by_session(session_id, phone, name=None, date=None, time_val=None, status="Enquiry"):
    """Updates the specific row tied to the unique Session ID"""
    if db is None: return
    try:
        sessions_col = db.col_values(7) # Column G: SessionID
        if session_id in sessions_col:
            row = sessions_col.index(session_id) + 1
            if name: db.update_cell(row, 6, name)
            if date: db.update_cell(row, 3, date)
            if time_val: db.update_cell(row, 4, time_val)
            db.update_cell(row, 5, status)
            db.update_cell(row, 1, str(datetime.now()))
        else:
            db.append_row([str(datetime.now()), str(phone), date or "", time_val or "", status, name or "", session_id])
    except Exception as e:
        print(f"Sync Error: {e}")

def send_wa(to, payload):
    url = f"https://graph.facebook.com/v21.0/{PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    res = requests.post(url, json=payload, headers=headers)
    print(f"Meta Status: {res.status_code} | Body: {res.text}")
    return res

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
        text = msg.get("text", {}).get("body", "").lower()

        # STEP 1: START / HI
        if text in ["hi", "hello", "menu", "start"]:
            new_sess = f"{phone}_{int(time.time())}"
            user_sessions[phone] = {"session_id": new_sess, "state": "START"}
            sync_by_session(new_sess, phone, status="Started Enquiry")
            
            send_wa(phone, {
                "messaging_product": "whatsapp", "to": phone, "type": "interactive",
                "interactive": {
                    "type": "button", "body": {"text": "🏟️ *Pro Turf Arena*\nWelcome! Ready to book a slot?"},
                    "action": {"buttons": [{"type": "reply", "reply": {"id": "book", "title": "⚽ Book Now"}}]}}
            })

        # STEP 2: CLICK BOOK -> ASK NAME
        elif msg.get("type") == "interactive" and msg["interactive"].get("button_reply", {}).get("id") == "book":
            user_sessions[phone]["state"] = "NAME"
            send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": "Great! Please enter your *Full Name*:"}})

        # STEP 3: NAME RECEIVED -> SHOW DATES
        elif phone in user_sessions and user_sessions[phone]["state"] == "NAME":
            name = msg["text"]["body"]
            sess_id = user_sessions[phone]["session_id"]
            user_sessions[phone].update({"state": "DATE", "name": name})
            sync_by_session(sess_id, phone, name=name, status="Entered Name")

            today = datetime.now()
            rows = []
            for i in range(4):
                d_str = (today + timedelta(days=i)).strftime('%Y-%m-%d')
                left = get_slots_left(d_str)
                rows.append({"id": f"date_{d_str}", "title": d_str, "description": f"{left} slots available"})

            send_wa(phone, {
                "messaging_product": "whatsapp", "to": phone, "type": "interactive",
                "interactive": {
                    "type": "list", "header": {"type": "text", "text": "Step 2: Select Date"},
                    "body": {"text": f"Hi {name}, pick a date to see available timings:"},
                    "action": {"button": "Pick Date", "sections": [{"title": "Availability", "rows": rows}]}}
            })

        # STEP 4: DATE SELECTED -> SHOW TIMES
        elif msg.get("type") == "interactive" and msg["interactive"].get("list_reply"):
            lid = msg["interactive"]["list_reply"]["id"]
            
            if lid.startswith("date_"):
                date_val = lid.split("_")[1]
                user_sessions[phone].update({"state": "TIME", "date": date_val})
                sync_by_session(user_sessions[phone]["session_id"], phone, date=date_val, status="Selected Date")

                slots = ["06:00 PM", "07:00 PM", "08:00 PM", "09:00 PM", "10:00 PM"]
                time_rows = [{"id": f"time_{date_val}_{s}", "title": s} for s in slots]
                
                send_wa(phone, {
                    "messaging_product": "whatsapp", "to": phone, "type": "interactive",
                    "interactive": {
                        "type": "list", "header": {"type": "text", "text": "Step 3: Select Time"},
                        "body": {"text": f"Available slots for {date_val}:"},
                        "action": {"button": "Pick Time", "sections": [{"title": "Evening Slots", "rows": time_rows}]}}
                })

            # STEP 5: TIME SELECTED -> FINAL CONFIRMATION
            elif lid.startswith("time_"):
                _, d_v, t_v = lid.split("_")
                sess = user_sessions[phone]
                u_name = sess.get("name", "Customer")
                
                sync_by_session(sess["session_id"], phone, date=d_v, time_val=t_v, status="Confirmed")
                
                msg_body = (
                    f"✅ *Booking Confirmed!*\n\n"
                    f"👤 *Name:* {u_name}\n"
                    f"📅 *Date:* {d_v}\n"
                    f"⏰ *Time:* {t_v}\n"
                    f"📍 *Location:* Bengaluru Turf Central\n\n"
                    f"Please show this message at the counter. See you soon!"
                )
                send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": msg_body}})
                user_sessions.pop(phone, None)

    except Exception as e: print(f"Bot Error: {e}")
    return {"status": "ok"}
