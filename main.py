import os, json, requests, gspread
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
        db = gc.open("turf").sheet1 # Matches your sheet name 'turf'
        print("✅ Successfully connected to Google Sheet: turf")
    except Exception as e:
        print(f"❌ CONNECTION ERROR: {e}")
        db = None

connect_to_sheet()
user_sessions = {}

# --- SYNC LOGIC (The Fixed Part) ---

def sync_to_sheets(phone, name=None, date=None, time=None, status="In-Progress"):
    if db is None: return
    try:
        # 1. Try to find existing phone in Column 2
        cell = db.find(str(phone), in_column=2)
        row = cell.row
        # 2. Update existing row
        if name: db.update_cell(row, 6, name)
        if date: db.update_cell(row, 3, date)
        if time: db.update_cell(row, 4, time)
        db.update_cell(row, 5, status)
        db.update_cell(row, 1, str(datetime.now()))
    except (gspread.exceptions.CellNotFound, gspread.CellNotFound):
        # 3. Handle 'Not Found' across all library versions
        db.append_row([str(datetime.now()), str(phone), date or "", time or "", status, name or ""])
    except Exception as e:
        # 4. Final safety net so the bot never crashes
        print(f"⚠️ Sheet Sync Error: {e}")

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

        # Logic Flow
        text = msg.get("text", {}).get("body", "").lower()
        if text in ["hi", "hello", "menu"]:
            user_sessions[phone] = {"state": "START"}
            sync_to_sheets(phone, status="Started Enquiry")
            send_wa(phone, {
                "messaging_product": "whatsapp", "to": phone, "type": "interactive",
                "interactive": {
                    "type": "button", "body": {"text": "🏟️ *Pro Turf Arena*\n\nTap below to book!"},
                    "action": {"buttons": [{"type": "reply", "reply": {"id": "book", "title": "⚽ Book Now"}}]}}
            })

        elif msg.get("type") == "interactive" and msg["interactive"].get("button_reply", {}).get("id") == "book":
            user_sessions[phone] = {"state": "NAME"}
            send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": "Great! What is your full name?"}})

        elif user_sessions.get(phone, {}).get("state") == "NAME":
            name = msg["text"]["body"]
            user_sessions[phone] = {"state": "DATE", "name": name}
            sync_to_sheets(phone, name=name, status="Entered Name")
            
            # Simplified Date logic for testing
            d_str = datetime.now().strftime('%Y-%m-%d')
            send_wa(phone, {
                "messaging_product": "whatsapp", "to": phone, "type": "interactive",
                "interactive": {
                    "type": "list", "header": {"type": "text", "text": "Pick Date"},
                    "body": {"text": f"Hi {name}, pick a day:"},
                    "action": {"button": "Select", "sections": [{"title": "Dates", "rows": [{"id": f"d_{d_str}", "title": d_str}]}]}}
            })

        elif msg.get("type") == "interactive" and msg["interactive"].get("list_reply"):
            lid = msg["interactive"]["list_reply"]["id"]
            if lid.startswith("d_"):
                date_val = lid.split("_")[1]
                sync_to_sheets(phone, date=date_val, status="Confirmed")
                send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": f"✅ Confirmed for {date_val}!"}})
                user_sessions.pop(phone, None)

    except Exception as e: print(f"Bot Error: {e}")
    return {"status": "ok"}
