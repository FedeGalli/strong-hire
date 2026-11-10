import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


class Settings:
    APP_TITLE: str = os.getenv("APP_TITLE", "StrongHire")
    INTERVIEWER_NAME: str = os.getenv("INTERVIEWER_NAME", "StrongHire")
    INTERVIEWER_HEADLINE: str = os.getenv(
        "INTERVIEWER_HEADLINE", "1-on-1 SWE Mock Interviews & Personalized Study Plans"
    )
    HOST_TIMEZONE: str = os.getenv("HOST_TIMEZONE", "Europe/Rome")
    BASE_URL: str = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
    DATABASE_PATH: Path = BASE_DIR / os.getenv("DATABASE_PATH", "interviews.db")
    UPLOADS_DIR: Path = BASE_DIR / "uploads" / "resumes"

    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "admin")
    ADMIN_SECRET: str = os.getenv("ADMIN_SECRET", "super-secret-interviewer-key")
    ADMIN_EMAIL: str = os.getenv("ADMIN_EMAIL", "coach@example.com")

    SESSION_PRICE_DISPLAY: str = os.getenv("SESSION_PRICE_DISPLAY", "€70")
    PRICE_SINGLE_STANDARD_EUR: int = int(os.getenv("PRICE_SINGLE_STANDARD_EUR", "70"))
    PRICE_BUNDLE3_STANDARD_EUR: int = int(os.getenv("PRICE_BUNDLE3_STANDARD_EUR", "190"))
    YOUTUBE_DISCOUNT_PERCENT: int = int(os.getenv("YOUTUBE_DISCOUNT_PERCENT", "20"))
    PRICE_SINGLE_YOUTUBE_EUR: int = round(
        PRICE_SINGLE_STANDARD_EUR * (100 - YOUTUBE_DISCOUNT_PERCENT) / 100
    )
    PRICE_BUNDLE3_YOUTUBE_EUR: int = round(
        PRICE_BUNDLE3_STANDARD_EUR * (100 - YOUTUBE_DISCOUNT_PERCENT) / 100
    )
    SESSION_DURATION_MINUTES: int = int(os.getenv("SESSION_DURATION_MINUTES", "60"))
    REVOLUT_PAYMENT_URL: str = os.getenv(
        "REVOLUT_PAYMENT_URL", "https://revolut.me/federim7n9"
    )
    REVOLUT_API_KEY: str = os.getenv("REVOLUT_API_KEY", "")
    REVOLUT_WEBHOOK_SECRET: str = os.getenv(
        "REVOLUT_WEBHOOK_SECRET", "wsk_dev_mock_webhook_secret_key"
    )
    REVOLUT_API_BASE: str = os.getenv(
        "REVOLUT_API_BASE", "https://sandbox-merchant.revolut.com"
    ).rstrip("/")
    REVOLUT_API_VERSION: str = os.getenv("REVOLUT_API_VERSION", "2024-09-01")
    TRUST_PROXY_HEADERS: bool = os.getenv("TRUST_PROXY_HEADERS", "false").lower() in (
        "1",
        "true",
        "yes",
    )

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
