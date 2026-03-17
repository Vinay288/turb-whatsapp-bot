import os
import json
import requests
import gspread
import time
import razorpay
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Response, Query
from oauth2client.service_account import ServiceAccountCredentials

app = FastAPI()

# --- CONFIGURATION ---
WA_TOKEN = os.getenv("WA_TOKEN")
PHONE_ID = os.getenv("PHONE_ID")
RZP_KEY_ID = os.getenv("RZP_KEY_ID")
RZP_KEY_SECRET = os.getenv("RZP_KEY_SECRET")
VERIFY_TOKEN = "MY_TURF_TOKEN_123"

# --- INITIALIZE CLIENTS ---
rzp_client = razorpay.Client(auth=(RZP_KEY_ID, RZP_KEY_SECRET))
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

def get_db():
    try:
        creds_dict = json.loads(os.getenv("SHEET_JSON"))
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        gc = gspread.authorize(creds)
        return gc.open("turf").sheet1
    except Exception as e:
        print(f"❌ Sheet Error: {e}")
        return None

# User session: {"name": str, "chosen_slots": list, "state": str}
user_sessions = {}

# --- HELPER FUNCTIONS ---

def send_wa(to, payload):
    url = f"https://graph.facebook.com/v21.0/{PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    return requests.post(url, json=payload, headers=headers)

def get_user_name_from_db(phone):
    db = get_db()
    if not db: return None
    try:
        records = db.get_all_records()
        for r in reversed(records):
            if str(r.get('Phone', '')).strip() == str(phone).strip() and r.get('Name'):
                return str(r.get('Name')).strip()
    except: pass
    return None

def calculate_price(time_str):
    try:
        t_str = time_str.strip().upper()
        if len(t_str.split(":")[0]) == 1: t_str = "0" + t_str
        time_obj = datetime.strptime(t_str, "%I:%M %p").time()
        day_limit = datetime.strptime("06:00 PM", "%I:%M %p").time()
        return 800 if time_obj < day_limit else 1000
    except:
        return 900

def get_slots_for_date(date_str):
    """
    Returns a list of available slots and the count.
    Logic: All slots from 06:00 AM to 11:00 PM are available by default 
    UNLESS the sheet has a record for that Date/Time with status != 'available'.
    """
    db = get_db()
    now = datetime.now()
    
    # Define the full schedule
    all_timings = [
        "06:00 AM", "07:00 AM", "08:00 AM", "09:00 AM", "10:00 AM", "11:00 AM",
        "12:00 PM", "01:00 PM", "02:00 PM", "03:00 PM", "04:00 PM", "05:00 PM",
        "06:00 PM", "07:00 PM", "08:00 PM", "09:00 PM", "10:00 PM", "11:00 PM"
    ]
    
    booked_slots = []
    if db:
        try:
            records = db.get_all_records()
            booked_slots = [
                str(r.get('Time')).strip().upper() 
                for r in records 
                if str(r.get('Date')).strip() == date_str and str(r.get('Status')).strip().lower() != 'available'
            ]
        except: pass

    available = []
    for t in all_timings:
        if t.upper() not in booked_slots:
            try:
                # Filter out past slots
                slot_dt = datetime.strptime(f"{date_str} {t}", "%Y-%m-%d %I:%M %p")
                if slot_dt > (now - timedelta(minutes=5)):
                    available.append(t)
            except:
                available.append(t)
                
    return available

def send_date_menu(phone, name):
    today = datetime.now()
    rows = []
    for i in range(7): 
        d_obj = today + timedelta(days=i)
        d_str = d_obj.strftime('%Y-%m-%d')
        available_slots = get_slots_for_date(d_str)
        count = len(available_slots)
        
        rows.append({
            "id": f"date_{d_str}", 
            "title": d_str, 
            "description": f"{d_obj.strftime('%A')} | {count} slots available"
        })
    
    send_wa(phone, {
        "messaging_product": "whatsapp", "to": phone, "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": "Step 2: Choose Date"},
            "body": {"text": f"Hi {name}, pick your match date below:"},
            "action": {"button": "📅 Select Date", "sections": [{"title": "7-Day Schedule", "rows": rows}]}
        }
    })

def send_slot_list(phone, date_str):
    slots = get_slots_for_date(date_str)
    if not slots:
        send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": "❌ Sorry! No slots left for this date."}})
        return

    rows = []
    for s in slots[:10]:
        price = calculate_price(s)
        rows.append({
            "id": f"select_{date_str}_{s}", 
            "title": s,
            "description": f"Rate: ₹{price}/hr"
        })

    send_wa(phone, {
        "messaging_product": "whatsapp", "to": phone, "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": "Step 3: Choose Time"},
            "body": {"text": f"Available slots for *{date_str}*:"},
            "action": {"button": "🕒 Pick Timing", "sections": [{"title": "Timings", "rows": rows}]}
        }
    })

def send_chosen_slots_summary(phone):
    sess = user_sessions.get(phone, {"chosen_slots": []})
    chosen_slots = sess.get("chosen_slots", [])
    if not chosen_slots:
        send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": "No slots chosen."}})
        return

    summary_lines = []
    total_price = 0
    for item in chosen_slots:
        time_part = item.split(" | ")[1]
        price = calculate_price(time_part)
        summary_lines.append(f"• {item} (₹{price})")
        total_price += price

    # Test rate: ₹1 per slot
    test_total = len(chosen_slots) * 1 
    
    send_wa(phone, {
        "messaging_product": "whatsapp", "to": phone, "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": f"*Chosen Slots:*\n" + "\n".join(summary_lines) + f"\n\n*Total (Test): ₹{test_total}*"},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": "add_more", "title": "➕ Add More"}},
                {"type": "reply", "reply": {"id": "pay_now", "title": "💳 Checkout"}},
                {"type": "reply", "reply": {"id": "clear", "title": "❌ Clear All"}}
            ]}
        }
    })

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

        if phone not in user_sessions:
            user_sessions[phone] = {"chosen_slots": [], "name": None, "state": None}

        if msg.get("type") == "interactive":
            interactive = msg["interactive"]
            reply = interactive.get("list_reply") or interactive.get("button_reply")
            rid = reply.get("id")

            if rid == "book" or rid == "add_more":
                known_name = user_sessions[phone].get("name") or get_user_name_from_db(phone)
                if known_name:
                    user_sessions[phone]["name"] = known_name
                    send_date_menu(phone, known_name)
                else:
                    user_sessions[phone]["state"] = "AWAITING_NAME"
                    send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": "⚽ *Welcome!*\nPlease type your *Full Name* to begin:"}})

            elif rid == "clear":
                user_sessions[phone]["chosen_slots"] = []
                send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": "Selection cleared."}})

            elif rid.startswith("date_"):
                date_val = rid.split("_")[1]
                send_slot_list(phone, date_val)

            elif rid.startswith("select_"):
                _, d_v, t_v = rid.split("_", 2)
                item = f"{d_v} | {t_v}"
                if item not in user_sessions[phone]["chosen_slots"]:
                    user_sessions[phone]["chosen_slots"].append(item)
                send_chosen_slots_summary(phone)

            elif rid == "pay_now":
                chosen_slots = user_sessions[phone]["chosen_slots"]
                test_total = len(chosen_slots) * 1 
                payment = rzp_client.payment_link.create({
                    "amount": int(test_total * 100),
                    "currency": "INR",
                    "description": f"Match Booking: {', '.join(chosen_slots)}",
                    "customer": {"contact": phone},
                    "notes": {"slots": ";".join(chosen_slots), "phone": phone, "name": user_sessions[phone]["name"]},
                    "callback_url": "https://your-turf-site.com/success",
                    "callback_method": "get"
                })
                send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": f"Pay here to confirm:\n🔗 {payment['short_url']}"}})

        elif msg.get("type") == "text":
            body = msg['text']['body'].strip()
            
            if user_sessions[phone].get("state") == "AWAITING_NAME":
                user_sessions[phone]["name"] = body
                user_sessions[phone]["state"] = None
                send_date_menu(phone, body)
                return {"ok": True}

            if any(greet in body.lower() for greet in ["hi", "hello", "book"]):
                user_sessions[phone]["chosen_slots"] = []
                known_name = get_user_name_from_db(phone)
                welcome_header = f"Welcome back, *{known_name}*! " if known_name else "Welcome to *The GSS Turf*! ⚽🏏"
                welcome_msg = (
                    f"{welcome_header}\n\n"
                    "Ready for a match?\n"
                    "💰 *Rates:*\n• 6 AM - 6 PM: ₹800\n• 6 PM - 11 PM: ₹1000"
                )
                send_wa(phone, {
                    "messaging_product": "whatsapp", "to": phone, "type": "interactive",
                    "interactive": {
                        "type": "button",
                        "body": {"text": welcome_msg},
                        "action": {"buttons": [{"type": "reply", "reply": {"id": "book", "title": "⚡ Book Now"}}]}}})

    except Exception as e: print(f"❌ Error: {e}")
    return {"ok": True}

@app.post("/razorpay-webhook")
async def handle_razorpay(request: Request):
    data = await request.json()
    if data.get("event") == "payment_link.paid":
        payload = data['payload']['payment_link']['entity']
        slots_str = payload['notes']['slots']
        phone = payload['notes']['phone']
        user_name = payload['notes'].get('name', 'Player')
        slots_list = slots_str.split(";")
        
        db = get_db()
        if db:
            for slot_item in slots_list:
                d_v, t_v = slot_item.split(" | ")
                db.append_row([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), str(phone), d_v, t_v, "Confirmed", user_name])
        
        send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": f"✅ *Confirmed!*\nYour slots for {slots_str.replace(';', ', ')} are booked. See you! 🏟️"}})
        if phone in user_sessions: user_sessions[phone]["chosen_slots"] = []

    return {"status": "ok"}
