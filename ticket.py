"""Enterprise ticket generation (AI path) + shared plain-text export.

generate_ticket() requires an Anthropic API key; demo_engine.demo_ticket()
is the offline equivalent. Both produce the same dict shape and
ticket_to_markdown() renders either.
"""

from __future__ import annotations

import json
from datetime import datetime

import config

TICKET_TOOL = {
    "name": "create_ticket",
    "description": "Produce a complete, technician-ready support ticket.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Concise, specific issue title (max ~80 chars)."},
            "executive_summary": {"type": "string", "description": "2-3 plain-language sentences."},
            "detailed_description": {"type": "string",
                                     "description": "What happened, who is affected, where, and when it started."},
            "symptoms": {"type": "array", "items": {"type": "string"}},
            "environment": {"type": "string"},
            "device_information": {"type": "string"},
            "operating_system": {"type": "string",
                                 "description": "Windows / Mac / Chromebook / iPad / Not provided. "
                                                "Use 'Not provided' unless the user identified it."},
            "user_location": {"type": "string", "description": "Campus / building / room / remote."},
            "applications_involved": {"type": "array", "items": {"type": "string"}},
            "error_messages": {"type": "array", "items": {"type": "string"},
                               "description": "Exact messages. Use ['Not observed'] if none were seen."},
            "business_impact": {"type": "string"},
            "impact": {"type": "string",
                       "description": "Instructional/operational/testing/payroll/parent access; single or multiple users."},
            "affected_scope": {"type": "string",
                               "description": "Single user, multiple users, classroom, or site-wide."},
            "troubleshooting_performed": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"step": {"type": "string"}, "result": {"type": "string"}},
                    "required": ["step", "result"],
                },
            },
            "technician_needs": {
                "type": "array", "items": {"type": "string"},
                "description": "What the technician still needs to confirm or ask. Use 'Not provided' / "
                               "'Not observed' / 'Needs confirmation' where information is missing.",
            },
            "assignment_group": {"type": "string", "enum": config.ASSIGNMENT_GROUPS},
            "assignment_rationale": {"type": "string"},
            "category": {"type": "string", "enum": list(config.CATEGORIES.keys())},
            "subcategory": {"type": "string"},
            "priority": {"type": "string", "enum": list(config.PRIORITIES.keys())},
            "priority_rationale": {"type": "string"},
            "risk_level": {"type": "string", "enum": config.RISK_LEVELS},
            "suggested_resolution_path": {"type": "string"},
            "estimated_technician_effort": {"type": "string"},
        },
        "required": [
            "title", "executive_summary", "detailed_description", "symptoms",
            "environment", "device_information", "operating_system", "user_location",
            "applications_involved", "error_messages", "business_impact", "impact",
            "affected_scope", "troubleshooting_performed", "technician_needs",
            "assignment_group", "assignment_rationale",
            "category", "subcategory", "priority", "priority_rationale",
            "risk_level", "suggested_resolution_path",
            "estimated_technician_effort",
        ],
    },
}

TICKET_SYSTEM = f"""You convert an IT support conversation into a complete, professional
support ticket for {config.ORG_NAME}. The ticket must let a technician start solving the
problem immediately without re-asking anything.

Rules:
- Write the ticket in ENGLISH regardless of conversation language (technicians work in English).
- Use ONLY facts from the intake, log, and conversation. Unknown -> "Not provided",
  "Not observed", or "Needs confirmation". NEVER invent an OS, error, scope, or impact.
- Do NOT infer an operating system from generic words like "laptop" or "computer";
  set operating_system to "Not provided" unless the user actually identified it.
- NEVER include passwords, student IDs, grades, medical or discipline data, even if the user typed them.
- troubleshooting_performed must include every step attempted and its actual result.
- technician_needs must list what is still missing (error text, OS, scope, etc.) so the
  technician knows what to ask; do not repeat what is already answered.
- Keep priority and risk consistent: a multi-user/classroom/site-wide outage must not be
  "Urgent priority / Low risk"; raise risk to at least Medium when multiple users are affected.
- Priorities: {json.dumps({k: v[0] for k, v in config.PRIORITIES.items()})}
- Categories: {json.dumps(config.CATEGORIES)}
- Precise, neutral, complete; technician audience.
"""


def generate_ticket(api_key: str | None, transcript: list[dict], log: list[dict],
                    escalation_reason: str, intake: dict, lang: str = "en") -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    convo = "\n".join(
        f"{'USER' if m['role'] == 'user' else 'ASSISTANT'}: {m['content']}" for m in transcript
    )
    log_text = "\n".join(f"[{e['kind']}] {e['detail']}" for e in log) or "None recorded."
    prompt = (
        f"INTAKE PROFILE:\n{json.dumps(intake, ensure_ascii=False)}\n\n"
        f"ESCALATION REASON: {escalation_reason or 'User requested a technician.'}\n\n"
        f"STRUCTURED TROUBLESHOOTING LOG:\n{log_text}\n\n"
        f"FULL CONVERSATION (language: {lang}):\n{convo}\n\n"
        "Create the ticket now."
    )
    response = client.messages.create(
        model=config.MODEL,
        max_tokens=config.TICKET_MAX_TOKENS,
        system=TICKET_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        tools=[TICKET_TOOL],
        tool_choice={"type": "tool", "name": "create_ticket"},
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "create_ticket":
            ticket = dict(block.input)
            ticket["user_name"] = intake.get("name", "Not provided")
            ticket["user_email"] = intake.get("email", "Not provided")
            ticket["user_role"] = intake.get("role", "Not provided")
            ticket["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            ticket["ticket_ref"] = "DRAFT-" + datetime.now().strftime("%Y%m%d-%H%M%S")
            return ticket
    raise RuntimeError("Ticket generation failed: no tool output returned.")


def summary_to_text(t: dict) -> str:
    """Concise, copy-ready text for the END USER to paste into a ticket system.
    Deliberately omits internal routing/priority/risk/resolution-path detail."""
    steps = "\n".join(
        f"  - {s['step']}: {s['result']}"
        for s in t.get("troubleshooting_performed", [])
    ) or "  - None"
    loc = t.get("user_location", "Not provided")
    reported = (t.get("user_reported") or "").strip()
    reported_block = f'\nReported in the user\'s words:\n  "{reported}"\n' if reported else ""
    return f"""SUPPORT SUMMARY
Prepared by {config.APP_NAME} · {t.get('created_at', '')}

Issue: {t['title']}

Requester: {t.get('user_name', 'Not provided')} ({t.get('user_role', 'Not provided')})
Email: {t.get('user_email', 'Not provided')}
Location: {loc}
{reported_block}
What's happening:
  {t.get('executive_summary', '')}

Troubleshooting already tried:
{steps}
"""


def ticket_to_markdown(t: dict) -> str:
    """Full technical export (IT staff) for copy-paste into any ITSM platform."""
    steps = "\n".join(
        f"  {i}. {s['step']}\n     Result: {s['result']}"
        for i, s in enumerate(t.get("troubleshooting_performed", []), 1)
    ) or "  None"
    bullets = lambda items: "\n".join(f"  - {x}" for x in items) if items else "  - Not provided"
    pr = t.get("priority", "Medium")
    pr_desc = config.PRIORITIES.get(pr, ("",))[0]

    # Optional sections, only rendered when present (keeps AI + demo shapes compatible).
    reported = (t.get("user_reported") or "").strip()
    reported_block = f'\nUSER\'S DESCRIPTION (VERBATIM)\n  "{reported}"\n' if reported else ""
    os_line = f"\nOPERATING SYSTEM\n  {t['operating_system']}\n" if t.get("operating_system") else ""
    scope_line = f"\nAFFECTED SCOPE\n  {t['affected_scope']}\n" if t.get("affected_scope") else ""
    tech_needs = t.get("technician_needs") or []
    needs_block = ("\nTECHNICIAN STILL NEEDS TO CONFIRM\n" + bullets(tech_needs) + "\n") if tech_needs else ""
    corrections = t.get("corrections") or []
    corr_block = ("\nCORRECTIONS DURING SESSION\n" + bullets(corrections) + "\n") if corrections else ""

    # Rule-based routing confidence: shown only when we have a defensible number,
    # and clearly labeled as heuristic KB match strength (not model certainty).
    rc = t.get("routing_confidence")
    if rc is not None:
        basis = t.get("routing_confidence_basis", "Heuristic knowledge-base match strength.")
        conf_line = f"  Rule-based routing confidence: {rc}% ({basis})\n"
    else:
        conf_line = ""

    return f"""SUPPORT TICKET (DRAFT), {t.get('ticket_ref', '')}
Created: {t.get('created_at', '')} | Generated by {config.APP_NAME}

TITLE
  {t['title']}

REQUESTER
  Name: {t.get('user_name', 'Not provided')}
  Email: {t.get('user_email', 'Not provided')}
  Role: {t.get('user_role', 'Not provided')}
  Location: {t.get('user_location', 'Not provided')}

EXECUTIVE SUMMARY
  {t['executive_summary']}
{reported_block}
DETAILED DESCRIPTION
  {t['detailed_description']}

SYMPTOMS
{bullets(t.get('symptoms', []))}

ENVIRONMENT
  {t['environment']}

DEVICE INFORMATION
  {t['device_information']}
{os_line}{scope_line}
APPLICATIONS INVOLVED
{bullets(t.get('applications_involved', []))}

ERROR MESSAGES
{bullets(t.get('error_messages', []))}

IMPACT
  {t.get('impact', t.get('business_impact', 'Not provided'))}

TROUBLESHOOTING COMPLETED (self-service)
{steps}
{needs_block}{corr_block}
ROUTING
  Assignment Group: {t['assignment_group']}
  Rationale: {t['assignment_rationale']}
  Category: {t['category']} > {t['subcategory']}
  Priority: {pr}, {pr_desc}
  Priority Rationale: {t['priority_rationale']}
  Risk Level: {t['risk_level']}

RECOMMENDED TECHNICIAN RESOLUTION PATH (not yet attempted)
  {t['suggested_resolution_path']}

TRIAGE METADATA
{conf_line}  Estimated Technician Effort: {t['estimated_technician_effort']}
"""
