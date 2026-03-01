import os, json, requests, gspread
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Response, Query
from oauth2client.service_account import ServiceAccountCredentials

app = FastAPI()

# --- CONFIGURATION ---
WA_TOKEN = os.getenv("WA_TOKEN")
PHONE_ID = os.getenv("PHONE_ID")
VERIFY_TOKEN = "MY_TURF_TOKEN_123"

# --- GOOGLE SHEETS SETUP ---
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_dict = json.loads(os.getenv("SHEET_JSON"))
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
db = gspread.authorize(creds).open("turf").sheet1

user_sessions = {}

# --- HELPER FUNCTIONS ---

def send_wa(to, payload):
    url = f"https://graph.facebook.com/v21.0/{PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    return requests.post(url, json=payload, headers=headers)

def sync_to_sheets(phone, name=None, date=None, time=None, status="In-Progress"):
    """
    Finds the user by phone. If exists, updates the row. 
    If not, creates a new row for the enquiry.
    Columns: Timestamp(1), Phone(2), Date(3), Time(4), Status(5), Name(6)
    """
    try:
        # Check if this user already exists in the Phone column (Column 2)
        cell = db.find(str(phone), in_column=2)
        row = cell.row
        if name: db.update_cell(row, 6, name)
        if date: db.update_cell(row, 3, date)
        if time: db.update_cell(row, 4, time)
        db.update_cell(row, 5, status)
        db.update_cell(row, 1, str(datetime.now())) 
    except gspread.CellNotFound: # FIXED ATTRIBUTE ERROR HERE
        # New Enquiry: Append a fresh row
        db.append_row([str(datetime.now()), str(phone), date or "", time or "", status, name or ""])

def get_live_availability(date_str):
    records = db.get_all_records()
    max_capacity = 10 
    # Counts how many rows for this date have 'Confirmed' status
    booked = [r for r in records if str(r.get('Date')) == date_str and r.get('Status') == 'Confirmed']
    return max(0, max_capacity - len(booked))

# --- WEBHOOK LOGIC ---

@app.get("/webhook")
async def verify(mode: str = Query(None, alias="hub.mode"), 
                 token: str = Query(None, alias="hub.verify_token"), 
                 challenge: str = Query(None, alias="hub.challenge")):
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain")
    return {"status": "bot_active"}

@app.post("/webhook")
async def handle_whatsapp(request: Request):
    data = await request.json()
    try:
        entry = data['entry'][0]['changes'][0]['value']
        if "messages" not in entry: return {"ok": True}
        
        msg = entry['messages'][0]
        phone = msg['from']
        
        # 1. START (HI / MENU)
        if msg.get("type") == "text" and msg["text"]["body"].lower() in ["hi", "hello", "menu"]:
            user_sessions[phone] = {"state": "START"}
            sync_to_sheets(phone, status="Started Enquiry")
            
            body = "🏟️ *Pro Turf Arena*\n📍 Bengaluru\n💰 ₹800 - ₹1200/hr\n\nTap below to check slots!"
            payload = {
                "messaging_product": "whatsapp", "to": phone, "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": body},
                    "action": {"buttons": [{"type": "reply", "reply": {"id": "btn_book", "title": "⚽ Book a Slot"}}]}}
            }
            send_wa(phone, payload)

        # 2. BUTTON: BOOK NOW -> ASK NAME
        elif msg.get("type") == "interactive" and msg["interactive"].get("button_reply"):
            bid = msg["interactive"]["button_reply"]["id"]
            if bid == "btn_book":
                user_sessions[phone] = {"state": "AWAITING_NAME"}
                send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": "Great! What is your full name?"}})

        # 3. NAME CAPTURE -> SHOW DATE MENU
        elif user_sessions.get(phone, {}).get("state") == "AWAITING_NAME":
            user_name = msg["text"]["body"]
            user_sessions[phone] = {"state": "SELECTING_DATE", "name": user_name}
            sync_to_sheets(phone, name=user_name, status="Entered Name")
            
            today = datetime.now()
            date_rows = []
            for i in range(4):
                d_str = (today + timedelta(days=i)).strftime('%Y-%m-%d')
                slots_left = get_live_availability(d_str)
                date_rows.append({
                    "id": f"date_{d_str}", 
                    "title": d_str, 
                    "description": f"{slots_left} slots available"
                })
            
            payload = {
                "messaging_product": "whatsapp", "to": phone, "type": "interactive",
                "interactive": {
                    "type": "list", "header": {"type": "text", "text": "Step 2: Pick a Date"},
                    "body": {"text": f"Hi {user_name}, select a day:"},
                    "action": {"button": "Select Date", "sections": [{"title": "Availability", "rows": date_rows}]}}}
            send_wa(phone, payload)

        # 4. LIST SELECTIONS
        elif msg.get("type") == "interactive" and msg["interactive"].get("list_reply"):
            lid = msg["interactive"]["list_reply"]["id"]
            
            if lid.startswith("date_"):
                date_val = lid.split("_")[1]
                user_sessions[phone]["date"] = date_val
                sync_to_sheets(phone, date=date_val, status="Selected Date")
                
                hours = ["06:00 PM", "07:00 PM", "08:00 PM", "09:00 PM"]
                time_rows = [{"id": f"final|{date_val}|{h}", "title": h} for h in hours]
                payload = {
                    "messaging_product": "whatsapp", "to": phone, "type": "interactive",
                    "interactive": {
                        "type": "list", "header": {"type": "text", "text": "Select Time"},
                        "body": {"text": f"Timings for {date_val}:"},
                        "action": {"button": "Choose Slot", "sections": [{"title": "Evening", "rows": time_rows}]}}}
                send_wa(phone, payload)
            
            elif lid.startswith("final|"):
                _, d_v, t_v = lid.split("|")
                u_name = user_sessions[phone].get("name", "Customer")
                
                sync_to_sheets(phone, date=d_v, time=t_v, status="Confirmed")
                
                confirm_body = f"✅ *Booking Confirmed!*\n\n👤 *Name:* {u_name}\n📅 *Date:* {d_v}\n⏰ *Time:* {t_v}\n\nOur team will contact you shortly. See you there!"
                send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": confirm_body}})
                user_sessions.pop(phone, None) 

    except Exception as e: 
        print(f"Error: {e}")
    return {"status": "ok"}
