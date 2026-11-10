# SWE Mock Interview Platform

A lightweight, self-hosted Python web application for running 1-on-1 Software Engineering Mock Interviews (Google-style DSA & future AI-Assisted Multi-File interviews).

## Key Features

1. **Public Booking & Candidate Intake (`/`)**
   - Candidates select an open time slot (rendered in their local browser timezone) and submit their intake profile:
     - Full Name & Email
     - LeetCode Problems Solved
     - Target Company & Target Role/Level
     - Preferred Coding Language
     - Resume (PDF upload or URL)
2. **Semi-Automated 0%-Fee Payment & Instant Admin Notification (`/booking/{id}/payment`)**
   - Reserves the slot as `PENDING_PAYMENT` and immediately emails the interviewer (`ADMIN_EMAIL`) with a link to `/admin`.
   - Directs the candidate to pay via a Revolut checkout link (supports Apple Pay, Google Pay, and any Credit/Debit card in the browser without requiring a Revolut account) or PayPal.me.
3. **Comprehensive 4-Tab Admin Panel (`/admin`)**
   - **Tab 1 — Pending Approvals:** 1-click **"Approve & Send Invite"** creates a Google Calendar event + free Google Meet link, builds a universal `.ics` calendar file, and sends **Email #1 (Thread Starter)** with a deterministic `Message-ID`.
   - **Tab 2 — Upcoming Interviews:** Quick links to open the Interviewer Room, join Google Meet, view the candidate's resume, or jump to the Breakdown editor.
   - **Tab 3 — Past Interviews & Judgement Archive:** Permanent log of all completed interviews showing the stored **Final Verdict Badge** (`Strong Hire`, `Hire`, `Lean Hire`, `Lean No Hire`, `No Hire`), rubric scores, code snapshot, and PDF delivery status.
   - **Tab 4 — Slot Management:** Add or remove availability slots.
4. **Standalone Real-Time Interview Room (`/room/{id}`)**
   - **Candidate View (`/room/{id}`):** Distraction-free Ace Editor (autocomplete disabled) synced over WebSockets + Google Meet button.
   - **Interviewer View (`/room/{id}?key={ADMIN_SECRET}`):** Remotely toggle syntax highlighting (`Plain Text` vs `Python/C++/Java/JS/Go`), switch to `v2 AI-Assisted Multi-File` mode, view the candidate intake card, and take auto-saving rubric notes.
5. **Post-Interview Breakdown PDF in the Same Email Thread (`/admin/session/{id}`)**
   - Finalize Sections 1–4 scores & notes, select the **Final Hiring Verdict**, write **Section 5 (Final Recap & Where to Push Preparation to Pass)**, preview the PDF, and click **"Generate PDF & Send in Email Thread"** (`In-Reply-To` & `References` match Email #1's `Message-ID`).

---

## Quick Start

```bash
cd /Users/federicogalli/Federico/Projects/swe-mock-platform
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Start the server
uvicorn app.main:app --reload --port 8000
```

- **Public Booking Page:** [http://localhost:8000/](http://localhost:8000/)
- **Interviewer Admin Panel:** [http://localhost:8000/admin](http://localhost:8000/admin) (Default password in `.env`: `admin`)

---

## Configuring Live Gmail SMTP & Google Calendar Meet Links

1. **Gmail SMTP (for Instant Admin Alerts & Threaded Emails):**
   - Enable 2-Step Verification on your Google Account and create an **App Password** at `https://myaccount.google.com/apppasswords`.
   - Set `SMTP_USER="your-email@gmail.com"`, `ADMIN_EMAIL="your-email@gmail.com"`, and `SMTP_APP_PASSWORD="your-16-char-password"` in `.env`.
   - *(Note: Even when `SMTP_APP_PASSWORD` is blank, the app saves exact `.eml` copies—including `Message-ID`, `In-Reply-To`, `References`, `.ics`, and `.pdf` attachments—inside `uploads/email_outbox/` for local testing.)*

2. **Google Calendar + Free 1-on-1 Google Meet Links:**
   - In Google Cloud Console, enable the **Google Calendar API** and create an **OAuth 2.0 Client ID (Desktop App)**.
   - Save the downloaded JSON file as `credentials.json` in the project root.
   - Run the one-time helper script to generate `token.json`:
     ```bash
     python3 scripts/setup_google_oauth.py
     ```
