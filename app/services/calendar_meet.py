import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from icalendar import Calendar, Event, vCalAddress, vText

from app.config import settings

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def create_google_meet_event(
    session: Dict[str, Any], custom_meet_url: Optional[str] = None
) -> Tuple[Optional[str], str]:
    """
    Creates a Google Calendar event on the interviewer's calendar with an auto-generated
    1-on-1 Google Meet link and native Google Calendar invite (sendUpdates='all').

    If token.json is not yet configured locally, falls back gracefully to a provided
    custom_meet_url or a standard Google Meet starter link.
    """
    room_url = f"{settings.BASE_URL}/room/{session['id']}"
    candidate_name = session["candidate_name"]
    candidate_email = session["candidate_email"]
    start_time_str = session["start_time"]
    end_time_str = session["end_time"]

    if settings.GOOGLE_TOKEN_FILE.exists():
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            creds = Credentials.from_authorized_user_file(
                str(settings.GOOGLE_TOKEN_FILE), SCOPES
            )
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                settings.GOOGLE_TOKEN_FILE.write_text(creds.to_json())

            service = build("calendar", "v3", credentials=creds)

            description = (
                f"1-on-1 SWE Mock Interview with {settings.INTERVIEWER_NAME}\n\n"
                f"• Live Coding Interview Room: {room_url}\n"
                f"• Candidate: {candidate_name} ({candidate_email})\n"
                f"• Target Role: {session.get('target_role', 'SWE')} @ {session.get('target_company', 'Tech')}\n"
                f"• Preferred Language: {session.get('preferred_language', 'python')}\n\n"
                f"Please open both the Google Meet call and the Live Coding Interview Room link above at the start of our session."
            )

            event_body = {
                "summary": f"SWE Mock Interview: {candidate_name} x {settings.INTERVIEWER_NAME}",
                "description": description,
                "start": {"dateTime": start_time_str, "timeZone": "UTC"},
                "end": {"dateTime": end_time_str, "timeZone": "UTC"},
                "attendees": [
                    {"email": candidate_email, "displayName": candidate_name},
                ],
                "conferenceData": {
                    "createRequest": {
                        "requestId": f"mock-{session['id']}",
                        "conferenceSolutionKey": {"type": "hangoutsMeet"},
                    }
                },
                "reminders": {
                    "useDefault": False,
                    "overrides": [
                        {"method": "email", "minutes": 60},
                        {"method": "popup", "minutes": 15},
                    ],
                },
            }

            created_event = (
                service.events()
                .insert(
                    calendarId=settings.GOOGLE_CALENDAR_ID,
                    body=event_body,
                    conferenceDataVersion=1,
                    sendUpdates="all",
                )
                .execute()
            )

            event_id = created_event.get("id")
            meet_url = created_event.get("hangoutLink") or custom_meet_url or "https://meet.google.com/new"
            logger.info("Created Google Calendar event %s with Meet URL %s", event_id, meet_url)
            return event_id, meet_url
        except Exception as exc:
            logger.warning("Google Calendar API call failed (%s); using fallback Meet URL.", exc)

    fallback_meet = (
        custom_meet_url.strip()
        if custom_meet_url and custom_meet_url.strip()
        else f"https://meet.google.com/lookup/mock-{session['id'][:8]}"
    )
    return None, fallback_meet


def generate_ics_invite(session: Dict[str, Any], meet_url: str) -> bytes:
    """
    Generates an RFC-5545 compliant .ics calendar invitation (METHOD:REQUEST)
    compatible with Apple Calendar, Outlook, ProtonMail, Yahoo, and Google Calendar.
    """
    cal = Calendar()
    cal.add("prodid", "-//SWE Mock Interview Platform//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "REQUEST")

    event = Event()
    start_dt = datetime.fromisoformat(session["start_time"].replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(session["end_time"].replace("Z", "+00:00"))
    now_dt = datetime.now(timezone.utc)

    room_url = f"{settings.BASE_URL}/room/{session['id']}"
    summary = f"SWE Mock Interview — {session['candidate_name']} & {settings.INTERVIEWER_NAME}"
    description = (
        f"Your 1-on-1 SWE Mock Interview is confirmed!\n\n"
        f"1) Video Call (Google Meet):\n{meet_url}\n\n"
        f"2) Standalone Live Coding Room:\n{room_url}\n\n"
        f"Candidate Details:\n"
        f"- Target Company: {session.get('target_company') or 'General SWE'}\n"
        f"- Target Role: {session.get('target_role') or 'SWE'}\n"
        f"- Preferred Language: {session.get('preferred_language') or 'python'}\n"
    )

    event.add("uid", f"swe-mock-{session['id']}@{settings.SMTP_DOMAIN}")
    event.add("dtstamp", now_dt)
    event.add("dtstart", start_dt)
    event.add("dtend", end_dt)
    event.add("summary", summary)
    event.add("description", description)
    event.add("location", f"Google Meet ({meet_url}) & Coding Room ({room_url})")
    event.add("status", "CONFIRMED")
    event.add("sequence", 0)

    organizer_email = settings.SMTP_USER or settings.ADMIN_EMAIL
    organizer = vCalAddress(f"MAILTO:{organizer_email}")
    organizer.params["cn"] = vText(settings.INTERVIEWER_NAME)
    event["organizer"] = organizer

    attendee = vCalAddress(f"MAILTO:{session['candidate_email']}")
    attendee.params["cn"] = vText(session["candidate_name"])
    attendee.params["ROLE"] = vText("REQ-PARTICIPANT")
    attendee.params["PARTSTAT"] = vText("NEEDS-ACTION")
    attendee.params["RSVP"] = vText("TRUE")
    event.add("attendee", attendee, encode=0)

    cal.add_component(event)
    return cal.to_ical()
