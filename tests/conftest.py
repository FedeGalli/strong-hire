import os
from pathlib import Path
import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def prevent_external_test_side_effects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """
    Global autouse fixture for all tests:
    1. Sets TESTING="1" environment variable.
    2. Disables SMTP password and user to prevent any live SMTP connections.
    3. Mocks Google Calendar API / Meet creation to avoid polluting the user's real calendar.
    4. Mocks _deliver_message to prevent outgoing email transmission.
    """
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setattr(settings, "SMTP_APP_PASSWORD", "")
    monkeypatch.setattr(settings, "SMTP_USER", "")
    monkeypatch.setattr(
        settings, "GOOGLE_TOKEN_FILE", tmp_path / "non_existent_token.json"
    )

    # Mock external delivery calls
    monkeypatch.setattr(
        "app.services.calendar_meet.create_google_meet_event",
        lambda session, custom_meet_url=None: (
            f"test-cal-event-{session.get('id', 'mock')[:8]}",
            custom_meet_url or "https://meet.google.com/test-mock-interview",
        ),
    )
    monkeypatch.setattr(
        "app.services.email_service._deliver_message",
        lambda msg, prefix="email": False,
    )
