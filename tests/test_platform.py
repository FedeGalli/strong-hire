import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import (
    cancel_or_reject_session,
    expire_stale_pending_bookings,
    get_bundle,
    get_db_connection,
    get_session_with_slot,
    get_slot,
    init_db,
    list_available_slots,
    list_window_slots,
    mark_session_reschedule_requested,
)
from app.main import app, reset_rate_limits
from app.services.revolut import build_completed_webhook_request, get_fake_order


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    test_db = tmp_path / "test_interviews.db"
    monkeypatch.setattr(settings, "DATABASE_PATH", test_db)
    reset_rate_limits()
    init_db()
    with TestClient(app) as c:
        yield c


def _complete_revolut_payment_via_webhook(
    client: TestClient,
    session_id: str,
    package_type: str = "single",
    youtube_consent: str = "0",
) -> dict:
    """
    Helper that goes through the real two-step payment flow:
    1. Candidate submits `/booking/{session_id}/checkout` -> creates a Revolut order (stays PENDING_PAYMENT).
    2. Revolut delivers a signed `ORDER_COMPLETED` webhook to `/webhooks/revolut` -> marks CONFIRMED.
    """
    r_checkout = client.post(
        f"/booking/{session_id}/checkout",
        data={"package_type": package_type, "youtube_consent": youtube_consent},
        follow_redirects=False,
    )
    assert r_checkout.status_code == 303
    s_pending = get_session_with_slot(session_id)
    assert s_pending["status"] == "PENDING_PAYMENT"
    assert s_pending["revolut_order_id"] != ""

    raw_bytes, headers = build_completed_webhook_request(
        order_id=s_pending["revolut_order_id"],
        session_id=session_id,
        amount_minor=int(s_pending["price_eur"]) * 100,
    )
    r_wh = client.post("/webhooks/revolut", content=raw_bytes, headers=headers)
    assert r_wh.status_code == 200
    return r_wh.json()


def test_rome_schedule_hours_and_booking_calendar(client: TestClient):
    r_landing = client.get("/")
    assert r_landing.status_code == 200
    assert "StrongHire" in r_landing.text

    r_book = client.get("/book")
    assert r_book.status_code == 200
    assert "isOverHalfFree ? 'bg-emerald-500' : 'bg-amber-400'" in r_book.text

    slots = list_available_slots()
    assert len(slots) > 50

    rome = ZoneInfo("Europe/Rome")
    workday_hours = set()
    weekend_hours = set()
    for s in slots:
        dt_rome = datetime.fromisoformat(s["start_time"]).astimezone(rome)
        if dt_rome.weekday() < 5:
            workday_hours.add(dt_rome.hour)
        else:
            weekend_hours.add(dt_rome.hour)

    assert sorted(workday_hours) == [17, 18, 19, 20, 21]
    assert sorted(weekend_hours) == [9, 10, 11, 14, 15, 16, 17]


def test_admin_visual_availability_manager(client: TestClient):
    r_login = client.post(
        "/admin/login",
        data={"password": settings.ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert r_login.status_code == 303

    r_toggle = client.post(
        "/api/admin/availability/toggle-hour",
        json={"date": "2026-11-20", "hour": 12},
    )
    assert r_toggle.status_code == 200
    assert r_toggle.json()["result"]["action"] == "opened"

    r_block = client.post(
        "/api/admin/availability/set-date-hours",
        json={"date": "2026-11-21", "hours": []},
    )
    assert r_block.status_code == 200
    assert "2026-11-21" not in r_block.json()["slots_by_rome_date"]

    r_weekly = client.post(
        "/api/admin/availability/weekly",
        json={
            "schedule": {
                "0": [17, 18, 19, 20, 21],
                "1": [17, 18, 19, 20, 21],
                "2": [17, 18, 19, 20, 21],
                "3": [17, 18, 19, 20, 21],
                "4": [17, 18, 19, 20, 21],
                "5": [9, 10, 11, 14, 15, 16, 17],
                "6": [9, 10, 11, 14, 15, 16, 17],
            },
            "days_ahead": 60,
            "clear_unbooked": True,
        },
    )
    assert r_weekly.status_code == 200
    assert r_weekly.json()["status"] == "ok"


def test_end_to_end_booking_room_and_threaded_pdf(client: TestClient):
    slots = list_available_slots()
    slot_id = slots[0]["id"]

    # 1. Candidate submits booking
    r_book = client.post(
        "/book",
        data={
            "slot_id": str(slot_id),
            "candidate_name": "Elena Bianchi",
            "candidate_email": "elena@example.com",
            "leetcode_solved": "185",
            "target_company": "Google",
            "target_role": "Mid-Level (L4)",
            "preferred_language": "python",
        },
        follow_redirects=False,
    )
    assert r_book.status_code == 303
    payment_url = r_book.headers["location"]
    session_id = payment_url.split("/booking/")[1].split("/payment")[0]

    window_slots = list_window_slots()
    booked_slot = next(s for s in window_slots if s["id"] == slot_id)
    assert booked_slot["is_booked"] == 1

    # 2. Complete checkout + signed Revolut ORDER_COMPLETED webhook
    _complete_revolut_payment_via_webhook(client, session_id)

    r_ics = client.get(f"/booking/{session_id}/ics")
    assert r_ics.status_code == 200
    assert b"BEGIN:VCALENDAR" in r_ics.content

    session = get_session_with_slot(session_id)
    assert session["status"] == "CONFIRMED"
    assert session["email_message_id"] == f"<interview-{session_id}@{settings.SMTP_DOMAIN}>"

    # 2b. Admin requests a reschedule -> status moves to RESCHEDULE_REQUESTED, old slot is freed,
    #     and candidate picks a new slot via /reschedule/{id}
    client.post("/admin/login", data={"password": settings.ADMIN_PASSWORD})
    new_slot_id = slots[1]["id"]
    r_req_resched = client.post(
        f"/admin/session/{session_id}/request-reschedule", follow_redirects=False
    )
    assert r_req_resched.status_code == 303
    assert get_session_with_slot(session_id)["status"] == "RESCHEDULE_REQUESTED"
    assert slot_id in {s["id"] for s in list_available_slots()}

    r_resched_page = client.get(f"/reschedule/{session_id}")
    assert r_resched_page.status_code == 200
    assert "Reschedule Your Mock Interview" in r_resched_page.text

    r_resched_submit = client.post(
        f"/reschedule/{session_id}",
        data={"slot_id": str(new_slot_id)},
        follow_redirects=False,
    )
    assert r_resched_submit.status_code == 303
    rescheduled_session = get_session_with_slot(session_id)
    assert rescheduled_session["slot_id"] == new_slot_id
    assert rescheduled_session["status"] == "CONFIRMED"
    assert new_slot_id not in {s["id"] for s in list_available_slots()}

    # 3. Real-time WebSocket room sync (authenticated interviewer toggles syntax + candidate updates code)
    interviewer_secret = rescheduled_session["interviewer_secret"]
    with client.websocket_connect(
        f"/ws/room/{session_id}?key={interviewer_secret}"
    ) as ws_int, client.websocket_connect(f"/ws/room/{session_id}") as ws_cand:
        ws_int.send_json(
            {
                "type": "config_update",
                "syntax_highlighting_enabled": True,
                "editor_language": "python",
                "interview_mode": "classic_dsa",
            }
        )
        cfg_msg = ws_cand.receive_json()
        assert cfg_msg["type"] == "config_update"
        assert cfg_msg["syntax_highlighting_enabled"] is True

        ws_cand.send_json(
            {
                "type": "code_update",
                "active_filename": "solution.txt",
                "workspace_files": {
                    "solution.txt": {
                        "content": "def binary_search(arr, x):\n    return 0\n",
                        "language": "python",
                    }
                },
            }
        )
        code_msg = ws_int.receive_json()
        assert code_msg["type"] == "code_update"

    # 4. Save Rubric & Final Verdict:
    #    Before breakdown is sent, unauthenticated candidate gets 404 on /feedback/{id},
    #    while authenticated interviewer can preview it.
    r_rubric = client.post(
        f"/api/session/{session_id}/rubric?key={interviewer_secret}",
        json={
            "notes_clarification": "Clear questions on constraints.",
            "score_clarification": 5,
            "notes_approach_complexity": "Optimal O(log N) approach.",
            "score_approach_complexity": 5,
            "notes_code_quality": "Clean implementation.",
            "score_code_quality": 4,
            "notes_verification": "Dry-ran boundary conditions.",
            "score_verification": 5,
            "final_verdict": "Strong Hire",
            "final_recap_and_next_steps": "Ready for onsite loop.",
        },
    )
    assert r_rubric.status_code == 200

    # Admin preview works before sending
    r_feedback_admin = client.get(f"/feedback/{session_id}")
    assert r_feedback_admin.status_code == 200
    assert "Strong Hire" in r_feedback_admin.text
    assert "19 / 20" in r_feedback_admin.text

    # Send breakdown email -> marks COMPLETED and unlocks /feedback/{id} for the candidate
    r_send = client.post(
        f"/admin/session/{session_id}/send-breakdown", follow_redirects=False
    )
    assert r_send.status_code == 303

    completed_session = get_session_with_slot(session_id)
    assert completed_session["status"] == "COMPLETED"
    assert completed_session["final_verdict"] == "Strong Hire"
    assert completed_session["breakdown_sent_at"] is not None

    # 5. Once feedback is sent, the candidate is redirected from /room/{id} to /feedback/{id}
    #    and can view their published breakdown
    client.get("/admin/logout")
    r_room_candidate = client.get(f"/room/{session_id}", follow_redirects=False)
    assert r_room_candidate.status_code == 303
    assert r_room_candidate.headers["location"] == f"/feedback/{session_id}"

    r_feedback_public = client.get(f"/feedback/{session_id}")
    assert r_feedback_public.status_code == 200
    assert "Strong Hire" in r_feedback_public.text


def test_four_hour_hold_expiry_and_anti_spam(client: TestClient):
    slots = list_available_slots()
    slot_a = slots[0]["id"]
    slot_b = slots[1]["id"]
    slot_c = slots[2]["id"]
    slot_d = slots[3]["id"]

    # 1. Honeypot blocks bot submissions
    r_bot = client.post(
        "/book",
        data={
            "slot_id": str(slot_a),
            "candidate_name": "Spam Bot",
            "candidate_email": "bot@example.com",
            "leetcode_solved": "0",
            "target_company": "Any",
            "target_role": "L3",
            "preferred_language": "python",
            "website_url": "http://spammer.biz",
        },
        follow_redirects=False,
    )
    assert r_bot.status_code == 303
    assert "error=Bot" in r_bot.headers["location"]
    assert slot_a in {s["id"] for s in list_available_slots()}

    # 2. Same email booking a second slot replaces their previous unpaid hold and frees slot_a
    r1 = client.post(
        "/book",
        data={
            "slot_id": str(slot_a),
            "candidate_name": "Marco Rossi",
            "candidate_email": "marco@example.com",
            "leetcode_solved": "100",
            "target_company": "Meta",
            "target_role": "L4",
            "preferred_language": "python",
        },
        follow_redirects=False,
    )
    assert r1.status_code == 303
    session_1 = r1.headers["location"].split("/booking/")[1].split("/payment")[0]

    r2 = client.post(
        "/book",
        data={
            "slot_id": str(slot_b),
            "candidate_name": "Marco Rossi",
            "candidate_email": "marco@example.com",
            "leetcode_solved": "100",
            "target_company": "Meta",
            "target_role": "L4",
            "preferred_language": "python",
        },
        follow_redirects=False,
    )
    assert r2.status_code == 303
    session_2 = r2.headers["location"].split("/booking/")[1].split("/payment")[0]

    assert get_session_with_slot(session_1)["status"] == "CANCELLED"
    free_ids = {s["id"] for s in list_available_slots()}
    assert slot_a in free_ids
    assert slot_b not in free_ids

    # 3. Per-IP unpaid hold cap (max 2 concurrent PENDING_PAYMENT holds per IP across different emails)
    r3 = client.post(
        "/book",
        data={
            "slot_id": str(slot_c),
            "candidate_name": "Fake Two",
            "candidate_email": "fake2@example.com",
            "leetcode_solved": "50",
            "target_company": "Amazon",
            "target_role": "L4",
            "preferred_language": "python",
        },
        follow_redirects=False,
    )
    assert r3.status_code == 303
    session_3 = r3.headers["location"].split("/booking/")[1].split("/payment")[0]

    # Third concurrent unpaid hold from the same IP (even with a spoofed X-Forwarded-For header!) is rejected
    r4_blocked = client.post(
        "/book",
        data={
            "slot_id": str(slot_d),
            "candidate_name": "Fake Three",
            "candidate_email": "fake3@example.com",
            "leetcode_solved": "50",
            "target_company": "Amazon",
            "target_role": "L4",
            "preferred_language": "python",
        },
        headers={"x-forwarded-for": "99.88.77.66"},
        follow_redirects=False,
    )
    assert r4_blocked.status_code == 303
    assert "/book?error=" in r4_blocked.headers["location"]
    assert slot_d in {s["id"] for s in list_available_slots()}

    # 4. Simulate 30m+ passing -> expire_stale_pending_bookings frees stale PENDING_PAYMENT slots
    thirty_five_mins_ago = (datetime.now(timezone.utc) - timedelta(minutes=35)).isoformat()
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE interview_sessions SET created_at = ?, hold_expires_at = ? WHERE id = ?",
            (thirty_five_mins_ago, thirty_five_mins_ago, session_2),
        )
        conn.commit()
    finally:
        conn.close()

    expired_count = expire_stale_pending_bookings()
    assert expired_count == 1
    assert get_session_with_slot(session_2)["status"] == "CANCELLED"
    free_ids_after = {s["id"] for s in list_available_slots()}
    assert slot_b in free_ids_after

    # 5. Admin rejecting a booking sends a rejection email to the candidate and reopens the slot
    client.post("/admin/login", data={"password": settings.ADMIN_PASSWORD})
    r_reject = client.post(f"/admin/session/{session_3}/cancel", follow_redirects=False)
    assert r_reject.status_code == 303
    assert "candidate+notified" in r_reject.headers["location"]
    assert get_session_with_slot(session_3)["status"] == "CANCELLED"
    assert slot_c in {s["id"] for s in list_available_slots()}


def test_three_mock_bundle_and_youtube_discount(client: TestClient):
    slots = list_available_slots()
    slot_1, slot_2, slot_3 = slots[0]["id"], slots[1]["id"], slots[2]["id"]

    # 1. Book Session #1 selecting 3-Mock Bundle
    r_book = client.post(
        "/book",
        data={
            "slot_id": str(slot_1),
            "candidate_name": "Elena Conti",
            "candidate_email": "elena@example.com",
            "leetcode_solved": "200",
            "target_company": "Google",
            "target_role": "L4 (SWE III)",
            "preferred_language": "python",
            "package_type": "bundle_3",
        },
        follow_redirects=False,
    )
    assert r_book.status_code == 303
    s1_id = r_book.headers["location"].split("/booking/")[1].split("/payment")[0]

    r_checkout = client.get(f"/booking/{s1_id}/payment")
    assert r_checkout.status_code == 200
    assert "Recording, YouTube Publication &amp; Monetization Waiver Agreement" in r_checkout.text

    # 2. Complete checkout + signed Revolut webhook with 3-Mock Bundle + YouTube Discount (€152)
    _complete_revolut_payment_via_webhook(
        client, s1_id, package_type="bundle_3", youtube_consent="1"
    )

    s1 = get_session_with_slot(s1_id)
    assert s1["status"] == "CONFIRMED"
    assert s1["package_type"] == "bundle_3"
    assert s1["youtube_consent"] is True
    assert s1["price_eur"] == 152
    assert s1["bundle_id"] != ""
    assert s1["bundle_credits_remaining"] == 2
    assert s1["bundle_code"].startswith("SH-")
    assert len(s1["bundle_access_token"]) >= 32

    bundle_id = s1["bundle_id"]
    access_token = s1["bundle_access_token"]

    # 3. Lookup bundle by email NEVER redirects to the bundle page (prevents bundle takeover);
    #    direct `/bundle/{id}` is disabled (404).
    r_lookup = client.post(
        "/book/lookup-bundle",
        data={"query_str": "elena@example.com"},
        follow_redirects=False,
    )
    assert r_lookup.status_code == 303
    assert r_lookup.headers["location"].startswith("/book?bundle_msg=")
    assert client.get(f"/bundle/{bundle_id}").status_code == 404

    # 4. Book Session #2 using the secret long-lived token `/bundle/b/{access_token}`
    r_b2 = client.post(
        f"/bundle/b/{access_token}",
        data={
            "slot_id": str(slot_2),
            "candidate_name": "Elena Conti",
            "leetcode_solved": "215",
            "target_company": "Google",
            "target_role": "L4 (SWE III)",
            "preferred_language": "python",
        },
        follow_redirects=False,
    )
    assert r_b2.status_code == 303
    s2_id = r_b2.headers["location"].split("/booking/")[1].split("/payment")[0]
    s2 = get_session_with_slot(s2_id)
    assert s2["status"] == "CONFIRMED"
    assert s2["bundle_session_number"] == 2
    assert s2["bundle_credits_remaining"] == 1
    assert s2["youtube_consent"] is True

    # 5. Book Session #3 using the final bundle credit
    r_b3 = client.post(
        f"/bundle/b/{access_token}",
        data={
            "slot_id": str(slot_3),
            "candidate_name": "Elena Conti",
            "leetcode_solved": "230",
            "target_company": "Google",
            "target_role": "L4 (SWE III)",
            "preferred_language": "python",
        },
        follow_redirects=False,
    )
    assert r_b3.status_code == 303
    s3_id = r_b3.headers["location"].split("/booking/")[1].split("/payment")[0]
    s3 = get_session_with_slot(s3_id)
    assert s3["status"] == "CONFIRMED"
    assert s3["bundle_session_number"] == 3
    assert s3["bundle_credits_remaining"] == 0


def test_security_regressions_and_slot_integrity(client: TestClient):
    """
    Covers every security & slot-integrity bug found in the audit:
    1. Calling `/mock-pay` or `/checkout` without a signed Revolut webhook NEVER confirms a session.
    2. Webhook signature verification rejects bad signatures, replayed timestamps, and underpayments;
       duplicate `ORDER_COMPLETED` deliveries are idempotent.
    3. Rescheduling a CANCELLED or non-reschedule-requested session is rejected (no leaked slots).
    4. Admin reschedule-request + candidate reschedule NEVER frees a slot subsequently booked by someone else.
    5. Cancelling a session twice NEVER frees a slot subsequently booked by someone else.
    6. Expiring a hold cancels the upstream Revolut order; if a late webhook still arrives after another
       candidate took the slot, the session transitions to `PAID_NEEDS_RESCHEDULE` instead of double-booking.
    7. `/feedback/{id}` returns 404 before breakdown is sent; candidate WebSocket cannot send `config_update`;
       `ADMIN_SECRET` is never leaked in `room.html`.
    """
    slots = list_available_slots()
    s1, s2, s3, s4, s5, s6 = [slots[i]["id"] for i in range(6)]

    def book(slot_id: int, email: str, pkg: str = "single") -> str:
        r = client.post(
            "/book",
            data={
                "slot_id": str(slot_id),
                "candidate_name": "Test User",
                "candidate_email": email,
                "leetcode_solved": "50",
                "target_company": "Stripe",
                "target_role": "Mid-Level (L4)",
                "preferred_language": "python",
                "package_type": pkg,
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        return r.headers["location"].split("/booking/")[1].split("/payment")[0]

    # --- 1. Unauthenticated /mock-pay or ?mock_paid=1 never confirms a booking ---
    sid1 = book(s1, "u1@example.com", pkg="bundle_3")
    client.post(f"/booking/{sid1}/mock-pay", data={"package_type": "bundle_3"}, follow_redirects=False)
    sess1 = get_session_with_slot(sid1)
    assert sess1["status"] == "PENDING_PAYMENT"
    assert sess1["bundle_id"] == ""
    r_pay_page = client.get(f"/booking/{sid1}/payment?mock_paid=1")
    assert "Almost done" in r_pay_page.text
    assert "Booking Confirmed" not in r_pay_page.text

    # --- 2. Webhook rejects bad signature, stale timestamp, and underpayment; idempotent on duplicates ---
    order_id_1 = sess1["revolut_order_id"]
    raw_valid, valid_headers = build_completed_webhook_request(
        order_id=order_id_1, session_id=sid1, amount_minor=19000
    )

    # Bad signature -> 401
    bad_headers = dict(valid_headers, **{"Revolut-Signature": "v1=deadbeef"})
    assert client.post("/webhooks/revolut", content=raw_valid, headers=bad_headers).status_code == 401

    # Stale timestamp (10 minutes ago) -> 401
    old_ts = int((time.time() - 600) * 1000)
    raw_old, old_headers = build_completed_webhook_request(
        order_id=order_id_1, session_id=sid1, amount_minor=19000, timestamp_ms=old_ts
    )
    assert client.post("/webhooks/revolut", content=raw_old, headers=old_headers).status_code == 401

    # Underpayment (€10 instead of €190) -> 400 and still PENDING_PAYMENT
    raw_under, under_headers = build_completed_webhook_request(
        order_id=order_id_1, session_id=sid1, amount_minor=1000
    )
    assert client.post("/webhooks/revolut", content=raw_under, headers=under_headers).status_code == 400
    assert get_session_with_slot(sid1)["status"] == "PENDING_PAYMENT"

    # Valid webhook -> 200 and CONFIRMED; duplicate webhook -> 200 with newly_confirmed=False
    r_ok1 = client.post("/webhooks/revolut", content=raw_valid, headers=valid_headers)
    assert r_ok1.status_code == 200
    assert r_ok1.json()["newly_confirmed"] is True
    assert get_session_with_slot(sid1)["status"] == "CONFIRMED"

    r_dup = client.post("/webhooks/revolut", content=raw_valid, headers=valid_headers)
    assert r_dup.status_code == 200
    assert r_dup.json()["newly_confirmed"] is False

    # --- 3. Rescheduling a CONFIRMED (not requested) or CANCELLED session is rejected ---
    assert client.get(f"/reschedule/{sid1}").status_code == 404
    assert client.post(f"/reschedule/{sid1}", data={"slot_id": str(s2)}).status_code == 400
    assert get_slot(s2)["is_booked"] == 0

    sid_cancel = book(s2, "cancel@example.com")
    cancel_or_reject_session(sid_cancel)
    assert client.post(f"/reschedule/{sid_cancel}", data={"slot_id": str(s3)}).status_code == 400
    assert get_slot(s3)["is_booked"] == 0

    # --- 4. Admin reschedule-request + candidate reschedule never frees another person's slot ---
    mark_session_reschedule_requested(sid1)  # frees s1
    assert get_slot(s1)["is_booked"] == 0
    sid_other = book(s1, "other@example.com")  # another candidate books s1
    assert get_slot(s1)["is_booked"] == 1

    r_resched = client.post(
        f"/reschedule/{sid1}", data={"slot_id": str(s3)}, follow_redirects=False
    )
    assert r_resched.status_code == 303
    assert get_slot(s1)["is_booked"] == 1  # other@example.com's slot MUST remain booked!
    assert get_slot(s3)["is_booked"] == 1

    # --- 5. Cancelling a session twice never frees another person's slot ---
    cancel_or_reject_session(sid_other)  # frees s1
    sid_third = book(s1, "third@example.com")  # third candidate books s1
    assert get_slot(s1)["is_booked"] == 1
    cancel_or_reject_session(sid_other)  # second cancel on already-cancelled session is a no-op
    assert get_slot(s1)["is_booked"] == 1

    # --- 6. Hold expiry cancels the Revolut order; late payment after slot taken -> PAID_NEEDS_RESCHEDULE ---
    cancel_or_reject_session(sid_third)
    sid_late = book(s4, "late@example.com")
    client.post(f"/booking/{sid_late}/checkout", data={"package_type": "single"}, follow_redirects=False)
    late_order_id = get_session_with_slot(sid_late)["revolut_order_id"]

    past_iso = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
    conn = get_db_connection()
    conn.execute(
        "UPDATE interview_sessions SET created_at = ?, hold_expires_at = ? WHERE id = ?",
        (past_iso, past_iso, sid_late),
    )
    conn.commit()
    conn.close()

    expire_stale_pending_bookings()
    assert get_fake_order(late_order_id)["state"] == "CANCELLED"
    assert get_slot(s4)["is_booked"] == 0

    # Someone else books s4 while sid_late is expired
    sid_taker = book(s4, "taker@example.com")
    _complete_revolut_payment_via_webhook(client, sid_taker)
    assert get_session_with_slot(sid_taker)["status"] == "CONFIRMED"

    # Now a late webhook arrives for sid_late -> must NOT double-book s4; moves to PAID_NEEDS_RESCHEDULE
    raw_late, late_headers = build_completed_webhook_request(
        order_id=late_order_id, session_id=sid_late, amount_minor=7000
    )
    r_late_wh = client.post("/webhooks/revolut", content=raw_late, headers=late_headers)
    assert r_late_wh.status_code == 200
    assert r_late_wh.json()["slot_conflict"] is True
    assert get_session_with_slot(sid_late)["status"] == "PAID_NEEDS_RESCHEDULE"
    assert get_session_with_slot(sid_taker)["status"] == "CONFIRMED"

    # And the late-paying candidate can now pick a new open slot via /reschedule/{sid_late}
    r_late_resched = client.post(
        f"/reschedule/{sid_late}", data={"slot_id": str(s5)}, follow_redirects=False
    )
    assert r_late_resched.status_code == 303
    assert get_session_with_slot(sid_late)["status"] == "CONFIRMED"
    assert get_session_with_slot(sid_late)["slot_id"] == s5

    # --- 7. Feedback 404 before send, candidate WS config_update blocked, ADMIN_SECRET not in room HTML ---
    assert client.get(f"/feedback/{sid1}").status_code == 404
    r_room_int = client.get(f"/room/{sid1}?key={get_session_with_slot(sid1)['interviewer_secret']}")
    assert r_room_int.status_code == 200
    assert settings.ADMIN_SECRET not in r_room_int.text

    with client.websocket_connect(f"/ws/room/{sid1}") as ws_cand_only:
        ws_cand_only.send_json(
            {
                "type": "config_update",
                "syntax_highlighting_enabled": True,
                "editor_language": "cpp",
                "interview_mode": "ai_multifile",
            }
        )
    assert get_session_with_slot(sid1)["syntax_highlighting_enabled"] is False
