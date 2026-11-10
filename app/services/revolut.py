import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Any, Dict, Mapping, Optional, Tuple

import httpx

from app.config import settings
from app.database import set_order_cancel_hook

logger = logging.getLogger(__name__)

WEBHOOK_MAX_AGE_SECONDS = 300  # 5-minute replay window

# In-memory store for fake/sandbox local orders when REVOLUT_API_KEY is not configured
_FAKE_ORDERS: Dict[str, Dict[str, Any]] = {}


def sign_revolut_webhook_payload(
    raw_body: bytes,
    timestamp_ms: Optional[int] = None,
    signing_secret: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Computes `(timestamp_header, signature_header)` using Revolut's official
    `v1.{timestamp_ms}.{raw_body}` HMAC-SHA256 signing scheme.
    Used both by tests and by the local dev sandbox simulator.
    """
    ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
    secret = (signing_secret if signing_secret is not None else settings.REVOLUT_WEBHOOK_SECRET) or ""
    payload_to_sign = f"v1.{ts}.".encode("utf-8") + raw_body
    digest = hmac.new(
        secret.encode("utf-8"), payload_to_sign, hashlib.sha256
    ).hexdigest()
    return ts, f"v1={digest}"


def verify_revolut_webhook(
    headers: Mapping[str, str],
    raw_body: bytes,
    signing_secret: Optional[str] = None,
    max_age_seconds: int = WEBHOOK_MAX_AGE_SECONDS,
) -> bool:
    """
    Verifies a Revolut Merchant API webhook request:
    1. Checks `Revolut-Request-Timestamp` (milliseconds) is within `max_age_seconds`.
    2. Computes `v1=` + HMAC_SHA256(signing_secret, f"v1.{timestamp}.{raw_body}").
    3. Compares in constant time against comma-separated signatures in `Revolut-Signature`.
    """
    secret = (signing_secret if signing_secret is not None else settings.REVOLUT_WEBHOOK_SECRET) or ""
    if not secret:
        return False

    lower_headers = {k.lower(): v for k, v in headers.items()}
    ts_str = lower_headers.get("revolut-request-timestamp", "").strip()
    sig_header = lower_headers.get("revolut-signature", "").strip()
    if not ts_str or not sig_header:
        return False

    try:
        ts_val = int(ts_str)
    except ValueError:
        return False

    # Revolut timestamps are UNIX milliseconds (fallback if seconds passed)
    ts_seconds = ts_val / 1000.0 if ts_val > 10_000_000_000 else float(ts_val)
    now_seconds = time.time()
    if abs(now_seconds - ts_seconds) > max_age_seconds:
        logger.warning("Rejected Revolut webhook outside replay window: ts=%s", ts_str)
        return False

    payload_to_sign = f"v1.{ts_str}.".encode("utf-8") + raw_body
    expected_sig = "v1=" + hmac.new(
        secret.encode("utf-8"), payload_to_sign, hashlib.sha256
    ).hexdigest()

    for candidate_sig in (s.strip() for s in sig_header.split(",") if s.strip()):
        if hmac.compare_digest(expected_sig, candidate_sig):
            return True
    return False


def create_revolut_order(session: Dict[str, Any]) -> Tuple[str, str]:
    """
    Creates a Revolut Merchant order for `session` and returns `(order_id, checkout_url)`.
    - When `settings.REVOLUT_API_KEY` is set, calls Revolut Merchant API (`POST /api/orders`).
    - When `REVOLUT_API_KEY` is empty (local/dev mode), uses the built-in fake provider
      and returns an order_id + local checkout URL (`/booking/{session_id}/payment?order_id=...`).
    """
    session_id = session["id"]
    price_eur = int(session.get("price_eur") or 70)
    amount_minor = price_eur * 100
    redirect_url = f"{settings.BASE_URL}/booking/{session_id}/payment?checkout_return=1"

    if settings.REVOLUT_API_KEY:
        try:
            resp = httpx.post(
                f"{settings.REVOLUT_API_BASE}/api/orders",
                headers={
                    "Authorization": f"Bearer {settings.REVOLUT_API_KEY}",
                    "Revolut-Api-Version": settings.REVOLUT_API_VERSION,
                    "Content-Type": "application/json",
                },
                json={
                    "amount": amount_minor,
                    "currency": "EUR",
                    "merchant_order_ext_ref": session_id,
                    "customer": {"email": session.get("candidate_email", "")},
                    "description": f"{settings.APP_TITLE} Mock Interview ({session.get('package_type', 'single')})",
                    "redirect_url": redirect_url,
                },
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
            order_id = str(data.get("id") or "")
            checkout_url = str(data.get("checkout_url") or redirect_url)
            if order_id:
                return order_id, checkout_url
        except Exception as exc:
            logger.error("Revolut Merchant API create_order failed: %s", exc)
            raise RuntimeError("Could not initialize payment with Revolut.") from exc

    # Fake/local provider when REVOLUT_API_KEY is not configured
    order_id = f"rev_ord_{uuid.uuid4().hex[:16]}"
    checkout_url = f"/booking/{session_id}/payment?order_id={order_id}"
    _FAKE_ORDERS[order_id] = {
        "id": order_id,
        "state": "PENDING",
        "amount": amount_minor,
        "currency": "EUR",
        "merchant_order_ext_ref": session_id,
        "checkout_url": checkout_url,
    }
    return order_id, checkout_url


def cancel_revolut_order(order_id: str) -> bool:
    """
    Cancels an unpaid Revolut order when the 30-minute hold expires so the candidate
    can no longer pay for an expired slot.
    """
    if not order_id:
        return False

    if settings.REVOLUT_API_KEY and not order_id.startswith("rev_ord_"):
        try:
            resp = httpx.post(
                f"{settings.REVOLUT_API_BASE}/api/orders/{order_id}/cancel",
                headers={
                    "Authorization": f"Bearer {settings.REVOLUT_API_KEY}",
                    "Revolut-Api-Version": settings.REVOLUT_API_VERSION,
                },
                timeout=8.0,
            )
            return resp.status_code in (200, 204)
        except Exception as exc:
            logger.warning("Could not cancel Revolut order %s: %s", order_id, exc)
            return False

    fake = _FAKE_ORDERS.get(order_id)
    if fake and fake.get("state") == "PENDING":
        fake["state"] = "CANCELLED"
        return True
    return False


def get_fake_order(order_id: str) -> Optional[Dict[str, Any]]:
    return _FAKE_ORDERS.get(order_id)


def build_completed_webhook_request(
    order_id: str,
    session_id: str,
    amount_minor: Optional[int] = None,
    currency: str = "EUR",
    timestamp_ms: Optional[int] = None,
    signing_secret: Optional[str] = None,
) -> Tuple[bytes, Dict[str, str]]:
    """
    Builds a signed `(raw_body_bytes, headers)` pair for an `ORDER_COMPLETED`
    webhook event matching Revolut's Merchant API payload format.
    """
    fake = _FAKE_ORDERS.get(order_id)
    if fake:
        fake["state"] = "COMPLETED"
        if amount_minor is None:
            amount_minor = int(fake.get("amount", 7000))

    payload: Dict[str, Any] = {
        "event": "ORDER_COMPLETED",
        "order_id": order_id,
        "merchant_order_ext_ref": session_id,
        "currency": currency,
    }
    if amount_minor is not None:
        payload["amount"] = int(amount_minor)

    raw_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ts, sig = sign_revolut_webhook_payload(
        raw_bytes, timestamp_ms=timestamp_ms, signing_secret=signing_secret
    )
    headers = {
        "Content-Type": "application/json",
        "Revolut-Request-Timestamp": ts,
        "Revolut-Signature": sig,
    }
    return raw_bytes, headers


set_order_cancel_hook(cancel_revolut_order)
