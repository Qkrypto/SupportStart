"""Centralized triage rules: device/OS inference, user corrections, and
priority/risk normalization.

These decisions used to live as scattered string checks inside the engine.
Keeping them here makes the behavior auditable and testable in one place, and
guarantees the two engines (offline demo + AI-assisted) reach the same verdict.

Design rules enforced here:
- Only EXPLICIT evidence maps a generic word to an operating system. "laptop",
  "computer", "desktop", "device" resolve to None so the engine asks instead of
  guessing (audit finding: "laptop" must not silently mean Windows).
- Priority and risk are computed together so they can never contradict
  (audit finding: no more "Urgent priority / Low risk").
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------
# Ordered scales (single source of truth, shared by both engines)
# --------------------------------------------------------------------------
PRIORITY_ORDER = ["Low", "Medium", "High", "Urgent"]
RISK_ORDER = ["Low", "Medium", "High"]


def _p_idx(p):
    return PRIORITY_ORDER.index(p) if p in PRIORITY_ORDER else 1


def _r_idx(r):
    return RISK_ORDER.index(r) if r in RISK_ORDER else 0


# --------------------------------------------------------------------------
# Device / OS inference
# --------------------------------------------------------------------------
# Each pattern maps to a config.DEVICE_TYPES label. Order matters: the most
# specific evidence wins. Generic hardware words are deliberately absent.
_DEVICE_PATTERNS = [
    (re.compile(r"\bchromebook\b|\bchrome ?os\b", re.I), "Chromebook"),
    (re.compile(r"\bmacbook\b|\bimac\b|\bmac ?os\b|\bmac\b", re.I), "Mac"),
    (re.compile(r"\bwindows\b|\bwindows\s+(laptop|desktop|computer|pc)\b|\bpc\b", re.I),
     "Windows laptop/desktop"),
    (re.compile(r"\bipad\b|\btablet\b|\btableta\b", re.I), "iPad / tablet"),
    (re.compile(r"\bsmart ?board\b|\binteractive display\b|\bpromethean\b|\bprojector\b|\bproyector\b",
                re.I), "Smartboard / display"),
    (re.compile(r"\bprinter\b|\bcopier\b|\bimpresora\b|\bcopiadora\b", re.I), "Printer / copier"),
    (re.compile(r"\biphone\b|\bandroid\b|\bcell ?phone\b|\bsmartphone\b|\btel[eé]fono\b", re.I), "Phone"),
]

# Generic words that do NOT identify an OS on their own.
_GENERIC_DEVICE = re.compile(
    r"\b(laptop|computer|desktop|device|machine|computadora|equipo|port[aá]til|m[aá]quina)\b", re.I)


def infer_device(text):
    """Return an explicit device/OS label, or None when evidence is only generic."""
    if not text:
        return None
    for rx, label in _DEVICE_PATTERNS:
        if rx.search(text):
            return label
    return None


def mentions_generic_device(text):
    """True when the user named a generic device but gave no OS-identifying detail."""
    if not text:
        return False
    return bool(_GENERIC_DEVICE.search(text)) and infer_device(text) is None


# --------------------------------------------------------------------------
# Scope / outage detection
# --------------------------------------------------------------------------
_MULTI = re.compile(
    r"\b(others too|other people|everyone|every ?one|whole class|entire class|the class|"
    r"multiple (users|people|students|teachers)|several (users|people|students|teachers)|"
    r"the (whole|entire) (office|school|building|department|site)|all of us|many of us|"
    r"we all|nobody can|no one can|everybody|"
    r"otros tambi[eé]n|todos|toda la clase|varios|nadie puede|todo el mundo)\b", re.I)

_SITEWIDE = re.compile(
    r"\b(whole school|entire school|the (whole|entire) (building|site|campus|district)|"
    r"site[- ]?wide|building[- ]?wide|everyone (is|'?s)? (down|offline|affected)|"
    r"toda la escuela|todo el edificio|todo el campus)\b", re.I)


def is_multiuser(text):
    return bool(text) and bool(_MULTI.search(text))


def is_sitewide(text):
    return bool(text) and bool(_SITEWIDE.search(text))


# --------------------------------------------------------------------------
# Corrections ("actually it's a Mac", "this affects everyone", ...)
# --------------------------------------------------------------------------
_CORRECTION_CUE = re.compile(
    r"\b(actually|really it'?s|it'?s actually|this is( a|n)|that'?s not|that isn'?t|"
    r"not (a|an|on|just)|no,|i (already )?(said|told you|meant)|"
    r"en realidad|ya (le )?dije|no es|en vez de|instead|correction)\b", re.I)

_ERROR_CHANGED = re.compile(
    r"\b(error (message )?(changed|is different|is now)|different error|new error|"
    r"now it says|it now says|el error cambi[oó]|otro error|error diferente)\b", re.I)

_NOT_THE_PROBLEM = re.compile(
    r"\b(that('?s| is)? ?(not|isn'?t) (the|my) (problem|issue)|that'?s not it|"
    r"wrong (problem|issue)|ese no es el problema|no es eso)\b", re.I)

_REMOTE = re.compile(r"\b(remotely|remote|from home|at home|a distancia|desde casa|en casa)\b", re.I)
_ON_CAMPUS = re.compile(r"\b(on campus|at school|in the building|en la escuela|en el edificio)\b", re.I)


def detect_correction(text, memory):
    """Inspect a user message for a correction to something already remembered.

    Returns a dict describing the correction, or None. Kinds:
      device   -> value = new device label        (re-route may be needed)
      scope    -> value = "multi"                 (raise priority)
      location -> value = "remote" | "on_campus"
      error    -> value = None                    (capture the corrected error next)
      issue    -> value = None                    (user says we matched the wrong issue)
    """
    if not text:
        return None
    t = text.strip()

    # Wrong-issue correction takes precedence: user says we misread the problem.
    if _NOT_THE_PROBLEM.search(t):
        return {"kind": "issue", "value": None}

    # Device correction: an explicit device that differs from what we stored.
    dev = infer_device(t)
    if dev and dev != memory.get("device_type"):
        # Fire on an explicit cue, or when the message is essentially just the device.
        if _CORRECTION_CUE.search(t) or len(t.split()) <= 6:
            return {"kind": "device", "value": dev}

    # Scope correction: now affects more than one person.
    if is_multiuser(t) and not memory.get("multiuser_confirmed"):
        return {"kind": "scope", "value": "multi"}

    # Error message changed.
    if _ERROR_CHANGED.search(t):
        return {"kind": "error", "value": None}

    # Location correction.
    if _CORRECTION_CUE.search(t):
        if _REMOTE.search(t):
            return {"kind": "location", "value": "remote"}
        if _ON_CAMPUS.search(t):
            return {"kind": "location", "value": "on_campus"}
    return None


# --------------------------------------------------------------------------
# Priority / risk normalization (computed together, never contradictory)
# --------------------------------------------------------------------------
_CRITICAL = re.compile(
    r"\b(testing|state test|exam|assessment|attendance|payroll|safety|emergency|911|"
    r"examen|prueba|asistencia|n[oó]mina|seguridad|emergencia)\b", re.I)


def normalize_priority_risk(base_priority, base_risk, *, issue_text="",
                            multiuser=False, sitewide=False, security=False,
                            has_workaround=False):
    """Return (priority, risk, rationale) from a single consistent rule set.

    - A single-user minor issue keeps its base priority/risk.
    - Security incidents => Urgent priority, High risk.
    - Critical-service impact (testing/attendance/payroll/safety) => at least High.
    - Multi-user/outage => at least High priority and at least Medium risk,
      Urgent only when it is also critical or site-wide (so we do not inflate
      every multi-user report to Urgent).
    """
    t = (issue_text or "").lower()
    critical = bool(_CRITICAL.search(t))
    outage = multiuser or sitewide or bool(_SITEWIDE.search(t))

    p, r = _p_idx(base_priority), _r_idx(base_risk)
    reasons = []

    if security:
        p = max(p, _p_idx("Urgent"))
        r = max(r, _r_idx("High"))
        reasons.append("security incident (routed to Security Operations)")

    if critical:
        p = max(p, _p_idx("High"))
        reasons.append("critical service impact (testing/attendance/payroll/safety)")

    if outage:
        p = max(p, _p_idx("High"))
        r = max(r, _r_idx("Medium"))
        reasons.append("site-wide outage" if (sitewide or _SITEWIDE.search(t))
                       else "multiple users affected")
        if critical or sitewide or _SITEWIDE.search(t):
            p = max(p, _p_idx("Urgent"))

    if not reasons:
        base_desc = "single user" + (", workaround available" if has_workaround else ", no workaround")
        reasons.append(f"{base_desc}; base routing priority retained")

    priority = PRIORITY_ORDER[min(p, len(PRIORITY_ORDER) - 1)]
    risk = RISK_ORDER[min(r, len(RISK_ORDER) - 1)]
    changed = (priority != base_priority) or (risk != base_risk)
    lead = f"Adjusted to {priority} priority / {risk} risk" if changed \
        else f"{priority} priority / {risk} risk"
    rationale = f"{lead}: " + "; ".join(reasons) + "."
    return priority, risk, rationale
