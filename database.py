"""
CalmMart Database Layer — SQLite
Referrals: UNLIMITED per user (no cap enforced).
Target: minimum 80 referrals per active member encouraged.
"""

import sqlite3
import uuid
from datetime import date
from contextlib import contextmanager

MIN_REFERRAL_TARGET = 80   # Encouraged minimum — NOT a hard cap


class Database:
    def __init__(self, db_path=None):
        import os
        self.db_path = db_path or os.getenv("DB_PATH", "calmmart.db")
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id     INTEGER UNIQUE NOT NULL,
                    username        TEXT DEFAULT '',
                    full_name       TEXT NOT NULL,
                    phone           TEXT NOT NULL,
                    email           TEXT DEFAULT '',
                    bank_name       TEXT NOT NULL,
                    account_number  TEXT NOT NULL,
                    referral_code   TEXT UNIQUE NOT NULL,
                    referrer_code   TEXT,
                    wallet_balance  REAL DEFAULT 0.0,
                    total_earnings  REAL DEFAULT 0.0,
                    is_active       INTEGER DEFAULT 0,
                    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (referrer_code) REFERENCES users(referral_code)
                );

                CREATE TABLE IF NOT EXISTS pending_registrations (
                    payment_ref     TEXT PRIMARY KEY,
                    telegram_id     INTEGER NOT NULL,
                    username        TEXT DEFAULT '',
                    full_name       TEXT NOT NULL,
                    phone           TEXT NOT NULL,
                    email           TEXT DEFAULT '',
                    bank_name       TEXT NOT NULL,
                    account_number  TEXT NOT NULL,
                    referrer_code   TEXT DEFAULT '',
                    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS transactions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER NOT NULL,
                    type        TEXT NOT NULL,
                    amount      REAL NOT NULL,
                    description TEXT NOT NULL,
                    reference   TEXT DEFAULT '',
                    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS commissions (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    payer_user_id   INTEGER NOT NULL,
                    earner_user_id  INTEGER NOT NULL,
                    generation      INTEGER NOT NULL,
                    amount          REAL NOT NULL,
                    payment_ref     TEXT NOT NULL,
                    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (payer_user_id)  REFERENCES users(id),
                    FOREIGN KEY (earner_user_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS withdrawals (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id      INTEGER NOT NULL,
                    amount       REAL NOT NULL,
                    status       TEXT DEFAULT 'pending',
                    created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
                    processed_at TEXT DEFAULT '',
                    FOREIGN KEY (user_id) REFERENCES users(id)
                );

                CREATE INDEX IF NOT EXISTS idx_users_referrer   ON users(referrer_code);
                CREATE INDEX IF NOT EXISTS idx_users_telegram   ON users(telegram_id);
                CREATE INDEX IF NOT EXISTS idx_txn_user         ON transactions(user_id);
                CREATE INDEX IF NOT EXISTS idx_comm_earner      ON commissions(earner_user_id);
            """)

    # ── Referral code ──────────────────────────────────────────
    def _gen_code(self, telegram_id: int) -> str:
        return ("CM" + str(telegram_id) + uuid.uuid4().hex[:4]).upper()[:10]

    # ── User CRUD ──────────────────────────────────────────────
    def get_user(self, telegram_id: int) -> dict | None:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
            return dict(r) if r else None

    def get_user_by_id(self, user_id: int) -> dict | None:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return dict(r) if r else None

    def get_user_by_referral(self, code: str) -> dict | None:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM users WHERE referral_code=?", (code,)).fetchone()
            return dict(r) if r else None

    def create_user(self, telegram_id, username, full_name, phone,
                    email, bank_name, account_number,
                    referrer_code=None, is_active=0) -> dict:
        """
        Create user as INACTIVE (is_active=0) by default.
        They become active only after payment is confirmed.
        Inactive users can share referral links but cannot withdraw.
        """
        code = self._gen_code(telegram_id)
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO users
                  (telegram_id,username,full_name,phone,email,
                   bank_name,account_number,referral_code,referrer_code,is_active)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (telegram_id, username, full_name, phone, email,
                  bank_name, account_number, code, referrer_code, is_active))
        return self.get_user(telegram_id)

    # ── Wallet ─────────────────────────────────────────────────
    def credit_wallet(self, user_id: int, amount: float, desc: str, ref: str = ""):
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET wallet_balance=wallet_balance+?, total_earnings=total_earnings+? WHERE id=?",
                (amount, amount, user_id)
            )
            conn.execute(
                "INSERT INTO transactions(user_id,type,amount,description,reference) VALUES(?,?,?,?,?)",
                (user_id, "credit", amount, desc, ref)
            )

    def activate_user(self, telegram_id: int):
        """Activate user after payment confirmed. Only active users can withdraw."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET is_active=1 WHERE telegram_id=?", (telegram_id,)
            )

    def debit_wallet(self, user_id: int, amount: float):
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET wallet_balance=wallet_balance-? WHERE id=?",
                (amount, user_id)
            )

    # ── Pending registrations ───────────────────────────────────
    def save_pending(self, ref: str, d: dict):
        with self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO pending_registrations
                  (payment_ref,telegram_id,username,full_name,phone,email,
                   bank_name,account_number,referrer_code)
                VALUES(?,?,?,?,?,?,?,?,?)
            """, (ref, d["telegram_id"], d.get("username",""), d["full_name"],
                  d["phone"], d.get("email",""), d["bank_name"],
                  d["account_number"], d.get("referrer_code","")))

    def get_pending(self, ref: str) -> dict | None:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM pending_registrations WHERE payment_ref=?", (ref,)).fetchone()
            return dict(r) if r else None

    def delete_pending(self, ref: str):
        with self._conn() as conn:
            conn.execute("DELETE FROM pending_registrations WHERE payment_ref=?", (ref,))

    # ── Commission ─────────────────────────────────────────────
    def record_commission(self, payer_id, earner_id, generation, amount, ref):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO commissions(payer_user_id,earner_user_id,generation,amount,payment_ref) VALUES(?,?,?,?,?)",
                (payer_id, earner_id, generation, amount, ref)
            )

    def get_referral_chain(self, referrer_code: str) -> list:
        """Walk up to 3 levels of referrers."""
        chain, code = [], referrer_code
        with self._conn() as conn:
            for _ in range(3):
                if not code:
                    break
                r = conn.execute(
                    "SELECT id,telegram_id,full_name,referrer_code FROM users WHERE referral_code=? AND is_active=1",
                    (code,)
                ).fetchone()
                if not r:
                    break
                chain.append(dict(r))
                code = r["referrer_code"]
        return chain

    # ── Stats ───────────────────────────────────────────────────
    def get_user_stats(self, referral_code: str) -> dict:
        """
        Returns gen1/gen2/gen3 counts.
        No cap on referrals — counts all active members in each gen.
        """
        with self._conn() as conn:
            gen1_rows = conn.execute(
                "SELECT referral_code FROM users WHERE referrer_code=? AND is_active=1",
                (referral_code,)
            ).fetchall()
            gen1 = len(gen1_rows)

            gen2_rows = []
            for r in gen1_rows:
                rows = conn.execute(
                    "SELECT referral_code FROM users WHERE referrer_code=? AND is_active=1",
                    (r["referral_code"],)
                ).fetchall()
                gen2_rows.extend(rows)
            gen2 = len(gen2_rows)

            gen3 = 0
            for r in gen2_rows:
                gen3 += conn.execute(
                    "SELECT COUNT(*) FROM users WHERE referrer_code=? AND is_active=1",
                    (r["referral_code"],)
                ).fetchone()[0]

        progress_to_80 = min(100, round(gen1 / MIN_REFERRAL_TARGET * 100))
        return {
            "gen1": gen1,
            "gen2": gen2,
            "gen3": gen3,
            "total_network": gen1 + gen2 + gen3,
            "progress_to_80": progress_to_80,
            "target": MIN_REFERRAL_TARGET,
        }

    def get_network_members(self, referral_code: str) -> list:
        """Full paginated list of gen1 direct referrals (unlimited)."""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT u.full_name, u.referral_code, u.phone, u.created_at,
                       COALESCE((SELECT COUNT(*) FROM users r WHERE r.referrer_code=u.referral_code AND r.is_active=1),0) as their_gen1
                FROM users u
                WHERE u.referrer_code=? AND u.is_active=1
                ORDER BY u.created_at DESC
            """, (referral_code,)).fetchall()
        return [dict(r) for r in rows]

    def get_transactions(self, user_id: int, limit=20) -> list:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT type,amount,description,created_at FROM transactions
                WHERE user_id=? ORDER BY created_at DESC LIMIT ?
            """, (user_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def mark_withdrawal_processed(self, withdrawal_id: int, transfer_ref: str, status: str):
        """Update withdrawal status after auto-transfer attempt."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE withdrawals SET status=?, processed_at=CURRENT_TIMESTAMP WHERE id=?",
                (status, withdrawal_id)
            )

    def create_withdrawal(self, user_id: int, amount: float) -> int:
        """Only activated (paid) users can withdraw. Raises ValueError if inactive."""
        user = self.get_user_by_id(user_id)
        if not user or not user["is_active"]:
            raise ValueError("Account not activated. Please complete payment first.")
        if amount > user["wallet_balance"]:
            raise ValueError("Insufficient balance.")
        self.debit_wallet(user_id, amount)
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO withdrawals(user_id,amount) VALUES(?,?)", (user_id, amount)
            )
            # Record as debit transaction
            conn.execute(
                "INSERT INTO transactions(user_id,type,amount,description) VALUES(?,?,?,?)",
                (user_id, "debit", amount, "Withdrawal request submitted")
            )
            return cur.lastrowid

    def get_global_stats(self) -> dict:
        with self._conn() as conn:
            total   = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            active  = conn.execute("SELECT COUNT(*) FROM users WHERE is_active=1").fetchone()[0]
            revenue = conn.execute("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE type='credit'").fetchone()[0]
            comm    = conn.execute("SELECT COALESCE(SUM(amount),0) FROM commissions").fetchone()[0]
            pending_w = conn.execute("SELECT COUNT(*) FROM withdrawals WHERE status='pending'").fetchone()[0]
            today   = date.today().isoformat()
            today_s = conn.execute("SELECT COUNT(*) FROM users WHERE DATE(created_at)=?", (today,)).fetchone()[0]
        return {
            "total_members":      total,
            "active_members":     active,
            "total_revenue":      revenue,
            "total_commissions":  comm,
            "pending_withdrawals": pending_w,
            "today_signups":      today_s,
            "min_referral_target": MIN_REFERRAL_TARGET,
        }
