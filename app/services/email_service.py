import html
import logging
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from typing import Any, Dict, Optional, Tuple

from app.config import BASE_DIR, settings

logger = logging.getLogger(__name__)


def _esc(val: Any) -> str:
    """HTML-escapes any user-supplied or dynamic string before embedding in HTML emails."""
    return html.escape(str(val if val is not None else ""), quote=True)


def _clean_header(val: Any) -> str:
    """Strips CR/LF control characters to prevent email header errors/injection."""
    s = str(val if val is not None else "")
    return s.replace("\r", " ").replace("\n", " ").strip()


def _format_date_and_time_parts(iso_str: str) -> Tuple[str, str]:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y"), dt.strftime("%H:%M UTC")
    except Exception:
        return _esc(iso_str), ""


def _format_utc_slot(iso_str: str) -> str:
    date_part, time_part = _format_date_and_time_parts(iso_str)
    return f"{date_part} at {time_part}" if time_part else date_part


def _deliver_message(msg: EmailMessage, prefix: str = "email") -> bool:
    """
    Delivers the EmailMessage via SMTP if `SMTP_USER` and `SMTP_APP_PASSWORD` are set:
    - Port 465 uses `SMTP_SSL`.
    - Port 587 (or any other port) uses `SMTP` + `starttls()`.
    When SMTP credentials are not configured (local dev/test mode), writes a full RFC-2822
    `.eml` file into `uploads/email_outbox/` for inspection.
    """
    if not settings.SMTP_USER or not settings.SMTP_APP_PASSWORD:
        now = datetime.now(timezone.utc)
        timestamp = now.strftime("%Y%m%d_%H%M%S_%f")
        outbox_dir = BASE_DIR / "uploads" / "email_outbox"
        outbox_dir.mkdir(parents=True, exist_ok=True)
        eml_path = outbox_dir / f"{timestamp}_{prefix}.eml"
        try:
            eml_path.write_bytes(msg.as_bytes())
        except Exception as exc:
            logger.warning("Could not save local .eml copy: %s", exc)
        logger.info(
            "SMTP_APP_PASSWORD not configured. Saved email to local outbox: %s",
            eml_path,
        )
        return False

    try:
        context = ssl.create_default_context()
        if int(settings.SMTP_PORT) == 465:
            with smtplib.SMTP_SSL(
                settings.SMTP_HOST, settings.SMTP_PORT, context=context, timeout=15
            ) as server:
                server.login(settings.SMTP_USER, settings.SMTP_APP_PASSWORD)
                server.send_message(msg)
        else:
            with smtplib.SMTP(
                settings.SMTP_HOST, settings.SMTP_PORT, timeout=15
            ) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(settings.SMTP_USER, settings.SMTP_APP_PASSWORD)
                server.send_message(msg)
        logger.info("Successfully sent live email (%s) via %s:%s", prefix, settings.SMTP_HOST, settings.SMTP_PORT)
        return True
    except Exception as exc:
        logger.error("Failed to send live SMTP email (%s): %s", prefix, exc)
        return False


def _render_unified_email_card(
    *,
    badge_text: str,
    badge_bg: str = "#ecfdf5",
    badge_border: str = "#a7f3d0",
    badge_color: str = "#047857",
    title: str,
    intro_html: str,
    details_box_html: str,
    extra_section_html: str = "",
    footer_note_html: str = "",
) -> str:
    """
    Renders a single unified, minimalist white card layout (matching the website's
    'You're all set' page aesthetic) used by every email sent from the platform.
    """
    app_title_esc = _esc(settings.APP_TITLE)
    return f"""
    <div style="background-color: #fafafa; padding: 32px 16px; font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #18181b; line-height: 1.6;">
        <div style="max-width: 600px; margin: 0 auto;">
            <!-- Brand Header -->
            <div style="margin-bottom: 16px; display: flex; align-items: center;">
                <span style="display: inline-block; background: #18181b; color: #ffffff; font-family: monospace; font-weight: 700; font-size: 12px; padding: 5px 8px; border-radius: 8px; margin-right: 10px;">&lt;/&gt;</span>
                <span style="font-weight: 700; font-size: 15px; color: #18181b; letter-spacing: -0.01em;">{app_title_esc}</span>
            </div>

            <!-- Main White Card -->
            <div style="background-color: #ffffff; border: 1px solid #e4e4e7; border-radius: 16px; padding: 32px 28px; box-shadow: 0 1px 2px rgba(0,0,0,0.03);">
                <!-- Status Pill Badge -->
                <div style="margin-bottom: 12px;">
                    <span style="display: inline-block; font-size: 11px; font-weight: 600; padding: 4px 10px; border-radius: 9999px; background-color: {badge_bg}; color: {badge_color}; border: 1px solid {badge_border};">
                        &bull; {_esc(badge_text)}
                    </span>
                </div>

                <!-- Heading & Intro -->
                <h1 style="font-size: 24px; font-weight: 700; color: #18181b; margin: 0 0 10px 0; letter-spacing: -0.02em;">
                    {_esc(title)}
                </h1>
                <div style="font-size: 14px; color: #52525b; margin-bottom: 24px;">
                    {intro_html}
                </div>

                <!-- Gray Summary Box + Action Buttons -->
                <div style="background-color: #fafafa; border: 1px solid #e4e4e7; border-radius: 12px; padding: 20px; margin-bottom: 20px; font-size: 14px;">
                    {details_box_html}
                </div>

                {extra_section_html}

                {f'<div style="font-size: 13px; color: #71717a; margin-top: 18px;">{footer_note_html}</div>' if footer_note_html else ''}
            </div>

            <!-- Unified Footer -->
            <div style="text-align: center; font-size: 12px; color: #a1a1aa; margin-top: 18px;">
                &copy; {app_title_esc} &mdash; 1-on-1 Software Engineering Mock Interviews
            </div>
        </div>
    </div>
    """


def send_admin_booking_notification(session: Dict[str, Any]) -> bool:
    """
    Sends a notification email to the Interviewer (ADMIN_EMAIL) when a new interview
    session is created, using the unified platform email format with escaped user fields.
    """
    slot_str = _format_utc_slot(session["start_time"])
    admin_url = f"{settings.BASE_URL}/admin"
    sender_email = _clean_header(settings.SMTP_USER or settings.ADMIN_EMAIL)
    cand_name = _clean_header(session["candidate_name"])
    cand_email = _clean_header(session["candidate_email"])

    msg = EmailMessage()
    msg["From"] = formataddr((_clean_header(settings.APP_TITLE), sender_email))
    msg["To"] = _clean_header(settings.ADMIN_EMAIL)
    msg["Date"] = formatdate(localtime=True)
    msg["Subject"] = f"New Mock Interview Booking — {cand_name} ({slot_str})"

    pkg_label = (
        f"3-Mock Bundle (Session {session.get('bundle_session_number', 1)}/3)"
        if session.get("package_type") == "bundle_3"
        else "Single Session"
    )
    yt_label = (
        "Yes — YouTube Publication Allowed (Discount Applied)"
        if session.get("youtube_consent")
        else "No — Private Session"
    )
    price_eur = int(session.get("price_eur") or 70)

    plain_body = (
        f"Hello {settings.INTERVIEWER_NAME},\n\n"
        f"A new candidate has booked a mock interview slot.\n\n"
        f"• Candidate: {cand_name} ({cand_email})\n"
        f"• Slot: {slot_str}\n"
        f"• Package: {pkg_label} (€{price_eur})\n"
        f"• YouTube Publication Consent: {yt_label}\n"
        f"• Target Role & Company: {session['target_role']} @ {session['target_company']}\n"
        f"• Coding Language: {session['preferred_language']} ({session['leetcode_solved']} LeetCode)\n\n"
        f"Open Admin Dashboard:\n{admin_url}\n"
    )

    details_html = f"""
        <table style="width: 100%; border-collapse: collapse; font-size: 13px; color: #18181b;">
            <tr>
                <td style="padding: 6px 0; color: #71717a; width: 40%;">Candidate</td>
                <td style="padding: 6px 0; font-weight: 600;">{_esc(cand_name)} ({_esc(cand_email)})</td>
            </tr>
            <tr>
                <td style="padding: 6px 0; color: #71717a;">Date &amp; Time</td>
                <td style="padding: 6px 0; font-weight: 600;">{_esc(slot_str)}</td>
            </tr>
            <tr>
                <td style="padding: 6px 0; color: #71717a;">Package &amp; Price</td>
                <td style="padding: 6px 0; font-weight: 600;">{_esc(pkg_label)} &bull; &euro;{price_eur}</td>
            </tr>
            <tr>
                <td style="padding: 6px 0; color: #71717a;">YouTube Consent</td>
                <td style="padding: 6px 0; font-weight: 600;">{_esc(yt_label)}</td>
            </tr>
            <tr>
                <td style="padding: 6px 0; color: #71717a;">Target Role &amp; Company</td>
                <td style="padding: 6px 0; font-weight: 600;">{_esc(session['target_role'])} @ {_esc(session['target_company'])}</td>
            </tr>
            <tr>
                <td style="padding: 6px 0; color: #71717a;">Coding Profile</td>
                <td style="padding: 6px 0; font-weight: 600;">{_esc(session['preferred_language'])} &bull; {int(session['leetcode_solved'])} LeetCode</td>
            </tr>
        </table>
        <div style="margin-top: 16px; padding-top: 14px; border-top: 1px solid #e4e4e7;">
            <a href="{_esc(admin_url)}" style="display: inline-block; background-color: #18181b; color: #ffffff; text-decoration: none; font-weight: 600; font-size: 13px; padding: 10px 18px; border-radius: 10px;">
                Open Admin Control Center &rarr;
            </a>
        </div>
    """

    html_body = _render_unified_email_card(
        badge_text="New Interview Booking",
        title=f"New Session: {cand_name}",
        intro_html="A candidate has scheduled a 1-on-1 SWE Mock Interview session.",
        details_box_html=details_html,
    )

    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")
    return _deliver_message(msg, f"admin_notify_{session['id'][:8]}")


def send_invitation_email(
    session: Dict[str, Any], meet_url: str, ics_bytes: bytes
) -> Tuple[str, str, bool]:
    """
    Sends Email #1 (Thread Starter — or in-thread reply if rescheduled) to the candidate
    using the exact same layout and content as the 'You're all set!' confirmation page.
    """
    session_id = session["id"]
    date_part, time_part = _format_date_and_time_parts(session["start_time"])
    short_date = date_part
    lang_display = str(session.get("preferred_language") or "").capitalize()
    cand_name = _clean_header(session["candidate_name"])
    cand_email = _clean_header(session["candidate_email"])

    existing_msg_id = _clean_header(session.get("email_message_id") or "")
    existing_subject = _clean_header(session.get("email_subject") or "")

    message_id = existing_msg_id or f"<interview-{session_id}@{settings.SMTP_DOMAIN}>"
    subject = existing_subject or f"Mock Interview Session - {cand_name} ({short_date})"
    room_url = f"{settings.BASE_URL}/room/{session_id}"
    ics_url = f"{settings.BASE_URL}/booking/{session_id}/ics"
    sender_email = _clean_header(settings.SMTP_USER or settings.ADMIN_EMAIL)

    msg = EmailMessage()
    msg["From"] = formataddr((_clean_header(settings.INTERVIEWER_NAME), sender_email))
    msg["To"] = formataddr((cand_name, cand_email))
    if settings.ADMIN_EMAIL and settings.ADMIN_EMAIL != cand_email:
        msg["Cc"] = _clean_header(settings.ADMIN_EMAIL)
    msg["Date"] = formatdate(localtime=True)

    if existing_msg_id:
        reply_subj = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        msg["Subject"] = reply_subj
        msg["In-Reply-To"] = existing_msg_id
        msg["References"] = existing_msg_id
    else:
        msg["Subject"] = subject
        msg["Message-ID"] = message_id

    plain_body = (
        f"You're all set, {cand_name}!\n\n"
        f"Your 1-on-1 Software Engineering Mock Interview is confirmed.\n\n"
        f"SESSION DETAILS\n"
        f"• Date & Time:\n  {date_part}\n  {time_part}\n"
        f"• Duration:\n  {settings.SESSION_DURATION_MINUTES} minutes\n  (Coding + Result Discussion)\n"
        f"• Target Role & Company: {session['target_role']} @ {session['target_company']}\n"
        f"• Coding Language: {lang_display}\n"
        f"• Join Google Meet Call: {meet_url}\n"
        f"• Open Live Coding Editor: {room_url}\n\n"
        f"INTERVIEW LOOP & PREPARATION CHECKLIST\n"
        f"• Requirements: Make sure you have a working microphone, a PC (desktop or laptop browser), and stable internet access so we can communicate clearly while coding in real time.\n"
        f"• Arrival Time: Please join 5 minutes before the call starts to quickly verify your audio and setup so we can begin right on time and dedicate the full hour to your session.\n"
        f"• The Loop: You will need to join the Google Meet call and open the Live Coding Editor ({room_url} - the workspace is already open, so feel free to visit the link beforehand to familiarize yourself with it if needed). The session is structured as a 45 min mock interview + 15 min feedback discussion, followed by a written Feedback Breakdown with your score and targeted practice assignments.\n\n"
        f"After our session, I will reply directly in this same email thread with the link to your personalized Feedback Breakdown page.\n"
    )

    details_html = f"""
        <table style="width: 100%; border-collapse: collapse; font-size: 13px; color: #18181b;">
            <tr>
                <td style="padding: 0 12px 14px 0; width: 50%; vertical-align: top;">
                    <div style="font-size: 12px; color: #a1a1aa; margin-bottom: 2px;">Date &amp; Time</div>
                    <div style="font-weight: 600; color: #18181b;">{_esc(date_part)}</div>
                    <div style="font-weight: 600; color: #18181b;">{_esc(time_part)}</div>
                </td>
                <td style="padding: 0 0 14px 12px; width: 50%; vertical-align: top;">
                    <div style="font-size: 12px; color: #a1a1aa; margin-bottom: 2px;">Duration</div>
                    <div style="font-weight: 600; color: #18181b;">{int(settings.SESSION_DURATION_MINUTES)} minutes</div>
                    <div style="font-size: 12px; color: #71717a; font-weight: 400;">(Coding + Result Discussion)</div>
                </td>
            </tr>
            <tr>
                <td style="padding: 0 12px 4px 0; width: 50%; vertical-align: top;">
                    <div style="font-size: 12px; color: #a1a1aa; margin-bottom: 2px;">Target Role &amp; Company</div>
                    <div style="font-weight: 600; color: #18181b;">{_esc(session['target_role'])} @ {_esc(session['target_company'])}</div>
                </td>
                <td style="padding: 0 0 4px 12px; width: 50%; vertical-align: top;">
                    <div style="font-size: 12px; color: #a1a1aa; margin-bottom: 2px;">Coding Language</div>
                    <div style="font-weight: 600; color: #18181b;">{_esc(lang_display)}</div>
                </td>
            </tr>
        </table>

        <div style="margin-top: 18px; padding-top: 16px; border-top: 1px solid #e4e4e7;">
            <table style="width: 100%; border-collapse: collapse;">
                <tr>
                    <td style="width: 50%; padding-right: 6px; padding-bottom: 10px;">
                        <a href="{_esc(meet_url)}" style="display: block; width: 100%; box-sizing: border-box; background-color: #18181b; color: #ffffff; text-decoration: none; font-weight: 600; font-size: 12px; padding: 12px 14px; border-radius: 12px; text-align: center;">
                            <span style="color: #34d399;">&bull;</span>&nbsp; Join Google Meet Call &nbsp;&nearr;
                        </a>
                    </td>
                    <td style="width: 50%; padding-left: 6px; padding-bottom: 10px;">
                        <a href="{_esc(room_url)}" style="display: block; width: 100%; box-sizing: border-box; background-color: #18181b; color: #ffffff; text-decoration: none; font-weight: 600; font-size: 12px; padding: 12px 14px; border-radius: 12px; text-align: center;">
                            <span style="color: #a1a1aa; font-family: monospace;">&gt;</span>&nbsp; Open Live Coding Editor &nbsp;&rarr;
                        </a>
                    </td>
                </tr>
                <tr>
                    <td colspan="2">
                        <a href="{_esc(ics_url)}" style="display: block; width: 100%; box-sizing: border-box; background-color: #ffffff; color: #18181b; border: 1px solid #e4e4e7; text-decoration: none; font-weight: 600; font-size: 12px; padding: 10px 16px; border-radius: 12px; text-align: center;">
                            Download Calendar Invite (.ics)
                        </a>
                    </td>
                </tr>
            </table>
        </div>
    """

    bundle_token = session.get("bundle_access_token") or ""
    bundle_credits_left = int(session.get("bundle_credits_remaining") or 0)
    bundle_code = session.get("bundle_code") or ""
    bundle_section_html = ""
    if bundle_token and bundle_credits_left > 0:
        bundle_url = f"{settings.BASE_URL}/bundle/b/{bundle_token}"
        plain_body += (
            f"\n3-MOCK BUNDLE CREDITS ({bundle_credits_left} of 3 REMAINING)\n"
            f"• Reference Code: {bundle_code}\n"
            f"• Schedule Your Next Bundle Session (Private Link): {bundle_url}\n"
        )
        bundle_section_html = f"""
        <div style="background-color: #f8fafc; border: 1px solid #e4e4e7; border-radius: 12px; padding: 18px 20px; margin-bottom: 16px;">
            <div style="font-size: 13px; font-weight: 700; color: #18181b; margin-bottom: 4px;">
                3-Mock Bundle &bull; {bundle_credits_left} of 3 Sessions Remaining
            </div>
            <div style="font-size: 12px; color: #52525b; margin-bottom: 12px;">
                Reference Code: <strong style="font-family: monospace; color: #18181b;">{_esc(bundle_code)}</strong> &mdash; Use your personal link below (or request it via email on the booking page) whenever you are ready to schedule your next session without extra payment.
            </div>
            <a href="{_esc(bundle_url)}" style="display: block; width: 100%; box-sizing: border-box; background-color: #18181b; color: #ffffff; text-decoration: none; font-weight: 600; font-size: 12px; padding: 11px 16px; border-radius: 10px; text-align: center;">
                Schedule Next Bundle Session ({bundle_credits_left} Remaining) &rarr;
            </a>
        </div>
        """

    checklist_html = f"""
        {bundle_section_html}
        <div style="background-color: #ffffff; border: 1px solid #e4e4e7; border-radius: 12px; padding: 18px 20px;">
            <div style="font-size: 14px; font-weight: 700; color: #18181b; margin-bottom: 10px;">
                Interview Loop &amp; Preparation Checklist
            </div>
            <ul style="margin: 0; padding-left: 18px; font-size: 13px; color: #52525b; line-height: 1.65;">
                <li style="margin-bottom: 8px;"><strong style="color: #18181b;">Requirements:</strong> Make sure you have a working microphone, a PC (desktop or laptop browser), and stable internet access so we can communicate clearly while coding in real time.</li>
                <li style="margin-bottom: 8px;"><strong style="color: #18181b;">Arrival Time:</strong> Please join <strong>5 minutes before</strong> the call starts to quickly verify your audio and setup so we can begin right on time and dedicate the full hour to your session.</li>
                <li style="margin-bottom: 0;"><strong style="color: #18181b;">The Loop:</strong> You will need to join the Google Meet call and open the <a href="{_esc(room_url)}" style="color: #18181b; font-weight: 600; text-decoration: underline;">Live Coding Editor</a> (the workspace is already open, so feel free to visit the link beforehand to familiarize yourself with it if needed). The session is structured as a <strong>45 min mock interview + 15 min feedback discussion</strong>, followed by a written <em>Feedback Breakdown</em> with your score and targeted practice assignments.</li>
            </ul>
        </div>
    """

    html_body = _render_unified_email_card(
        badge_text="Booking Confirmed • Invitation & Room Ready",
        title=f"You're all set, {cand_name}!",
        intro_html="Your 1-on-1 Software Engineering Mock Interview is confirmed, and a calendar <code>.ics</code> invite is attached to this email. Below you will find your session summary, direct links to the video call and shared coding workspace, and how to prepare.",
        details_box_html=details_html,
        extra_section_html=checklist_html,
        footer_note_html="After our session, you will receive a reply <strong>directly in this same email thread</strong> with the link to your personalized <em>Feedback Breakdown</em> page.",
    )

    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")

    msg.add_attachment(
        ics_bytes,
        maintype="text",
        subtype="calendar",
        filename="invite.ics",
        params={"method": "REQUEST", "charset": "UTF-8"},
    )

    sent_live = _deliver_message(msg, f"invite_{session_id[:8]}")
    return message_id, subject, sent_live


def send_breakdown_email(session: Dict[str, Any]) -> bool:
    """
    Sends Email #2 (Post-Interview Feedback Breakdown Link) in the exact same email thread
    using the unified card layout.
    """
    session_id = session["id"]
    cand_name = _clean_header(session["candidate_name"])
    cand_email = _clean_header(session["candidate_email"])
    original_msg_id = _clean_header(
        session.get("email_message_id") or f"<interview-{session_id}@{settings.SMTP_DOMAIN}>"
    )
    original_subject = _clean_header(
        session.get("email_subject") or f"Mock Interview Session - {cand_name}"
    )
    reply_subject = (
        original_subject
        if original_subject.lower().startswith("re:")
        else f"Re: {original_subject}"
    )

    sender_email = _clean_header(settings.SMTP_USER or settings.ADMIN_EMAIL)
    feedback_url = f"{settings.BASE_URL}/feedback/{session_id}"

    msg = EmailMessage()
    msg["From"] = formataddr((_clean_header(settings.INTERVIEWER_NAME), sender_email))
    msg["To"] = formataddr((cand_name, cand_email))
    if settings.ADMIN_EMAIL and settings.ADMIN_EMAIL != cand_email:
        msg["Cc"] = _clean_header(settings.ADMIN_EMAIL)
    msg["Date"] = formatdate(localtime=True)
    msg["Subject"] = reply_subject
    msg["In-Reply-To"] = original_msg_id
    msg["References"] = original_msg_id

    verdict = session.get("final_verdict") or "Completed"

    plain_body = (
        f"Hi {cand_name},\n\n"
        f"Thank you for completing your mock interview session! Your personalized Feedback Breakdown & Judgement page is now ready.\n\n"
        f"View your full evaluation and code snapshot here:\n"
        f"{feedback_url}\n\n"
        f"• Overall Result: {verdict}\n"
        f"• Includes section-by-section scores (1–5), detailed interviewer observations, targeted assignments, and your code snapshot.\n"
    )

    details_html = f"""
        <table style="width: 100%; border-collapse: collapse; font-size: 13px; color: #18181b;">
            <tr>
                <td style="padding: 6px 0; color: #71717a; width: 42%;">Final Judgement</td>
                <td style="padding: 6px 0; font-weight: 700;">{_esc(verdict)}</td>
            </tr>
            <tr>
                <td style="padding: 6px 0; color: #71717a;">Target Role &amp; Company</td>
                <td style="padding: 6px 0; font-weight: 600;">{_esc(session['target_role'])} @ {_esc(session['target_company'])}</td>
            </tr>
            <tr>
                <td style="padding: 6px 0; color: #71717a;">Included in Report</td>
                <td style="padding: 6px 0; font-weight: 600;">Scores (1–5), Action Plan &amp; Code Snapshot</td>
            </tr>
        </table>
        <div style="margin-top: 18px; padding-top: 16px; border-top: 1px solid #e4e4e7;">
            <a href="{_esc(feedback_url)}" style="display: inline-block; background-color: #18181b; color: #ffffff; text-decoration: none; font-weight: 600; font-size: 13px; padding: 11px 18px; border-radius: 10px;">
                Open Your Feedback Breakdown &rarr;
            </a>
        </div>
    """

    bundle_token = session.get("bundle_access_token") or ""
    bundle_credits_left = int(session.get("bundle_credits_remaining") or 0)
    bundle_code = session.get("bundle_code") or ""
    bundle_extra_html = ""
    if bundle_token and bundle_credits_left > 0:
        bundle_url = f"{settings.BASE_URL}/bundle/b/{bundle_token}"
        plain_body += (
            f"\n• 3-Mock Bundle ({bundle_credits_left} of 3 sessions remaining | Ref: {bundle_code}):\n"
            f"  Schedule your next session here: {bundle_url}\n"
        )
        bundle_extra_html = f"""
        <div style="background-color: #ffffff; border: 1px solid #e4e4e7; border-radius: 12px; padding: 16px 18px;">
            <div style="font-size: 13px; font-weight: 700; color: #18181b; margin-bottom: 4px;">
                Ready for Your Next Mock? ({bundle_credits_left} of 3 Bundle Sessions Remaining)
            </div>
            <div style="font-size: 12px; color: #52525b; margin-bottom: 10px;">
                Reference Code: <strong style="font-family: monospace; color: #18181b;">{_esc(bundle_code)}</strong> &mdash; Pick your next date and time whenever you are ready.
            </div>
            <a href="{_esc(bundle_url)}" style="display: inline-block; background-color: #18181b; color: #ffffff; text-decoration: none; font-weight: 600; font-size: 12px; padding: 10px 16px; border-radius: 10px;">
                Schedule Next Bundle Session ({bundle_credits_left} Remaining) &rarr;
            </a>
        </div>
        """

    html_body = _render_unified_email_card(
        badge_text="Feedback Breakdown Ready",
        title=f"Your Feedback Breakdown is Ready, {cand_name}",
        intro_html="Great job completing our mock interview session! Your detailed <strong>Feedback Breakdown</strong>, final judgement, code snapshot, and targeted practice assignments are now live.",
        details_box_html=details_html,
        extra_section_html=bundle_extra_html,
        footer_note_html=f'Direct link: <a href="{_esc(feedback_url)}" style="color: #18181b;">{_esc(feedback_url)}</a>',
    )

    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")

    return _deliver_message(msg, f"breakdown_{session_id[:8]}")


def send_rejection_email(session: Dict[str, Any]) -> bool:
    """
    Sends a cancellation/rejection email in the unified card format.
    """
    session_id = session["id"]
    slot_str = _format_utc_slot(session["start_time"])
    sender_email = _clean_header(settings.SMTP_USER or settings.ADMIN_EMAIL)
    cand_name = _clean_header(session["candidate_name"])
    cand_email = _clean_header(session["candidate_email"])
    book_url = f"{settings.BASE_URL}/book"

    original_msg_id = _clean_header(session.get("email_message_id") or "")
    original_subject = _clean_header(session.get("email_subject") or "")

    msg = EmailMessage()
    msg["From"] = formataddr((_clean_header(settings.APP_TITLE), sender_email))
    msg["To"] = formataddr((cand_name, cand_email))
    if settings.ADMIN_EMAIL and settings.ADMIN_EMAIL != cand_email:
        msg["Cc"] = _clean_header(settings.ADMIN_EMAIL)
    msg["Date"] = formatdate(localtime=True)

    if original_msg_id and original_subject:
        msg["Subject"] = (
            original_subject
            if original_subject.lower().startswith("re:")
            else f"Re: {original_subject}"
        )
        msg["In-Reply-To"] = original_msg_id
        msg["References"] = original_msg_id
    else:
        msg["Subject"] = f"Update on Your Mock Interview Booking ({slot_str})"

    plain_body = (
        f"Hi {cand_name},\n\n"
        f"Your mock interview session for {slot_str} has been cancelled.\n\n"
        f"If you would like to select another available date and hour, you can book a new slot here:\n"
        f"{book_url}\n"
    )

    details_html = f"""
        <table style="width: 100%; border-collapse: collapse; font-size: 13px; color: #18181b;">
            <tr>
                <td style="padding: 6px 0; color: #71717a; width: 42%;">Cancelled Slot</td>
                <td style="padding: 6px 0; font-weight: 600;">{_esc(slot_str)}</td>
            </tr>
            <tr>
                <td style="padding: 6px 0; color: #71717a;">Target Role &amp; Company</td>
                <td style="padding: 6px 0; font-weight: 600;">{_esc(session['target_role'])} @ {_esc(session['target_company'])}</td>
            </tr>
        </table>
        <div style="margin-top: 18px; padding-top: 16px; border-top: 1px solid #e4e4e7;">
            <a href="{_esc(book_url)}" style="display: inline-block; background-color: #18181b; color: #ffffff; text-decoration: none; font-weight: 600; font-size: 13px; padding: 11px 18px; border-radius: 10px;">
                Select Another Time Slot &rarr;
            </a>
        </div>
    """

    html_body = _render_unified_email_card(
        badge_text="Session Cancelled",
        badge_bg="#fef2f2",
        badge_border="#fecaca",
        badge_color="#b91c1c",
        title="Update on Your Mock Interview",
        intro_html=f"Hi <strong>{_esc(cand_name)}</strong>, your mock interview booking for <strong>{_esc(slot_str)}</strong> could not be completed and has been cancelled. If you would like to pick a different time slot, you can schedule a new appointment below.",
        details_box_html=details_html,
    )

    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")

    return _deliver_message(msg, f"rejected_{session_id[:8]}")


def send_reschedule_request_email(session: Dict[str, Any]) -> bool:
    """
    Sends a reschedule link email in the exact same email thread and unified card format.
    """
    session_id = session["id"]
    slot_str = _format_utc_slot(session["start_time"])
    sender_email = _clean_header(settings.SMTP_USER or settings.ADMIN_EMAIL)
    cand_name = _clean_header(session["candidate_name"])
    cand_email = _clean_header(session["candidate_email"])
    reschedule_url = f"{settings.BASE_URL}/reschedule/{session_id}"

    original_msg_id = _clean_header(
        session.get("email_message_id") or f"<interview-{session_id}@{settings.SMTP_DOMAIN}>"
    )
    original_subject = _clean_header(
        session.get("email_subject") or f"Mock Interview Session - {cand_name}"
    )

    msg = EmailMessage()
    msg["From"] = formataddr((_clean_header(settings.INTERVIEWER_NAME), sender_email))
    msg["To"] = formataddr((cand_name, cand_email))
    if settings.ADMIN_EMAIL and settings.ADMIN_EMAIL != cand_email:
        msg["Cc"] = _clean_header(settings.ADMIN_EMAIL)
    msg["Date"] = formatdate(localtime=True)
    msg["Subject"] = (
        original_subject
        if original_subject.lower().startswith("re:")
        else f"Re: {original_subject}"
    )
    msg["In-Reply-To"] = original_msg_id
    msg["References"] = original_msg_id

    plain_body = (
        f"Hi {cand_name},\n\n"
        f"We need to reschedule your mock interview session originally booked for {slot_str}.\n\n"
        f"Please use the link below to select a new available day and hour on the calendar (no additional payment is required):\n"
        f"{reschedule_url}\n"
    )

    details_html = f"""
        <table style="width: 100%; border-collapse: collapse; font-size: 13px; color: #18181b;">
            <tr>
                <td style="padding: 6px 0; color: #71717a; width: 42%;">Previous Slot</td>
                <td style="padding: 6px 0; font-weight: 600;">{_esc(slot_str)}</td>
            </tr>
            <tr>
                <td style="padding: 6px 0; color: #71717a;">Target Role &amp; Company</td>
                <td style="padding: 6px 0; font-weight: 600;">{_esc(session['target_role'])} @ {_esc(session['target_company'])}</td>
            </tr>
            <tr>
                <td style="padding: 6px 0; color: #71717a;">Payment Status</td>
                <td style="padding: 6px 0; font-weight: 600;">Confirmed (No Extra Payment Required)</td>
            </tr>
        </table>
        <div style="margin-top: 18px; padding-top: 16px; border-top: 1px solid #e4e4e7;">
            <a href="{_esc(reschedule_url)}" style="display: inline-block; background-color: #18181b; color: #ffffff; text-decoration: none; font-weight: 600; font-size: 13px; padding: 11px 18px; border-radius: 10px;">
                Choose a New Date &amp; Time &rarr;
            </a>
        </div>
    """

    html_body = _render_unified_email_card(
        badge_text="Reschedule Requested",
        badge_bg="#fffbeb",
        badge_border="#fde68a",
        badge_color="#b45309",
        title=f"Reschedule Your Mock Interview, {cand_name}",
        intro_html=f"We need to reschedule your mock interview session originally scheduled for <strong>{_esc(slot_str)}</strong>. Please pick a new day and hour below &mdash; your session details and payment are preserved automatically.",
        details_box_html=details_html,
        footer_note_html=f'Direct reschedule link: <a href="{_esc(reschedule_url)}" style="color: #18181b;">{_esc(reschedule_url)}</a>',
    )

    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")

    return _deliver_message(msg, f"reschedule_{session_id[:8]}")


def send_bundle_lookup_email(bundles: list) -> bool:
    """
    Sends the candidate their active 3-Mock Bundle secret booking link(s)
    when they request a lookup by email on `/book`.
    """
    if not bundles:
        return False
    first = bundles[0]
    candidate_name = _clean_header(first["candidate_name"])
    candidate_email = _clean_header(first["candidate_email"])
    sender_email = _clean_header(settings.SMTP_USER or settings.ADMIN_EMAIL)

    msg = EmailMessage()
    msg["From"] = formataddr((_clean_header(settings.INTERVIEWER_NAME), sender_email))
    msg["To"] = formataddr((candidate_name, candidate_email))
    msg["Date"] = formatdate(localtime=True)
    msg["Subject"] = f"Your 3-Mock Bundle Booking Link — {_clean_header(settings.APP_TITLE)}"

    plain_lines = [
        f"Hi {candidate_name},\n",
        "Here is your private 3-Mock Bundle link to schedule your remaining sessions:\n",
    ]
    rows_html = ""
    for b in bundles:
        tok = b.get("access_token") or ""
        b_url = f"{settings.BASE_URL}/bundle/b/{tok}"
        plain_lines.append(
            f"• Reference {b['bundle_code']} ({b['credits_remaining']} of {b['total_sessions']} sessions remaining): {b_url}"
        )
        rows_html += f"""
        <div style="margin-bottom: 14px; padding-bottom: 14px; border-bottom: 1px solid #e4e4e7;">
            <div style="font-size: 13px; font-weight: 700; color: #18181b;">
                Reference Code: <span style="font-family: monospace;">{_esc(b['bundle_code'])}</span> &bull; {int(b['credits_remaining'])} of {int(b['total_sessions'])} Sessions Remaining
            </div>
            <div style="font-size: 12px; color: #71717a; margin: 4px 0 10px 0;">
                Target: {_esc(b['target_role'])} @ {_esc(b['target_company'])}
            </div>
            <a href="{_esc(b_url)}" style="display: inline-block; background-color: #18181b; color: #ffffff; text-decoration: none; font-weight: 600; font-size: 12px; padding: 10px 16px; border-radius: 10px;">
                Schedule Next Bundle Session ({int(b['credits_remaining'])} Remaining) &rarr;
            </a>
        </div>
        """

    html_body = _render_unified_email_card(
        badge_text="3-Mock Bundle Active",
        title=f"Your Bundle Booking Link, {candidate_name}",
        intro_html="Use your personal secret bundle link below to schedule your remaining 1-on-1 mock interview sessions without any additional checkout. Keep this link private.",
        details_box_html=rows_html,
    )

    msg.set_content("\n".join(plain_lines))
    msg.add_alternative(html_body, subtype="html")
    return _deliver_message(msg, f"bundle_lookup_{first['id'][:8]}")
