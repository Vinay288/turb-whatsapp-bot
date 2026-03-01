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

# user_sessions now stores: { phone: { "session_id": "...", "state": "...", "name": "..." } }
user_sessions = {}

# --- SESSION-BASED SYNC LOGIC ---

def sync_by_session(session_id, phone, name=None, date=None, time_val=None, status="Enquiry"):
    if db is None: return
    
    # We store the Session ID in a new Column (Column 7) to track unique bookings
    # Columns: 1:Timestamp, 2:Phone, 3:Date, 4:Time, 5:Status, 6:Name, 7:SessionID
    try:
        sessions_col = db.col_values(7)
        if session_id in sessions_col:
            row = sessions_col.index(session_id) + 1
            if name: db.update_cell(row, 6, name)
            if date: db.update_cell(row, 3, date)
            if time_val: db.update_cell(row, 4, time_val)
            db.update_cell(row, 5, status)
            db.update_cell(row, 1, str(datetime.now()))
        else:
            # First time for this session - create the row
            db.append_row([str(datetime.now()), str(phone), date or "", time_val or "", status, name or "", session_id])
    except Exception as e:
        print(f"Session Sync Error: {e}")

# --- BOT LOGIC ---

def send_wa(to, payload):
    url = f"https://graph.facebook.com/v21.0/{PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    return requests.post(url, json=payload, headers=headers)

@app.get("/webhook")
async def verify(mode: str = Query(None, alias="hub.mode"), 
                 token: str = Query(None, alias="hub.verify_token"), 
                 challenge: str = Query(None, alias="hub.challenge")):
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
        
        # 1. START: Generate a New Session ID
        if text in ["hi", "hello", "menu"]:
            new_session_id = f"{phone}_{int(time.time())}"
            user_sessions[phone] = {"session_id": new_session_id, "state": "START"}
            
            sync_by_session(new_session_id, phone, status="Started New Enquiry")
            
            send_wa(phone, {
                "messaging_product": "whatsapp", "to": phone, "type": "interactive",
                "interactive": {
                    "type": "button", "body": {"text": "🏟️ *Pro Turf Arena*\nWelcome back! Ready for a new booking?"},
                    "action": {"buttons": [{"type": "reply", "reply": {"id": "book", "title": "⚽ Book Now"}}]}}
            })

        # 2. NAME: Uses the active session ID
        elif msg.get("type") == "interactive" and msg["interactive"].get("button_reply", {}).get("id") == "book":
            if phone in user_sessions:
                user_sessions[phone]["state"] = "NAME"
                send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": "Great! What is your full name?"}})

        # 3. DATE & SYNC:
        elif phone in user_sessions and user_sessions[phone]["state"] == "NAME":
            name = msg["text"]["body"]
            sess_id = user_sessions[phone]["session_id"]
            
            user_sessions[phone].update({"state": "DATE", "name": name})
            sync_by_session(sess_id, phone, name=name, status="Entered Name")
            
            d_str = datetime.now().strftime('%Y-%m-%d')
            send_wa(phone, {
                "messaging_product": "whatsapp", "to": phone, "type": "interactive",
                "interactive": {
                    "type": "list", "header": {"type": "text", "text": "Pick Date"},
                    "body": {"text": f"Hi {name}, pick a day:"},
                    "action": {"button": "Select", "sections": [{"title": "Dates", "rows": [{"id": f"d_{d_str}", "title": d_str}]}]}}
            })

        # 4. FINAL CONFIRM:
        elif msg.get("type") == "interactive" and msg["interactive"].get("list_reply"):
            lid = msg["interactive"]["list_reply"]["id"]
            if lid.startswith("d_") and phone in user_sessions:
                date_val = lid.split("_")[1]
                sess_id = user_sessions[phone]["session_id"]
                
                sync_by_session(sess_id, phone, date=date_val, status="Confirmed")
                send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": "✅ Booking Saved!"}})
                
                # Close this session
                user_sessions.pop(phone, None)

    except Exception as e: 
        print(f"Bot Error: {e}")
        
    return {"status": "ok"}
