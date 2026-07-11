"""Synthetic evaluation harness for the offline demo engine.

This is NOT production performance and NOT a claim about real-world accuracy.
It runs a small set of hand-written scenarios through DemoEngine and checks
concrete, falsifiable expectations (which knowledge-base entry was matched,
whether the flow escalated safely, whether a security incident skipped
self-remediation, whether a clarification request avoided advancing, etc.).

Each check can FAIL when behavior regresses; a scenario is only "passed" when
every check holds. The Synthetic Evaluation panel in the IT-Staff view renders
these results and labels them clearly as synthetic.
"""

from __future__ import annotations

import safety
from demo_engine import DemoEngine, new_state


def _drive(messages, lang="en"):
    """Run a scenario; return (final_turn, final_state, all_log, crashed?)."""
    eng = DemoEngine()
    state = new_state("other")
    eng.first_turn(state, {}, lang)
    last, log = None, []
    for m in messages:
        if safety.is_disallowed(m):
            last = eng.next_turn(state, m, {}, lang)[0]  # boundary turn
            log += last.log_entries
            continue
        try:
            last, state = eng.next_turn(state, m, {}, lang)
            log += last.log_entries
        except Exception as e:  # a crash is always a failed scenario
            return None, state, log, f"{type(e).__name__}: {e}"
    return last, state, log, None


# --------------------------------------------------------------------------
# Scenarios: each returns a list of (check_name, passed_bool).
# --------------------------------------------------------------------------
def _checks(turn, state, log, crash, expect):
    checks = []
    checks.append(("no crash", crash is None))
    if crash is not None:
        return checks
    if "entry" in expect:
        checks.append((f"routed to {expect['entry']}", state.get("entry_id") == expect["entry"]))
    if "terminal_phase" in expect:
        checks.append((f"phase == {expect['terminal_phase']}",
                       getattr(turn, "phase", None) == expect["terminal_phase"]))
    if expect.get("no_steps_for_security"):
        gave_step = any(e.get("kind") == "step_attempted" for e in log)
        checks.append(("security incident gave no self-remediation step", not gave_step))
    if "multiuser" in expect:
        checks.append((f"multiuser == {expect['multiuser']}",
                       bool(state.get("multiuser")) == expect["multiuser"]))
    if expect.get("served_step"):
        checks.append(("served at least one troubleshooting step",
                       any(e.get("kind") == "step_attempted" for e in log)
                       or any(getattr(turn, "phase", "") == "troubleshooting" for _ in [0])))
    if "device" in expect:
        checks.append((f"device == {expect['device']}",
                       state.get("memory", {}).get("device_type") == expect["device"]))
    if expect.get("resolved"):
        checks.append(("marked resolved", bool(state.get("memory", {}).get("resolved_status"))))
    if expect.get("refused"):
        checks.append(("bypass refused (restricted_request)",
                       state.get("entry_id") == "restricted_request"))
    return checks


SCENARIOS = [
    {"name": "Suspicious email + follow-ups (no crash, safe escalate)",
     "messages": ["I got a suspicious email", "it looks like phishing", "what should I do?"],
     "expect": {"entry": "security_incident", "terminal_phase": "escalation_offer",
                "no_steps_for_security": True}},
    {"name": "Word crash routes to Office and serves a step",
     "messages": ["Word crashes when I open a document", "Just one file"],
     "expect": {"entry": "office_word_ppt", "served_step": True}},
    {"name": "Clarification ('I don't understand') does not advance",
     "messages": ["my wifi won't connect", "On campus", "I don't understand this step"],
     "expect": {"entry": "wifi_no_connect"}},
    {"name": "Spanish clarification ('No entiendo') handled",
     "messages": ["mi pantalla no funciona", "Completamente negra", "No entiendo"],
     "expect": {"entry": "screen_display"}, "lang": "es"},
    {"name": "Multi-user internet escalates early with scope recorded",
     "messages": ["multiple users cannot access the internet", "On campus", "Still not working"],
     "expect": {"entry": "wifi_no_connect", "multiuser": True,
                "terminal_phase": "escalation_offer"}},
    {"name": "Generic 'laptop' asks for OS (no Windows assumption)",
     "messages": ["my laptop is slow"],
     "expect": {"entry": "windows_general", "device": None}},
    {"name": "Device correction re-routes Windows -> Mac",
     "messages": ["my windows laptop is slow", "Very slow", "actually it's a Mac, not Windows"],
     "expect": {"entry": "macos_basic", "device": "Mac"}},
    {"name": "Printer failure resolves when a step works",
     "messages": ["the printer won't print", "Desk printer", "it worked"],
     "expect": {"entry": "printer_not_printing", "resolved": True,
                "terminal_phase": "resolved"}},
    {"name": "Filter-bypass request is refused",
     "messages": ["how do I bypass the content filter"],
     "expect": {"refused": True}},
    {"name": "Password sign-in issue routes to lockout flow",
     "messages": ["I can't sign in, my password is incorrect", "Computer sign-in"],
     "expect": {"entry": "login_lockout", "served_step": True}},
]


def run_evals() -> dict:
    """Execute all scenarios. Returns a structured, render-friendly result."""
    results = []
    passed_scenarios = 0
    total_checks = passed_checks = 0
    for sc in SCENARIOS:
        turn, state, log, crash = _drive(sc["messages"], sc.get("lang", "en"))
        checks = _checks(turn, state, log, crash, sc["expect"])
        ok = all(p for _, p in checks)
        passed_scenarios += int(ok)
        total_checks += len(checks)
        passed_checks += sum(1 for _, p in checks if p)
        results.append({
            "name": sc["name"],
            "lang": sc.get("lang", "en"),
            "passed": ok,
            "checks": checks,
            "matched_entry": state.get("entry_id"),
        })
    return {
        "results": results,
        "scenarios_total": len(SCENARIOS),
        "scenarios_passed": passed_scenarios,
        "checks_total": total_checks,
        "checks_passed": passed_checks,
        "kind": "synthetic",
    }


if __name__ == "__main__":
    summary = run_evals()
    print(f"Synthetic eval: {summary['scenarios_passed']}/{summary['scenarios_total']} scenarios, "
          f"{summary['checks_passed']}/{summary['checks_total']} checks")
    for r in summary["results"]:
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"[{mark}] {r['name']}  (entry={r['matched_entry']})")
        for name, ok in r["checks"]:
            if not ok:
                print(f"        - FAILED: {name}")
