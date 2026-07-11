# SupportStart (Demo)

> **Portfolio prototype — not an official system for any school or district, and it
> does not submit real tickets.** Please don't enter real district or student data.
> The IT Staff view is intentionally viewable for demonstration; demo access code `admin123`.

I built a prototype AI support assistant for school technology issues. The goal is to
reduce bad tickets by guiding users through safe troubleshooting first, then generating
a technician-ready summary only when needed. The backend demo shows how IT staff could
review summaries, routing suggestions, failed steps, and improvement metrics.

It's bilingual (English/Español) and accessible, with a structured knowledge base of
30+ common school IT issues (Chromebooks, Windows/Mac, Wi-Fi, printers, displays,
Google Workspace, Microsoft Office, Schoology, accounts, and more).

**Works out of the box — no API key required.** A built-in engine drives guided flows
from the knowledge base. With an Anthropic API key, an adaptive Claude engine takes
over, grounded in the same knowledge base.

## Safety

The assistant refuses requests to bypass security, disable monitoring, hack Wi-Fi,
access accounts without permission, or work around school/district policy, and it warns
users not to enter passwords, student IDs, SSNs, grades, medical, or other private data.

## Run

```bash
pip install -r requirements.txt
streamlit run app.py
```

### Production configuration (Streamlit Cloud → App settings → Secrets)

```toml
# Persistent database (C1) — create a free Supabase project, copy the
# connection string from Project Settings > Database ("URI" format).
# Without this, the app uses local SQLite (wiped on redeploy).
DATABASE_URL = "postgresql://postgres:...@db.xxxx.supabase.co:5432/postgres"

# Real ticket dispatch via email (C2). Without SMTP config, tickets are
# stored + downloadable and the UI says dispatch isn't configured.
SMTP_HOST = "smtp.gmail.com"        # or smtp.sendgrid.net etc.
SMTP_PORT = "587"
SMTP_USER = "helpdesk-bot@district.org"
SMTP_PASS = "app-password-or-api-key"
SERVICE_DESK_EMAIL = "helpdesk@district.org"
CC_REQUESTER = "false"              # "true" to copy users on their tickets

# Admin access (C4). Prefer the SHA-256 form; generate with:
#   python -c "import hashlib;print(hashlib.sha256(b'YourCode').hexdigest())"
IT_ASSISTANT_ADMIN_CODE_SHA256 = "…hex…"

# Optional adaptive AI engine
ANTHROPIC_API_KEY = "sk-ant-…"
```

Abuse limits (C5) are on by default: 60 messages/session, 1,000-char inputs,
1.5 s cooldown (override via `IT_ASSISTANT_MAX_MESSAGES` etc.). Admin sign-in
locks after 5 failed attempts and every attempt is audit-logged.

## Key features

- **Personalized intake** — name, email, role, campus, room, device, category, and a
  required data-sensitivity question, with a privacy notice before anything is collected.
- **Knowledge-base-driven diagnostics** — `knowledge_base.py` organizes troubleshooting
  by product → issue → symptoms. Both engines identify the product, match symptoms to
  the closest entry, ask a targeted follow-up when confidence is low, present only the
  next best step, and adapt to answers. Adding knowledge = appending an entry.
- **Guided steps** — every step card shows what to do / why it matters / expected result,
  with buttons: It worked · Still not working · I need help with this step, plus an
  optional "Show me how" visual (accessible SVG + alt text + video placeholder).
- **Safety** — privacy notices, sensitive-data warnings, security issues fast-escalate
  to Security Operations with no user self-remediation.
- **Escalation & smart routing** — stops when steps are exhausted, admin rights are
  needed, security risk, multiple users, or testing/instruction impact; recommends
  assignment group, category, priority (Low→Urgent), risk, and explains why.
- **Ticket preview** — edit, copy, download, submit, start over; stored in SQLite.
- **Accessibility** — WCAG-friendly palettes, high contrast, text scaling, reduce
  motion, keyboard focus outlines, screen-reader labels, simple-language mode,
  read-aloud (browser TTS), and voice input (browser speech recognition, modular).
- **Spanish support** — full UI + flows in respectful, parent-friendly Spanish;
  switchable mid-conversation. Tickets are always generated in English for technicians.
- **Feedback & improvement loop** — post-resolution star ratings stored in SQLite;
  Improvement Dashboard (tickets prevented, common issues, failed steps, routing
  accuracy signals, time saved) and an Admin Review Queue where AI-suggested
  improvements require explicit approval — nothing changes in production automatically.

## Structure

| File | Purpose |
|---|---|
| `app.py` | Orchestration: intake, chat, feedback, ticket actions, layout |
| `knowledge_base.py` | Structured KB (product/issue/symptom) + scored retrieval |
| `demo_engine.py` | Offline KB-driven engine + offline ticket builder |
| `engine.py` | Claude engine (KB-grounded, structured turns via forced tool use) |
| `triage.py` | Centralized rules: device/OS inference, user corrections, priority/risk |
| `ticket.py` | AI ticket generation + plain-text export |
| `evals.py` | Synthetic scenario harness (regression checks for the demo engine) |
| `storage.py` | SQLite/Postgres: tickets, sessions, feedback, suggestions + analytics |
| `safety.py` | Disallowed-request refusals + PII detection gates |
| `admin.py` | Improvement Dashboard, Ticket History, Review Queue, Evaluation Lab |
| `ui.py` | Theming, accessibility, components, voice widgets |
| `visuals.py` | Accessible SVG step diagrams |
| `strings.py` | English/Spanish UI strings |
| `config.py` | Branding, taxonomy, campuses, roles, priorities |

## Customization

- Branding/campuses/taxonomy: `config.py`
- Add troubleshooting knowledge: append an entry in `knowledge_base.py`
- Add step visuals: `visuals.py`
- Real ITSM submission: replace the simulated submit in `app.py` with your platform's
  API (e.g., ServiceNow Table API) — the ticket dict maps directly.

## Architecture

The app is a single Streamlit server with a clear split of responsibilities:

- **Application orchestration (`app.py`)** wires intake, the chat loop, safety gates,
  feedback, and ticket actions together, and picks the engine (offline vs. AI) based on
  whether an API key is present.
- **Offline/demo troubleshooting engine (`demo_engine.py`)** is deterministic and needs
  no API key. It matches the user's description to a knowledge-base entry, serves one step
  at a time, tracks a session "memory" object, and produces the offline support summary.
- **Optional AI-assisted engine (`engine.py`)** uses the Anthropic API with forced tool
  use, grounded in the *same* knowledge base, for more adaptive phrasing. It is never
  required, and it is never used on the no-key path.
- **Knowledge base (`knowledge_base.py`)** holds every troubleshooting flow as data
  (product → issue → symptoms → questions → ordered steps → routing). Both engines read
  it; adding knowledge means appending an entry, not changing engine code.
- **Triage rules (`triage.py`)** centralize device/OS inference, correction handling, and
  a single `normalize_priority_risk()` function so priority and risk are always consistent.
- **Safety and PII gates (`safety.py`)** run before the engine: disallowed requests
  (bypass/monitoring/unauthorized access) are refused, and likely PII (student IDs,
  passwords, SSNs, DOB, grades, medical/discipline) pauses the turn with a warning.
- **Ticket generation (`ticket.py` + `demo_engine.demo_ticket`)** produces one shared
  ticket shape from either engine and renders an end-user summary and a technician export.
- **Storage (`storage.py`)** uses Postgres when `DATABASE_URL` is set, else local SQLite,
  for tickets/sessions/feedback/suggestions/attachments/audit.
- **Backend portfolio dashboard (`admin.py`)** shows Improvement Dashboard, Ticket
  History, a human-in-the-loop Review Queue, and the Evaluation Lab.
- **Evaluation layer (`evals.py`)** runs synthetic scenarios with concrete assertions and
  reports pass/fail in the Evaluation Lab.

## Evaluation

The Evaluation Lab (IT-Staff view) runs the scenarios in `evals.py` against the offline
engine. Each scenario asserts concrete, falsifiable expectations — which KB entry was
matched, whether a security incident skipped self-remediation, whether a clarification
request avoided advancing, whether a device correction re-routed the flow, whether a
multi-user report escalated early — and a scenario only passes when *every* check holds.

What synthetic evaluation tests: that the engine's behavior does what it claims for a set
of hand-written cases, and that regressions get caught. What it does **not** prove:
real-world accuracy, field resolution rates, or user satisfaction. Three different things
are kept distinct throughout the app: **deterministic checks** (the synthetic suite, which
can fail), **user feedback** (subjective ratings people submitted), and **production
outcomes** (which this prototype does not measure at all). Feature presence is not proof of
effectiveness — a flow existing in the KB says nothing about whether it resolves real
issues; only measured outcomes would, and those are out of scope for a demo. The
dashboard labels every number as Actual, Feedback, or Illustrative for this reason, and
the "estimated time saved" figure is an illustrative formula, not a measured saving.

Run the suite headless:

```bash
python evals.py
```

## Known limitations

- This is a **portfolio prototype**, not a production system.
- In no-key mode it uses **deterministic knowledge-base retrieval and branching**, not a
  learning model. It is scripted-but-adaptive, not open-ended reasoning.
- It **does not submit official tickets** and is **not connected to any real school
  district**, ITSM platform, or help desk.
- It is **not production-ready**: no real authentication for end users, and the IT-Staff
  view is intentionally openable in the demo (code `admin123`).
- **Dashboard estimates are illustrative** where labeled; routing confidence is heuristic
  KB match strength, not model certainty or ticket accuracy.
- **Accessibility and mobile validation are ongoing.** The app follows WCAG-friendly
  practices (focus outlines, contrast, reduced motion, labels, large touch targets), but
  real assistive-technology and physical-device testing still needs to be done.

## Security and privacy

- **Secrets belong in deployment configuration, never in source control.** API keys, the
  database URL, SMTP credentials, and the admin hash go in Streamlit secrets or
  environment variables. `.gitignore` excludes `.env`, `*.db`, and `.streamlit/secrets.toml`.
- **Do not enter real student or district information.** This is a demo; it asks users to
  describe issues in general terms.
- **Sensitive inputs are detected and paused** where possible (`safety.py`), and tickets
  are built to avoid echoing passwords, IDs, grades, or medical details.
- **Detection is a safeguard, not a compliance guarantee.** PII and disallowed-request
  matching is best-effort pattern matching, not a certified DLP or content-filtering
  control, and should not be relied on as one.
