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

# User session: {"name": str, "chosen_slots": list}
user_sessions = {}

# --- HELPER FUNCTIONS ---

def send_wa(to, payload):
    url = f"https://graph.facebook.com/v21.0/{PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    return requests.post(url, json=payload, headers=headers)

def get_available_slots(date_str):
    """Filters slots by date, availability, and ensures they aren't in the past."""
    db = get_db()
    if not db: return []
    
    records = db.get_all_records()
    now = datetime.now()
    available = []
    
    for r in records:
        if str(r.get('Date')).strip() == date_str and str(r.get('Status')).strip().lower() == 'available':
            try:
                # Expected format: YYYY-MM-DD and HH:MM AM/PM
                slot_time_str = str(r.get('Time')).strip()
                slot_dt = datetime.strptime(f"{date_str} {slot_time_str}", "%Y-%m-%d %I:%M %p")
                
                # Only show slots that are at least 15 minutes in the future
                if slot_dt > (now + timedelta(minutes=15)):
                    available.append(slot_time_str)
            except Exception as e:
                # Fallback if time parsing fails (shows all for that date)
                available.append(str(r.get('Time')))
                
    return available

def send_date_menu(phone, name):
    today = datetime.now()
    rows = []
    for i in range(3): # Show Today, Tomorrow, and Day After
        d_obj = today + timedelta(days=i)
        d_str = d_obj.strftime('%Y-%m-%d')
        rows.append({
            "id": f"date_{d_str}", 
            "title": d_str, 
            "description": d_obj.strftime('%A')
        })
    
    send_wa(phone, {
        "messaging_product": "whatsapp", "to": phone, "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": "Step 1: Choose Date"},
            "body": {"text": f"Hi {name}, pick your match date below to see available timings:"},
            "action": {"button": "📅 Select Date", "sections": [{"title": "Dates", "rows": rows}]}
        }
    })

def send_slot_list(phone, date_str):
    slots = get_available_slots(date_str)
    if not slots:
        send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": "❌ Sorry! No more slots available for this date."}})
        return

    rows = [{"id": f"select_{date_str}_{s}", "title": s} for s in slots[:10]]
    send_wa(phone, {
        "messaging_product": "whatsapp", "to": phone, "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": "Step 2: Choose Time"},
            "body": {"text": f"Showing available slots for *{date_str}*:"},
            "action": {"button": "🕒 Pick Timing", "sections": [{"title": "Available Slots", "rows": rows}]}
        }
    })

def send_chosen_slots_summary(phone):
    sess = user_sessions.get(phone, {"chosen_slots": []})
    chosen_slots = sess.get("chosen_slots", [])
    if not chosen_slots:
        send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": "You haven't chosen any slots yet."}})
        return

    summary = "*Chosen Slots:*\n" + "\n".join([f"• {item}" for item in chosen_slots])
    total = len(chosen_slots) * 900 
    
    send_wa(phone, {
        "messaging_product": "whatsapp", "to": phone, "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": f"{summary}\n\n*Grand Total: ₹{total}*\n\nWould you like to add another hour or proceed to payment?"},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": "add_more", "title": "➕ Add More"}},
                {"type": "reply", "reply": {"id": "pay_now", "title": f"💳 Checkout"}},
                {"type": "reply", "reply": {"id": "clear", "title": "❌ Clear Chosen"}}
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

        # Init session
        if phone not in user_sessions:
            user_sessions[phone] = {"chosen_slots": [], "name": "Player"}

        if msg.get("type") == "interactive":
            interactive = msg["interactive"]
            reply = interactive.get("list_reply") or interactive.get("button_reply")
            rid = reply.get("id")

            if rid == "book" or rid == "add_more":
                send_date_menu(phone, user_sessions[phone]["name"])

            elif rid == "clear":
                user_sessions[phone]["chosen_slots"] = []
                send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": "Chosen slots cleared. Type 'Hi' to start over."}})

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
                total = len(chosen_slots) * 900
                
                # Generate Razorpay Link
                payment = rzp_client.payment_link.create({
                    "amount": total * 100,
                    "currency": "INR",
                    "description": f"Match Booking: {', '.join(chosen_slots)}",
                    "customer": {"contact": phone},
                    "notes": {"slots": ";".join(chosen_slots), "phone": phone},
                    "callback_url": "https://your-turf-site.com/success",
                    "callback_method": "get"
                })
                
                send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": f"Please complete your payment to secure these slots:\n\n🔗 {payment['short_url']}"}})

        elif msg.get("type") == "text":
            body = msg['text']['body'].lower()
            if any(greet in body for greet in ["hi", "hello", "book", "hey"]):
                user_sessions[phone]["chosen_slots"] = [] 
                welcome_msg = (
                    "Welcome to *The GSS Turf*! ⚽🏏\n\n"
                    "Ready for a match? Book your slots in seconds.\n"
                    "💰 *Rate:* ₹900 per hour"
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
        slots_list = slots_str.split(";")
        
        db = get_db()
        if db:
            records = db.get_all_records()
            for slot_item in slots_list:
                d_v, t_v = slot_item.split(" | ")
                for idx, r in enumerate(records):
                    if str(r.get('Date')).strip() == d_v and str(r.get('Time')).strip() == t_v:
                        db.update_cell(idx + 2, 3, "Confirmed") # Marking Column C as Confirmed
        
        confirmation = (
            "✅ *PAYMENT SUCCESSFUL!*\n"
            f"Your booking for {slots_str.replace(';', ', ')} is confirmed.\n\n"
            "Get your gear ready! See you at the turf. 🏟️"
        )
        send_wa(phone, {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": confirmation}})
        if phone in user_sessions: user_sessions[phone]["chosen_slots"] = []

    return {"status": "ok"}
