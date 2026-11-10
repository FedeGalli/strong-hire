import hashlib
import hmac
import json
import logging
import re
import time
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import (
    BackgroundTasks,
    Body,
    FastAPI,
    Form,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR, settings
from app.database import (
    MAX_BOOKING_DAYS_AHEAD,
    ROME_TZ,
    append_ai_chat_log,
    book_bundle_credit_session,
    cancel_or_reject_session,
    confirm_paid_session,
    create_interview_session,
    create_slot,
    delete_slot,
    find_active_bundles_by_email,
    get_bundle,
    get_bundle_by_token,
    get_session_by_revolut_order_id,
    get_session_with_slot,
    get_weekly_schedule,
    init_db,
    list_all_slots,
    list_available_slots,
    list_sessions_by_category,
    list_slots_by_rome_date,
    list_window_slots,
    mark_session_completed,
    mark_session_reschedule_requested,
    reschedule_interview_session,
    rotate_bundle_token,
    save_weekly_schedule_and_sync,
    seed_rome_availability_schedule,
    set_date_hours_rome,
    toggle_date_hour_rome,
    update_rubric_notes,
    update_session_approval,
    update_session_checkout_selection,
    update_workspace_state,
)
from app.models import (
    SUPPORTED_LANGUAGES,
    TARGET_ROLES,
    VERDICT_OPTIONS,
    RubricUpdatePayload,
)
from app.services.ai_assistant import generate_ai_coding_response
from app.services.calendar_meet import create_google_meet_event, generate_ics_invite
from app.services.email_service import (
    send_admin_booking_notification,
    send_breakdown_email,
    send_bundle_lookup_email,
    send_invitation_email,
    send_rejection_email,
    send_reschedule_request_email,
)
from app.services.revolut import (
    build_completed_webhook_request,
    create_revolut_order,
    verify_revolut_webhook,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title=settings.APP_TITLE, lifespan=lifespan)

app.mount(
    "/uploads/resumes",
    StaticFiles(directory=str(settings.UPLOADS_DIR)),
    name="resumes",
)

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
ADMIN_COOKIE_NAME = "swe_mock_admin_token"

EMAIL_RE = re.compile(r"^[^@\s\r\n]+@[^@\s\r\n]+\.[^@\s\r\n]{2,}$")
SAFE_FILENAME_RE = re.compile(r"^[\w.\-]{1,64}$")
ALLOWED_LANGUAGE_KEYS = {k for k, _ in SUPPORTED_LANGUAGES}
MAX_WORKSPACE_FILES = 20
MAX_WORKSPACE_BYTES = 200_000
MAX_AI_PROMPT_CHARS = 2_000

# Simple in-memory sliding-window rate limiter keyed by (bucket, key)
_RATE_BUCKETS: Dict[Tuple[str, str], List[float]] = {}


def reset_rate_limits() -> None:
    _RATE_BUCKETS.clear()


def _check_rate_limit(bucket: str, key: str, max_hits: int, window_seconds: int) -> bool:
    now = time.time()
    k = (bucket, key or "anon")
    hits = [t for t in _RATE_BUCKETS.get(k, []) if now - t < window_seconds]
    if len(hits) >= max_hits:
        _RATE_BUCKETS[k] = hits
        return False
    hits.append(now)
    _RATE_BUCKETS[k] = hits
    return True


def _get_client_ip(request: Request) -> str:
    """
    Returns the client IP address.
    Ignores spoofable `X-Forwarded-For` headers unless `settings.TRUST_PROXY_HEADERS`
    is explicitly enabled behind a trusted reverse proxy.
    """
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _sanitize_single_line(val: str, max_len: int = 100) -> str:
    cleaned = (val or "").replace("\r", " ").replace("\n", " ").strip()
    return cleaned[:max_len]


def _validate_candidate_intake(
    candidate_name: str,
    candidate_email: Optional[str],
    target_company: str,
    target_role: str,
    preferred_language: str,
) -> Tuple[str, str, str, str, str]:
    name = _sanitize_single_line(candidate_name, 100)
    if not name:
        raise ValueError("Please provide your full name.")

    email = ""
    if candidate_email is not None:
        email = _sanitize_single_line(candidate_email, 254).lower()
        if not EMAIL_RE.match(email):
            raise ValueError("Please provide a valid email address.")

    company = _sanitize_single_line(target_company, 100)
    if not company:
        raise ValueError("Please specify a target company.")

    role = _sanitize_single_line(target_role, 100)
    if not role:
        raise ValueError("Please select a target role.")

    lang = _sanitize_single_line(preferred_language, 32).lower()
    if lang not in ALLOWED_LANGUAGE_KEYS:
        raise ValueError("Unsupported coding language selected.")

    return name, email, company, role, lang


def _sanitize_workspace_files(
    raw_files: Any, fallback: Dict[str, Any]
) -> Optional[Dict[str, Dict[str, str]]]:
    if not isinstance(raw_files, dict) or not raw_files:
        return None
    if len(raw_files) > MAX_WORKSPACE_FILES:
        return None
    cleaned: Dict[str, Dict[str, str]] = {}
    total_bytes = 0
    for fname, fobj in raw_files.items():
        if not isinstance(fname, str) or not SAFE_FILENAME_RE.match(fname):
            return None
        if not isinstance(fobj, dict):
            return None
        content = str(fobj.get("content") or "")
        lang = str(fobj.get("language") or "python").lower()
        if lang not in ALLOWED_LANGUAGE_KEYS:
            lang = "python"
        total_bytes += len(content.encode("utf-8", errors="ignore"))
        if total_bytes > MAX_WORKSPACE_BYTES:
            return None
        cleaned[fname] = {"content": content, "language": lang}
    return cleaned or fallback


def _expected_admin_cookie() -> str:
    return hmac.new(
        settings.ADMIN_SECRET.encode("utf-8"),
        settings.ADMIN_PASSWORD.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _is_admin_authenticated(request: Request) -> bool:
    cookie_val = request.cookies.get(ADMIN_COOKIE_NAME, "")
    return hmac.compare_digest(cookie_val, _expected_admin_cookie())


def _is_interviewer_for_session(
    session: Dict[str, Any],
    key: Optional[str] = None,
    cookie_val: str = "",
) -> bool:
    if cookie_val and hmac.compare_digest(cookie_val, _expected_admin_cookie()):
        return True
    if key:
        if hmac.compare_digest(key, settings.ADMIN_SECRET):
            return True
        if session.get("interviewer_secret") and hmac.compare_digest(
            key, session["interviewer_secret"]
        ):
            return True
    return False


def _finalize_confirmed_session_invite(
    session_id: str, custom_meet_url: Optional[str] = None
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """
    Creates the Google Calendar/Meet event, builds the `.ics` invite, sends the
    threaded confirmation email, and stores the message-id/meet link on the session.
    """
    session = get_session_with_slot(session_id)
    if not session:
        return None, False
    event_id, meet_url = create_google_meet_event(
        session, custom_meet_url=custom_meet_url or session.get("google_meet_url")
    )
    ics_bytes = generate_ics_invite(session, meet_url)
    email_message_id, email_subject, sent_live = send_invitation_email(
        session, meet_url, ics_bytes
    )
    updated = update_session_approval(
        session_id=session_id,
        google_event_id=event_id,
        google_meet_url=meet_url,
        email_message_id=email_message_id,
        email_subject=email_subject,
    )
    return updated, sent_live


# =====================================================================
# 1. PUBLIC MINIMALIST LANDING PAGE (/) & CAL.COM-STYLE BOOKING (/book)
# =====================================================================


@app.get("/", response_class=HTMLResponse)
def public_landing_page(request: Request):
    slots = list_available_slots(max_days_ahead=MAX_BOOKING_DAYS_AHEAD)
    return templates.TemplateResponse(
        request=request,
        name="landing.html",
        context={
            "settings": settings,
            "open_slots_count": len(slots),
            "next_slot": slots[0] if slots else None,
        },
    )


@app.get("/book", response_class=HTMLResponse)
def booking_calendar_page(
    request: Request,
    error: Optional[str] = None,
    package: Optional[str] = Query("single"),
    bundle_msg: Optional[str] = Query(None),
):
    slots = list_available_slots(max_days_ahead=MAX_BOOKING_DAYS_AHEAD)
    all_window_slots = list_window_slots(max_days_ahead=MAX_BOOKING_DAYS_AHEAD)
    weekly_schedule = get_weekly_schedule()
    initial_package = "bundle_3" if package == "bundle_3" else "single"
    return templates.TemplateResponse(
        request=request,
        name="book.html",
        context={
            "settings": settings,
            "slots": slots,
            "all_window_slots": all_window_slots,
            "weekly_schedule": weekly_schedule,
            "max_days_ahead": MAX_BOOKING_DAYS_AHEAD,
            "supported_languages": SUPPORTED_LANGUAGES,
            "target_roles": TARGET_ROLES,
            "error_message": _sanitize_single_line(error or "", 220) if error else None,
            "initial_package": initial_package,
            "bundle_msg": _sanitize_single_line(bundle_msg or "", 220) if bundle_msg else None,
        },
    )


@app.post("/book")
def submit_booking(
    request: Request,
    background_tasks: BackgroundTasks,
    slot_id: int = Form(...),
    candidate_name: str = Form(...),
    candidate_email: str = Form(...),
    leetcode_solved: int = Form(0),
    target_company: str = Form(...),
    target_role: str = Form(...),
    preferred_language: str = Form("python"),
    package_type: str = Form("single"),
    youtube_consent: Optional[str] = Form(None),
    website_url: Optional[str] = Form(None),
):
    # Anti-bot honeypot field check
    if website_url and website_url.strip():
        return RedirectResponse(url="/book?error=Bot+submission+rejected", status_code=303)

    client_ip = _get_client_ip(request)
    if not _check_rate_limit("book_ip", client_ip, max_hits=15, window_seconds=60):
        return RedirectResponse(
            url="/book?error=Too+many+booking+requests.+Please+wait+a+minute.",
            status_code=303,
        )

    yt_bool = str(youtube_consent or "").strip().lower() in ("1", "true", "on", "yes")

    try:
        name, email, company, role, lang = _validate_candidate_intake(
            candidate_name=candidate_name,
            candidate_email=candidate_email,
            target_company=target_company,
            target_role=target_role,
            preferred_language=preferred_language,
        )
        session = create_interview_session(
            slot_id=slot_id,
            candidate_name=name,
            candidate_email=email,
            leetcode_solved=max(0, min(10000, int(leetcode_solved))),
            target_company=company,
            preferred_language=lang,
            target_role=role,
            resume_path_or_url="",
            client_ip=client_ip,
            package_type=package_type.strip(),
            youtube_consent=yt_bool,
        )
    except ValueError as exc:
        q = urllib.parse.urlencode({"error": str(exc)})
        return RedirectResponse(url=f"/book?{q}", status_code=303)

    background_tasks.add_task(send_admin_booking_notification, session)

    return RedirectResponse(
        url=f"/booking/{session['id']}/payment",
        status_code=303,
    )


@app.post("/book/lookup-bundle")
def lookup_bundle(
    request: Request,
    background_tasks: BackgroundTasks,
    query_str: str = Form(...),
):
    """
    Self-service bundle lookup by email ONLY.
    Never redirects the caller to the bundle page (preventing bundle takeover if someone
    knows a customer's email). Always responds with a generic confirmation message and
    emails the secret `/bundle/b/{access_token}` link to the bundle owner.
    """
    client_ip = _get_client_ip(request)
    q = _sanitize_single_line(query_str, 254).lower()

    generic_msg = (
        "If an active 3-Mock Bundle exists for that email address, we have sent your private booking link to your inbox."
    )
    if not EMAIL_RE.match(q):
        params = urllib.parse.urlencode(
            {"error": "Please enter the email address used when purchasing your 3-Mock Bundle."}
        )
        return RedirectResponse(url=f"/book?{params}", status_code=303)

    if _check_rate_limit("bundle_lookup_ip", client_ip, max_hits=5, window_seconds=600) and _check_rate_limit(
        "bundle_lookup_email", q, max_hits=2, window_seconds=600
    ):
        bundles = find_active_bundles_by_email(q)
        if bundles:
            background_tasks.add_task(send_bundle_lookup_email, bundles)

    params = urllib.parse.urlencode({"bundle_msg": generic_msg})
    return RedirectResponse(url=f"/book?{params}", status_code=303)


@app.get("/bundle/b/{access_token}", response_class=HTMLResponse)
def bundle_booking_page_by_token(
    request: Request, access_token: str, error: Optional[str] = None
):
    bundle = get_bundle_by_token(access_token)
    if not bundle:
        raise HTTPException(
            status_code=404, detail="3-Mock Bundle link is invalid or has been revoked."
        )

    slots = list_available_slots(max_days_ahead=MAX_BOOKING_DAYS_AHEAD)
    all_window_slots = list_window_slots(max_days_ahead=MAX_BOOKING_DAYS_AHEAD)
    weekly_schedule = get_weekly_schedule()
    return templates.TemplateResponse(
        request=request,
        name="book.html",
        context={
            "settings": settings,
            "slots": slots,
            "all_window_slots": all_window_slots,
            "weekly_schedule": weekly_schedule,
            "max_days_ahead": MAX_BOOKING_DAYS_AHEAD,
            "supported_languages": SUPPORTED_LANGUAGES,
            "target_roles": TARGET_ROLES,
            "error_message": _sanitize_single_line(error or "", 220) if error else None,
            "bundle_session": bundle,
            "bundle_access_token": access_token,
        },
    )


@app.post("/bundle/b/{access_token}")
def submit_bundle_booking_by_token(
    request: Request,
    access_token: str,
    slot_id: int = Form(...),
    candidate_name: str = Form(...),
    leetcode_solved: int = Form(0),
    target_company: str = Form(...),
    target_role: str = Form(...),
    preferred_language: str = Form("python"),
):
    bundle = get_bundle_by_token(access_token)
    if not bundle:
        raise HTTPException(
            status_code=404, detail="3-Mock Bundle link is invalid or has been revoked."
        )

    client_ip = _get_client_ip(request)
    try:
        name, _, company, role, lang = _validate_candidate_intake(
            candidate_name=candidate_name,
            candidate_email=None,
            target_company=target_company,
            target_role=target_role,
            preferred_language=preferred_language,
        )
        new_session = book_bundle_credit_session(
            bundle_id=bundle["id"],
            slot_id=slot_id,
            candidate_name=name,
            target_company=company,
            target_role=role,
            preferred_language=lang,
            leetcode_solved=max(0, min(10000, int(leetcode_solved))),
            client_ip=client_ip,
        )
    except ValueError as exc:
        q = urllib.parse.urlencode({"error": str(exc)})
        return RedirectResponse(
            url=f"/bundle/b/{access_token}?{q}",
            status_code=303,
        )

    _finalize_confirmed_session_invite(new_session["id"])
    send_admin_booking_notification(new_session)

    return RedirectResponse(
        url=f"/booking/{new_session['id']}/payment",
        status_code=303,
    )


@app.get("/bundle/{bundle_id}")
@app.post("/bundle/{bundle_id}")
def legacy_bundle_id_route_disabled(bundle_id: str):
    """Direct access by raw bundle database ID is disabled; secret token `/bundle/b/{token}` is required."""
    raise HTTPException(
        status_code=404,
        detail="Direct bundle ID links are disabled. Please use the private link sent to your email.",
    )


@app.get("/reschedule/{session_id}", response_class=HTMLResponse)
def reschedule_calendar_page(
    request: Request, session_id: str, error: Optional[str] = None
):
    session = get_session_with_slot(session_id)
    if not session or session["status"] not in (
        "RESCHEDULE_REQUESTED",
        "PAID_NEEDS_RESCHEDULE",
    ):
        raise HTTPException(
            status_code=404,
            detail="No active reschedule request found for this session.",
        )

    slots = list_available_slots(max_days_ahead=MAX_BOOKING_DAYS_AHEAD)
    all_window_slots = list_window_slots(max_days_ahead=MAX_BOOKING_DAYS_AHEAD)
    weekly_schedule = get_weekly_schedule()
    return templates.TemplateResponse(
        request=request,
        name="book.html",
        context={
            "settings": settings,
            "slots": slots,
            "all_window_slots": all_window_slots,
            "weekly_schedule": weekly_schedule,
            "max_days_ahead": MAX_BOOKING_DAYS_AHEAD,
            "supported_languages": SUPPORTED_LANGUAGES,
            "target_roles": TARGET_ROLES,
            "error_message": _sanitize_single_line(error or "", 220) if error else None,
            "reschedule_session": session,
        },
    )


@app.post("/reschedule/{session_id}")
def submit_reschedule(
    session_id: str,
    slot_id: int = Form(...),
):
    try:
        updated_session = reschedule_interview_session(session_id, slot_id)
    except ValueError as exc:
        session = get_session_with_slot(session_id)
        if not session or session["status"] not in (
            "RESCHEDULE_REQUESTED",
            "PAID_NEEDS_RESCHEDULE",
        ):
            raise HTTPException(status_code=400, detail=str(exc))
        q = urllib.parse.urlencode({"error": str(exc)})
        return RedirectResponse(
            url=f"/reschedule/{session_id}?{q}",
            status_code=303,
        )

    if updated_session["status"] == "CONFIRMED":
        _finalize_confirmed_session_invite(
            session_id, custom_meet_url=updated_session.get("google_meet_url")
        )
        return RedirectResponse(
            url=f"/booking/{session_id}/payment",
            status_code=303,
        )

    return RedirectResponse(
        url=f"/booking/{session_id}/payment",
        status_code=303,
    )


@app.get("/booking/{session_id}/payment", response_class=HTMLResponse)
def payment_instructions_page(
    request: Request,
    session_id: str,
    order_id: Optional[str] = Query(None),
    checkout_return: Optional[int] = Query(None),
):
    """
    Renders the checkout or confirmation screen.
    IMPORTANT: Query parameters (like `mock_paid=1` or `checkout_return=1`) NEVER mark a
    session as paid; `is_confirmed` is derived strictly from the server-side `session["status"]`.
    """
    session = get_session_with_slot(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Booking session not found.")
    is_confirmed = session["status"] in ("CONFIRMED", "COMPLETED")
    return templates.TemplateResponse(
        request=request,
        name="payment.html",
        context={
            "settings": settings,
            "session": session,
            "mock_paid": is_confirmed,
            "active_order_id": order_id or session.get("revolut_order_id") or "",
            "checkout_return": bool(checkout_return),
            "revolut_live_mode": bool(settings.REVOLUT_API_KEY),
        },
    )


@app.get("/booking/{session_id}/status")
def booking_payment_status_api(session_id: str):
    session = get_session_with_slot(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Booking session not found.")
    return JSONResponse(
        {
            "session_id": session_id,
            "status": session["status"],
            "confirmed": session["status"] in ("CONFIRMED", "COMPLETED"),
            "revolut_order_id": session.get("revolut_order_id") or "",
        }
    )


@app.post("/booking/{session_id}/checkout")
def start_revolut_checkout(
    request: Request,
    session_id: str,
    package_type: Optional[str] = Form(None),
    youtube_consent: Optional[str] = Form(None),
):
    """
    Saves the candidate's package & YouTube consent selection, refreshes the 30-minute hold,
    creates a Revolut Merchant order, and redirects to the Revolut checkout URL.
    NEVER transitions the session to `CONFIRMED`—only a verified Revolut `ORDER_COMPLETED`
    webhook (or admin manual override) can confirm a booking.
    """
    session = get_session_with_slot(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Booking session not found.")

    if session["status"] in ("CONFIRMED", "COMPLETED"):
        return RedirectResponse(url=f"/booking/{session_id}/payment", status_code=303)

    if session["status"] != "PENDING_PAYMENT":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot start checkout for booking in status '{session['status']}'.",
        )

    client_ip = _get_client_ip(request)
    chosen_pkg = package_type or session.get("package_type") or "single"
    yt_bool = (
        str(youtube_consent or "").strip().lower() in ("1", "true", "on", "yes")
        if youtube_consent is not None
        else bool(session.get("youtube_consent"))
    )

    session = (
        update_session_checkout_selection(
            session_id=session_id,
            package_type=chosen_pkg,
            youtube_consent=yt_bool,
            client_ip=client_ip,
        )
        or session
    )

    order_id, checkout_url = create_revolut_order(session)
    update_session_checkout_selection(
        session_id=session_id,
        package_type=chosen_pkg,
        youtube_consent=yt_bool,
        client_ip=client_ip,
        revolut_order_id=order_id,
        revolut_checkout_url=checkout_url,
    )

    return RedirectResponse(url=checkout_url, status_code=303)


@app.post("/booking/{session_id}/mock-pay")
def legacy_mock_pay_endpoint(
    request: Request,
    session_id: str,
    package_type: Optional[str] = Form(None),
    youtube_consent: Optional[str] = Form(None),
):
    """
    Security replacement for the old `/mock-pay` route: delegates to `start_revolut_checkout`
    and NEVER confirms the session without a verified webhook.
    """
    return start_revolut_checkout(
        request=request,
        session_id=session_id,
        package_type=package_type,
        youtube_consent=youtube_consent,
    )


@app.post("/webhooks/revolut")
async def revolut_order_webhook(request: Request):
    """
    Receives signed Revolut Merchant API webhook events (`ORDER_COMPLETED`).
    1. Verifies `Revolut-Signature` (HMAC-SHA256 over `v1.{Revolut-Request-Timestamp}.{raw_body}`)
       and enforces a 5-minute replay window.
    2. Matches the session by `order_id` or `merchant_order_ext_ref`.
    3. Verifies currency (`EUR`) and paid amount (`>= price_eur * 100`).
    4. Atomically confirms the session (idempotent on duplicate deliveries) and sends the
       Google Meet + `.ics` calendar invitation email.
    """
    raw_body = await request.body()
    if not verify_revolut_webhook(request.headers, raw_body):
        raise HTTPException(status_code=401, detail="Invalid Revolut webhook signature.")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    event_type = str(payload.get("event") or "")
    if event_type != "ORDER_COMPLETED":
        return JSONResponse({"status": "ignored", "event": event_type})

    order_id = str(payload.get("order_id") or "").strip()
    ext_ref = str(payload.get("merchant_order_ext_ref") or "").strip()
    currency = str(payload.get("currency") or "EUR").upper()
    if currency != "EUR":
        raise HTTPException(status_code=400, detail="Unsupported currency.")

    raw_amount = payload.get("amount")
    amount_minor: Optional[int] = int(raw_amount) if raw_amount is not None else None

    def _process_sync() -> Dict[str, Any]:
        target = get_session_by_revolut_order_id(order_id) if order_id else None
        if not target and ext_ref:
            target = get_session_with_slot(ext_ref)
        if not target:
            raise HTTPException(status_code=404, detail="Order session not found.")

        try:
            updated, newly_confirmed, slot_conflict = confirm_paid_session(
                session_id=target["id"],
                amount_minor=amount_minor,
                revolut_order_id=order_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        if newly_confirmed and updated:
            _finalize_confirmed_session_invite(updated["id"])
        elif slot_conflict and updated:
            send_reschedule_request_email(updated)
            send_admin_booking_notification(updated)

        return {
            "status": "ok",
            "session_id": target["id"],
            "newly_confirmed": newly_confirmed,
            "slot_conflict": slot_conflict,
        }

    result = await run_in_threadpool(_process_sync)
    return JSONResponse(result)


@app.post("/api/dev/revolut/simulate-webhook/{session_id}")
async def dev_simulate_revolut_webhook(session_id: str, request: Request):
    """
    Local development / sandbox helper (only active when `REVOLUT_API_KEY` is empty):
    generates a genuinely signed `ORDER_COMPLETED` webhook payload for the session's
    fake order and dispatches it through the real verification & confirmation pipeline.
    """
    if settings.REVOLUT_API_KEY:
        raise HTTPException(
            status_code=403,
            detail="Sandbox webhook simulator is disabled when live REVOLUT_API_KEY is configured.",
        )

    session = await run_in_threadpool(get_session_with_slot, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    order_id = session.get("revolut_order_id") or ""
    if not order_id:
        order_id, checkout_url = await run_in_threadpool(create_revolut_order, session)
        await run_in_threadpool(
            update_session_checkout_selection,
            session_id,
            session.get("package_type") or "single",
            bool(session.get("youtube_consent")),
            _get_client_ip(request),
            order_id,
            checkout_url,
        )

    amount_minor = int(session.get("price_eur") or 70) * 100
    raw_bytes, signed_headers = build_completed_webhook_request(
        order_id=order_id,
        session_id=session_id,
        amount_minor=amount_minor,
    )
    if not verify_revolut_webhook(signed_headers, raw_bytes):
        raise HTTPException(status_code=500, detail="Webhook signature self-check failed.")

    def _confirm_sync() -> None:
        updated, newly_confirmed, slot_conflict = confirm_paid_session(
            session_id=session_id,
            amount_minor=amount_minor,
            revolut_order_id=order_id,
        )
        if newly_confirmed and updated:
            _finalize_confirmed_session_invite(updated["id"])
        elif slot_conflict and updated:
            send_reschedule_request_email(updated)

    await run_in_threadpool(_confirm_sync)
    return RedirectResponse(url=f"/booking/{session_id}/payment", status_code=303)


@app.get("/booking/{session_id}/ics")
def download_session_ics(session_id: str):
    session = get_session_with_slot(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Booking session not found.")
    meet_url = session.get("google_meet_url") or f"https://meet.google.com/lookup/mock-{session_id[:8]}"
    ics_bytes = generate_ics_invite(session, meet_url)
    return Response(
        content=ics_bytes,
        media_type="text/calendar",
        headers={"Content-Disposition": f'attachment; filename="invite_{session_id[:8]}.ics"'},
    )


# =====================================================================
# 2. COMPREHENSIVE ADMIN PANEL (/admin) & AVAILABILITY API
# =====================================================================


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(
    request: Request,
    tab: str = "upcoming",
    msg: Optional[str] = None,
    error: Optional[str] = None,
):
    is_auth = _is_admin_authenticated(request)
    if not is_auth:
        return templates.TemplateResponse(
            request=request,
            name="admin_dashboard.html",
            context={
                "settings": settings,
                "is_authenticated": False,
                "flash_error": _sanitize_single_line(error or "", 200) if error else None,
            },
        )

    categorized = list_sessions_by_category()
    all_slots = list_all_slots()
    weekly_schedule = get_weekly_schedule()
    slots_by_rome_date = list_slots_by_rome_date()

    return templates.TemplateResponse(
        request=request,
        name="admin_dashboard.html",
        context={
            "settings": settings,
            "is_authenticated": True,
            "initial_tab": tab,
            "sessions": categorized,
            "all_slots": all_slots,
            "weekly_schedule": weekly_schedule,
            "slots_by_rome_date": slots_by_rome_date,
            "flash_message": _sanitize_single_line(msg or "", 240) if msg else None,
        },
    )


@app.post("/admin/login")
def admin_login(request: Request, password: str = Form(...)):
    client_ip = _get_client_ip(request)
    if not _check_rate_limit("admin_login", client_ip, max_hits=10, window_seconds=300):
        return RedirectResponse(
            url="/admin?error=Too+many+login+attempts.+Please+wait+5+minutes.",
            status_code=303,
        )

    if hmac.compare_digest(password.strip(), settings.ADMIN_PASSWORD):
        response = RedirectResponse(url="/admin", status_code=303)
        response.set_cookie(
            key=ADMIN_COOKIE_NAME,
            value=_expected_admin_cookie(),
            httponly=True,
            max_age=60 * 60 * 24 * 14,
            samesite="lax",
        )
        return response
    return RedirectResponse(url="/admin?error=Invalid+admin+password", status_code=303)


@app.get("/admin/logout")
def admin_logout():
    response = RedirectResponse(url="/admin", status_code=303)
    response.delete_cookie(ADMIN_COOKIE_NAME)
    return response


@app.post("/api/admin/availability/weekly")
def api_save_weekly_availability(
    request: Request,
    payload: Dict[str, Any] = Body(...),
):
    if not _is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")

    try:
        schedule_map = payload.get("schedule") or {}
        if not isinstance(schedule_map, dict):
            raise ValueError("schedule must be an object")
        days_ahead = int(payload.get("days_ahead", 60))
        clear_unbooked = bool(payload.get("clear_unbooked", True))
        added = save_weekly_schedule_and_sync(
            schedule_map=schedule_map,
            days_ahead=days_ahead,
            clear_unbooked=clear_unbooked,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid schedule payload: {exc}")

    return JSONResponse(
        {
            "status": "ok",
            "added": added,
            "weekly_schedule": get_weekly_schedule(),
            "slots_by_rome_date": list_slots_by_rome_date(),
        }
    )


@app.post("/api/admin/availability/toggle-hour")
def api_toggle_date_hour(
    request: Request,
    payload: Dict[str, Any] = Body(...),
):
    if not _is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")

    try:
        date_str = str(payload.get("date", "")).strip()
        hour_rome = int(payload.get("hour", 17))
        result = toggle_date_hour_rome(date_str=date_str, hour_rome=hour_rome)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid date or hour: {exc}")

    return JSONResponse(
        {
            "status": "ok",
            "result": result,
            "slots_by_rome_date": list_slots_by_rome_date(),
        }
    )


@app.post("/api/admin/availability/set-date-hours")
def api_set_date_hours(
    request: Request,
    payload: Dict[str, Any] = Body(...),
):
    if not _is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")

    try:
        date_str = str(payload.get("date", "")).strip()
        raw_hours = payload.get("hours") or []
        if not isinstance(raw_hours, list):
            raise ValueError("hours must be a list")
        hours_rome = [int(h) for h in raw_hours]
        result = set_date_hours_rome(date_str=date_str, hours_rome=hours_rome)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid date or hours list: {exc}")

    return JSONResponse(
        {
            "status": "ok",
            "result": result,
            "slots_by_rome_date": list_slots_by_rome_date(),
        }
    )


@app.post("/admin/slots/standard-rome")
def admin_apply_standard_rome_schedule(
    request: Request,
    days_ahead: int = Form(60),
    clear_unbooked: Optional[str] = Form(None),
):
    if not _is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")

    added = seed_rome_availability_schedule(
        days_ahead=min(60, max(7, days_ahead)),
        clear_unbooked=(clear_unbooked == "on"),
    )
    q = urllib.parse.urlencode(
        {"tab": "slots", "msg": f"Synced weekly Europe/Rome schedule ({added} slots generated)"}
    )
    return RedirectResponse(url=f"/admin?{q}", status_code=303)


@app.post("/admin/slots/add")
def admin_add_slot(
    request: Request,
    slot_date: str = Form(...),
    slot_time: str = Form(...),
    duration_minutes: int = Form(60),
):
    if not _is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")

    try:
        naive_dt = datetime.fromisoformat(f"{slot_date.strip()}T{slot_time.strip()}:00")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid slot_date or slot_time format.")
    rome_dt = naive_dt.replace(tzinfo=ROME_TZ)
    start_utc = rome_dt.astimezone(timezone.utc)
    end_utc = start_utc + timedelta(minutes=max(15, min(240, int(duration_minutes))))
    create_slot(start_utc.isoformat(), end_utc.isoformat())
    return RedirectResponse(
        url="/admin?tab=slots&msg=Added+new+availability+slot+(Europe/Rome)", status_code=303
    )


@app.post("/admin/slots/{slot_id}/delete")
def admin_delete_slot(request: Request, slot_id: int):
    if not _is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")
    delete_slot(slot_id)
    return RedirectResponse(url="/admin?tab=slots&msg=Slot+removed", status_code=303)


@app.post("/admin/session/{session_id}/approve")
def admin_approve_booking(
    request: Request,
    session_id: str,
    custom_meet_url: Optional[str] = Form(None),
):
    """
    Admin manual override to confirm a booking (guarded by `confirm_paid_session`
    so an expired hold whose slot was taken by someone else cannot double-book the slot).
    """
    if not _is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")

    session = get_session_with_slot(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    updated, newly_confirmed, slot_conflict = confirm_paid_session(session_id=session_id)
    if slot_conflict:
        q = urllib.parse.urlencode(
            {
                "tab": "upcoming",
                "msg": "Original slot was already taken by another booking; session marked for reschedule.",
            }
        )
        return RedirectResponse(url=f"/admin?{q}", status_code=303)

    _, sent_live = _finalize_confirmed_session_invite(
        session_id, custom_meet_url=custom_meet_url
    )
    delivery_note = "Sent live via Gmail SMTP" if sent_live else "Saved .eml + .ics to uploads/email_outbox"
    q = urllib.parse.urlencode(
        {"tab": "upcoming", "msg": f"Booking approved and invitation prepared ({delivery_note})"}
    )
    return RedirectResponse(url=f"/admin?{q}", status_code=303)


@app.post("/admin/session/{session_id}/cancel")
def admin_cancel_booking(request: Request, session_id: str):
    if not _is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")
    session = get_session_with_slot(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    did_cancel = cancel_or_reject_session(session_id)
    if did_cancel:
        sent_live = send_rejection_email(session)
        delivery_note = (
            "Rejection email sent via Gmail SMTP"
            if sent_live
            else "Rejection email saved to uploads/email_outbox"
        )
        msg = f"Booking cancelled, slot reopened, and candidate notified ({delivery_note})"
    else:
        msg = "Booking was already cancelled or completed"

    q = urllib.parse.urlencode({"tab": "upcoming", "msg": msg})
    return RedirectResponse(url=f"/admin?{q}", status_code=303)


@app.post("/admin/session/{session_id}/request-reschedule")
def admin_request_reschedule(request: Request, session_id: str):
    if not _is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        session = mark_session_reschedule_requested(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    sent_live = send_reschedule_request_email(session)
    delivery_note = (
        "Reschedule link sent via Gmail SMTP"
        if sent_live
        else "Reschedule link email saved to uploads/email_outbox"
    )
    q = urllib.parse.urlencode(
        {
            "tab": "upcoming",
            "msg": f"Previous slot freed and reschedule link emailed to {session['candidate_name']} ({delivery_note})",
        }
    )
    return RedirectResponse(url=f"/admin?{q}", status_code=303)


@app.post("/admin/bundle/{bundle_id}/resend")
def admin_resend_bundle_link(request: Request, bundle_id: str):
    if not _is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")
    bundle = rotate_bundle_token(bundle_id, revoke=False)
    if not bundle:
        raise HTTPException(status_code=404, detail="Bundle not found")
    send_bundle_lookup_email([bundle])
    q = urllib.parse.urlencode(
        {"tab": "upcoming", "msg": f"Rotated and emailed fresh bundle link to {bundle['candidate_email']}"}
    )
    return RedirectResponse(url=f"/admin?{q}", status_code=303)


@app.post("/admin/bundle/{bundle_id}/revoke")
def admin_revoke_bundle_link(request: Request, bundle_id: str):
    if not _is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")
    bundle = rotate_bundle_token(bundle_id, revoke=True)
    if not bundle:
        raise HTTPException(status_code=404, detail="Bundle not found")
    q = urllib.parse.urlencode(
        {"tab": "upcoming", "msg": f"Revoked bundle link for {bundle['bundle_code']}"}
    )
    return RedirectResponse(url=f"/admin?{q}", status_code=303)


# =====================================================================
# 3. POST-INTERVIEW BREAKDOWN EDITOR & SAME-THREAD FEEDBACK SENDER
# =====================================================================


@app.get("/admin/session/{session_id}", response_class=HTMLResponse)
def admin_breakdown_editor(
    request: Request,
    session_id: str,
    msg: Optional[str] = None,
):
    if not _is_admin_authenticated(request):
        return RedirectResponse(url="/admin", status_code=303)

    session = get_session_with_slot(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return templates.TemplateResponse(
        request=request,
        name="admin_breakdown.html",
        context={
            "settings": settings,
            "session": session,
            "verdict_options": VERDICT_OPTIONS,
            "flash_message": _sanitize_single_line(msg or "", 240) if msg else None,
        },
    )


def _int_or_none(val: Optional[str]) -> Optional[int]:
    if val is None or str(val).strip() == "":
        return None
    try:
        n = int(val)
        return max(1, min(5, n))
    except ValueError:
        return None


@app.post("/admin/session/{session_id}/save")
def admin_save_breakdown(
    request: Request,
    session_id: str,
    final_verdict: Optional[str] = Form(None),
    score_clarification: Optional[str] = Form(None),
    notes_clarification: str = Form(""),
    score_approach_complexity: Optional[str] = Form(None),
    notes_approach_complexity: str = Form(""),
    score_code_quality: Optional[str] = Form(None),
    notes_code_quality: str = Form(""),
    score_verification: Optional[str] = Form(None),
    notes_verification: str = Form(""),
    final_recap_and_next_steps: str = Form(""),
):
    if not _is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")

    verdict_clean = final_verdict if final_verdict in VERDICT_OPTIONS else None
    update_rubric_notes(
        session_id,
        {
            "final_verdict": verdict_clean,
            "score_clarification": _int_or_none(score_clarification),
            "notes_clarification": notes_clarification,
            "score_approach_complexity": _int_or_none(score_approach_complexity),
            "notes_approach_complexity": notes_approach_complexity,
            "score_code_quality": _int_or_none(score_code_quality),
            "notes_code_quality": notes_code_quality,
            "score_verification": _int_or_none(score_verification),
            "notes_verification": notes_verification,
            "final_recap_and_next_steps": final_recap_and_next_steps,
        },
    )
    return RedirectResponse(
        url=f"/admin/session/{session_id}?msg=Saved+rubric+notes+and+final+judgement",
        status_code=303,
    )


@app.get("/feedback/{session_id}", response_class=HTMLResponse)
def candidate_feedback_page(
    request: Request,
    session_id: str,
    key: Optional[str] = Query(None),
):
    """
    Candidate-facing session breakdown & judgement page.
    Restricted until the interviewer has actually sent the breakdown (`breakdown_sent_at`),
    unless viewed by an authenticated interviewer/admin (preventing candidates from
    peeking at live draft rubric notes during the interview).
    """
    session = get_session_with_slot(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    is_admin_viewer = _is_interviewer_for_session(
        session,
        key=key,
        cookie_val=request.cookies.get(ADMIN_COOKIE_NAME, ""),
    )
    if not session.get("breakdown_sent_at") and session.get("status") != "COMPLETED" and not is_admin_viewer:
        raise HTTPException(
            status_code=404,
            detail="Feedback report is not yet published for this session.",
        )

    return templates.TemplateResponse(
        request=request,
        name="candidate_feedback.html",
        context={
            "settings": settings,
            "session": session,
            "verdict_options": VERDICT_OPTIONS,
        },
    )


@app.get("/admin/session/{session_id}/pdf")
def admin_preview_breakdown_redirect(request: Request, session_id: str):
    return RedirectResponse(url=f"/feedback/{session_id}", status_code=303)


@app.post("/admin/session/{session_id}/send-breakdown")
async def admin_send_breakdown_in_thread(request: Request, session_id: str):
    if not _is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")

    session = await run_in_threadpool(get_session_with_slot, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    sent_live = await run_in_threadpool(send_breakdown_email, session)
    await run_in_threadpool(mark_session_completed, session_id, True)

    # Immediately lock any open candidate room tabs and redirect them to the feedback breakdown page
    await room_manager.broadcast(
        session_id,
        {
            "type": "room_locked",
            "redirect_url": f"/feedback/{session_id}",
        },
    )

    status_note = (
        "Sent breakdown page link in original email thread via Gmail SMTP"
        if sent_live
        else "Saved threaded reply .eml with breakdown link to uploads/email_outbox"
    )
    q = urllib.parse.urlencode({"msg": status_note})
    return RedirectResponse(
        url=f"/admin/session/{session_id}?{q}",
        status_code=303,
    )


# =====================================================================
# 4. STANDALONE REAL-TIME INTERVIEW ROOM & WEBSOCKET BROADCASTER
# =====================================================================


@app.get("/room/{session_id}", response_class=HTMLResponse)
def interview_room_page(
    request: Request,
    session_id: str,
    key: Optional[str] = Query(None),
):
    session = get_session_with_slot(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Interview room not found.")

    is_interviewer = _is_interviewer_for_session(
        session,
        key=key,
        cookie_val=request.cookies.get(ADMIN_COOKIE_NAME, ""),
    )

    # Once feedback has been sent, restrict the shared coding room document to the end user
    if (session["status"] == "COMPLETED" or session.get("breakdown_sent_at")) and not is_interviewer:
        return RedirectResponse(url=f"/feedback/{session_id}", status_code=303)

    # Never expose the global ADMIN_SECRET in JavaScript; use the per-session interviewer_secret
    return templates.TemplateResponse(
        request=request,
        name="room.html",
        context={
            "settings": settings,
            "session": session,
            "is_interviewer": is_interviewer,
            "interviewer_key": session["interviewer_secret"] if is_interviewer else "",
            "supported_languages": SUPPORTED_LANGUAGES,
            "verdict_options": VERDICT_OPTIONS,
        },
    )


@app.post("/api/session/{session_id}/rubric")
def api_autosave_rubric(
    request: Request,
    session_id: str,
    payload: RubricUpdatePayload,
    key: Optional[str] = Query(None),
):
    session = get_session_with_slot(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    authorized = _is_interviewer_for_session(
        session,
        key=key,
        cookie_val=request.cookies.get(ADMIN_COOKIE_NAME, ""),
    )
    if not authorized:
        raise HTTPException(status_code=403, detail="Forbidden")

    updated = update_rubric_notes(session_id, payload.model_dump())
    return JSONResponse({"status": "saved", "final_verdict": updated.get("final_verdict")})


class RoomConnectionManager:
    def __init__(self) -> None:
        self.active_rooms: Dict[str, List[WebSocket]] = {}

    async def connect(self, session_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_rooms.setdefault(session_id, []).append(websocket)

    def disconnect(self, session_id: str, websocket: WebSocket) -> None:
        conns = self.active_rooms.get(session_id, [])
        if websocket in conns:
            conns.remove(websocket)
        if not conns and session_id in self.active_rooms:
            del self.active_rooms[session_id]

    async def broadcast(
        self, session_id: str, message: Dict[str, Any], sender: Optional[WebSocket] = None
    ) -> None:
        conns = list(self.active_rooms.get(session_id, []))
        for conn in conns:
            if sender is not None and conn is sender:
                continue
            try:
                await conn.send_json(message)
            except Exception:
                self.disconnect(session_id, conn)


room_manager = RoomConnectionManager()


@app.websocket("/ws/room/{session_id}")
async def websocket_interview_room(websocket: WebSocket, session_id: str):
    session = await run_in_threadpool(get_session_with_slot, session_id)
    if not session:
        await websocket.close(code=1008)
        return

    ws_key = websocket.query_params.get("key")
    ws_cookie = websocket.cookies.get(ADMIN_COOKIE_NAME, "")
    is_interviewer_ws = _is_interviewer_for_session(
        session, key=ws_key, cookie_val=ws_cookie
    )

    await room_manager.connect(session_id, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            current = await run_in_threadpool(get_session_with_slot, session_id)
            if not current:
                break

            # Once feedback has been sent, prevent any edits to the shared code document
            if current["status"] == "COMPLETED" or current.get("breakdown_sent_at"):
                continue

            if msg_type == "code_update":
                cleaned_files = _sanitize_workspace_files(
                    data.get("workspace_files"), current["workspace_files"]
                )
                if cleaned_files is None:
                    continue
                active_filename = str(data.get("active_filename") or current["active_filename"])
                if active_filename not in cleaned_files:
                    active_filename = next(iter(cleaned_files.keys()))
                await run_in_threadpool(
                    update_workspace_state,
                    session_id,
                    cleaned_files,
                    active_filename,
                    current["syntax_highlighting_enabled"],
                    current["editor_language"],
                    current["interview_mode"],
                )
                await room_manager.broadcast(
                    session_id,
                    {
                        "type": "code_update",
                        "active_filename": active_filename,
                        "workspace_files": cleaned_files,
                    },
                    sender=websocket,
                )

            elif msg_type == "config_update":
                # Only an authenticated interviewer connection may toggle syntax highlighting or mode
                if not is_interviewer_ws:
                    logger.warning(
                        "Ignored unauthorized config_update on session %s from candidate WebSocket",
                        session_id,
                    )
                    continue
                syntax_enabled = bool(data.get("syntax_highlighting_enabled", False))
                editor_lang = str(data.get("editor_language") or current["editor_language"]).lower()
                if editor_lang not in ALLOWED_LANGUAGE_KEYS:
                    editor_lang = current["editor_language"]
                interview_mode = str(data.get("interview_mode") or current["interview_mode"])
                if interview_mode not in ("classic_dsa", "ai_multifile"):
                    interview_mode = current["interview_mode"]
                await run_in_threadpool(
                    update_workspace_state,
                    session_id,
                    current["workspace_files"],
                    current["active_filename"],
                    syntax_enabled,
                    editor_lang,
                    interview_mode,
                )
                await room_manager.broadcast(
                    session_id,
                    {
                        "type": "config_update",
                        "syntax_highlighting_enabled": syntax_enabled,
                        "editor_language": editor_lang,
                        "interview_mode": interview_mode,
                    },
                    sender=websocket,
                )

            elif msg_type == "file_switch":
                cleaned_files = _sanitize_workspace_files(
                    data.get("workspace_files"), current["workspace_files"]
                )
                if cleaned_files is None:
                    continue
                active_filename = str(data.get("active_filename") or current["active_filename"])
                if active_filename not in cleaned_files:
                    active_filename = next(iter(cleaned_files.keys()))
                await run_in_threadpool(
                    update_workspace_state,
                    session_id,
                    cleaned_files,
                    active_filename,
                    current["syntax_highlighting_enabled"],
                    current["editor_language"],
                    current["interview_mode"],
                )
                await room_manager.broadcast(
                    session_id,
                    {
                        "type": "file_switch",
                        "active_filename": active_filename,
                        "workspace_files": cleaned_files,
                    },
                    sender=websocket,
                )

            elif msg_type == "ai_prompt":
                if current.get("interview_mode") != "ai_multifile" and not is_interviewer_ws:
                    continue
                prompt_text = str(data.get("prompt") or "").strip()[:MAX_AI_PROMPT_CHARS]
                if not prompt_text:
                    continue
                active_filename = str(data.get("active_filename") or current["active_filename"])
                ai_entry = await run_in_threadpool(
                    generate_ai_coding_response,
                    prompt_text,
                    active_filename,
                    current["workspace_files"],
                )
                updated_logs = await run_in_threadpool(
                    append_ai_chat_log, session_id, ai_entry
                )
                await room_manager.broadcast(
                    session_id,
                    {
                        "type": "ai_chat_update",
                        "ai_chat_logs": updated_logs,
                    },
                    sender=None,
                )
    except WebSocketDisconnect:
        room_manager.disconnect(session_id, websocket)
    except Exception as exc:
        logger.warning("WebSocket error in room %s: %s", session_id, exc)
        room_manager.disconnect(session_id, websocket)
