from datetime import datetime
from typing import Any, Dict, Tuple
from fpdf import FPDF

from app.config import settings


def _sanitize_latin1(text: str) -> str:
    """
    Replaces common Unicode punctuation (em-dashes, smart quotes, bullets, arrows, Euro symbol)
    so standard PDF core fonts (Helvetica / Courier) render cleanly without encoding errors.
    """
    if not text:
        return ""
    replacements = {
        "\u2014": "--",
        "\u2013": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2022": "*",
        "\u2026": "...",
        "\u2192": "->",
        "\u20ac": "EUR ",
        "\t": "    ",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text.encode("latin-1", "replace").decode("latin-1")


def _verdict_colors(verdict: str) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
    """Returns (bg_rgb, text_rgb) for the Final Verdict badge."""
    mapping = {
        "Strong Hire": ((22, 163, 74), (255, 255, 255)),
        "Hire": ((37, 99, 235), (255, 255, 255)),
        "Lean Hire": ((13, 148, 136), (255, 255, 255)),
        "Lean No Hire": ((217, 119, 6), (255, 255, 255)),
        "No Hire": ((220, 38, 38), (255, 255, 255)),
    }
    return mapping.get(verdict, ((71, 85, 105), (255, 255, 255)))


class InterviewBreakdownPDF(FPDF):
    def footer(self) -> None:
        self.set_y(-14)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(148, 163, 184)
        self.cell(
            0,
            8,
            _sanitize_latin1(
                f"{settings.APP_TITLE} -- Confidential Mock Interview Evaluation | Page {self.page_no()}/{{nb}}"
            ),
            align="C",
        )


def generate_breakdown_pdf(session: Dict[str, Any]) -> bytes:
    """
    Generates the 'Your Interview Breakdown' PDF report containing:
    - Candidate & Session Metadata + Final Verdict Badge
    - Sections 1-4 Rubric Scores (1-5) & Detailed Notes
    - Section 5: Final Recap & Where to Push Preparation to Pass
    - Appendix: Code Snapshot from the Live Interview Room
    """
    pdf = InterviewBreakdownPDF(orientation="P", unit="mm", format="A4")
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    # --- Top Header Banner ---
    pdf.set_fill_color(15, 23, 42)  # Slate 900
    pdf.rect(0, 0, 210, 42, style="F")

    pdf.set_xy(14, 10)
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(125, 9, _sanitize_latin1("Your Mock Interview Breakdown"), ln=0)

    # Final Verdict Pill in Header
    verdict = session.get("final_verdict") or "Assessment Complete"
    bg_rgb, fg_rgb = _verdict_colors(verdict)
    pdf.set_fill_color(*bg_rgb)
    pdf.set_text_color(*fg_rgb)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_xy(142, 11)
    pdf.cell(54, 9, _sanitize_latin1(f"VERDICT: {verdict}"), border=0, ln=1, align="C", fill=True)

    try:
        dt_str = datetime.fromisoformat(session["start_time"].replace("Z", "+00:00")).strftime(
            "%B %d, %Y (%H:%M UTC)"
        )
    except Exception:
        dt_str = session.get("start_time", "")

    pdf.set_xy(14, 22)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(203, 213, 225)
    pdf.cell(
        0,
        6,
        _sanitize_latin1(f"Interviewer: {settings.INTERVIEWER_NAME}   |   Session Date: {dt_str}"),
        ln=1,
    )
    pdf.set_x(14)
    pdf.cell(
        0,
        6,
        _sanitize_latin1(
            f"Candidate: {session['candidate_name']} ({session['candidate_email']})   |   "
            f"Target: {session.get('target_role', 'SWE')} @ {session.get('target_company', 'Tech')}"
        ),
        ln=1,
    )

    pdf.set_y(48)

    # --- Candidate Summary Bar ---
    pdf.set_fill_color(241, 245, 249)
    pdf.set_draw_color(203, 213, 225)
    pdf.set_text_color(30, 41, 59)
    pdf.set_font("Helvetica", "B", 9)

    scores = [
        session.get("score_clarification"),
        session.get("score_approach_complexity"),
        session.get("score_code_quality"),
        session.get("score_verification"),
    ]
    valid_scores = [int(s) for s in scores if s is not None]
    avg_score_str = f"{sum(valid_scores) / len(valid_scores):.1f} / 5.0" if valid_scores else "N/A"

    summary_line = (
        f"Preferred Language: {str(session.get('preferred_language', 'python'))}    |    "
        f"LeetCode Solved at Booking: {session.get('leetcode_solved', 0)}    |    "
        f"Average Rubric Score: {avg_score_str}    |    "
        f"Final Verdict: {verdict}"
    )
    pdf.cell(0, 9, _sanitize_latin1(summary_line), border=1, ln=1, align="C", fill=True)
    pdf.ln(4)

    # --- Sections 1 to 4: Detailed Evaluation Rubric ---
    rubric_sections = [
        (
            "1. Problem Clarification & Requirements",
            session.get("score_clarification"),
            session.get("notes_clarification") or "No specific notes recorded for this section.",
        ),
        (
            "2. Algorithmic Approach & Time/Space Complexity",
            session.get("score_approach_complexity"),
            session.get("notes_approach_complexity") or "No specific notes recorded for this section.",
        ),
        (
            "3. Code Quality, Syntax & Implementation Speed",
            session.get("score_code_quality"),
            session.get("notes_code_quality") or "No specific notes recorded for this section.",
        ),
        (
            "4. Verification, Dry-Run & Edge Cases",
            session.get("score_verification"),
            session.get("notes_verification") or "No specific notes recorded for this section.",
        ),
    ]

    for title, score, notes in rubric_sections:
        pdf.set_fill_color(248, 250, 252)
        pdf.set_draw_color(226, 232, 240)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(15, 23, 42)
        score_badge = f"Score: {score} / 5" if score is not None else "Score: N/A"
        pdf.cell(145, 8, _sanitize_latin1(f"  {title}"), border="LTB", fill=True)
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(79, 70, 229)
        pdf.cell(37, 8, _sanitize_latin1(f"{score_badge}  "), border="RTB", ln=1, align="R", fill=True)

        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(51, 65, 85)
        pdf.multi_cell(0, 5.5, _sanitize_latin1(notes.strip()), border="LRB")
        pdf.ln(3)

    # --- Section 5: Final Recap & Where to Push Preparation to Pass ---
    pdf.ln(2)
    pdf.set_fill_color(238, 242, 255)  # Indigo 50
    pdf.set_draw_color(99, 102, 241)  # Indigo 500
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(49, 46, 129)
    pdf.cell(
        0,
        9,
        _sanitize_latin1("  5. Final Recap & Where to Push Your Preparation to Pass"),
        border="LTR",
        ln=1,
        fill=True,
    )

    recap_text = (
        session.get("final_recap_and_next_steps")
        or "Review the section feedback above and focus on verbalizing complexity trade-offs before coding, writing clean modular helper functions, and proactively dry-running edge cases."
    )
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(30, 41, 59)
    pdf.multi_cell(0, 6, _sanitize_latin1(recap_text.strip()), border="LRB", fill=True)
    pdf.ln(5)

    # --- Appendix: Session Code Snapshot ---
    workspace_files: Dict[str, Any] = session.get("workspace_files") or {}
    if workspace_files:
        if pdf.get_y() > 215:
            pdf.add_page()
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(15, 23, 42)
        pdf.cell(0, 8, _sanitize_latin1("Appendix: Live Session Code Snapshot"), ln=1)

        for filename, file_obj in workspace_files.items():
            lang = file_obj.get("language", session.get("editor_language", "text"))
            code_content = file_obj.get("content", "")
            pdf.set_fill_color(30, 41, 59)
            pdf.set_text_color(248, 250, 252)
            pdf.set_font("Courier", "B", 9)
            pdf.cell(
                0,
                7,
                _sanitize_latin1(f"  File: {filename} ({lang})"),
                ln=1,
                fill=True,
            )

            pdf.set_fill_color(248, 250, 252)
            pdf.set_draw_color(203, 213, 225)
            pdf.set_text_color(15, 23, 42)
            pdf.set_font("Courier", "", 8.5)
            numbered_lines = []
            for idx, line in enumerate(code_content.splitlines() or [""], start=1):
                numbered_lines.append(f"{idx:02d} | {line}")
            pdf.multi_cell(
                0,
                4.5,
                _sanitize_latin1("\n".join(numbered_lines)),
                border=1,
                fill=True,
            )
            pdf.ln(3)

    return bytes(pdf.output())
