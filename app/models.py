from typing import Optional
from pydantic import BaseModel, Field, field_validator


SUPPORTED_LANGUAGES = [
    ("python", "Python"),
    ("cpp", "C++"),
    ("java", "Java"),
    ("javascript", "JavaScript / TypeScript"),
    ("go", "Go"),
    ("csharp", "C#"),
    ("rust", "Rust"),
]

TARGET_ROLES = [
    "Internship",
    "Junior / New Grad (L3)",
    "Mid-Level (L4)",
    "Senior (L5+)",
    "Staff / Principal (L6+)",
]

VERDICT_OPTIONS = [
    "Strong Hire",
    "Hire",
    "Lean Hire",
    "Lean No Hire",
    "No Hire",
]


class RubricUpdatePayload(BaseModel):
    notes_clarification: str = Field(default="", max_length=5_000)
    score_clarification: Optional[int] = Field(default=None, ge=1, le=5)
    notes_approach_complexity: str = Field(default="", max_length=5_000)
    score_approach_complexity: Optional[int] = Field(default=None, ge=1, le=5)
    notes_code_quality: str = Field(default="", max_length=5_000)
    score_code_quality: Optional[int] = Field(default=None, ge=1, le=5)
    notes_verification: str = Field(default="", max_length=5_000)
    score_verification: Optional[int] = Field(default=None, ge=1, le=5)
    final_verdict: Optional[str] = None
    final_recap_and_next_steps: str = Field(default="", max_length=10_000)

    @field_validator("final_verdict")
    @classmethod
    def _validate_final_verdict(cls, value: Optional[str]) -> Optional[str]:
        if value is None or value == "":
            return None
        if value not in VERDICT_OPTIONS:
            raise ValueError("Invalid final verdict.")
        return value
