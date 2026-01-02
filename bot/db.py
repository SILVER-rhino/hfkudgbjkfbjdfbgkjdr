import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import List

DEFAULT_DB_PATH = "db.sqlite3"


def _db_path() -> str:
    explicit = os.getenv("DB_PATH", "").strip()
    if explicit:
        return explicit

    # Railway: when you attach a Volume, it typically exposes a mount path via env.
    # Using it by default prevents losing SQLite data on redeploy.
    railway_mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if railway_mount:
        return os.path.join(railway_mount, DEFAULT_DB_PATH)

    return DEFAULT_DB_PATH


def init_db() -> None:
    db_path = _db_path()
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                username TEXT,
                is_subscribed INTEGER NOT NULL DEFAULT 0,
                subscribed_at TEXT,
                unsubscribed_at TEXT
            );
            """
        )

        user_cols = {row[1] for row in con.execute("PRAGMA table_info(users)").fetchall()}
        if "first_seen_at" not in user_cols:
            con.execute("ALTER TABLE users ADD COLUMN first_seen_at TEXT")
        if "last_seen_at" not in user_cols:
            con.execute("ALTER TABLE users ADD COLUMN last_seen_at TEXT")
        if "username" not in user_cols:
            con.execute("ALTER TABLE users ADD COLUMN username TEXT")
        if "is_subscribed" not in user_cols:
            con.execute("ALTER TABLE users ADD COLUMN is_subscribed INTEGER NOT NULL DEFAULT 0")
        if "subscribed_at" not in user_cols:
            con.execute("ALTER TABLE users ADD COLUMN subscribed_at TEXT")
        if "unsubscribed_at" not in user_cols:
            con.execute("ALTER TABLE users ADD COLUMN unsubscribed_at TEXT")

        con.execute("CREATE INDEX IF NOT EXISTS idx_users_is_subscribed ON users(is_subscribed);")
        con.execute("CREATE INDEX IF NOT EXISTS idx_users_last_seen_at ON users(last_seen_at);")

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                reserved_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'booked',
                group_link TEXT,
                promo_photo_file_id TEXT,
                reminder_sent_at TEXT,
                username TEXT,
                destination_links TEXT
            );
            """
        )

        # Migrate existing DBs (SQLite has no IF NOT EXISTS for columns)
        cols = {row[1] for row in con.execute("PRAGMA table_info(reservations)").fetchall()}
        if "group_link" not in cols:
            con.execute("ALTER TABLE reservations ADD COLUMN group_link TEXT")
        if "promo_photo_file_id" not in cols:
            con.execute("ALTER TABLE reservations ADD COLUMN promo_photo_file_id TEXT")
        if "reminder_sent_at" not in cols:
            con.execute("ALTER TABLE reservations ADD COLUMN reminder_sent_at TEXT")
        if "username" not in cols:
            con.execute("ALTER TABLE reservations ADD COLUMN username TEXT")
        if "destination_links" not in cols:
            con.execute("ALTER TABLE reservations ADD COLUMN destination_links TEXT")
        # Prevent double-booking the same time slot for active statuses.
        # Keep the previous index (if exists) but also enforce for pending payments.
        con.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_reservations_reserved_at_active
            ON reservations(reserved_at)
            WHERE status IN ('booked', 'pending_payment');
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_reservations_user_id ON reservations(user_id);"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_reservations_reserved_at ON reservations(reserved_at);"
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS payment_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reservation_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                card_number TEXT NOT NULL,
                coupon_code TEXT,
                coupon_percent INTEGER,
                receipt_photo_file_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                reviewed_at TEXT,
                reviewer_id INTEGER,
                reject_reason TEXT,
                FOREIGN KEY(reservation_id) REFERENCES reservations(id)
            );
            """
        )

        pay_cols = {row[1] for row in con.execute("PRAGMA table_info(payment_requests)").fetchall()}
        if "coupon_code" not in pay_cols:
            con.execute("ALTER TABLE payment_requests ADD COLUMN coupon_code TEXT")
        if "coupon_percent" not in pay_cols:
            con.execute("ALTER TABLE payment_requests ADD COLUMN coupon_percent INTEGER")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_payment_requests_status ON payment_requests(status);"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_payment_requests_reservation_id ON payment_requests(reservation_id);"
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS discount_codes (
                code TEXT PRIMARY KEY,
                percent INTEGER NOT NULL,
                max_uses INTEGER NOT NULL,
                used_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_discount_codes_expires_at ON discount_codes(expires_at);")
        con.execute("CREATE INDEX IF NOT EXISTS idx_discount_codes_is_active ON discount_codes(is_active);")

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS verification_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                card_number TEXT NOT NULL,
                photo_file_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                reviewed_at TEXT,
                reviewer_id INTEGER,
                decision_reason TEXT
            );
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_verification_requests_user_id ON verification_requests(user_id);"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_verification_requests_status ON verification_requests(status);"
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS verified_cards (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                card_number TEXT NOT NULL,
                verified_at TEXT NOT NULL,
                verifier_id INTEGER
            );
            """
        )


def upsert_user(user_id: int, username: str | None) -> None:
    db_path = _db_path()
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            INSERT INTO users(user_id, first_seen_at, last_seen_at, username)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                username = COALESCE(excluded.username, users.username)
            """,
            (user_id, now_iso, now_iso, username),
        )


def set_user_subscription(user_id: int, subscribed: bool, username: str | None = None) -> None:
    db_path = _db_path()
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as con:
        # Ensure user exists
        con.execute(
            """
            INSERT INTO users(user_id, first_seen_at, last_seen_at, username)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                username = COALESCE(excluded.username, users.username)
            """,
            (user_id, now_iso, now_iso, username),
        )

        if subscribed:
            con.execute(
                """
                UPDATE users
                SET is_subscribed = 1,
                    subscribed_at = ?,
                    unsubscribed_at = NULL,
                    last_seen_at = ?,
                    username = COALESCE(?, username)
                WHERE user_id = ?
                """,
                (now_iso, now_iso, username, user_id),
            )
        else:
            con.execute(
                """
                UPDATE users
                SET is_subscribed = 0,
                    unsubscribed_at = ?,
                    last_seen_at = ?,
                    username = COALESCE(?, username)
                WHERE user_id = ?
                """,
                (now_iso, now_iso, username, user_id),
            )


def list_subscribed_user_ids(limit: int | None = None) -> list[int]:
    db_path = _db_path()
    q = "SELECT user_id FROM users WHERE is_subscribed = 1 ORDER BY subscribed_at ASC"
    params: tuple = ()
    if limit is not None:
        q += " LIMIT ?"
        params = (limit,)
    with sqlite3.connect(db_path) as con:
        rows = con.execute(q, params).fetchall()
    return [int(r[0]) for r in rows]


@dataclass(frozen=True)
class AdminStats:
    total_users: int
    subscribed_users: int
    active_24h_users: int
    active_7d_users: int
    reservations_total: int
    reservations_booked: int
    reservations_pending_payment: int
    reservations_cancelled: int
    payment_total: int
    payment_pending: int
    payment_approved: int
    payment_rejected: int
    verification_total: int
    verification_pending: int
    verification_approved: int
    verification_rejected: int
    last_user_seen_at: str | None


def get_admin_stats(active_24h_since_iso: str, active_7d_since_iso: str) -> AdminStats:
    db_path = _db_path()
    with sqlite3.connect(db_path) as con:
        total_users = int(con.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        subscribed_users = int(con.execute("SELECT COUNT(*) FROM users WHERE is_subscribed = 1").fetchone()[0])
        active_24h_users = int(
            con.execute(
                "SELECT COUNT(*) FROM users WHERE last_seen_at >= ?",
                (active_24h_since_iso,),
            ).fetchone()[0]
        )
        active_7d_users = int(
            con.execute(
                "SELECT COUNT(*) FROM users WHERE last_seen_at >= ?",
                (active_7d_since_iso,),
            ).fetchone()[0]
        )

        last_seen_row = con.execute("SELECT MAX(last_seen_at) FROM users").fetchone()
        last_user_seen_at = str(last_seen_row[0]) if last_seen_row and last_seen_row[0] else None

        reservations_total = int(con.execute("SELECT COUNT(*) FROM reservations").fetchone()[0])
        reservations_booked = int(con.execute("SELECT COUNT(*) FROM reservations WHERE status = 'booked'").fetchone()[0])
        reservations_pending_payment = int(
            con.execute("SELECT COUNT(*) FROM reservations WHERE status = 'pending_payment'").fetchone()[0]
        )
        reservations_cancelled = int(
            con.execute("SELECT COUNT(*) FROM reservations WHERE status = 'cancelled'").fetchone()[0]
        )

        payment_total = int(con.execute("SELECT COUNT(*) FROM payment_requests").fetchone()[0])
        payment_pending = int(con.execute("SELECT COUNT(*) FROM payment_requests WHERE status = 'pending'").fetchone()[0])
        payment_approved = int(
            con.execute("SELECT COUNT(*) FROM payment_requests WHERE status = 'approved'").fetchone()[0]
        )
        payment_rejected = int(
            con.execute("SELECT COUNT(*) FROM payment_requests WHERE status = 'rejected'").fetchone()[0]
        )

        verification_total = int(con.execute("SELECT COUNT(*) FROM verification_requests").fetchone()[0])
        verification_pending = int(
            con.execute("SELECT COUNT(*) FROM verification_requests WHERE status = 'pending'").fetchone()[0]
        )
        verification_approved = int(
            con.execute("SELECT COUNT(*) FROM verification_requests WHERE status = 'approved'").fetchone()[0]
        )
        verification_rejected = int(
            con.execute("SELECT COUNT(*) FROM verification_requests WHERE status = 'rejected'").fetchone()[0]
        )

    return AdminStats(
        total_users=total_users,
        subscribed_users=subscribed_users,
        active_24h_users=active_24h_users,
        active_7d_users=active_7d_users,
        reservations_total=reservations_total,
        reservations_booked=reservations_booked,
        reservations_pending_payment=reservations_pending_payment,
        reservations_cancelled=reservations_cancelled,
        payment_total=payment_total,
        payment_pending=payment_pending,
        payment_approved=payment_approved,
        payment_rejected=payment_rejected,
        verification_total=verification_total,
        verification_pending=verification_pending,
        verification_approved=verification_approved,
        verification_rejected=verification_rejected,
        last_user_seen_at=last_user_seen_at,
    )


def add_reservation(user_id: int, reserved_at: datetime) -> int:
    db_path = _db_path()
    created_at = datetime.utcnow().isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as con:
        cur = con.execute(
            """
            INSERT INTO reservations(user_id, reserved_at, created_at, status)
            VALUES (?, ?, ?, 'booked')
            """,
            (user_id, reserved_at.isoformat(timespec="seconds"), created_at),
        )
        return int(cur.lastrowid)


def is_slot_reserved(reserved_at: datetime) -> bool:
    db_path = _db_path()
    reserved_iso = reserved_at.isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as con:
        row = con.execute(
            """
            SELECT 1
            FROM reservations
            WHERE reserved_at = ? AND status IN ('booked', 'pending_payment')
            LIMIT 1
            """,
            (reserved_iso,),
        ).fetchone()
    return row is not None


def get_slot_owner_user_id(reserved_at: datetime) -> int | None:
    db_path = _db_path()
    reserved_iso = reserved_at.isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as con:
        row = con.execute(
            """
            SELECT user_id
            FROM reservations
            WHERE reserved_at = ? AND status IN ('booked', 'pending_payment')
            LIMIT 1
            """,
            (reserved_iso,),
        ).fetchone()
    return int(row[0]) if row else None


def try_reserve_slot(user_id: int, reserved_at: datetime) -> bool:
    """Returns True if reservation was created, False if slot already reserved."""
    db_path = _db_path()
    created_at = datetime.utcnow().isoformat(timespec="seconds")
    try:
        with sqlite3.connect(db_path) as con:
            con.execute(
                """
                INSERT INTO reservations(user_id, reserved_at, created_at, status)
                VALUES (?, ?, ?, 'booked')
                """,
                (user_id, reserved_at.isoformat(timespec="seconds"), created_at),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def try_hold_slot_pending_payment(user_id: int, reserved_at: datetime) -> int | None:
    """Creates a pending_payment reservation. Returns reservation id or None if slot already taken."""
    db_path = _db_path()
    created_at = datetime.utcnow().isoformat(timespec="seconds")
    try:
        with sqlite3.connect(db_path) as con:
            cur = con.execute(
                """
                INSERT INTO reservations(user_id, reserved_at, created_at, status)
                VALUES (?, ?, ?, 'pending_payment')
                """,
                (user_id, reserved_at.isoformat(timespec="seconds"), created_at),
            )
            return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None


@dataclass(frozen=True)
class Reservation:
    id: int
    user_id: int
    reserved_at: str
    created_at: str
    status: str


@dataclass(frozen=True)
class ReservationFull:
    id: int
    user_id: int
    reserved_at: str
    created_at: str
    status: str
    group_link: str | None
    promo_photo_file_id: str | None
    reminder_sent_at: str | None
    username: str | None
    destination_links: str | None


@dataclass(frozen=True)
class ReminderCandidate:
    reservation_id: int
    user_id: int
    reserved_at: str
    group_link: str | None
    promo_photo_file_id: str | None
    username: str | None


def list_reservations_for_user(user_id: int, limit: int = 20) -> List[Reservation]:
    db_path = _db_path()
    with sqlite3.connect(db_path) as con:
        rows = con.execute(
            """
            SELECT id, user_id, reserved_at, created_at, status
            FROM reservations
            WHERE user_id = ? AND status = 'booked'
            ORDER BY reserved_at ASC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()

    return [Reservation(*row) for row in rows]


def get_reservation(reservation_id: int) -> Reservation | None:
    db_path = _db_path()
    with sqlite3.connect(db_path) as con:
        row = con.execute(
            """
            SELECT id, user_id, reserved_at, created_at, status
            FROM reservations
            WHERE id = ?
            """,
            (reservation_id,),
        ).fetchone()
    return Reservation(*row) if row else None


def get_reservation_full(reservation_id: int) -> ReservationFull | None:
    db_path = _db_path()
    with sqlite3.connect(db_path) as con:
        row = con.execute(
            """
            SELECT id, user_id, reserved_at, created_at, status,
                   group_link, promo_photo_file_id, reminder_sent_at, username, destination_links
            FROM reservations
            WHERE id = ?
            """,
            (reservation_id,),
        ).fetchone()
    return ReservationFull(*row) if row else None


def set_reservation_status(reservation_id: int, status: str) -> None:
    db_path = _db_path()
    with sqlite3.connect(db_path) as con:
        con.execute(
            "UPDATE reservations SET status = ? WHERE id = ?",
            (status, reservation_id),
        )


def update_reservation_promo(
    reservation_id: int,
    username: str | None,
    group_link: str | None,
    promo_photo_file_id: str | None,
) -> None:
    db_path = _db_path()
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            UPDATE reservations
            SET username = COALESCE(?, username),
                group_link = COALESCE(?, group_link),
                promo_photo_file_id = COALESCE(?, promo_photo_file_id)
            WHERE id = ?
            """,
            (username, group_link, promo_photo_file_id, reservation_id),
        )


def update_reservation_destination_links(reservation_id: int, destination_links: str | None) -> None:
    db_path = _db_path()
    with sqlite3.connect(db_path) as con:
        con.execute(
            "UPDATE reservations SET destination_links = ? WHERE id = ?",
            (destination_links, reservation_id),
        )


def mark_reservation_reminded(reservation_id: int, reminded_at_iso: str) -> None:
    db_path = _db_path()
    with sqlite3.connect(db_path) as con:
        con.execute(
            "UPDATE reservations SET reminder_sent_at = ? WHERE id = ?",
            (reminded_at_iso, reservation_id),
        )


def list_reservations_due_for_reminder(window_start_iso: str, window_end_iso: str) -> list[ReminderCandidate]:
    """Booked reservations in [window_start, window_end] with no reminder yet."""
    db_path = _db_path()
    with sqlite3.connect(db_path) as con:
        rows = con.execute(
            """
            SELECT id, user_id, reserved_at, group_link, promo_photo_file_id, username
            FROM reservations
            WHERE status = 'booked'
              AND reminder_sent_at IS NULL
              AND reserved_at >= ?
              AND reserved_at <= ?
            ORDER BY reserved_at ASC
            """,
            (window_start_iso, window_end_iso),
        ).fetchall()

    return [ReminderCandidate(*row) for row in rows]


@dataclass(frozen=True)
class PaymentRequest:
    id: int
    reservation_id: int
    user_id: int
    username: str | None
    card_number: str
    coupon_code: str | None
    coupon_percent: int | None
    receipt_photo_file_id: str
    status: str
    created_at: str
    reviewed_at: str | None
    reviewer_id: int | None
    reject_reason: str | None


def create_payment_request(
    reservation_id: int,
    user_id: int,
    username: str | None,
    card_number: str,
    coupon_code: str | None,
    coupon_percent: int | None,
    receipt_photo_file_id: str,
) -> int:
    db_path = _db_path()
    created_at = datetime.utcnow().isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as con:
        cur = con.execute(
            """
            INSERT INTO payment_requests(
                reservation_id, user_id, username, card_number, coupon_code, coupon_percent, receipt_photo_file_id, status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (reservation_id, user_id, username, card_number, coupon_code, coupon_percent, receipt_photo_file_id, created_at),
        )
        return int(cur.lastrowid)


def get_payment_request(payment_id: int) -> PaymentRequest | None:
    db_path = _db_path()
    with sqlite3.connect(db_path) as con:
        row = con.execute(
            """
            SELECT id, reservation_id, user_id, username, card_number, coupon_code, coupon_percent, receipt_photo_file_id,
                   status, created_at, reviewed_at, reviewer_id, reject_reason
            FROM payment_requests
            WHERE id = ?
            """,
            (payment_id,),
        ).fetchone()
    return PaymentRequest(*row) if row else None


@dataclass(frozen=True)
class DiscountCode:
    code: str
    percent: int
    max_uses: int
    used_count: int
    created_at: str
    created_by: int
    expires_at: str
    is_active: int


def normalize_discount_code(code: str) -> str:
    return code.strip().lower()


def create_discount_code(
    code: str,
    percent: int,
    max_uses: int,
    expires_at_iso: str,
    created_by: int,
) -> None:
    """Create a new discount code. Raises sqlite3.IntegrityError if code already exists."""
    db_path = _db_path()
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    norm = normalize_discount_code(code)
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            INSERT INTO discount_codes(code, percent, max_uses, used_count, created_at, created_by, expires_at, is_active)
            VALUES (?, ?, ?, 0, ?, ?, ?, 1)
            """,
            (norm, int(percent), int(max_uses), now_iso, int(created_by), expires_at_iso),
        )


def get_discount_code(code: str) -> DiscountCode | None:
    db_path = _db_path()
    norm = normalize_discount_code(code)
    with sqlite3.connect(db_path) as con:
        row = con.execute(
            """
            SELECT code, percent, max_uses, used_count, created_at, created_by, expires_at, is_active
            FROM discount_codes
            WHERE code = ?
            """,
            (norm,),
        ).fetchone()
    return DiscountCode(*row) if row else None


def can_use_discount_code(code: str, now_iso: str) -> tuple[bool, str, int | None]:
    """Returns (ok, reason, percent). now_iso should be UTC ISO string."""
    dc = get_discount_code(code)
    if dc is None:
        return False, "not_found", None
    if int(dc.is_active) != 1:
        return False, "inactive", None
    try:
        if datetime.fromisoformat(now_iso) >= datetime.fromisoformat(dc.expires_at):
            return False, "expired", None
    except Exception:
        # If parsing fails, be safe.
        return False, "expired", None
    if int(dc.used_count) >= int(dc.max_uses):
        return False, "used_up", None
    return True, "ok", int(dc.percent)


def consume_discount_code(code: str, now_iso: str) -> bool:
    """Atomically increments used_count if usable. Returns True if consumed."""
    db_path = _db_path()
    norm = normalize_discount_code(code)
    with sqlite3.connect(db_path) as con:
        cur = con.execute(
            """
            UPDATE discount_codes
            SET used_count = used_count + 1
            WHERE code = ?
              AND is_active = 1
              AND used_count < max_uses
              AND expires_at > ?
            """,
            (norm, now_iso),
        )
        return cur.rowcount == 1


def set_payment_status(
    payment_id: int,
    status: str,
    reviewer_id: int | None,
    reject_reason: str | None = None,
) -> None:
    db_path = _db_path()
    reviewed_at = datetime.utcnow().isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            UPDATE payment_requests
            SET status = ?, reviewed_at = ?, reviewer_id = ?, reject_reason = ?
            WHERE id = ?
            """,
            (status, reviewed_at, reviewer_id, reject_reason, payment_id),
        )


@dataclass(frozen=True)
class VerificationRequest:
    id: int
    user_id: int
    username: str | None
    card_number: str
    photo_file_id: str
    status: str
    created_at: str
    reviewed_at: str | None
    reviewer_id: int | None
    decision_reason: str | None


def create_verification_request(user_id: int, username: str | None, card_number: str, photo_file_id: str) -> int:
    db_path = _db_path()
    created_at = datetime.utcnow().isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as con:
        cur = con.execute(
            """
            INSERT INTO verification_requests(user_id, username, card_number, photo_file_id, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (user_id, username, card_number, photo_file_id, created_at),
        )
        return int(cur.lastrowid)


def get_verification_request(request_id: int) -> VerificationRequest | None:
    db_path = _db_path()
    with sqlite3.connect(db_path) as con:
        row = con.execute(
            """
            SELECT id, user_id, username, card_number, photo_file_id, status,
                   created_at, reviewed_at, reviewer_id, decision_reason
            FROM verification_requests
            WHERE id = ?
            """,
            (request_id,),
        ).fetchone()
    return VerificationRequest(*row) if row else None


def set_verification_status(
    request_id: int,
    status: str,
    reviewer_id: int | None,
    decision_reason: str | None = None,
) -> None:
    db_path = _db_path()
    reviewed_at = datetime.utcnow().isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            UPDATE verification_requests
            SET status = ?, reviewed_at = ?, reviewer_id = ?, decision_reason = ?
            WHERE id = ?
            """,
            (status, reviewed_at, reviewer_id, decision_reason, request_id),
        )


def upsert_verified_card(user_id: int, username: str | None, card_number: str, verifier_id: int | None) -> None:
    db_path = _db_path()
    verified_at = datetime.utcnow().isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            INSERT INTO verified_cards(user_id, username, card_number, verified_at, verifier_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                card_number = excluded.card_number,
                verified_at = excluded.verified_at,
                verifier_id = excluded.verifier_id
            """,
            (user_id, username, card_number, verified_at, verifier_id),
        )


def get_verified_card_number(user_id: int) -> str | None:
    db_path = _db_path()
    with sqlite3.connect(db_path) as con:
        row = con.execute(
            "SELECT card_number FROM verified_cards WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return str(row[0]) if row else None
