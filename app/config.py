import logging
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

logger = logging.getLogger(__name__)

DEFAULT_ADMIN_PASSWORD = "admin"
DEFAULT_ADMIN_SECRET = "super-secret-interviewer-key"
DEFAULT_REVOLUT_WEBHOOK_SECRET = "wsk_dev_mock_webhook_secret_key"


def _infer_app_env() -> str:
    explicit = os.getenv("APP_ENV", "").strip().lower()
    if explicit:
        return explicit
    base_url = os.getenv("BASE_URL", "http://localhost:8000")
    if any(
        host in base_url for host in ("localhost", "127.0.0.1", "0.0.0.0", "testserver")
    ):
        return "development"
    return "production"


class Settings:
    APP_TITLE: str = os.getenv("APP_TITLE", "StrongHire")
    INTERVIEWER_NAME: str = os.getenv("INTERVIEWER_NAME", "StrongHire")
    HOST_TIMEZONE: str = os.getenv("HOST_TIMEZONE", "Europe/Rome")
    BASE_URL: str = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
    DATABASE_PATH: Path = BASE_DIR / os.getenv("DATABASE_PATH", "interviews.db")
    UPLOADS_DIR: Path = BASE_DIR / "uploads" / "resumes"

    # Environment: "development" (localhost), "production", or "test".
    # When APP_ENV is unset we infer it from BASE_URL so a real deployment
    # cannot silently stay in development mode with mock payments enabled.
    APP_ENV: str = _infer_app_env()
    IS_DEV: bool = APP_ENV in ("development", "dev", "local", "test", "testing")
    IS_PRODUCTION: bool = not IS_DEV

    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD)
    ADMIN_SECRET: str = os.getenv("ADMIN_SECRET", DEFAULT_ADMIN_SECRET)
    ADMIN_EMAIL: str = os.getenv("ADMIN_EMAIL", "coach@example.com")
    ADMIN_SESSION_TTL_SECONDS: int = int(
        os.getenv("ADMIN_SESSION_TTL_SECONDS", str(12 * 60 * 60))
    )
    # Cookies are only marked Secure when the public URL is HTTPS (so local
    # HTTP development and the test client keep working).
    SESSION_COOKIE_SECURE: bool = os.getenv(
        "SESSION_COOKIE_SECURE",
        "true" if BASE_URL.startswith("https://") else "false",
    ).lower() in ("1", "true", "yes")

    PRICE_SINGLE_STANDARD_EUR: int = int(os.getenv("PRICE_SINGLE_STANDARD_EUR", "70"))
    PRICE_BUNDLE3_STANDARD_EUR: int = int(os.getenv("PRICE_BUNDLE3_STANDARD_EUR", "190"))
    BUNDLE3_DISCOUNT_PERCENT: int = round(
        (PRICE_SINGLE_STANDARD_EUR * 3 - PRICE_BUNDLE3_STANDARD_EUR)
        * 100
        / (PRICE_SINGLE_STANDARD_EUR * 3)
    )
    YOUTUBE_DISCOUNT_PERCENT: int = int(os.getenv("YOUTUBE_DISCOUNT_PERCENT", "25"))
    PRICE_SINGLE_YOUTUBE_EUR: int = round(
        PRICE_SINGLE_STANDARD_EUR * (100 - YOUTUBE_DISCOUNT_PERCENT) / 100
    )
    PRICE_BUNDLE3_YOUTUBE_EUR: int = round(
        PRICE_BUNDLE3_STANDARD_EUR * (100 - YOUTUBE_DISCOUNT_PERCENT) / 100
    )
    MIN_BOOKING_HOURS_AHEAD: int = int(os.getenv("MIN_BOOKING_HOURS_AHEAD", "48"))
    SESSION_DURATION_MINUTES: int = int(os.getenv("SESSION_DURATION_MINUTES", "60"))

    REVOLUT_API_KEY: str = os.getenv("REVOLUT_API_KEY", "")
    REVOLUT_WEBHOOK_SECRET: str = os.getenv(
        "REVOLUT_WEBHOOK_SECRET", DEFAULT_REVOLUT_WEBHOOK_SECRET
    )
    REVOLUT_API_BASE: str = os.getenv(
        "REVOLUT_API_BASE", "https://sandbox-merchant.revolut.com"
    ).rstrip("/")
    REVOLUT_API_VERSION: str = os.getenv("REVOLUT_API_VERSION", "2024-09-01")
    # Explicit opt-in for a staging/demo deployment that intentionally runs
    # without a real Revolut account. Never inferred automatically in production.
    ALLOW_MOCK_PAYMENTS: bool = os.getenv("ALLOW_MOCK_PAYMENTS", "false").lower() in (
        "1",
        "true",
        "yes",
    )

    # Bundle links are derived (HMAC) instead of stored in plaintext. Rotating
    # this secret revokes every existing bundle link.
    BUNDLE_TOKEN_SECRET: str = os.getenv("BUNDLE_TOKEN_SECRET", ADMIN_SECRET)

    TRUST_PROXY_HEADERS: bool = os.getenv("TRUST_PROXY_HEADERS", "false").lower() in (
        "1",
        "true",
        "yes",
    )
    # Only these proxy addresses may supply X-Forwarded-For. Empty means the
    # header is ignored entirely, even when TRUST_PROXY_HEADERS is enabled.
    TRUSTED_PROXIES: str = os.getenv("TRUSTED_PROXIES", "")

    SMTP_HOST: str = os.getenv("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER: str = os.getenv("SMTP_USER", "")
    SMTP_APP_PASSWORD: str = os.getenv("SMTP_APP_PASSWORD", "")
    SMTP_DOMAIN: str = os.getenv("SMTP_DOMAIN", "gmail.com")

    GOOGLE_CLIENT_SECRETS_FILE: Path = BASE_DIR / os.getenv(
        "GOOGLE_CLIENT_SECRETS_FILE", "credentials.json"
    )
    GOOGLE_TOKEN_FILE: Path = BASE_DIR / os.getenv("GOOGLE_TOKEN_FILE", "token.json")
    GOOGLE_CALENDAR_ID: str = os.getenv("GOOGLE_CALENDAR_ID", "primary")


settings = Settings()
settings.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


def validate_security_settings() -> None:
    """
    Fail fast rather than silently deploying with development defaults.

    In development we only warn. In production every credential must be
    explicitly overridden, and mock payments require an explicit opt-in.
    """
    problems: list[str] = []

    if settings.ADMIN_PASSWORD == DEFAULT_ADMIN_PASSWORD:
        problems.append("ADMIN_PASSWORD is still the default 'admin'")
    elif len(settings.ADMIN_PASSWORD) < 12:
        problems.append("ADMIN_PASSWORD must be at least 12 characters")

    if settings.ADMIN_SECRET == DEFAULT_ADMIN_SECRET:
        problems.append("ADMIN_SECRET is still the public development default")
    elif len(settings.ADMIN_SECRET) < 32:
        problems.append("ADMIN_SECRET must be at least 32 characters")

    if settings.REVOLUT_WEBHOOK_SECRET == DEFAULT_REVOLUT_WEBHOOK_SECRET:
        problems.append("REVOLUT_WEBHOOK_SECRET is still the public development default")
    elif len(settings.REVOLUT_WEBHOOK_SECRET) < 16:
        problems.append("REVOLUT_WEBHOOK_SECRET must be at least 16 characters")

    if not settings.REVOLUT_API_KEY and not settings.ALLOW_MOCK_PAYMENTS:
        problems.append(
            "REVOLUT_API_KEY is empty; set ALLOW_MOCK_PAYMENTS=true only for an "
            "intentional staging/demo deployment"
        )

    if settings.IS_DEV:
        if problems:
            logger.warning(
                "Development mode is using insecure defaults: %s", "; ".join(problems)
            )
        return

    if problems:
        raise RuntimeError(
            "Refusing to start in production with insecure configuration: "
            + "; ".join(problems)
        )

    if not settings.BASE_URL.startswith("https://"):
        logger.warning(
            "BASE_URL is not HTTPS (%s). Admin cookies will not be marked Secure.",
            settings.BASE_URL,
        )
