"""Admin views: Improvement Dashboard, Ticket History, and the
Admin Review Queue for AI-suggested improvements.

The assistant never changes troubleshooting logic, routing rules, ticket
templates, or safety policies on its own. It analyzes history and files
suggestions here; an authorized admin approves, edits, rejects, or defers.

Access control (C4): constant-time comparison, optional SHA-256 stored code
(IT_ASSISTANT_ADMIN_CODE_SHA256), per-session attempt lockout, and a
persistent audit log of sign-in attempts.
"""

import hashlib
import hmac
import json
import os

import pandas as pd
import streamlit as st

import config
import evals
import storage
import ui
from strings import L

MAX_ATTEMPTS = 5


def _code_ok(code: str) -> bool:
    """Constant-time check against SHA-256 secret if set, else plain code."""
    if not code:
        return False
    stored_hash = os.environ.get("IT_ASSISTANT_ADMIN_CODE_SHA256")
    if not stored_hash:
        try:
            stored_hash = st.secrets["IT_ASSISTANT_ADMIN_CODE_SHA256"]
        except Exception:
            stored_hash = None
    if stored_hash:
        digest = hashlib.sha256(code.encode()).hexdigest()
        return hmac.compare_digest(digest.lower(), stored_hash.strip().lower())
    return hmac.compare_digest(code, config.ADMIN_CODE)


def _gate() -> bool:
    lang = st.session_state.get("lang", "en")
    st.info(L("demo_framing", lang))
    if st.session_state.get("admin_ok"):
        return True
    st.markdown(f"### {L('mode_admin', lang)}")
    attempts = st.session_state.get("admin_attempts", 0)
    if attempts >= MAX_ATTEMPTS:
        st.error("Too many failed attempts. Access is locked for this session, "
                 "reload the page to try again.")
        return False
    code = st.text_input(L("admin_code", lang), type="password", key="admin_code_input",
                         value="", placeholder="admin123")
    if st.button("Sign in", type="primary"):
        if _code_ok(code):
            st.session_state.admin_ok = True
            st.session_state.admin_attempts = 0
            storage.log_admin_attempt(True)
            st.rerun()
        else:
            st.session_state.admin_attempts = attempts + 1
            storage.log_admin_attempt(False, f"attempt {attempts + 1}")
            remaining = MAX_ATTEMPTS - st.session_state.admin_attempts
            st.error(f"Incorrect access code. {remaining} attempt(s) remaining.")
    return False


def render():
    if not _gate():
        return
    tab_dash, tab_tickets, tab_queue, tab_eval = st.tabs(
        [":material/insights: Improvement Dashboard", ":material/receipt_long: Ticket History",
         ":material/task_alt: Review Queue", ":material/science: Evaluation Lab"])
    with tab_dash:
        _dashboard()
    with tab_tickets:
        _tickets()
    with tab_queue:
        _queue()
    with tab_eval:
        _eval_lab()


# ---------------------------------------------------------------------------
def _dashboard():
    m = storage.metrics()
    st.caption("Data sources are labeled below: **Actual** = this demo's stored sessions/tickets · "
               "**Feedback** = user-submitted ratings · **Illustrative** = a demo estimate from a "
               "fixed formula, not a measured organizational outcome.")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Tickets prevented (Actual)", m["tickets_prevented"],
              help="Actual: demo sessions marked resolved without generating a ticket.")
    c2.metric("Tickets generated (Actual)", m["tickets_generated"],
              help="Actual: support summaries generated in this demo.")
    c3.metric("Avg. feedback score (Feedback)", f"{m['avg_helpful']:.1f} / 3" if m["avg_helpful"] else "N/A",
              help="Feedback: average of user-submitted helpfulness ratings.")
    c4.metric("Est. time saved (Illustrative)",
              f"{m['time_saved_minutes'] // 60}h {m['time_saved_minutes'] % 60}m",
              help="Illustrative estimate only, NOT measured savings. Formula: ~18 min per prevented "
                   "ticket + ~7 min per generated summary, applied to this demo's counts.")

    c5, c6, c7 = st.columns(3)
    c5.metric("Total sessions (Actual)", m["total_sessions"])
    c6.metric("Steps easy to follow (Feedback)", f"{m['avg_easy']:.1f} / 3" if m["avg_easy"] else "N/A")
    c7.metric("Summary accuracy (Feedback)", f"{m['avg_accurate']:.1f} / 3" if m["avg_accurate"] else "N/A")
    st.caption("‘Summary accuracy’ is a user's subjective rating of whether the summary matched their "
               "issue. It is not an independent correctness measurement.")

    st.divider()
    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("**Most common issue categories**")
        if m["top_categories"]:
            df = pd.DataFrame(m["top_categories"]).set_index("category")
            st.bar_chart(df, height=220)
        else:
            st.caption("No session data yet.")

        st.markdown("**Top failed troubleshooting steps**")
        if m["top_failed_steps"]:
            for row in m["top_failed_steps"]:
                st.markdown(f"- {row['failed_step']}, **{row['n']}×**")
        else:
            st.caption("No failed steps recorded yet.")

    with col_b:
        st.markdown("**Most common campus issues**")
        if m["top_campus_issues"]:
            for row in m["top_campus_issues"]:
                st.markdown(f"- {row['campus']}: {row['category']}, **{row['n']}×**")
        else:
            st.caption("No campus data yet.")

        st.markdown("**Escalation rate by category**")
        if m["escalation_by_category"]:
            for row in m["escalation_by_category"]:
                if row["n"]:
                    pct = int(100 * (row["esc"] or 0) / row["n"])
                    st.markdown(f"- {row['category']}: {pct}% ({row['esc'] or 0}/{row['n']})")
        else:
            st.caption("No escalation data yet.")

    st.divider()
    st.markdown("**Ticket volume by assignment group**")
    if m["groups"]:
        df = pd.DataFrame(m["groups"]).set_index("assignment_group")
        st.bar_chart(df, height=220)
    else:
        st.caption("No tickets yet.")

    st.divider()
    st.markdown("**Self-resolved (no ticket): what got fixed and the step that worked**")
    res = storage.list_resolutions()
    if res:
        rows = [{"When": r["created_at"], "Issue": r["category"],
                 "Device": r.get("device_type") or "N/A",
                 "Resolved by step": r.get("resolved_step") or "N/A",
                 "Steps tried": r.get("steps_attempted") or 0} for r in res]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.caption("No self-resolved sessions recorded yet.")

    st.divider()
    if st.button(":material/search: Analyze history and suggest improvements", type="primary"):
        storage.generate_suggestions()
        st.success("Analysis complete. New suggestions (if any) were added to the Review Queue.")


# ---------------------------------------------------------------------------
def _tickets():
    tickets = storage.list_tickets()
    if not tickets:
        st.caption("No tickets stored yet.")
        return
    df = pd.DataFrame(tickets)[
        ["id", "created_at", "status", "user_name", "campus", "category",
         "title", "assignment_group", "priority", "risk"]
    ]
    st.dataframe(df, use_container_width=True, hide_index=True)

    ids = [t["id"] for t in tickets]
    sel = st.selectbox("Open ticket", ids)
    t = next(t for t in tickets if t["id"] == sel)
    col1, col2 = st.columns([3, 1])
    with col2:
        new_status = st.selectbox("Status", ["Open", "In Progress", "Resolved", "Closed"],
                                  index=["Open", "In Progress", "Resolved", "Closed"].index(t["status"])
                                  if t["status"] in ["Open", "In Progress", "Resolved", "Closed"] else 0)
        if st.button("Update status", use_container_width=True):
            storage.update_ticket_status(sel, new_status)
            st.rerun()
    with col1:
        ui.ticket_preview(json.loads(t["ticket_json"]))
        atts = storage.list_attachments(sel)
        if atts:
            st.markdown(f"**Attachments ({len(atts)})**")
            img_cols = st.columns(min(len(atts), 3))
            for i, a in enumerate(atts):
                with img_cols[i % len(img_cols)]:
                    st.image(a["content"], caption=a["filename"], use_container_width=True)
        with st.expander("Full troubleshooting log"):
            for e in json.loads(t["log_json"]):
                st.markdown(f"- **{e['kind']}**, {e['detail']}")


# ---------------------------------------------------------------------------
STATUS_ICONS = {"Pending": "", "Approved": "", "Rejected": "",
                "Needs Review": "", "Edited": ""}


def _queue():
    st.caption("The assistant suggests improvements from feedback and ticket history. "
               "Nothing changes in production without approval here.")
    pending = storage.list_suggestions("Pending")
    others = [s for s in storage.list_suggestions() if s["status"] != "Pending"]

    if not pending:
        st.info("No pending suggestions. Run the analysis from the Improvement Dashboard.")
    for s in pending:
        with st.container(border=True):
            st.markdown(f"**{STATUS_ICONS.get(s['status'], '')} [{s['kind']}] {s['title']}**")
            st.markdown(f"*Reason:* {s['reason']}")
            st.markdown(f"*Supporting evidence:* {s['examples']}  \n"
                        f"*Heuristic suggestion confidence:* {s['confidence']}% "
                        f"(rule-based, not a measured outcome) · *Potential risk:* {s['risk']}")
            b1, b2, b3, b4 = st.columns(4)
            if b1.button("Approve", key=f"ap{s['id']}", use_container_width=True):
                storage.set_suggestion_status(s["id"], "Approved")
                st.rerun()
            if b2.button("Edit", key=f"ed{s['id']}", use_container_width=True):
                storage.set_suggestion_status(s["id"], "Edited")
                st.rerun()
            if b3.button("Reject", key=f"rj{s['id']}", use_container_width=True):
                storage.set_suggestion_status(s["id"], "Rejected")
                st.rerun()
            if b4.button("Needs review", key=f"nr{s['id']}", use_container_width=True):
                storage.set_suggestion_status(s["id"], "Needs Review")
                st.rerun()

    if others:
        with st.expander(f"Previously reviewed ({len(others)})"):
            for s in others:
                st.markdown(f"{STATUS_ICONS.get(s['status'], '')} **[{s['kind']}]** {s['title']}, {s['status']}")


# ---------------------------------------------------------------------------
def _eval_lab():
    st.markdown("#### Synthetic evaluation")
    st.warning("These are **synthetic** scenarios run against the offline demo engine. They test "
               "behavior (routing, safe escalation, clarification handling, device corrections), "
               "not real-world accuracy or production performance. They are kept separate from the "
               "Actual/Feedback analytics on the dashboard.", icon=":material/science:")

    if st.button(":material/play_arrow: Run synthetic evaluation", type="primary"):
        st.session_state["_eval_run"] = evals.run_evals()

    summary = st.session_state.get("_eval_run")
    if not summary:
        st.caption("Press run to execute the scenario suite. Each scenario asserts concrete "
                   "expectations and fails when behavior regresses.")
        return

    a, b = st.columns(2)
    a.metric("Scenarios passed (Synthetic)", f"{summary['scenarios_passed']} / {summary['scenarios_total']}")
    b.metric("Checks passed (Synthetic)", f"{summary['checks_passed']} / {summary['checks_total']}")
    st.caption("Synthetic pass rate is a regression signal for this prototype only. It does not "
               "represent field accuracy, user satisfaction, or ticket-resolution success.")

    for r in summary["results"]:
        icon = ":material/check_circle:" if r["passed"] else ":material/cancel:"
        with st.container(border=True):
            st.markdown(f"{icon} **{r['name']}**  \n"
                        f"<span style='color:gray'>matched entry: {r['matched_entry']} · lang: {r['lang']}</span>",
                        unsafe_allow_html=True)
            if not r["passed"]:
                for name, ok in r["checks"]:
                    if not ok:
                        st.markdown(f"- :material/error: Failed check: {name}")
