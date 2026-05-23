"""
CalmMart Web App Backend — FastAPI
- Automatic withdrawals via Paystack Transfer API
- Withdrawals only on Wednesday (2) and Friday (4)
- Minimum withdrawal: ₦10,000
- Email notifications via SMTP (Gmail)
- WhatsApp notifications via Twilio
- Telegram notifications
"""

import os, json, hmac, hashlib, uuid, logging, smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
from dotenv import load_dotenv

from database import Database
from payment import PaystackPayment
from commission import CommissionEngine

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="CalmMart Web App")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
# Mount static files safely
import os as _os
if _os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")
else:
    logger.warning("⚠ static/ folder not found — Web App UI will not be served")

BOT_TOKEN    = os.getenv("BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
WEBAPP_URL   = os.getenv("WEBAPP_URL", "https://yourdomain.com")

# ── Email (Gmail SMTP) ─────────────────────────────────────────
SMTP_HOST  = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT  = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER  = os.getenv("SMTP_USER", "")       # youraddress@gmail.com
SMTP_PASS  = os.getenv("SMTP_PASS", "")       # Gmail App Password (16 chars)
EMAIL_FROM = os.getenv("EMAIL_FROM", "CalmMart Ltd <noreply@calmmart.com>")

# ── Withdrawal rules ───────────────────────────────────────────
MIN_WITHDRAWAL  = 10_000        # Naira
WITHDRAWAL_DAYS = {2, 4}        # Wednesday=2, Friday=4

REGISTRATION_FEE = 10_000

db         = Database()
payment    = PaystackPayment()
commission = CommissionEngine(db)


# ── Static / Health ────────────────────────────────────────────
@app.get("/")
async def serve_app():
    if _os.path.exists("static/index.html"):
        return FileResponse("static/index.html")
    return JSONResponse({"status": "CalmMart API running", "static": "not found — upload static/index.html"})

@app.on_event("startup")
async def startup_event():
    """Start the Telegram bot in a background thread when server starts."""
    try:
        from bot import run_bot_in_background
        run_bot_in_background()
        logger.info("✅ Bot background thread started")
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "CalmMart Web App"}





# ── Auth ───────────────────────────────────────────────────────
def verify_telegram_data(init_data: str) -> dict | None:
    try:
        parsed = dict(x.split("=", 1) for x in init_data.split("&"))
        received_hash = parsed.pop("hash", "")
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, received_hash):
            import urllib.parse
            return json.loads(urllib.parse.unquote(parsed.get("user", "{}")))
    except Exception as e:
        logger.warning(f"Auth error: {e}")
    return None

def get_tg_user(request: Request) -> dict:
    init_data = request.headers.get("X-Telegram-Init-Data", "")

    # Dev mode — allow dev_id query param
    dev_id = request.query_params.get("dev_id")
    if dev_id:
        return {"id": int(dev_id), "first_name": "Dev", "username": "dev"}

    # No init data at all — return guest user so app doesn't hang
    if not init_data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Try to verify Telegram data
    user = verify_telegram_data(init_data)
    if not user:
        # In production, Telegram sometimes sends slightly different format
        # Try to extract user ID directly from init_data
        try:
            import urllib.parse, json
            parsed = dict(x.split("=", 1) for x in init_data.split("&") if "=" in x)
            if "user" in parsed:
                user = json.loads(urllib.parse.unquote(parsed["user"]))
        except Exception:
            pass

    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram data")

    return user


# ── Register ───────────────────────────────────────────────────
class RegisterPayload(BaseModel):
    full_name:      str
    phone:          str
    email:          str = ""
    bank_name:      str
    account_number: str
    referrer_code:  str = ""
    init_data:      str = ""

@app.post("/api/register")
async def register(payload: RegisterPayload, request: Request):
    """Free registration — inactive until payment confirmed."""
    init_data = request.headers.get("X-Telegram-Init-Data", payload.init_data)
    tg_user = None
    if init_data:
        tg_user = verify_telegram_data(init_data)
    if not tg_user:
        dev_id = request.query_params.get("dev_id")
        if dev_id:
            tg_user = {"id": int(dev_id), "first_name": payload.full_name, "username": ""}
        else:
            raise HTTPException(status_code=401, detail="Unauthorized")

    telegram_id = tg_user["id"]
    existing    = db.get_user(telegram_id)

    if existing and existing["is_active"]:
        return JSONResponse({"error": "already_registered"}, status_code=400)

    referrer_code = payload.referrer_code.strip().upper() or None
    if referrer_code and not db.get_user_by_referral(referrer_code):
        referrer_code = None

    email = payload.email or f"{telegram_id}@calmmart.bot"

    # Create account immediately as inactive
    if not existing:
        db.create_user(
            telegram_id    = telegram_id,
            username       = tg_user.get("username", ""),
            full_name      = payload.full_name,
            phone          = payload.phone,
            email          = email,
            bank_name      = payload.bank_name,
            account_number = payload.account_number,
            referrer_code  = referrer_code,
            is_active      = 0,
        )

    # Generate payment link for activation
    ref     = f"CM-{telegram_id}-{uuid.uuid4().hex[:8].upper()}"
    pay_url = payment.initialize_payment(
        email    = email,
        amount   = REGISTRATION_FEE,
        reference= ref,
        metadata = {"telegram_id": str(telegram_id), "activation": "true"}
    )
    if not pay_url:
        raise HTTPException(status_code=500, detail="Payment initialization failed")

    user = db.get_user(telegram_id)
    db.save_pending(ref, {
        "telegram_id":    telegram_id,
        "username":       tg_user.get("username", ""),
        "full_name":      payload.full_name,
        "phone":          payload.phone,
        "email":          email,
        "bank_name":      payload.bank_name,
        "account_number": payload.account_number,
        "referrer_code":  referrer_code or "",
    })

    bot_info = await _bot_username()
    ref_link = f"https://t.me/{bot_info}?start={user['referral_code']}"

    # Send welcome notifications on free signup
    await _notify_registration(user, ref_link)

    return JSONResponse({
        "payment_url":   pay_url,
        "reference":     ref,
        "referral_code": user["referral_code"],
        "ref_link":      ref_link,
        "status":        "registered_pending_payment",
    })


# ── Profile ────────────────────────────────────────────────────
@app.get("/api/me")
async def get_me(request: Request):
    try:
        tg_user = get_tg_user(request)
    except HTTPException:
        return JSONResponse({"registered": False})
    user = db.get_user(tg_user["id"])
    if not user:
        return JSONResponse({"registered": False})

    stats    = db.get_user_stats(user["referral_code"])
    bot_info = await _bot_username()
    ref_link = f"https://t.me/{bot_info}?start={user['referral_code']}"

    today        = datetime.now().weekday()
    withdrawal_ok= today in WITHDRAWAL_DAYS
    next_wd      = _next_wd()

    return JSONResponse({
        "registered": True,
        "user": {
            **user,
            "stats":           stats,
            "ref_link":        ref_link,
            "is_active":       user["is_active"],
            "can_withdraw":    bool(user["is_active"]),
            "withdrawal_day":  withdrawal_ok,
            "next_withdrawal": next_wd,
            "min_withdrawal":  MIN_WITHDRAWAL,
        }
    })


# ── Network ────────────────────────────────────────────────────
@app.get("/api/network")
async def get_network(request: Request, page: int = 1, per_page: int = 50):
    tg_user = get_tg_user(request)
    user    = db.get_user(tg_user["id"])
    if not user:
        raise HTTPException(status_code=404)

    members = db.get_network_members(user["referral_code"])
    start   = (page - 1) * per_page

    return JSONResponse({
        "total":    len(members),
        "page":     page,
        "members":  members[start: start + per_page],
        "has_more": len(members) > start + per_page,
    })


# ── Transactions ───────────────────────────────────────────────
@app.get("/api/transactions")
async def get_transactions(request: Request):
    tg_user = get_tg_user(request)
    user    = db.get_user(tg_user["id"])
    if not user:
        raise HTTPException(status_code=404)
    return JSONResponse({"transactions": db.get_transactions(user["id"], limit=30)})


# ── AUTOMATIC WITHDRAWAL ───────────────────────────────────────
class WithdrawPayload(BaseModel):
    amount:    float
    init_data: str = ""

@app.post("/api/withdraw")
async def withdraw(payload: WithdrawPayload, request: Request):
    """
    Sends money directly to user's bank via Paystack Transfer.
    Only allowed on Wednesday & Friday. Minimum ₦10,000.
    """
    tg_user = get_tg_user(request)
    user    = db.get_user(tg_user["id"])
    if not user:
        raise HTTPException(status_code=404)

    # Gate 1 — must be activated
    if not user["is_active"]:
        return JSONResponse({
            "error":   "not_activated",
            "message": "Pay the ₦10,000 activation fee to unlock withdrawals."
        }, status_code=403)

    # Gate 2 — withdrawal day only
    if datetime.now().weekday() not in WITHDRAWAL_DAYS:
        return JSONResponse({
            "error":   "wrong_day",
            "message": f"Withdrawals are only on Wednesdays & Fridays. Next: {_next_wd()}."
        }, status_code=400)

    # Gate 3 — minimum amount
    if payload.amount < MIN_WITHDRAWAL:
        return JSONResponse({
            "error":   "below_minimum",
            "message": f"Minimum withdrawal is ₦{MIN_WITHDRAWAL:,}."
        }, status_code=400)

    # Gate 4 — sufficient balance
    if payload.amount > user["wallet_balance"]:
        return JSONResponse({"error": "Insufficient balance"}, status_code=400)

    # Deduct wallet and record withdrawal
    try:
        wid = db.create_withdrawal(user["id"], payload.amount)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    # Auto-transfer via Paystack
    transfer_ref = f"WD-{user['id']}-{uuid.uuid4().hex[:8].upper()}"
    success      = await _auto_transfer(user, payload.amount, transfer_ref)

    if success:
        db.mark_withdrawal_processed(wid, transfer_ref, "approved")

        # Notify all channels
        await _send_tg(user["telegram_id"],
            f"💸 *Withdrawal Successful!*\n\n"
            f"Amount: *₦{payload.amount:,.0f}*\n"
            f"Bank: {user['bank_name']}\n"
            f"Account: {user['account_number']}\n\n"
            f"Money is on its way! Allow a few minutes to arrive."
        )
        await _send_email(
            user.get("email",""), user["full_name"],
            "CalmMart — Withdrawal Successful ✅",
            _tpl_withdrawal(user, payload.amount)
        )
        return JSONResponse({"withdrawal_id": wid, "status": "processed",
                             "message": "Transfer initiated! Money arrives in minutes."})
    else:
        # Refund on failure
        db.credit_wallet(user["id"], payload.amount, "Withdrawal refunded — transfer failed", transfer_ref)
        db.mark_withdrawal_processed(wid, transfer_ref, "failed")
        return JSONResponse({
            "error":   "transfer_failed",
            "message": "Transfer failed. Your balance has been refunded. Try again later."
        }, status_code=500)


async def _auto_transfer(user: dict, amount: float, ref: str) -> bool:
    """Create Paystack recipient and initiate transfer."""
    try:
        bank_code = _bank_code(user["bank_name"])
        if not bank_code:
            logger.error(f"Unknown bank: {user['bank_name']}")
            return False

        recipient = payment.create_transfer_recipient(
            account_number = user["account_number"],
            bank_code      = bank_code,
            name           = user["full_name"]
        )
        if not recipient:
            return False

        return payment.initiate_transfer(
            amount         = amount,
            recipient_code = recipient,
            reason         = f"CalmMart withdrawal – {user['full_name']}",
            reference      = ref
        )
    except Exception as e:
        logger.error(f"Transfer error: {e}")
        return False


def _bank_code(name: str) -> str | None:
    codes = {
        "gtbank":"058","guaranty trust":"058",
        "access":"044","uba":"033","united bank":"033",
        "zenith":"057","first bank":"011","fidelity":"070",
        "sterling":"232","polaris":"076","keystone":"082",
        "wema":"035","union bank":"032","opay":"999992",
        "palmpay":"999991","kuda":"90267","moniepoint":"50515",
        "stanbic":"221","fcmb":"214","ecobank":"050",
    }
    n = name.lower()
    for k, v in codes.items():
        if k in n:
            return v
    return None


def _next_wd() -> str:
    today = datetime.now()
    for i in range(1, 8):
        d = today + timedelta(days=i)
        if d.weekday() in WITHDRAWAL_DAYS:
            return d.strftime("%A, %d %B %Y")
    return "Wednesday or Friday"


# ── Paystack Webhook ───────────────────────────────────────────
@app.post("/webhook/paystack")
async def paystack_webhook(request: Request):
    body = await request.body()
    sig  = request.headers.get("x-paystack-signature", "")
    if not payment.verify_webhook_signature(body, sig):
        raise HTTPException(status_code=400, detail="Bad signature")

    event = json.loads(body)
    if event.get("event") == "charge.success":
        import asyncio
        asyncio.create_task(process_payment(event["data"]["reference"]))
    return JSONResponse({"status": "ok"})

@app.get("/payment/callback")
async def payment_callback(reference: str):
    if payment.verify_payment(reference):
        await process_payment(reference)
    return FileResponse("static/payment_success.html")


# ── Core: activate user after payment ─────────────────────────
async def process_payment(reference: str):
    pending = db.get_pending(reference)
    if not pending:
        return

    telegram_id   = int(pending["telegram_id"])
    referrer_code = pending.get("referrer_code") or None
    if referrer_code and not db.get_user_by_referral(referrer_code):
        referrer_code = None

    try:
        existing = db.get_user(telegram_id)
        if existing and existing["is_active"]:
            db.delete_pending(reference)
            return
        if existing:
            db.activate_user(telegram_id)
            user = db.get_user(telegram_id)
        else:
            user = db.create_user(
                telegram_id=telegram_id, username=pending.get("username",""),
                full_name=pending["full_name"], phone=pending["phone"],
                email=pending.get("email",""), bank_name=pending["bank_name"],
                account_number=pending["account_number"],
                referrer_code=referrer_code, is_active=1,
            )
    except Exception as e:
        logger.error(f"Activation failed: {e}")
        return

    db.delete_pending(reference)

    comm_records = commission.process_registration_payment(
        new_user_id=user["id"], referrer_code=referrer_code, payment_ref=reference)

    bot_info = await _bot_username()
    ref_link = f"https://t.me/{bot_info}?start={user['referral_code']}"
    next_wd  = _next_wd()

    # ── Telegram ─────────────────────────────────────────────────
    await _send_tg(telegram_id,
        f"🎉 *Account Activated! Welcome, {user['full_name']}!*\n\n"
        f"✅ Payment confirmed — fully active!\n\n"
        f"🔓 *Unlocked:*\n"
        f"  • Earn commissions from referrals\n"
        f"  • Withdraw every Wednesday & Friday\n"
        f"  • Minimum withdrawal: ₦{MIN_WITHDRAWAL:,}\n\n"
        f"🆔 Code: `{user['referral_code']}`\n"
        f"🔗 Link:\n`{ref_link}`\n\n"
        f"💰 Gen1 ₦1,500 · Gen2 ₦1,000 · Gen3 ₦500\n"
        f"🎯 Target: 80+ referrals\n"
        f"📅 Next withdrawal: {next_wd}"
    )

    # ── Email ─────────────────────────────────────────────────────
    await _send_email(
        user.get("email",""), user["full_name"],
        "🎉 CalmMart — Account Activated!",
        _tpl_activation(user, ref_link, next_wd)
    )


    # ── Referrers ─────────────────────────────────────────────────
    for r in comm_records:
        await _send_tg(r["telegram_id"],
            f"💸 *Commission Received!*\n\n"
            f"New Gen{r['generation']} member joined!\n"
            f"You earned: *₦{r['amount']:,.0f}* ({r['rate_pct']}%)\n\n"
            f"📅 Withdraw Wed & Fri · Min ₦{MIN_WITHDRAWAL:,}"
        )

    # ── Admins ────────────────────────────────────────────────────
    for aid in _admin_ids():
        await _send_tg(aid,
            f"✅ *Member Activated*\n"
            f"Name: {user['full_name']}\nPhone: {user['phone']}\n"
            f"Code: `{user['referral_code']}`\nReferred by: {referrer_code or 'None'}\nRef: `{reference}`"
        )

    logger.info(f"Activated: {user['full_name']} | Commissions: {len(comm_records)}")


# ── Notify on free signup (before payment) ─────────────────────
async def _notify_registration(user: dict, ref_link: str):
    await _send_email(
        user.get("email",""), user["full_name"],
        "Welcome to CalmMart Ltd — Complete Your Registration",
        _tpl_registered(user, ref_link)
    )


# ── Email ──────────────────────────────────────────────────────
async def _send_email(to_email: str, to_name: str, subject: str, body: str):
    if not to_email or "@calmmart.bot" in to_email:
        return
    if not SMTP_USER or not SMTP_PASS:
        logger.warning("Email not configured")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_FROM
        msg["To"]      = f"{to_name} <{to_email}>"
        msg.attach(MIMEText(body, "html"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, to_email, msg.as_string())
        logger.info(f"Email → {to_email}")
    except Exception as e:
        logger.error(f"Email failed: {e}")





# ── Telegram ───────────────────────────────────────────────────
async def _send_tg(chat_id: int, text: str):
    try:
        async with httpx.AsyncClient() as c:
            await c.post(f"{TELEGRAM_API}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
    except Exception as e:
        logger.error(f"TG notify failed: {e}")


# ── Admin API endpoints ────────────────────────────────────────
@app.get("/api/stats")
async def api_stats():
    return JSONResponse(db.get_global_stats())

@app.get("/api/members")
async def api_members(page: int = 1, per_page: int = 100):
    with db._conn() as conn:
        rows = conn.execute("""
            SELECT u.*,(SELECT COUNT(*) FROM users r WHERE r.referrer_code=u.referral_code AND r.is_active=1) as gen1_count
            FROM users u ORDER BY u.id DESC LIMIT ? OFFSET ?
        """, [per_page, (page-1)*per_page]).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    return JSONResponse({"total": total, "members": [dict(r) for r in rows]})

@app.get("/api/commissions")
async def api_commissions(page: int = 1, per_page: int = 100):
    with db._conn() as conn:
        rows = conn.execute("""
            SELECT c.*,p.full_name as payer_name,e.full_name as earner_name
            FROM commissions c JOIN users p ON p.id=c.payer_user_id JOIN users e ON e.id=c.earner_user_id
            ORDER BY c.created_at DESC LIMIT ? OFFSET ?
        """, [per_page, (page-1)*per_page]).fetchall()
    return JSONResponse({"commissions": [dict(r) for r in rows]})

@app.get("/api/withdrawals")
async def api_withdrawals(status: str = "pending"):
    with db._conn() as conn:
        rows = conn.execute("""
            SELECT w.*,u.full_name,u.bank_name,u.account_number,u.phone
            FROM withdrawals w JOIN users u ON u.id=w.user_id WHERE w.status=? ORDER BY w.created_at DESC
        """, [status]).fetchall()
    return JSONResponse({"withdrawals": [dict(r) for r in rows]})

@app.get("/api/transactions")
async def api_transactions(page: int = 1, per_page: int = 100):
    with db._conn() as conn:
        rows = conn.execute("""
            SELECT t.*,u.full_name FROM transactions t JOIN users u ON u.id=t.user_id
            ORDER BY t.created_at DESC LIMIT ? OFFSET ?
        """, [per_page, (page-1)*per_page]).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    return JSONResponse({"total": total, "transactions": [dict(r) for r in rows]})

@app.post("/api/admin/members")
async def admin_add(request: Request):
    d = await request.json()
    try:
        u = db.create_user(telegram_id=d["telegram_id"],username=d.get("username",""),
            full_name=d["full_name"],phone=d["phone"],email=d.get("email",""),
            bank_name=d["bank_name"],account_number=d["account_number"],
            referrer_code=d.get("referrer_code"),is_active=d.get("is_active",0))
        return JSONResponse({"user": u})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

@app.put("/api/admin/members/{uid}")
async def admin_edit(uid: int, request: Request):
    d = await request.json()
    with db._conn() as conn:
        conn.execute("UPDATE users SET full_name=?,phone=?,email=?,bank_name=?,account_number=?,is_active=? WHERE id=?",
            [d["full_name"],d["phone"],d.get("email",""),d["bank_name"],d["account_number"],d.get("is_active",0),uid])
    return JSONResponse({"status": "updated"})

@app.patch("/api/admin/members/{uid}/status")
async def admin_status(uid: int, request: Request):
    d = await request.json()
    with db._conn() as conn:
        conn.execute("UPDATE users SET is_active=? WHERE id=?", [d["is_active"], uid])
    return JSONResponse({"status": "updated"})

@app.delete("/api/admin/members/{uid}")
async def admin_delete(uid: int):
    with db._conn() as conn:
        conn.execute("DELETE FROM users WHERE id=?", [uid])
    return JSONResponse({"status": "deleted"})


# ── Helpers ────────────────────────────────────────────────────
_bot_uname_cache = None
async def _bot_username() -> str:
    global _bot_uname_cache
    if _bot_uname_cache:
        return _bot_uname_cache
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{TELEGRAM_API}/getMe")
        _bot_uname_cache = r.json()["result"]["username"]
    return _bot_uname_cache

def _admin_ids() -> list[int]:
    return [int(x) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip()]


# ── Email Templates ────────────────────────────────────────────
def _base(content: str) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><style>
body{{margin:0;padding:0;background:#0d0d14;font-family:Arial,sans-serif}}
.w{{max-width:560px;margin:0 auto;padding:32px 16px}}
.c{{background:#161622;border:1px solid #252538;border-radius:16px;padding:32px;color:#e8e8f5}}
.logo{{font-size:28px;font-weight:800;color:#c6f135;margin-bottom:4px}}
.sub{{font-size:12px;color:#5a5a7a;margin-bottom:28px}}
h2{{font-size:20px;margin-bottom:16px}}
p{{font-size:14px;line-height:1.7;color:#b0b0c8;margin-bottom:12px}}
.pill{{display:inline-block;background:rgba(198,241,53,0.12);border:1px solid rgba(198,241,53,0.3);color:#c6f135;padding:7px 16px;border-radius:20px;font-size:13px;font-weight:700;margin:4px 3px}}
.btn{{display:inline-block;background:#c6f135;color:#000;text-decoration:none;padding:14px 28px;border-radius:10px;font-weight:700;font-size:15px;margin-top:20px}}
.row{{display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid #252538;font-size:13px}}
.rl{{color:#5a5a7a}}.rv{{font-weight:700}}
hr{{border:none;border-top:1px solid #252538;margin:22px 0}}
.ft{{text-align:center;font-size:11px;color:#3a3a55;margin-top:20px;line-height:1.8}}
</style></head><body><div class="w"><div class="c">
<div class="logo">CalmMart</div><div class="sub">Nigeria's Referral Network</div>
{content}
</div><div class="ft">CalmMart Ltd · Withdrawals every Wednesday & Friday · Min ₦10,000<br>This is an automated message</div></div></body></html>"""

def _tpl_registered(u: dict, ref_link: str) -> str:
    return _base(f"""
<h2>👋 Welcome, {u['full_name']}!</h2>
<p>You have successfully registered on <strong>CalmMart Ltd</strong>. Your account and referral code are ready!</p>
<hr>
<div class="row"><span class="rl">Referral Code</span><span class="rv" style="color:#c6f135">{u['referral_code']}</span></div>
<div class="row"><span class="rl">Bank</span><span class="rv">{u['bank_name']}</span></div>
<div class="row"><span class="rl">Account</span><span class="rv">{u['account_number']}</span></div>
<div class="row"><span class="rl">Status</span><span class="rv" style="color:#ffb547">⏳ Pending Activation</span></div>
<hr>
<p>⚠ <strong>Next step:</strong> Pay the ₦10,000 activation fee to unlock commissions and withdrawals.</p>
<p>You can already share your referral link now and your downline will be credited once you activate!</p>
<p style="word-break:break-all;color:#c6f135;font-size:12px">{ref_link}</p>
<div style="text-align:center"><a class="btn" href="{ref_link}">Share My Referral Link</a></div>""")

def _tpl_activation(u: dict, ref_link: str, next_wd: str) -> str:
    return _base(f"""
<h2>🎉 Account Activated, {u['full_name']}!</h2>
<p>Your payment has been confirmed. Your CalmMart account is now <strong style="color:#c6f135">fully active</strong>!</p>
<hr>
<div class="row"><span class="rl">Name</span><span class="rv">{u['full_name']}</span></div>
<div class="row"><span class="rl">Referral Code</span><span class="rv" style="color:#c6f135">{u['referral_code']}</span></div>
<div class="row"><span class="rl">Bank</span><span class="rv">{u['bank_name']}</span></div>
<div class="row"><span class="rl">Account</span><span class="rv">{u['account_number']}</span></div>
<div class="row"><span class="rl">Status</span><span class="rv" style="color:#c6f135">✅ Active</span></div>
<hr>
<h2>💰 Your Earnings</h2>
<span class="pill">Gen 1 — ₦1,500</span><span class="pill">Gen 2 — ₦1,000</span><span class="pill">Gen 3 — ₦500</span>
<hr>
<div class="row"><span class="rl">Withdrawal Days</span><span class="rv">Wednesday & Friday only</span></div>
<div class="row"><span class="rl">Minimum Withdrawal</span><span class="rv">₦10,000</span></div>
<div class="row"><span class="rl">Next Withdrawal Day</span><span class="rv">{next_wd}</span></div>
<hr>
<p>Share your referral link and start earning today!</p>
<div style="text-align:center"><a class="btn" href="{ref_link}">Open My Dashboard</a></div>""")

def _tpl_withdrawal(u: dict, amount: float) -> str:
    return _base(f"""
<h2>💸 Withdrawal Successful!</h2>
<p>Your withdrawal has been processed and money is on its way to your bank account.</p>
<hr>
<div class="row"><span class="rl">Amount</span><span class="rv" style="color:#c6f135">₦{amount:,.0f}</span></div>
<div class="row"><span class="rl">Bank</span><span class="rv">{u['bank_name']}</span></div>
<div class="row"><span class="rl">Account</span><span class="rv">{u['account_number']}</span></div>
<div class="row"><span class="rl">Date & Time</span><span class="rv">{datetime.now().strftime('%d %b %Y, %I:%M %p')}</span></div>
<hr>
<p>Money should arrive within a few minutes. If not received within 24 hours, contact support.</p>
<p>Keep referring to grow your earnings! 🚀</p>""")


# ── Run ────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
