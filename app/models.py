from enum import Enum
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field


class SessionStatus(str, Enum):
    PENDING_PAYMENT = "PENDING_PAYMENT"
    CONFIRMED = "CONFIRMED"
    RESCHEDULE_REQUESTED = "RESCHEDULE_REQUESTED"
    PAID_NEEDS_RESCHEDULE = "PAID_NEEDS_RESCHEDULE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class FinalVerdict(str, Enum):
    STRONG_HIRE = "Strong Hire"
    HIRE = "Hire"
    LEAN_HIRE = "Lean Hire"
    LEAN_NO_HIRE = "Lean No Hire"
    NO_HIRE = "No Hire"


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


class WorkspaceFile(BaseModel):
    content: str = "# Write your solution here\n"
    language: str = "python"


class RubricUpdatePayload(BaseModel):
    notes_clarification: str = ""
    score_clarification: Optional[int] = None
    notes_approach_complexity: str = ""
    score_approach_complexity: Optional[int] = None
    notes_code_quality: str = ""
    score_code_quality: Optional[int] = None
    notes_verification: str = ""
    score_verification: Optional[int] = None
    final_verdict: Optional[str] = None
    final_recap_and_next_steps: str = ""
