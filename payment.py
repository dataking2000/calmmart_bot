"""
CalmMart Payment Integration — Paystack
Handles payment initialization, verification, and webhook processing
"""

import os
import hmac
import hashlib
import requests
import uuid
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

PAYSTACK_SECRET = os.getenv("PAYSTACK_SECRET_KEY")
PAYSTACK_BASE   = "https://api.paystack.co"
CALLBACK_URL    = os.getenv("CALLBACK_URL", "https://yourdomain.com/payment/callback")
WEBHOOK_URL     = os.getenv("WEBHOOK_URL",  "https://yourdomain.com/webhook/paystack")


class PaystackPayment:
    def __init__(self):
        self.secret = PAYSTACK_SECRET
        self.headers = {
            "Authorization": f"Bearer {self.secret}",
            "Content-Type": "application/json",
        }

    # ─────────────────────────────────────────────
    #  Generate unique payment reference
    # ─────────────────────────────────────────────
    def generate_reference(self, telegram_id: int) -> str:
        unique = uuid.uuid4().hex[:8].upper()
        return f"CM-{telegram_id}-{unique}"

    # ─────────────────────────────────────────────
    #  Initialize payment — returns checkout URL
    # ─────────────────────────────────────────────
    def initialize_payment(self, email: str, amount: int,
                           reference: str, metadata: dict) -> str | None:
        payload = {
            "email": email,
            "amount": amount * 100,  # Paystack uses kobo
            "reference": reference,
            "callback_url": CALLBACK_URL,
            "metadata": {
                "custom_fields": [
                    {"display_name": k, "variable_name": k, "value": str(v)}
                    for k, v in metadata.items()
                ],
                **metadata
            },
            "currency": "NGN",
            "channels": ["card", "bank", "ussd", "mobile_money", "bank_transfer"],
        }

        try:
            resp = requests.post(
                f"{PAYSTACK_BASE}/transaction/initialize",
                json=payload,
                headers=self.headers,
                timeout=15
            )
            data = resp.json()
            if data.get("status"):
                logger.info(f"Payment initialized: {reference}")
                return data["data"]["authorization_url"]
            logger.error(f"Paystack init failed: {data}")
            return None
        except Exception as e:
            logger.error(f"Paystack request error: {e}")
            return None

    # ─────────────────────────────────────────────
    #  Verify payment by reference
    # ─────────────────────────────────────────────
    def verify_payment(self, reference: str) -> dict | None:
        try:
            resp = requests.get(
                f"{PAYSTACK_BASE}/transaction/verify/{reference}",
                headers=self.headers,
                timeout=15
            )
            data = resp.json()
            if data.get("status") and data["data"]["status"] == "success":
                logger.info(f"Payment verified: {reference}")
                return data["data"]
            logger.warning(f"Payment not successful: {reference} — {data.get('message')}")
            return None
        except Exception as e:
            logger.error(f"Verify error: {e}")
            return None

    # ─────────────────────────────────────────────
    #  Verify Paystack webhook signature
    # ─────────────────────────────────────────────
    def verify_webhook_signature(self, payload_bytes: bytes, signature: str) -> bool:
        expected = hmac.new(
            self.secret.encode("utf-8"),
            payload_bytes,
            hashlib.sha512
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    # ─────────────────────────────────────────────
    #  Initiate transfer to user's bank account
    # ─────────────────────────────────────────────
    def create_transfer_recipient(self, account_number: str,
                                  bank_code: str, name: str) -> str | None:
        payload = {
            "type": "nuban",
            "name": name,
            "account_number": account_number,
            "bank_code": bank_code,
            "currency": "NGN"
        }
        try:
            resp = requests.post(
                f"{PAYSTACK_BASE}/transferrecipient",
                json=payload,
                headers=self.headers,
                timeout=15
            )
            data = resp.json()
            if data.get("status"):
                return data["data"]["recipient_code"]
            logger.error(f"Recipient creation failed: {data}")
            return None
        except Exception as e:
            logger.error(f"Transfer recipient error: {e}")
            return None

    def initiate_transfer(self, amount: float, recipient_code: str,
                          reason: str, reference: str) -> bool:
        payload = {
            "source": "balance",
            "amount": int(amount * 100),
            "recipient": recipient_code,
            "reason": reason,
            "reference": reference,
        }
        try:
            resp = requests.post(
                f"{PAYSTACK_BASE}/transfer",
                json=payload,
                headers=self.headers,
                timeout=15
            )
            data = resp.json()
            success = data.get("status") and data["data"]["status"] in ("success", "pending")
            if success:
                logger.info(f"Transfer initiated: {reference} — ₦{amount:,.2f}")
            else:
                logger.error(f"Transfer failed: {data}")
            return success
        except Exception as e:
            logger.error(f"Transfer error: {e}")
            return False

    # ─────────────────────────────────────────────
    #  Resolve bank account name (for verification)
    # ─────────────────────────────────────────────
    def resolve_account(self, account_number: str, bank_code: str) -> dict | None:
        try:
            resp = requests.get(
                f"{PAYSTACK_BASE}/bank/resolve",
                params={"account_number": account_number, "bank_code": bank_code},
                headers=self.headers,
                timeout=15
            )
            data = resp.json()
            return data.get("data") if data.get("status") else None
        except Exception as e:
            logger.error(f"Account resolve error: {e}")
            return None

    # ─────────────────────────────────────────────
    #  Fetch list of Nigerian banks from Paystack
    # ─────────────────────────────────────────────
    def get_banks(self) -> list:
        try:
            resp = requests.get(
                f"{PAYSTACK_BASE}/bank?country=nigeria&perPage=100",
                headers=self.headers,
                timeout=15
            )
            data = resp.json()
            return data.get("data", []) if data.get("status") else []
        except Exception as e:
            logger.error(f"Get banks error: {e}")
            return []
