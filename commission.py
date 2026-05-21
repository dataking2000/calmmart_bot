"""
CalmMart Commission Engine
Automatically calculates and distributes referral commissions
across 3 generations upon successful payment
"""

import logging
from database import Database

logger = logging.getLogger(__name__)

REGISTRATION_FEE = 10_000  # NGN

COMMISSION_RATES = {
    1: 0.15,   # 15% — 1st generation (direct referrer)
    2: 0.10,   # 10% — 2nd generation
    3: 0.05,   #  5% — 3rd generation
}


class CommissionEngine:
    def __init__(self, db: Database):
        self.db = db

    # ─────────────────────────────────────────────
    #  Main entry point — called after payment verified
    # ─────────────────────────────────────────────
    def process_registration_payment(self, new_user_id: int,
                                     referrer_code: str | None,
                                     payment_ref: str) -> list[dict]:
        """
        Distributes commissions up the referral chain.
        Returns list of commission records processed.
        """
        if not referrer_code:
            logger.info(f"User {new_user_id} has no referrer — no commissions to distribute.")
            return []

        chain = self.db.get_referral_chain(referrer_code)
        records = []

        for gen_index, referrer in enumerate(chain):
            generation = gen_index + 1  # 1, 2, or 3
            rate = COMMISSION_RATES.get(generation)
            if not rate:
                break

            amount = REGISTRATION_FEE * rate
            earner_id = referrer["id"]
            earner_tg = referrer["telegram_id"]
            earner_name = referrer["full_name"]

            # Credit wallet
            desc = (
                f"Gen{generation} commission from new member registration "
                f"(Ref: {payment_ref})"
            )
            self.db.credit_wallet(earner_id, amount, desc, payment_ref)

            # Record commission
            self.db.record_commission(
                payer_user_id=new_user_id,
                earner_user_id=earner_id,
                generation=generation,
                amount=amount,
                ref=payment_ref
            )

            logger.info(
                f"Commission ₦{amount:,.2f} (Gen{generation}) → "
                f"{earner_name} (tg:{earner_tg})"
            )

            records.append({
                "generation": generation,
                "telegram_id": earner_tg,
                "full_name": earner_name,
                "amount": amount,
                "rate_pct": int(rate * 100),
            })

        return records

    # ─────────────────────────────────────────────
    #  Summary string for notification messages
    # ─────────────────────────────────────────────
    def format_commission_breakdown(self, records: list[dict]) -> str:
        if not records:
            return ""
        lines = ["💰 *Commission Distribution:*"]
        for r in records:
            lines.append(
                f"  Gen{r['generation']} ({r['rate_pct']}%) → "
                f"{r['full_name']}: ₦{r['amount']:,.0f}"
            )
        return "\n".join(lines)

    # ─────────────────────────────────────────────
    #  Compute expected commission for display
    # ─────────────────────────────────────────────
    @staticmethod
    def calculate_potential(registration_fee: int = REGISTRATION_FEE) -> dict:
        return {
            gen: {
                "rate_pct": int(rate * 100),
                "amount": registration_fee * rate
            }
            for gen, rate in COMMISSION_RATES.items()
        }
