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
        print("✅ SUCCESS: Connected to Google Sheet.")
    except Exception as e:
        print(f"❌ ERROR: Sheet Connection failed: {e}")
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
    except Exception as e:
        print(f"⚠️ Slot Check Error: {e}")
        return ([], TOTAL_CAPACITY_PER_DAY)

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
        print(f"📊 Sheet Synced: Status='{status}' for Session='{session_id}'")
    except Exception as e:
        print(f"⚠️ Sync Error: {e}")

def send_wa(to, payload):
    url = f"https://graph.facebook.com/v21.0/{PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    resp = requests.post(url, json=payload, headers=headers)
    if resp.status_code != 200:
        print(f"❌ WhatsApp API Error: {resp.text}")
    return resp

# --- WEBHOOKS ---
@app.get("/webhook")
async def verify(mode: str = Query(None, alias="hub.mode"), token: str = Query(None, alias="hub.verify_token"), challenge: str = Query(None, alias="hub.challenge")):
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain")
    return {"status": "online"}

@app.post("/webhook")
async def handle_whatsapp(request: Request):
    data = await request.json()
    
    # 📝 DEBUG: SEE INCOMING JSON
    print(f"\n📩 NEW INCOMING MESSAGE: {json.dumps(data, indent=2)}")

    try:
        entry = data.get('entry', [{}])[0].get('changes', [{}])[0].get('value', {})
        if "messages" not in entry: return {"ok": True}
        
        msg = entry['messages'][0]
        phone = msg['from']
        msg_type = msg.get("type")

        # --- 1. HANDLE INTERACTIVE (LISTS/BUTTONS) ---
        if msg_type == "interactive":
            interact = msg.get("interactive", {})
            reply = interact.get("list_reply") or interact.get("button_reply")
            if not reply: return {"ok": True}
            
            selection_id = reply.get("id")
            print(f"🔘 User Clicked ID: {selection_id}")

            # CASE: DATE SELECTED
            if selection_id.startswith("date_"):
                date_val = selection_id.split("_")[1]
                print(f"📅 Step: Date '{date_val}' chosen. Generating time list...")
                
                booked, _ = get_slots_info(date_val)
                
                if phone not in user_sessions:
                    user_sessions[phone] = {"session_id": f"{phone}_{int(time.time())}", "name": "Player"}
                
                user_sessions[phone].update({"state": "TIME", "date": date_val})
                sync_by_session(user_sessions[phone]["session_id"], phone, date=date_val, status="Selecting Time")

                # Split slots into Morning and Afternoon to avoid 10-row limit error
                morning = ["06:00 AM", "07:00 AM", "08:00 AM", "09:00 AM", "10:00 AM", "11:00 AM"]
                afternoon = ["12:00 PM", "01:00 PM", "02:00 PM", "03:00 PM", "04:00 PM", "05:00 PM"]
                
                m_rows = [{"id": f"time_{date_val}_{t}", "title": t} for t in morning if t not in booked]
                a_rows = [{"id": f"time_{date_val}_{t}", "title": t} for t in afternoon if t not in booked]

                payload = {
                    "messaging_product": "whatsapp", "to": phone, "type": "interactive",
                    "interactive": {
                        "type": "list",
                        "header": {"type": "text", "text": "Step 3: Select Time"},
                        "body": {"text": f"Showing available slots for *{date_val}*:"},
                        "footer": {"text": "GSS Turf - 6 AM to 6 PM"},
                        "action": {
                            "button": "🕒 View Slots",
                            "sections": [
                                {"title": "🌅 Morning", "rows": m_rows},
                                {"title": "☀️ Afternoon", "rows": a_rows}
                            ]
                        }
                    }
                }
                send_wa(phone, payload)

            # CASE: TIME SELECTED
            elif selection_id.startswith("time_"):
                _, d_v, t_v = selection_id.split("_", 2)
                print(f"⏰ Step: Time '{t_v}' chosen. Finalizing booking...")
                
                sess = user_sessions.get(phone, {})
                s_id = sess.get("session_id", f"{phone}_lost")
                sync_by_session(s_id, phone, date=d_v, time_val=t_v, status="Confirmed")
                
                confirm_msg = (
                    "🎉 *BOOKING SUCCESSFUL!*\n\n"
                    f"👤 *Player:* {sess.get('name', 'Player')}\n"
                    f"📅 *Date:* {d_v}\n"
                    f"⏰ *Time:* {t_v}\n\n"
                    "Ready to play? Let's kick off and hit it out of the park! ⚽🏏"
                )
                send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": confirm_msg}})
                user_sessions.pop(phone, None)

            # CASE: START BUTTON
            elif selection_id == "book":
                user_sessions[phone] = {"session_id": f"{phone}_{int(time.time())}", "state": "NAME"}
                send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": "Great! Please type your *Full Name*:"}})
            
            return {"ok": True}

        # --- 2. HANDLE TEXT MESSAGES ---
        text_body = msg.get("text", {}).get("body", "").lower().strip()
        
        # Capture Name
        if phone in user_sessions and user_sessions[phone].get("state") == "NAME":
            full_name = msg.get("text", {}).get("body")
            print(f"👤 Step: Name '{full_name}' captured.")
            
            user_sessions[phone].update({"name": full_name, "state": "DATE"})
            sync_by_session(user_sessions[phone]["session_id"], phone, name=full_name, status="Entered Name")
            
            today = datetime.now()
            rows = []
            for i in range(7):
                d_str = (today + timedelta(days=i)).strftime('%Y-%m-%d')
                _, left = get_slots_info(d_str)
                rows.append({"id": f"date_{d_str}", "title": d_str, "description": f"{left} slots open"})

            send_wa(phone, {
                "messaging_product": "whatsapp", "to": phone, "type": "interactive",
                "interactive": {
                    "type": "list",
                    "header": {"type": "text", "text": "Step 2: Date"},
                    "body": {"text": f"Hi {full_name}, choose a date for your match:"},
                    "action": {"button": "📅 Select Date", "sections": [{"title": "Availability", "rows": rows}]}}
            })

        # Greeting
        elif re.search(r"\b(hi|hello|hey|book|start)\b", text_body):
            print(f"👋 Greeting received from {phone}")
            welcome_msg = (
                "Welcome to *The GSS Turf* ⚽🏏🔥\n\n"
                "Ready to play?\n"
                "Book your slot, gather your squad, and let’s kick off! 🥅🏏\n\n"
                "💰 *Only ₹900 per hour*\n\n"
                "👇 Tap below to secure your slot now."
            )
            send_wa(phone, {
                "messaging_product": "whatsapp", "to": phone, "type": "interactive",
                "interactive": {
                    "type": "button", 
                    "body": {"text": welcome_msg},
                    "action": {"buttons": [{"type": "reply", "reply": {"id": "book", "title": "⚡ Book Now"}}]}}
            })

    except Exception as e:
        print(f"🔥 CRITICAL ERROR: {e}")
    
    return {"ok": True}
