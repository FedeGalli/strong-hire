from datetime import datetime, timezone
from typing import Any, Dict


def generate_ai_coding_response(
    prompt: str, active_filename: str, workspace_files: Dict[str, Any]
) -> Dict[str, Any]:
    """
    v2 AI-Assisted Interview Assistant stub.
    Provides structured guidance while logging both the candidate's prompt and
    assistant reply so the interviewer can evaluate prompt engineering & code verification skills.
    """
    active_code = (workspace_files.get(active_filename) or {}).get("content", "")
    line_count = len(active_code.splitlines())

    reply_text = (
        f"[AI Coding Assistant — Context: {active_filename} ({line_count} lines)]\n"
        f"Here is a structured hint for your query (\"{prompt[:90]}\"):\n"
        f"1. Clarify the invariants of the current file before applying changes.\n"
        f"2. Consider edge cases (empty inputs, duplicates, overflow, or cross-module boundaries).\n"
        f"3. Write a concise helper function and verify its time and space complexity."
    )

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "active_filename": active_filename,
        "candidate_prompt": prompt,
        "assistant_reply": reply_text,
    }
