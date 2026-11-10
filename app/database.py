import hashlib
import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from app.config import settings

MAX_BOOKING_DAYS_AHEAD = 60  # 2 months ahead
PENDING_HOLD_MINUTES = 30  # Automatically free unpaid PENDING_PAYMENT slots after 30 minutes
MAX_PENDING_PER_IP = 2  # Anti-spam: max concurrent unpaid holds from a single IP
ROME_TZ = ZoneInfo(settings.HOST_TIMEZONE)

ACTIVE_SLOT_STATUSES = ("PENDING_PAYMENT", "CONFIRMED")

# Workdays (Mon-Fri): 17:00 to 22:00 Rome time -> start hours 17, 18, 19, 20, 21
WORKDAY_START_HOURS_ROME = [17, 18, 19, 20, 21]
# Weekends (Sat-Sun): 09:00 to 12:00 and 14:00 to 18:00 Rome time -> start hours 9, 10, 11 and 14, 15, 16, 17
WEEKEND_START_HOURS_ROME = [9, 10, 11, 14, 15, 16, 17]

DEFAULT_WEEKLY_SCHEDULE_ROME: Dict[str, List[int]] = {
    "0": list(WORKDAY_START_HOURS_ROME),  # Monday
    "1": list(WORKDAY_START_HOURS_ROME),  # Tuesday
    "2": list(WORKDAY_START_HOURS_ROME),  # Wednesday
    "3": list(WORKDAY_START_HOURS_ROME),  # Thursday
    "4": list(WORKDAY_START_HOURS_ROME),  # Friday
    "5": list(WEEKEND_START_HOURS_ROME),  # Saturday
    "6": list(WEEKEND_START_HOURS_ROME),  # Sunday
}


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").strip().encode("utf-8")).hexdigest()


def generate_bundle_token() -> Tuple[str, str]:
    raw_token = secrets.token_urlsafe(32)
    return raw_token, hash_token(raw_token)


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(settings.DATABASE_PATH), timeout=30.0, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    return conn


def _transition_session_status(
    cur: sqlite3.Cursor,
    session_id: str,
    allowed_from: Set[str],
    to_status: str,
) -> None:
    if not allowed_from:
        raise ValueError("allowed_from cannot be empty")
    placeholders = ",".join("?" for _ in allowed_from)
    params = [to_status, session_id, *sorted(allowed_from)]
    cur.execute(
        f"UPDATE interview_sessions SET status = ? WHERE id = ? AND status IN ({placeholders})",
        params,
    )
    if cur.rowcount == 0:
        row = cur.execute(
            "SELECT status FROM interview_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        current = row["status"] if row else "MISSING"
        raise ValueError(
            f"Cannot transition session {session_id} from '{current}' to '{to_status}'."
        )


def _release_slot_if_unused(cur: sqlite3.Cursor, slot_id: int) -> bool:
    """
    Sets `availability_slots.is_booked = 0` ONLY when no active session
    (`PENDING_PAYMENT` or `CONFIRMED`) currently references `slot_id`.
    Prevents a late reschedule/cancel/expiry from freeing someone else's slot.
    """
    active_row = cur.execute(
        """
        SELECT COUNT(*) AS cnt FROM interview_sessions
        WHERE slot_id = ? AND status IN ('PENDING_PAYMENT', 'CONFIRMED')
        """,
        (slot_id,),
    ).fetchone()
    if active_row and int(active_row["cnt"]) > 0:
        return False
    cur.execute(
        "UPDATE availability_slots SET is_booked = 0 WHERE id = ?",
        (slot_id,),
    )
    return cur.rowcount > 0


def get_weekly_schedule() -> Dict[str, List[int]]:
    conn = get_db_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS weekly_schedule_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            schedule_json TEXT NOT NULL
        );
        """
    )
    row = conn.execute("SELECT schedule_json FROM weekly_schedule_settings WHERE id = 1").fetchone()
    if not row:
        conn.execute(
            "INSERT INTO weekly_schedule_settings (id, schedule_json) VALUES (1, ?)",
            (json.dumps(DEFAULT_WEEKLY_SCHEDULE_ROME),),
        )
        conn.commit()
        conn.close()
        return dict(DEFAULT_WEEKLY_SCHEDULE_ROME)
    conn.close()
    try:
        parsed = json.loads(row["schedule_json"])
        return {str(k): sorted([int(h) for h in v]) for k, v in parsed.items()}
    except Exception:
        return dict(DEFAULT_WEEKLY_SCHEDULE_ROME)


def save_weekly_schedule_and_sync(
    schedule_map: Dict[str, List[int]],
    days_ahead: int = MAX_BOOKING_DAYS_AHEAD,
    clear_unbooked: bool = True,
) -> int:
    """
    Saves the interviewer's recurring weekly schedule (in Europe/Rome hours) and
    syncs availability slots across the next `days_ahead` days (max 60).
    Booked slots are always preserved.
    """
    normalized: Dict[str, List[int]] = {}
    for d in range(7):
        raw_hours = schedule_map.get(str(d), [])
        normalized[str(d)] = sorted(
            list({int(h) for h in raw_hours if 0 <= int(h) <= 23})
        )

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS weekly_schedule_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            schedule_json TEXT NOT NULL
        );
        """
    )
    cur.execute(
        """
        INSERT INTO weekly_schedule_settings (id, schedule_json)
        VALUES (1, ?)
        ON CONFLICT(id) DO UPDATE SET schedule_json = excluded.schedule_json;
        """,
        (json.dumps(normalized),),
    )

    if clear_unbooked:
        cur.execute("DELETE FROM availability_slots WHERE is_booked = 0;")

    existing_starts = {
        r["start_time"]
        for r in conn.execute("SELECT start_time FROM availability_slots").fetchall()
    }

    now_rome = datetime.now(ROME_TZ).replace(minute=0, second=0, microsecond=0)
    days_ahead = min(max(1, days_ahead), MAX_BOOKING_DAYS_AHEAD)
    added = 0

    for day_offset in range(1, days_ahead + 1):
        day_rome = now_rome + timedelta(days=day_offset)
        hours = normalized.get(str(day_rome.weekday()), [])
        for hr in hours:
            slot_start_rome = day_rome.replace(hour=hr, minute=0, second=0, microsecond=0)
            slot_start_utc = slot_start_rome.astimezone(timezone.utc)
            slot_end_utc = slot_start_utc + timedelta(minutes=settings.SESSION_DURATION_MINUTES)
            start_iso = slot_start_utc.isoformat()
            if start_iso not in existing_starts:
                cur.execute(
                    "INSERT INTO availability_slots (start_time, end_time, is_booked) VALUES (?, ?, 0)",
                    (start_iso, slot_end_utc.isoformat()),
                )
                existing_starts.add(start_iso)
                added += 1

    conn.commit()
    conn.close()
    return added


def seed_rome_availability_schedule(
    days_ahead: int = MAX_BOOKING_DAYS_AHEAD, clear_unbooked: bool = False
) -> int:
    current_weekly = get_weekly_schedule()
    return save_weekly_schedule_and_sync(
        schedule_map=current_weekly,
        days_ahead=days_ahead,
        clear_unbooked=clear_unbooked,
    )


def toggle_date_hour_rome(date_str: str, hour_rome: int) -> Dict[str, Any]:
    """
    Toggles a single 1-hour slot on `date_str` (YYYY-MM-DD) at `hour_rome` (0..23)
    in Europe/Rome timezone:
    - If an unbooked slot exists at that time, deletes it (closes the hour).
    - If no slot exists at that time, creates it (opens the hour).
    - If a booked slot exists, leaves it untouched and returns status='booked'.
    """
    parts = date_str.split("-")
    if len(parts) != 3:
        raise ValueError("Invalid date format; expected YYYY-MM-DD.")
    year, month, day = [int(p) for p in parts]
    hr = int(hour_rome)
    if not (0 <= hr <= 23):
        raise ValueError("Hour must be between 0 and 23.")
    slot_start_rome = datetime(year, month, day, hr, 0, 0, tzinfo=ROME_TZ)
    slot_start_utc = slot_start_rome.astimezone(timezone.utc)
    slot_end_utc = slot_start_utc + timedelta(minutes=settings.SESSION_DURATION_MINUTES)
    start_iso = slot_start_utc.isoformat()

    conn = get_db_connection()
    cur = conn.cursor()
    row = cur.execute(
        "SELECT id, is_booked FROM availability_slots WHERE start_time = ?",
        (start_iso,),
    ).fetchone()

    if row:
        if row["is_booked"]:
            conn.close()
            return {"action": "locked_booked", "date": date_str, "hour": hr}
        cur.execute("DELETE FROM availability_slots WHERE id = ?", (row["id"],))
        conn.commit()
        conn.close()
        return {"action": "closed", "date": date_str, "hour": hr}
    else:
        cur.execute(
            "INSERT INTO availability_slots (start_time, end_time, is_booked) VALUES (?, ?, 0)",
            (start_iso, slot_end_utc.isoformat()),
        )
        slot_id = cur.lastrowid
        conn.commit()
        conn.close()
        return {"action": "opened", "slot_id": slot_id, "date": date_str, "hour": hr}


def set_date_hours_rome(date_str: str, hours_rome: List[int]) -> Dict[str, Any]:
    """
    Sets the exact open hours (in Europe/Rome time) for a single date `date_str` (YYYY-MM-DD).
    Pass `hours_rome=[]` to block the entire day. Preserves any already-booked slots.
    """
    parts = date_str.split("-")
    if len(parts) != 3:
        raise ValueError("Invalid date format; expected YYYY-MM-DD.")
    year, month, day = [int(p) for p in parts]
    day_start_rome = datetime(year, month, day, 0, 0, 0, tzinfo=ROME_TZ)
    day_end_rome = day_start_rome + timedelta(days=1)

    start_utc_iso = day_start_rome.astimezone(timezone.utc).isoformat()
    end_utc_iso = day_end_rome.astimezone(timezone.utc).isoformat()

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        DELETE FROM availability_slots
        WHERE is_booked = 0 AND start_time >= ? AND start_time < ?
        """,
        (start_utc_iso, end_utc_iso),
    )

    existing_booked = {
        r["start_time"]
        for r in cur.execute(
            "SELECT start_time FROM availability_slots WHERE start_time >= ? AND start_time < ?",
            (start_utc_iso, end_utc_iso),
        ).fetchall()
    }

    added = 0
    for hr in sorted(set(int(h) for h in hours_rome if 0 <= int(h) <= 23)):
        slot_start_rome = datetime(year, month, day, hr, 0, 0, tzinfo=ROME_TZ)
        slot_start_utc = slot_start_rome.astimezone(timezone.utc)
        slot_end_utc = slot_start_utc + timedelta(minutes=settings.SESSION_DURATION_MINUTES)
        s_iso = slot_start_utc.isoformat()
        if s_iso not in existing_booked:
            cur.execute(
                "INSERT INTO availability_slots (start_time, end_time, is_booked) VALUES (?, ?, 0)",
                (s_iso, slot_end_utc.isoformat()),
            )
            added += 1

    conn.commit()
    conn.close()
    return {"date": date_str, "open_hours_set": added}


def list_slots_by_rome_date() -> Dict[str, Dict[str, Any]]:
    """
    Returns a dictionary keyed by `YYYY-MM-DD` (in Europe/Rome timezone) mapping
    each hour (`0`..`23`) to its slot info (`open` vs `booked`, `slot_id`, `candidate_name`).
    """
    all_slots = list_all_slots()
    by_date: Dict[str, Dict[str, Any]] = {}
    for s in all_slots:
        try:
            dt_utc = datetime.fromisoformat(s["start_time"].replace("Z", "+00:00"))
            dt_rome = dt_utc.astimezone(ROME_TZ)
            date_key = dt_rome.strftime("%Y-%m-%d")
            hr_key = str(dt_rome.hour)
            by_date.setdefault(date_key, {})[hr_key] = {
                "slot_id": s["id"],
                "hour": dt_rome.hour,
                "is_booked": bool(s["is_booked"]),
                "candidate_name": s.get("candidate_name") or "",
                "session_id": s.get("session_id") or "",
            }
        except Exception:
            continue
    return by_date


def compute_package_price_eur(package_type: str, youtube_consent: bool) -> int:
    if package_type == "bundle_3":
        return (
            settings.PRICE_BUNDLE3_YOUTUBE_EUR
            if youtube_consent
            else settings.PRICE_BUNDLE3_STANDARD_EUR
        )
    return (
        settings.PRICE_SINGLE_YOUTUBE_EUR
        if youtube_consent
        else settings.PRICE_SINGLE_STANDARD_EUR
    )


def _ensure_schema_columns(cur: sqlite3.Cursor) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS booking_bundles (
            id TEXT PRIMARY KEY,
            bundle_code TEXT NOT NULL UNIQUE,
            access_token_hash TEXT NOT NULL DEFAULT '',
            access_token TEXT NOT NULL DEFAULT '',
            revoked INTEGER NOT NULL DEFAULT 0,
            last_lookup_sent_at TEXT NOT NULL DEFAULT '',
            candidate_name TEXT NOT NULL,
            candidate_email TEXT NOT NULL,
            leetcode_solved INTEGER NOT NULL DEFAULT 0,
            target_company TEXT NOT NULL DEFAULT '',
            preferred_language TEXT NOT NULL DEFAULT 'python',
            target_role TEXT NOT NULL DEFAULT '',
            total_sessions INTEGER NOT NULL DEFAULT 3,
            credits_remaining INTEGER NOT NULL DEFAULT 2,
            youtube_consent INTEGER NOT NULL DEFAULT 0,
            youtube_consent_timestamp TEXT NOT NULL DEFAULT '',
            youtube_consent_ip TEXT NOT NULL DEFAULT '',
            price_eur INTEGER NOT NULL DEFAULT 190,
            email_message_id TEXT,
            email_subject TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    bundle_cols = {
        r["name"] for r in cur.execute("PRAGMA table_info(booking_bundles);").fetchall()
    }
    bundle_migrations = [
        ("access_token_hash", "TEXT NOT NULL DEFAULT ''"),
        ("access_token", "TEXT NOT NULL DEFAULT ''"),
        ("revoked", "INTEGER NOT NULL DEFAULT 0"),
        ("last_lookup_sent_at", "TEXT NOT NULL DEFAULT ''"),
    ]
    for col_name, col_def in bundle_migrations:
        if col_name not in bundle_cols:
            cur.execute(f"ALTER TABLE booking_bundles ADD COLUMN {col_name} {col_def};")

    # Backfill tokens for any existing bundles missing access_token_hash
    missing_token_rows = cur.execute(
        "SELECT id FROM booking_bundles WHERE access_token_hash = '' OR access_token = ''"
    ).fetchall()
    for r in missing_token_rows:
        raw_tok, tok_hash = generate_bundle_token()
        cur.execute(
            "UPDATE booking_bundles SET access_token = ?, access_token_hash = ? WHERE id = ?",
            (raw_tok, tok_hash, r["id"]),
        )

    existing_cols = {
        r["name"] for r in cur.execute("PRAGMA table_info(interview_sessions);").fetchall()
    }
    migrations = [
        ("client_ip", "TEXT NOT NULL DEFAULT ''"),
        ("package_type", "TEXT NOT NULL DEFAULT 'single'"),
        ("bundle_id", "TEXT NOT NULL DEFAULT ''"),
        ("bundle_session_number", "INTEGER NOT NULL DEFAULT 1"),
        ("bundle_total_sessions", "INTEGER NOT NULL DEFAULT 1"),
        ("youtube_consent", "INTEGER NOT NULL DEFAULT 0"),
        ("youtube_consent_timestamp", "TEXT NOT NULL DEFAULT ''"),
        ("youtube_consent_ip", "TEXT NOT NULL DEFAULT ''"),
        ("price_eur", "INTEGER NOT NULL DEFAULT 70"),
        ("hold_expires_at", "TEXT NOT NULL DEFAULT ''"),
        ("revolut_order_id", "TEXT NOT NULL DEFAULT ''"),
        ("revolut_checkout_url", "TEXT NOT NULL DEFAULT ''"),
        ("paid_at", "TEXT NOT NULL DEFAULT ''"),
        ("payment_amount_minor", "INTEGER NOT NULL DEFAULT 0"),
    ]
    for col_name, col_def in migrations:
        if col_name not in existing_cols:
            cur.execute(f"ALTER TABLE interview_sessions ADD COLUMN {col_name} {col_def};")

    # Ensure partial unique index so two active sessions can never occupy the same slot
    try:
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_session_per_slot
            ON interview_sessions(slot_id)
            WHERE status IN ('PENDING_PAYMENT', 'CONFIRMED');
            """
        )
    except sqlite3.IntegrityError:
        # Clean up any legacy duplicate active rows before creating index
        dups = cur.execute(
            """
            SELECT id FROM interview_sessions
            WHERE status IN ('PENDING_PAYMENT', 'CONFIRMED')
              AND rowid NOT IN (
                  SELECT MIN(rowid) FROM interview_sessions
                  WHERE status IN ('PENDING_PAYMENT', 'CONFIRMED')
                  GROUP BY slot_id
              )
            """
        ).fetchall()
        for d in dups:
            cur.execute("UPDATE interview_sessions SET status = 'CANCELLED' WHERE id = ?", (d["id"],))
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_session_per_slot
            ON interview_sessions(slot_id)
            WHERE status IN ('PENDING_PAYMENT', 'CONFIRMED');
            """
        )


def init_db() -> None:
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS availability_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            is_booked INTEGER NOT NULL DEFAULT 0
        );
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS weekly_schedule_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            schedule_json TEXT NOT NULL
        );
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS interview_sessions (
            id TEXT PRIMARY KEY,
            interviewer_secret TEXT NOT NULL,
            slot_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING_PAYMENT',
            created_at TEXT NOT NULL,
            hold_expires_at TEXT NOT NULL DEFAULT '',

            -- Candidate Intake Fields
            candidate_name TEXT NOT NULL,
            candidate_email TEXT NOT NULL,
            client_ip TEXT NOT NULL DEFAULT '',
            leetcode_solved INTEGER NOT NULL DEFAULT 0,
            target_company TEXT NOT NULL DEFAULT '',
            preferred_language TEXT NOT NULL DEFAULT 'python',
            target_role TEXT NOT NULL DEFAULT '',
            resume_path_or_url TEXT NOT NULL DEFAULT '',

            -- Package, Bundle & YouTube Consent Fields
            package_type TEXT NOT NULL DEFAULT 'single',
            bundle_id TEXT NOT NULL DEFAULT '',
            bundle_session_number INTEGER NOT NULL DEFAULT 1,
            bundle_total_sessions INTEGER NOT NULL DEFAULT 1,
            youtube_consent INTEGER NOT NULL DEFAULT 0,
            youtube_consent_timestamp TEXT NOT NULL DEFAULT '',
            youtube_consent_ip TEXT NOT NULL DEFAULT '',
            price_eur INTEGER NOT NULL DEFAULT 70,

            -- Revolut Merchant Order & Payment Tracking
            revolut_order_id TEXT NOT NULL DEFAULT '',
            revolut_checkout_url TEXT NOT NULL DEFAULT '',
            paid_at TEXT NOT NULL DEFAULT '',
            payment_amount_minor TEXT NOT NULL DEFAULT 0,

            -- Meeting & Email Threading Fields
            google_event_id TEXT,
            google_meet_url TEXT,
            email_message_id TEXT,
            email_subject TEXT,

            -- Live Editor & Future v2 Workspace Fields
            interview_mode TEXT NOT NULL DEFAULT 'classic_dsa',
            syntax_highlighting_enabled INTEGER NOT NULL DEFAULT 0,
            editor_language TEXT NOT NULL DEFAULT 'python',
            workspace_files TEXT NOT NULL,
            active_filename TEXT NOT NULL DEFAULT 'solution.txt',
            ai_chat_logs TEXT NOT NULL DEFAULT '[]',

            -- Interviewer Breakdown Rubric & Notes
            notes_clarification TEXT NOT NULL DEFAULT '',
            score_clarification INTEGER,
            notes_approach_complexity TEXT NOT NULL DEFAULT '',
            score_approach_complexity INTEGER,
            notes_code_quality TEXT NOT NULL DEFAULT '',
            score_code_quality INTEGER,
            notes_verification TEXT NOT NULL DEFAULT '',
            score_verification INTEGER,
            final_verdict TEXT,
            final_recap_and_next_steps TEXT NOT NULL DEFAULT '',
            breakdown_sent_at TEXT,

            FOREIGN KEY (slot_id) REFERENCES availability_slots(id)
        );
        """
    )

    _ensure_schema_columns(cursor)

    cursor.execute("SELECT COUNT(*) as cnt FROM availability_slots;")
    row = cursor.fetchone()
    cnt = row["cnt"] if row else 0

    conn.commit()
    conn.close()

    if cnt == 0:
        seed_rome_availability_schedule(days_ahead=MAX_BOOKING_DAYS_AHEAD, clear_unbooked=False)
    else:
        expire_stale_pending_bookings()


# Optional hook called when a session with a Revolut order expires so the order can be cancelled upstream
_order_cancel_hook: Optional[Callable[[str], None]] = None


def set_order_cancel_hook(hook: Optional[Callable[[str], None]]) -> None:
    global _order_cancel_hook
    _order_cancel_hook = hook


def expire_stale_pending_bookings(hold_minutes: int = PENDING_HOLD_MINUTES) -> int:
    """
    Frees any slot whose session is still in PENDING_PAYMENT after its hold window.
    Marks those expired sessions as CANCELLED (or EXPIRED if desired, kept compatible
    with existing tests expecting CANCELLED) and cancels any open Revolut order.
    """
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()
    legacy_cutoff_iso = (now_utc - timedelta(minutes=hold_minutes)).isoformat()

    conn = get_db_connection()
    cur = conn.cursor()
    _ensure_schema_columns(cur)
    stale_rows = cur.execute(
        """
        SELECT id, slot_id, revolut_order_id, hold_expires_at, created_at
        FROM interview_sessions
        WHERE status = 'PENDING_PAYMENT'
          AND (
              (hold_expires_at != '' AND hold_expires_at <= ? AND created_at <= ?)
              OR (hold_expires_at = '' AND created_at <= ?)
              OR created_at <= ?
          )
        """,
        (now_iso, now_iso, legacy_cutoff_iso, legacy_cutoff_iso),
    ).fetchall()

    orders_to_cancel: List[str] = []
    for r in stale_rows:
        # If hold_expires_at was explicitly extended into the future AND created_at wasn't manually backdated past cutoff, respect hold_expires_at
        hold_exp = r["hold_expires_at"] or ""
        created = r["created_at"] or ""
        if hold_exp > now_iso and created > legacy_cutoff_iso:
            continue
        cur.execute(
            "UPDATE interview_sessions SET status = 'CANCELLED' WHERE id = ? AND status = 'PENDING_PAYMENT'",
            (r["id"],),
        )
        _release_slot_if_unused(cur, int(r["slot_id"]))
        if r["revolut_order_id"]:
            orders_to_cancel.append(r["revolut_order_id"])

    conn.commit()
    conn.close()

    if _order_cancel_hook:
        for oid in orders_to_cancel:
            try:
                _order_cancel_hook(oid)
            except Exception:
                pass

    return len(stale_rows)


def _parse_session_row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    data = dict(row)
    data["syntax_highlighting_enabled"] = bool(data.get("syntax_highlighting_enabled", 0))
    data["youtube_consent"] = bool(data.get("youtube_consent", 0))
    data["package_type"] = data.get("package_type") or "single"
    data["bundle_id"] = data.get("bundle_id") or ""
    data["bundle_session_number"] = int(data.get("bundle_session_number") or 1)
    data["bundle_total_sessions"] = int(
        data.get("bundle_total_sessions")
        or (3 if data["package_type"] == "bundle_3" else 1)
    )
    if not data.get("price_eur"):
        data["price_eur"] = compute_package_price_eur(
            data["package_type"], data["youtube_consent"]
        )
    try:
        data["workspace_files"] = json.loads(data.get("workspace_files") or "{}")
    except Exception:
        data["workspace_files"] = {
            "solution.txt": {"content": "# Write your solution here\n", "language": "python"}
        }
    try:
        data["ai_chat_logs"] = json.loads(data.get("ai_chat_logs") or "[]")
    except Exception:
        data["ai_chat_logs"] = []
    return data


def list_available_slots(max_days_ahead: int = MAX_BOOKING_DAYS_AHEAD) -> List[Dict[str, Any]]:
    expire_stale_pending_bookings()
    conn = get_db_connection()
    now_dt = datetime.now(timezone.utc)
    max_dt = now_dt + timedelta(days=max_days_ahead)
    rows = conn.execute(
        """
        SELECT * FROM availability_slots
        WHERE is_booked = 0
          AND start_time >= ?
          AND start_time <= ?
        ORDER BY start_time ASC
        """,
        (now_dt.isoformat(), max_dt.isoformat()),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_window_slots(max_days_ahead: int = MAX_BOOKING_DAYS_AHEAD) -> List[Dict[str, Any]]:
    """Returns all slots (both free and booked) within the booking window to compute >50% availability."""
    expire_stale_pending_bookings()
    conn = get_db_connection()
    now_dt = datetime.now(timezone.utc)
    max_dt = now_dt + timedelta(days=max_days_ahead)
    rows = conn.execute(
        """
        SELECT * FROM availability_slots
        WHERE start_time >= ?
          AND start_time <= ?
        ORDER BY start_time ASC
        """,
        (now_dt.isoformat(), max_dt.isoformat()),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_all_slots() -> List[Dict[str, Any]]:
    expire_stale_pending_bookings()
    conn = get_db_connection()
    rows = conn.execute(
        """
        SELECT s.*, i.id as session_id, i.candidate_name, i.status as session_status
        FROM availability_slots s
        LEFT JOIN interview_sessions i
          ON i.slot_id = s.id AND i.status IN ('PENDING_PAYMENT', 'CONFIRMED')
        ORDER BY s.start_time ASC
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_slot(start_time_iso: str, end_time_iso: str) -> int:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO availability_slots (start_time, end_time, is_booked) VALUES (?, ?, 0)",
        (start_time_iso, end_time_iso),
    )
    slot_id = cur.lastrowid
    conn.commit()
    conn.close()
    return int(slot_id)


def create_bulk_slots(
    weekdays: List[int],
    hours_rome: List[int],
    days_ahead: int = MAX_BOOKING_DAYS_AHEAD,
    duration_minutes: int = 60,
) -> int:
    days_ahead = min(max(1, days_ahead), MAX_BOOKING_DAYS_AHEAD)
    conn = get_db_connection()
    cur = conn.cursor()
    now_rome = datetime.now(ROME_TZ).replace(minute=0, second=0, microsecond=0)
    added = 0

    existing_starts = {
        r["start_time"]
        for r in conn.execute("SELECT start_time FROM availability_slots").fetchall()
    }

    for day_offset in range(1, days_ahead + 1):
        day_rome = now_rome + timedelta(days=day_offset)
        if day_rome.weekday() in weekdays:
            for hr in hours_rome:
                slot_start_rome = day_rome.replace(hour=hr, minute=0, second=0, microsecond=0)
                slot_start_utc = slot_start_rome.astimezone(timezone.utc)
                start_iso = slot_start_utc.isoformat()
                if start_iso not in existing_starts:
                    slot_end_utc = slot_start_utc + timedelta(minutes=duration_minutes)
                    cur.execute(
                        "INSERT INTO availability_slots (start_time, end_time, is_booked) VALUES (?, ?, 0)",
                        (start_iso, slot_end_utc.isoformat()),
                    )
                    existing_starts.add(start_iso)
                    added += 1

    conn.commit()
    conn.close()
    return added


def delete_slot(slot_id: int) -> bool:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM availability_slots WHERE id = ? AND is_booked = 0", (slot_id,))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def get_slot(slot_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM availability_slots WHERE id = ?", (slot_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_interview_session(
    slot_id: int,
    candidate_name: str,
    candidate_email: str,
    leetcode_solved: int,
    target_company: str,
    preferred_language: str,
    target_role: str,
    resume_path_or_url: str,
    client_ip: str = "",
    package_type: str = "single",
    youtube_consent: bool = False,
) -> Dict[str, Any]:
    expire_stale_pending_bookings()
    session_id = str(uuid.uuid4())
    interviewer_secret = secrets.token_urlsafe(24)
    now_utc = datetime.now(timezone.utc)
    created_at = now_utc.isoformat()
    hold_expires_at = (now_utc + timedelta(minutes=PENDING_HOLD_MINUTES)).isoformat()
    normalized_email = candidate_email.strip().lower()
    pkg = "bundle_3" if package_type == "bundle_3" else "single"
    total_sessions = 3 if pkg == "bundle_3" else 1
    yt_int = 1 if youtube_consent else 0
    yt_ts = created_at if youtube_consent else ""
    yt_ip = client_ip if youtube_consent else ""
    price_eur = compute_package_price_eur(pkg, youtube_consent)

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema_columns(cur)
        conn.commit()

        conn.execute("BEGIN IMMEDIATE")

        # Anti-Spam Rule 1: If this email already has an unpaid PENDING_PAYMENT hold,
        # automatically release their previous held slot so 1 email can never freeze >1 slot.
        prev_email_holds = cur.execute(
            """
            SELECT id, slot_id FROM interview_sessions
            WHERE LOWER(candidate_email) = ? AND status = 'PENDING_PAYMENT'
            """,
            (normalized_email,),
        ).fetchall()
        for old_hold in prev_email_holds:
            cur.execute(
                "UPDATE interview_sessions SET status = 'CANCELLED' WHERE id = ? AND status = 'PENDING_PAYMENT'",
                (old_hold["id"],),
            )
            _release_slot_if_unused(cur, int(old_hold["slot_id"]))

        # Anti-Spam Rule 2: Cap concurrent PENDING_PAYMENT holds per IP address (even with fake emails)
        if client_ip:
            ip_holds_row = cur.execute(
                """
                SELECT COUNT(*) as cnt FROM interview_sessions
                WHERE client_ip = ? AND status = 'PENDING_PAYMENT'
                """,
                (client_ip,),
            ).fetchone()
            if ip_holds_row and ip_holds_row["cnt"] >= MAX_PENDING_PER_IP:
                raise ValueError(
                    "You already have pending unpaid bookings from your network. Please complete payment on your existing booking first."
                )

        starter_comments = {
            "python": f"# Mock Interview Session — {candidate_name}\n# Target Role: {target_role} | Target Company: {target_company}\n\ndef solve():\n    pass\n",
            "cpp": f"// Mock Interview Session — {candidate_name}\n// Target Role: {target_role} | Target Company: {target_company}\n\n#include <iostream>\n#include <vector>\nusing namespace std;\n\nint main() {{\n    return 0;\n}}\n",
            "java": f"// Mock Interview Session — {candidate_name}\n// Target Role: {target_role} | Target Company: {target_company}\n\nimport java.util.*;\n\npublic class Solution {{\n    public static void main(String[] args) {{\n        \n    }}\n}}\n",
            "javascript": f"// Mock Interview Session — {candidate_name}\n// Target Role: {target_role} | Target Company: {target_company}\n\nfunction solve() {{\n    \n}}\n",
            "go": f"// Mock Interview Session — {candidate_name}\n// Target Role: {target_role} | Target Company: {target_company}\n\npackage main\n\nfunc main() {{\n    \n}}\n",
        }
        initial_code = starter_comments.get(
            preferred_language,
            f"// Mock Interview Session — {candidate_name}\n\n",
        )

        workspace_files = {
            "solution.txt": {
                "content": initial_code,
                "language": preferred_language,
            }
        }

        cur.execute(
            "UPDATE availability_slots SET is_booked = 1 WHERE id = ? AND is_booked = 0",
            (slot_id,),
        )
        if cur.rowcount == 0:
            raise ValueError("Selected time slot is no longer available.")

        cur.execute(
            """
            INSERT INTO interview_sessions (
                id, interviewer_secret, slot_id, status, created_at, hold_expires_at,
                candidate_name, candidate_email, client_ip, leetcode_solved, target_company,
                preferred_language, target_role, resume_path_or_url,
                package_type, bundle_id, bundle_session_number, bundle_total_sessions,
                youtube_consent, youtube_consent_timestamp, youtube_consent_ip, price_eur,
                interview_mode, syntax_highlighting_enabled, editor_language,
                workspace_files, active_filename, ai_chat_logs
            ) VALUES (?, ?, ?, 'PENDING_PAYMENT', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 1, ?, ?, ?, ?, ?, 'classic_dsa', 0, ?, ?, 'solution.txt', '[]')
            """,
            (
                session_id,
                interviewer_secret,
                slot_id,
                created_at,
                hold_expires_at,
                candidate_name,
                candidate_email,
                client_ip,
                leetcode_solved,
                target_company,
                preferred_language,
                target_role,
                resume_path_or_url,
                pkg,
                total_sessions,
                yt_int,
                yt_ts,
                yt_ip,
                price_eur,
                preferred_language,
                json.dumps(workspace_files),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_session_with_slot(session_id)


def _attach_bundle_metadata(conn: sqlite3.Connection, item: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not item:
        return None
    bundle_id = item.get("bundle_id") or ""
    if bundle_id:
        try:
            b_row = conn.execute(
                "SELECT bundle_code, access_token, credits_remaining, total_sessions, revoked FROM booking_bundles WHERE id = ?",
                (bundle_id,),
            ).fetchone()
            if b_row:
                item["bundle_code"] = b_row["bundle_code"]
                item["bundle_access_token"] = b_row["access_token"]
                item["bundle_revoked"] = bool(b_row["revoked"])
                item["bundle_credits_remaining"] = int(b_row["credits_remaining"])
                item["bundle_total_sessions"] = int(b_row["total_sessions"])
        except Exception:
            pass
    return item


def get_session_with_slot(session_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    _ensure_schema_columns(conn.cursor())
    row = conn.execute(
        """
        SELECT i.*, s.start_time, s.end_time
        FROM interview_sessions i
        JOIN availability_slots s ON s.id = i.slot_id
        WHERE i.id = ?
        """,
        (session_id,),
    ).fetchone()
    parsed = _parse_session_row(row)
    parsed = _attach_bundle_metadata(conn, parsed)
    conn.close()
    return parsed


def get_session_by_revolut_order_id(order_id: str) -> Optional[Dict[str, Any]]:
    if not order_id:
        return None
    conn = get_db_connection()
    _ensure_schema_columns(conn.cursor())
    row = conn.execute(
        """
        SELECT i.*, s.start_time, s.end_time
        FROM interview_sessions i
        JOIN availability_slots s ON s.id = i.slot_id
        WHERE i.revolut_order_id = ?
        """,
        (order_id,),
    ).fetchone()
    parsed = _parse_session_row(row)
    parsed = _attach_bundle_metadata(conn, parsed)
    conn.close()
    return parsed


def list_sessions_by_category() -> Dict[str, List[Dict[str, Any]]]:
    expire_stale_pending_bookings()
    conn = get_db_connection()
    _ensure_schema_columns(conn.cursor())
    rows = conn.execute(
        """
        SELECT i.*, s.start_time, s.end_time
        FROM interview_sessions i
        JOIN availability_slots s ON s.id = i.slot_id
        ORDER BY s.start_time ASC
        """
    ).fetchall()

    now_iso = datetime.now(timezone.utc).isoformat()
    pending: List[Dict[str, Any]] = []
    upcoming: List[Dict[str, Any]] = []
    past: List[Dict[str, Any]] = []

    for r in rows:
        item = _parse_session_row(r)
        item = _attach_bundle_metadata(conn, item)
        if not item:
            continue
        status = item["status"]
        if status in ("PENDING_PAYMENT", "RESCHEDULE_REQUESTED", "PAID_NEEDS_RESCHEDULE"):
            pending.append(item)
        elif status == "CONFIRMED" and item["end_time"] >= now_iso:
            upcoming.append(item)
        elif status in ("COMPLETED", "CONFIRMED"):
            past.append(item)

    conn.close()
    past.sort(key=lambda x: x["start_time"], reverse=True)
    return {
        "pending": pending,
        "upcoming": upcoming,
        "past": past,
    }


def update_session_checkout_selection(
    session_id: str,
    package_type: str,
    youtube_consent: bool,
    client_ip: str = "",
    revolut_order_id: str = "",
    revolut_checkout_url: str = "",
) -> Optional[Dict[str, Any]]:
    pkg = "bundle_3" if package_type == "bundle_3" else "single"
    total_sessions = 3 if pkg == "bundle_3" else 1
    yt_int = 1 if youtube_consent else 0
    now_utc = datetime.now(timezone.utc)
    yt_ts = now_utc.isoformat() if youtube_consent else ""
    yt_ip = client_ip if youtube_consent else ""
    price_eur = compute_package_price_eur(pkg, youtube_consent)
    new_hold_expires_at = (now_utc + timedelta(minutes=PENDING_HOLD_MINUTES)).isoformat()

    conn = get_db_connection()
    _ensure_schema_columns(conn.cursor())
    conn.execute(
        """
        UPDATE interview_sessions
        SET package_type = ?,
            bundle_total_sessions = ?,
            youtube_consent = ?,
            youtube_consent_timestamp = ?,
            youtube_consent_ip = ?,
            price_eur = ?,
            hold_expires_at = ?,
            revolut_order_id = CASE WHEN ? != '' THEN ? ELSE revolut_order_id END,
            revolut_checkout_url = CASE WHEN ? != '' THEN ? ELSE revolut_checkout_url END
        WHERE id = ? AND status = 'PENDING_PAYMENT'
        """,
        (
            pkg,
            total_sessions,
            yt_int,
            yt_ts,
            yt_ip,
            price_eur,
            new_hold_expires_at,
            revolut_order_id,
            revolut_order_id,
            revolut_checkout_url,
            revolut_checkout_url,
            session_id,
        ),
    )
    conn.commit()
    conn.close()
    return get_session_with_slot(session_id)


def confirm_paid_session(
    session_id: str,
    amount_minor: Optional[int] = None,
    revolut_order_id: str = "",
) -> Tuple[Optional[Dict[str, Any]], bool, bool]:
    """
    Atomically transitions a session to CONFIRMED when a verified Revolut payment arrives
    (or admin approves).
    Returns `(session, newly_confirmed, slot_conflict)`:
    - If already CONFIRMED/COMPLETED, returns `(session, False, False)` (idempotent).
    - If PENDING_PAYMENT, marks CONFIRMED and creates bundle if `bundle_3`.
    - If CANCELLED/EXPIRED (late webhook after hold timeout):
      - If the original slot is still free, re-reserves it and marks CONFIRMED.
      - If another candidate already took the slot, marks `PAID_NEEDS_RESCHEDULE`
        and returns `(session, False, True)` so the candidate/admin can be notified.
    """
    conn = get_db_connection()
    newly_confirmed = False
    slot_conflict = False
    try:
        cur = conn.cursor()
        _ensure_schema_columns(cur)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")

        row = cur.execute(
            "SELECT * FROM interview_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not row:
            conn.rollback()
            return None, False, False

        status = row["status"]
        slot_id = int(row["slot_id"])
        expected_minor = int(row["price_eur"] or 70) * 100
        if amount_minor is not None and amount_minor < expected_minor:
            conn.rollback()
            raise ValueError(
                f"Paid amount ({amount_minor}) is less than expected ({expected_minor})."
            )

        now_iso = datetime.now(timezone.utc).isoformat()
        paid_minor = amount_minor if amount_minor is not None else expected_minor

        if status in ("CONFIRMED", "COMPLETED"):
            conn.rollback()
            return get_session_with_slot(session_id), False, False

        if status == "PENDING_PAYMENT":
            cur.execute("UPDATE availability_slots SET is_booked = 1 WHERE id = ?", (slot_id,))
            _transition_session_status(cur, session_id, {"PENDING_PAYMENT"}, "CONFIRMED")
            cur.execute(
                """
                UPDATE interview_sessions
                SET paid_at = ?, payment_amount_minor = ?,
                    revolut_order_id = CASE WHEN ? != '' THEN ? ELSE revolut_order_id END
                WHERE id = ?
                """,
                (now_iso, paid_minor, revolut_order_id, revolut_order_id, session_id),
            )
            newly_confirmed = True
        elif status in ("CANCELLED", "EXPIRED"):
            # Late payment after hold expired: check if slot is still free
            cur.execute(
                "UPDATE availability_slots SET is_booked = 1 WHERE id = ? AND is_booked = 0",
                (slot_id,),
            )
            if cur.rowcount == 1:
                _transition_session_status(
                    cur, session_id, {"CANCELLED", "EXPIRED"}, "CONFIRMED"
                )
                cur.execute(
                    """
                    UPDATE interview_sessions
                    SET paid_at = ?, payment_amount_minor = ?,
                        revolut_order_id = CASE WHEN ? != '' THEN ? ELSE revolut_order_id END
                    WHERE id = ?
                    """,
                    (now_iso, paid_minor, revolut_order_id, revolut_order_id, session_id),
                )
                newly_confirmed = True
            else:
                _transition_session_status(
                    cur, session_id, {"CANCELLED", "EXPIRED"}, "PAID_NEEDS_RESCHEDULE"
                )
                cur.execute(
                    """
                    UPDATE interview_sessions
                    SET paid_at = ?, payment_amount_minor = ?,
                        revolut_order_id = CASE WHEN ? != '' THEN ? ELSE revolut_order_id END
                    WHERE id = ?
                    """,
                    (now_iso, paid_minor, revolut_order_id, revolut_order_id, session_id),
                )
                slot_conflict = True
        else:
            conn.rollback()
            return get_session_with_slot(session_id), False, False

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    session = get_session_with_slot(session_id)
    if newly_confirmed and session and session.get("package_type") == "bundle_3":
        session = ensure_bundle_for_paid_session(session_id) or session
    return session, newly_confirmed, slot_conflict


def ensure_bundle_for_paid_session(session_id: str) -> Optional[Dict[str, Any]]:
    """
    When a 3-Mock Bundle session (#1) is paid, creates the `booking_bundles` record
    with `credits_remaining = 2` (since Session #1 uses the first of 3 sessions),
    generates a long-lived revocable `access_token`, and links Session #1 to `bundle_id`.
    """
    session = get_session_with_slot(session_id)
    if not session or session.get("package_type") != "bundle_3":
        return session
    if session.get("bundle_id"):
        return get_session_with_slot(session_id)

    bundle_id = str(uuid.uuid4())
    bundle_code = "SH-" + secrets.token_hex(4).upper()
    raw_token, token_hash = generate_bundle_token()
    now_iso = datetime.now(timezone.utc).isoformat()

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema_columns(cur)
        cur.execute(
            """
            INSERT INTO booking_bundles (
                id, bundle_code, access_token_hash, access_token, revoked,
                candidate_name, candidate_email,
                leetcode_solved, target_company, preferred_language, target_role,
                total_sessions, credits_remaining, youtube_consent,
                youtube_consent_timestamp, youtube_consent_ip, price_eur,
                email_message_id, email_subject, created_at
            ) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, 3, 2, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bundle_id,
                bundle_code,
                token_hash,
                raw_token,
                session["candidate_name"],
                session["candidate_email"],
                session["leetcode_solved"],
                session["target_company"],
                session["preferred_language"],
                session["target_role"],
                1 if session.get("youtube_consent") else 0,
                session.get("youtube_consent_timestamp") or "",
                session.get("youtube_consent_ip") or "",
                int(session.get("price_eur") or settings.PRICE_BUNDLE3_STANDARD_EUR),
                session.get("email_message_id"),
                session.get("email_subject"),
                now_iso,
            ),
        )
        cur.execute(
            """
            UPDATE interview_sessions
            SET bundle_id = ?, bundle_session_number = 1, bundle_total_sessions = 3
            WHERE id = ?
            """,
            (bundle_id, session_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_session_with_slot(session_id)


def get_bundle(bundle_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    _ensure_schema_columns(conn.cursor())
    row = conn.execute("SELECT * FROM booking_bundles WHERE id = ?", (bundle_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["youtube_consent"] = bool(d.get("youtube_consent", 0))
    d["revoked"] = bool(d.get("revoked", 0))
    return d


def get_bundle_by_token(access_token: str) -> Optional[Dict[str, Any]]:
    tok = (access_token or "").strip()
    if not tok:
        return None
    tok_hash = hash_token(tok)
    conn = get_db_connection()
    _ensure_schema_columns(conn.cursor())
    row = conn.execute(
        "SELECT * FROM booking_bundles WHERE access_token_hash = ? AND revoked = 0",
        (tok_hash,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["youtube_consent"] = bool(d.get("youtube_consent", 0))
    d["revoked"] = bool(d.get("revoked", 0))
    return d


def rotate_bundle_token(bundle_id: str, revoke: bool = False) -> Optional[Dict[str, Any]]:
    raw_tok, tok_hash = generate_bundle_token()
    conn = get_db_connection()
    _ensure_schema_columns(conn.cursor())
    conn.execute(
        """
        UPDATE booking_bundles
        SET access_token = ?, access_token_hash = ?, revoked = ?
        WHERE id = ?
        """,
        (raw_tok, tok_hash, 1 if revoke else 0, bundle_id),
    )
    conn.commit()
    conn.close()
    return get_bundle(bundle_id)


def find_active_bundles_by_email(email_str: str) -> List[Dict[str, Any]]:
    """
    Looks up active, unrevoked bundles with remaining credits by candidate email ONLY.
    Never matches by bundle_code or raw database id.
    """
    q = (email_str or "").strip().lower()
    if not q or "@" not in q:
        return []
    conn = get_db_connection()
    _ensure_schema_columns(conn.cursor())
    rows = conn.execute(
        """
        SELECT * FROM booking_bundles
        WHERE credits_remaining > 0
          AND revoked = 0
          AND LOWER(candidate_email) = ?
        ORDER BY created_at DESC
        """,
        (q,),
    ).fetchall()
    conn.close()
    results = []
    for r in rows:
        d = dict(r)
        d["youtube_consent"] = bool(d.get("youtube_consent", 0))
        d["revoked"] = bool(d.get("revoked", 0))
        results.append(d)
    return results


def find_active_bundles_by_email_or_code(query_str: str) -> List[Dict[str, Any]]:
    """Backwards-compatible alias restricted to email lookup."""
    return find_active_bundles_by_email(query_str)


def book_bundle_credit_session(
    bundle_id: str,
    slot_id: int,
    candidate_name: str,
    target_company: str,
    target_role: str,
    preferred_language: str,
    leetcode_solved: int,
    client_ip: str = "",
) -> Dict[str, Any]:
    """
    Uses 1 remaining credit from an existing paid 3-Mock Bundle to immediately
    create and confirm Session #2 or Session #3 without requiring another payment.
    """
    expire_stale_pending_bookings()
    session_id = str(uuid.uuid4())
    interviewer_secret = secrets.token_urlsafe(24)
    created_at = datetime.now(timezone.utc).isoformat()

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema_columns(cur)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")

        b_row = cur.execute(
            "SELECT * FROM booking_bundles WHERE id = ? AND revoked = 0", (bundle_id,)
        ).fetchone()
        if not b_row:
            raise ValueError("Bundle not found or link has been revoked.")
        bundle = dict(b_row)
        credits_left = int(bundle["credits_remaining"])
        if credits_left <= 0:
            raise ValueError("All 3 sessions in this bundle have already been booked.")

        total_sessions = int(bundle.get("total_sessions") or 3)
        session_number = total_sessions - credits_left + 1

        cur.execute(
            "UPDATE availability_slots SET is_booked = 1 WHERE id = ? AND is_booked = 0",
            (slot_id,),
        )
        if cur.rowcount == 0:
            raise ValueError("Selected time slot is no longer available.")

        cur.execute(
            "UPDATE booking_bundles SET credits_remaining = credits_remaining - 1 WHERE id = ? AND credits_remaining > 0 AND revoked = 0",
            (bundle_id,),
        )
        if cur.rowcount == 0:
            raise ValueError("No remaining credits in this bundle.")

        starter_comments = {
            "python": f"# Mock Interview Session #{session_number} — {candidate_name}\n# Target Role: {target_role} | Target Company: {target_company}\n\ndef solve():\n    pass\n",
            "cpp": f"// Mock Interview Session #{session_number} — {candidate_name}\n// Target Role: {target_role} | Target Company: {target_company}\n\n#include <iostream>\n#include <vector>\nusing namespace std;\n\nint main() {{\n    return 0;\n}}\n",
            "java": f"// Mock Interview Session #{session_number} — {candidate_name}\n// Target Role: {target_role} | Target Company: {target_company}\n\nimport java.util.*;\n\npublic class Solution {{\n    public static void main(String[] args) {{\n        \n    }}\n}}\n",
            "javascript": f"// Mock Interview Session #{session_number} — {candidate_name}\n// Target Role: {target_role} | Target Company: {target_company}\n\nfunction solve() {{\n    \n}}\n",
            "go": f"// Mock Interview Session #{session_number} — {candidate_name}\n// Target Role: {target_role} | Target Company: {target_company}\n\npackage main\n\nfunc main() {{\n    \n}}\n",
        }
        initial_code = starter_comments.get(
            preferred_language,
            f"// Mock Interview Session #{session_number} — {candidate_name}\n\n",
        )
        workspace_files = {
            "solution.txt": {
                "content": initial_code,
                "language": preferred_language,
            }
        }

        cur.execute(
            """
            INSERT INTO interview_sessions (
                id, interviewer_secret, slot_id, status, created_at,
                candidate_name, candidate_email, client_ip, leetcode_solved, target_company,
                preferred_language, target_role, resume_path_or_url,
                package_type, bundle_id, bundle_session_number, bundle_total_sessions,
                youtube_consent, youtube_consent_timestamp, youtube_consent_ip, price_eur,
                email_message_id, email_subject,
                interview_mode, syntax_highlighting_enabled, editor_language,
                workspace_files, active_filename, ai_chat_logs
            ) VALUES (?, ?, ?, 'CONFIRMED', ?, ?, ?, ?, ?, ?, ?, ?, '', 'bundle_3', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'classic_dsa', 0, ?, ?, 'solution.txt', '[]')
            """,
            (
                session_id,
                interviewer_secret,
                slot_id,
                created_at,
                candidate_name,
                bundle["candidate_email"],
                client_ip,
                leetcode_solved,
                target_company,
                preferred_language,
                target_role,
                bundle_id,
                session_number,
                total_sessions,
                int(bundle.get("youtube_consent") or 0),
                bundle.get("youtube_consent_timestamp") or "",
                bundle.get("youtube_consent_ip") or "",
                int(bundle.get("price_eur") or settings.PRICE_BUNDLE3_STANDARD_EUR),
                bundle.get("email_message_id"),
                bundle.get("email_subject"),
                preferred_language,
                json.dumps(workspace_files),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_session_with_slot(session_id)


def update_session_approval(
    session_id: str,
    google_event_id: Optional[str],
    google_meet_url: str,
    email_message_id: str,
    email_subject: str,
) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    _ensure_schema_columns(conn.cursor())
    conn.execute(
        """
        UPDATE interview_sessions
        SET status = CASE WHEN status IN ('PENDING_PAYMENT', 'CONFIRMED') THEN 'CONFIRMED' ELSE status END,
            google_event_id = ?,
            google_meet_url = ?,
            email_message_id = ?,
            email_subject = ?
        WHERE id = ?
        """,
        (google_event_id, google_meet_url, email_message_id, email_subject, session_id),
    )
    row = conn.execute(
        "SELECT bundle_id FROM interview_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row and row["bundle_id"]:
        conn.execute(
            """
            UPDATE booking_bundles
            SET email_message_id = COALESCE(email_message_id, ?),
                email_subject = COALESCE(email_subject, ?)
            WHERE id = ?
            """,
            (email_message_id, email_subject, row["bundle_id"]),
        )
    conn.commit()
    conn.close()
    return get_session_with_slot(session_id)


def cancel_or_reject_session(session_id: str) -> bool:
    """
    Atomically cancels an active session and frees its slot ONLY if no other active
    session is using that slot. Calling cancel a second time is a safe no-op.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema_columns(cur)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")

        row = cur.execute(
            "SELECT * FROM interview_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not row:
            conn.rollback()
            return False

        prev_status = row["status"]
        if prev_status in ("CANCELLED", "EXPIRED", "COMPLETED"):
            conn.rollback()
            return False

        slot_id = int(row["slot_id"])
        _transition_session_status(
            cur,
            session_id,
            {"PENDING_PAYMENT", "CONFIRMED", "RESCHEDULE_REQUESTED", "PAID_NEEDS_RESCHEDULE"},
            "CANCELLED",
        )
        _release_slot_if_unused(cur, slot_id)

        if (
            row["bundle_id"]
            and prev_status in ("CONFIRMED", "RESCHEDULE_REQUESTED")
            and int(row["bundle_session_number"] or 1) > 1
        ):
            cur.execute(
                "UPDATE booking_bundles SET credits_remaining = MIN(total_sessions, credits_remaining + 1) WHERE id = ?",
                (row["bundle_id"],),
            )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_session_reschedule_requested(session_id: str) -> Optional[Dict[str, Any]]:
    """
    Transitions a session to `RESCHEDULE_REQUESTED` and frees the currently booked slot
    once, while keeping the session record ready for the candidate to pick a new slot.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema_columns(cur)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")

        row = cur.execute(
            "SELECT * FROM interview_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not row:
            conn.rollback()
            return None

        status = row["status"]
        if status not in ("CONFIRMED", "PENDING_PAYMENT", "RESCHEDULE_REQUESTED", "PAID_NEEDS_RESCHEDULE"):
            conn.rollback()
            raise ValueError(f"Cannot request reschedule for session in status '{status}'.")

        slot_id = int(row["slot_id"])
        if status in ("CONFIRMED", "PENDING_PAYMENT"):
            _transition_session_status(
                cur, session_id, {"CONFIRMED", "PENDING_PAYMENT"}, "RESCHEDULE_REQUESTED"
            )
            _release_slot_if_unused(cur, slot_id)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_session_with_slot(session_id)


def reschedule_interview_session(session_id: str, new_slot_id: int) -> Dict[str, Any]:
    """
    Moves a session in `RESCHEDULE_REQUESTED` or `PAID_NEEDS_RESCHEDULE` to a newly
    selected available slot and transitions it to `CONFIRMED`.
    Does NOT blindly unbook `old_slot_id` (which was already released when reschedule
    was requested and may now belong to another candidate).
    """
    expire_stale_pending_bookings()
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    max_iso = (now_dt + timedelta(days=MAX_BOOKING_DAYS_AHEAD)).isoformat()

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _ensure_schema_columns(cur)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")

        row = cur.execute(
            "SELECT * FROM interview_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not row:
            raise ValueError("Interview session not found.")

        status = row["status"]
        if status not in ("RESCHEDULE_REQUESTED", "PAID_NEEDS_RESCHEDULE"):
            raise ValueError(
                "Rescheduling is only available when requested by the interviewer."
            )

        old_slot_id = int(row["slot_id"])

        slot_row = cur.execute(
            """
            SELECT * FROM availability_slots
            WHERE id = ? AND is_booked = 0 AND start_time >= ? AND start_time <= ?
            """,
            (new_slot_id, now_iso, max_iso),
        ).fetchone()
        if not slot_row:
            raise ValueError("Selected time slot is no longer available.")

        cur.execute(
            "UPDATE availability_slots SET is_booked = 1 WHERE id = ? AND is_booked = 0",
            (new_slot_id,),
        )
        if cur.rowcount == 0:
            raise ValueError("Selected time slot is no longer available.")

        cur.execute(
            "UPDATE interview_sessions SET slot_id = ? WHERE id = ?",
            (new_slot_id, session_id),
        )
        _transition_session_status(
            cur,
            session_id,
            {"RESCHEDULE_REQUESTED", "PAID_NEEDS_RESCHEDULE"},
            "CONFIRMED",
        )
        if old_slot_id != new_slot_id:
            _release_slot_if_unused(cur, old_slot_id)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_session_with_slot(session_id)


def update_workspace_state(
    session_id: str,
    workspace_files: Dict[str, Any],
    active_filename: str,
    syntax_highlighting_enabled: bool,
    editor_language: str,
    interview_mode: str = "classic_dsa",
) -> None:
    conn = get_db_connection()
    conn.execute(
        """
        UPDATE interview_sessions
        SET workspace_files = ?,
            active_filename = ?,
            syntax_highlighting_enabled = ?,
            editor_language = ?,
            interview_mode = ?
        WHERE id = ?
        """,
        (
            json.dumps(workspace_files),
            active_filename,
            1 if syntax_highlighting_enabled else 0,
            editor_language,
            interview_mode,
            session_id,
        ),
    )
    conn.commit()
    conn.close()


def append_ai_chat_log(session_id: str, entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    session = get_session_with_slot(session_id)
    if not session:
        return []
    logs = session.get("ai_chat_logs") or []
    logs.append(entry)
    conn = get_db_connection()
    conn.execute(
        "UPDATE interview_sessions SET ai_chat_logs = ? WHERE id = ?",
        (json.dumps(logs), session_id),
    )
    conn.commit()
    conn.close()
    return logs


def update_rubric_notes(session_id: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    conn.execute(
        """
        UPDATE interview_sessions
        SET notes_clarification = ?,
            score_clarification = ?,
            notes_approach_complexity = ?,
            score_approach_complexity = ?,
            notes_code_quality = ?,
            score_code_quality = ?,
            notes_verification = ?,
            score_verification = ?,
            final_verdict = ?,
            final_recap_and_next_steps = ?
        WHERE id = ?
        """,
        (
            payload.get("notes_clarification", ""),
            payload.get("score_clarification"),
            payload.get("notes_approach_complexity", ""),
            payload.get("score_approach_complexity"),
            payload.get("notes_code_quality", ""),
            payload.get("score_code_quality"),
            payload.get("notes_verification", ""),
            payload.get("score_verification"),
            payload.get("final_verdict") or None,
            payload.get("final_recap_and_next_steps", ""),
            session_id,
        ),
    )
    conn.commit()
    conn.close()
    return get_session_with_slot(session_id)


def mark_session_completed(session_id: str, breakdown_sent: bool = False) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    if breakdown_sent:
        sent_at = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE interview_sessions SET status = 'COMPLETED', breakdown_sent_at = ? WHERE id = ?",
            (sent_at, session_id),
        )
    else:
        conn.execute(
            "UPDATE interview_sessions SET status = 'COMPLETED' WHERE id = ?",
            (session_id,),
        )
    conn.commit()
    conn.close()
    return get_session_with_slot(session_id)
