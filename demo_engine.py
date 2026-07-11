"""Built-in bilingual diagnostic engine. No API key required.

Consumes the structured knowledge base (knowledge_base.py) rather than
hardcoded flows:

1. Identifies the product from intake hints + the user's description.
2. Matches symptoms to the closest KB entry (scored retrieval).
3. Asks a targeted disambiguation question when confidence is low.
4. Presents only the next best step, one at a time.
5. Adapts to responses: It worked -> resolved; I need help -> re-explain
   the same step; Still not working -> advance; multi-user/impact answers
   raise priority; security matches fast-escalate.

The app keeps the state dict in session_state; next_turn returns (Turn, state).
"""

from __future__ import annotations

import re
from datetime import datetime

import config
import knowledge_base as kb
import safety
import triage
from engine import Turn
from strings import L, tr

WORKED = re.compile(r"\b(it worked|worked|funcion[oó]|fixed|resolved|solved|resuelto|arreglado|working now|connected now|ya funciona)\b", re.I)

# District policy boundary: requests to bypass protections are refused politely
# and converted into a proper review request (never self-remediation).
BYPASS = re.compile(
    r"\b(bypass|unblock|un-block|get around|circumvent|jailbreak|"
    r"disable (the )?(filter|monitoring|goguardian|securly|lightspeed|management|mdm|antivirus|firewall)|"
    r"remove (the )?(mdm|management|restriction)|unenroll|un-enroll|"
    r"proxy (site|server)|vpn to|"
    r"(someone|somebody) else'?s (account|password)|another (user|student|person)'?s (account|password)|"
    r"desbloquear|evadir|saltar(se)? el filtro|quitar (el )?filtro|"
    r"cuenta de otra persona|contraseña de otra persona)\b", re.I)
# Natural requests for step clarification (EN/ES). Broad on purpose: the audit
# found "I don't understand this step" was silently treated as a failed result.
HELP = re.compile(
    r"\b(need help|help with this step|don'?t know how|not sure how|how do i|how do you|"
    r"i (don'?t|do not) understand|don'?t understand|didn'?t understand|i'?m confused|"
    r"i am confused|confused|what does that mean|what do you mean|not clear|unclear|"
    r"can you explain|please explain|explain that|explain this|make (that|this) simpler|"
    r"simpler|in simpler terms|where (do i|do you|can i) find|i (don'?t|do not|can'?t) see (that|the|it)|"
    r"i don'?t see (that|the) (option|button|menu)|can'?t find (it|that|the)|stuck|i'?m stuck|"
    r"necesito ayuda|no s[eé] c[oó]mo|no entiendo|no lo entiendo|estoy confundid[oa]|"
    r"qu[eé] significa|puede explicar|expl[ií]que|m[aá]s simple|no veo esa opci[oó]n|"
    r"no veo el bot[oó]n|d[oó]nde encuentro|d[oó]nde est[aá]|no lo encuentro)\b", re.I)
# MULTI stays for the scope answer; broader outage detection lives in triage.
MULTI = re.compile(r"\b(others too|otros tambi[eé]n|multiple|everyone|todos|whole class|toda la clase|"
                   r"several|many of us|all of us|nobody|no one|everybody)\b", re.I)
NEGATIVE = re.compile(r"\b(still|sigue|not|no|nope|doesn'?t|didn'?t)\b", re.I)

# Workstation flows that are inherently OS-specific. When the device is unknown
# and the user only said a generic word ("laptop"), we ask before serving these.
WORKSTATION_BY_DEVICE = {
    "Mac": "macos_basic",
    "Windows laptop/desktop": "windows_general",
    "Chromebook": "chromebook_frozen",
}
OS_SPECIFIC_ENTRIES = set(WORKSTATION_BY_DEVICE.values())

GENERIC_HELP = kb.B(
    "No problem. Let's slow down. Read the card again one line at a time, and only do the "
    "'What to do' part. If you can't find a button or menu it mentions, tell me what you see "
    "on your screen instead, or choose 'Still not working' and we'll move on. You won't break anything.",
    "No hay problema. Vamos más despacio. Lea la tarjeta de nuevo línea por línea y haga solo "
    "la parte de 'Qué hacer'. Si no encuentra un botón o menú, dígame qué ve en su pantalla, o "
    "elija 'Sigue sin funcionar' y continuamos. No dañará nada.",
)

DESCRIBE_Q = kb.B("In one or two sentences, what's happening?",
                  "En una o dos oraciones, ¿qué está pasando?")
DISAMBIG_Q = kb.B("To make sure I use the right fix, which of these best matches your situation?",
                  "Para usar la solución correcta, ¿cuál de estas opciones describe mejor su situación?")
DEVICE_Q = kb.B("Got it. What type of device or system is this about?",
                "Entendido. ¿Sobre qué tipo de equipo o sistema es esto?")
SCOPE_Q = kb.B("One quick check: is this affecting just you, or are others affected too?",
               "Una verificación rápida: ¿esto le afecta solo a usted o también a otras personas?")
SCOPE_QUICK = [kb.B("Just me", "Solo yo"), kb.B("Others too", "Otros también"), kb.B("Not sure", "No estoy seguro")]

PRIORITY_ORDER = ["Low", "Medium", "High", "Urgent"]

# KB entries whose issue is device-dependent. Only these prompt for device type.
DEVICE_RELEVANT_CATEGORIES = {"Hardware", "Network", "Facilities & AV"}


def bump(priority: str, levels: int = 1) -> str:
    i = min(PRIORITY_ORDER.index(priority) + levels, len(PRIORITY_ORDER) - 1)
    return PRIORITY_ORDER[i]


def _blank_memory() -> dict:
    """The session's remembered answers. The assistant checks this before asking."""
    return {
        "issue_summary": None,
        "device_type": None,
        "user_type": None,
        "user_name": None,
        "user_email": None,
        "school_site": None,
        "building_room": None,
        "software_or_system": None,
        "error_message": None,
        "troubleshooting_steps_given": [],
        "troubleshooting_steps_attempted": [],
        "resolved_status": None,      # None | True | False
        "escalation_needed": False,
        "multiuser_confirmed": False,
        "corrections": [],            # audit trail of corrected assumptions
        "location_context": None,     # "remote" | "on_campus"
    }


def new_state(category: str) -> dict:
    return {"category": category if category in config.CATEGORY_LABELS else "other",
            "entry_id": None, "candidates": [], "stage": "describe", "idx": 0,
            "answers": {}, "multiuser": False, "sitewide": False, "impact": "",
            "failed_steps": [], "done": False,
            "memory": _blank_memory(), "asked_device": False,
            "match_confidence": 0.0, "os_clarify": False,
            "pending_prefix": ""}


def infer_device(text: str, intake: dict | None = None) -> str | None:
    """Infer a device_type from EXPLICIT evidence or the intake profile.

    Generic words like 'laptop'/'computer' resolve to None (see triage.infer_device)
    so the engine asks for the OS instead of assuming Windows.
    """
    if intake and intake.get("device") and intake["device"] != config.DEVICE_TYPES["other"][0]:
        return intake["device"]
    return triage.infer_device(text)


def _device_relevant(entry: dict | None) -> bool:
    if not entry:
        return False
    return entry["routing"]["category"] in DEVICE_RELEVANT_CATEGORIES


def _entry(state: dict) -> dict | None:
    return kb.BY_ID.get(state["entry_id"]) if state["entry_id"] else None


def _total(state: dict) -> int:
    e = _entry(state)
    if not e:
        return 6
    dev = 1 if state.get("asked_device") else 0
    base = 1 + dev + len(e["questions"]) + len(e["steps"]) + 1  # describe (+device) + nodes + wrap-up
    return base + (0 if e.get("security_fast") else 1)          # + scope (no impact question)


def _pos(state: dict) -> int:
    e = _entry(state)
    stage, idx = state["stage"], state["idx"]
    dev = 1 if state.get("asked_device") else 0
    if stage in ("describe", "disambiguate", "device") or not e:
        return 1 + (0 if stage != "device" else dev)
    if stage == "questions":
        return 2 + dev + idx
    if stage == "steps":
        return 2 + dev + len(e["questions"]) + idx
    if stage == "scope":
        return 2 + dev + len(e["questions"]) + len(e["steps"])
    return _total(state)


class DemoEngine:
    """Stateful, KB-driven, bilingual guided-flow engine. No network, no key."""

    # ------------------------------------------------------------- entry
    def first_turn(self, state: dict, intake: dict, lang: str) -> Turn:
        """Issue-first opener: invite the problem description immediately."""
        name = (intake.get("name") or "").split()[0] if intake.get("name") else ""
        greet = {
            "en": (f"Hi{', ' + name if name else ''}, I'm your AI support assistant. Describe "
                   "what's happening in a sentence or two and we'll try to fix it. If we can't, "
                   "I'll write a clean summary for IT."),
            "es": (f"Hola{', ' + name if name else ''}, soy su asistente de soporte con "
                   "inteligencia artificial. Describa qué sucede en una o dos oraciones y lo "
                   "intentamos arreglar. Si no podemos, escribiré un resumen claro para TI."),
        }[lang]
        return Turn(
            reply=greet,
            phase="intake",
            status_label={"en": "Listening", "es": "Escuchando"}[lang],
            issue_summary="",
            progress_current=1, progress_total=_total(state),
            est_minutes_remaining=8, confidence=60, quick_replies=[],
            log_entries=[],
        )

    # ------------------------------------------------------------- turns
    def next_turn(self, state: dict, user_text: str, intake: dict, lang: str) -> tuple[Turn, dict]:
        # District policy boundary. Checked before anything else, at any stage.
        if safety.is_disallowed(user_text):
            return self._boundary_turn(state, user_text, lang), state
        stage = state["stage"]
        log = []
        mem = state["memory"]

        # Terminal guard: once we've escalated/resolved, never index into a step
        # or question again. Re-offer the summary safely (audit crash fix).
        if state.get("done") or stage in ("escalated", "resolved", "boundary"):
            entry = _entry(state) or kb.BY_ID["general_it"]
            if mem.get("resolved_status"):
                return self._closed_turn(entry, lang), state
            return self._reoffer_turn(entry, state, lang), state

        # Corrections can arrive at any active stage after the first description.
        if stage in ("questions", "steps", "scope", "device"):
            corrected = self._apply_correction(state, user_text, intake, lang, log)
            if corrected is not None:
                return corrected, state

        if stage == "describe":
            log.append({"kind": "info_collected", "detail": f"Issue description: {user_text}"})
            mem["issue_summary"] = user_text
            # Detect scope early so wider outages are prioritized from the start.
            if triage.is_multiuser(user_text) or triage.is_sitewide(user_text):
                state["multiuser"] = True
                mem["multiuser_confirmed"] = True
                if triage.is_sitewide(user_text):
                    state["sitewide"] = True
                log.append({"kind": "finding",
                            "detail": "Scope: multiple users / possible outage detected in the "
                                      "initial report. Escalation threshold lowered."})
            # Infer and remember the device from EXPLICIT words (never from "laptop").
            dev = infer_device(user_text, intake)
            if dev:
                mem["device_type"] = dev
                log.append({"kind": "finding", "detail": f"Inferred device type: {dev}"})
            entry, candidates, conf = kb.select_entry(
                user_text, state["category"], mem.get("device_type") or (intake or {}).get("device"))
            state["match_confidence"] = float(conf or 0.0)
            if entry is None:
                state["candidates"] = [c["id"] for c in candidates]
                state["stage"] = "disambiguate"
                log.append({"kind": "finding",
                            "detail": "Low-confidence KB match. Asking user to disambiguate among: "
                                      + ", ".join(state["candidates"])})
                quick = [tr(kb.BY_ID[cid]["issue"], lang) for cid in state["candidates"]]
                return self._q_turn(tr(DISAMBIG_Q, lang), quick,
                                    {"en": "Narrowing it down", "es": "Acotando el problema"}[lang],
                                    state, lang, log, confidence=40), state
            # OS-specific workstation flow but the device is unknown (generic word
            # like "laptop"): ask for the OS before giving OS-specific steps.
            if (entry["id"] in OS_SPECIFIC_ENTRIES and mem.get("device_type") is None
                    and not (intake or {}).get("device")):
                self._select(state, entry, log)
                mem["software_or_system"] = entry["product"]
                state["os_clarify"] = True
                state["asked_device"] = True
                state["stage"] = "device"
                state["pending_prefix"] = self._restate(entry, lang)
                quick = [v[0 if lang == "en" else 1] for v in config.DEVICE_TYPES.values()]
                return self._q_turn(tr(DEVICE_Q, lang), quick,
                                    {"en": "Identifying device", "es": "Identificando equipo"}[lang],
                                    state, lang, log), state
            self._select(state, entry, log)
            mem["software_or_system"] = entry["product"]
            return self._after_match(state, intake, lang, log), state

        if stage == "device":
            dev = self._match_device(user_text)
            mem["device_type"] = dev
            log.append({"kind": "info_collected", "detail": f"Device type: {dev}"})
            # If we asked to disambiguate an OS-specific workstation flow, re-route
            # (or restore the flow's own stage so we don't fall through to escalation).
            if state.pop("os_clarify", False):
                rerouted = self._reroute_for_device(state, dev, log)
                if rerouted is not None:
                    self._select(state, rerouted, log)
                    mem["software_or_system"] = rerouted["product"]
                else:
                    cur = _entry(state) or kb.BY_ID["general_it"]
                    state["stage"] = "questions" if cur["questions"] else "steps"
                    state["idx"] = 0
            return self._serve_current(state, intake, lang, log), state

        if stage == "disambiguate":
            log.append({"kind": "info_collected", "detail": f"User clarified issue as: {user_text}"})
            entry = self._match_candidate(state, user_text)
            if entry is None:
                # No confident match. Do NOT pivot to an unrelated flow.
                # Go straight to offering a summary with everything collected so far.
                fallback = kb.BY_ID["general_it"]
                if state["category"] not in (None, "other"):
                    fallback = next((e for e in kb.ENTRIES if state["category"] in e["categories"]),
                                    kb.BY_ID["general_it"])
                state["entry_id"] = fallback["id"]
                state["done"] = True
                state["stage"] = "escalated"
                log.append({"kind": "finding",
                            "detail": "Clarification did not match a known troubleshooting flow, "
                                      "escalating directly instead of guessing."})
                reason = ("The reported issue could not be confidently matched to a self-service "
                          "fix; routed directly to a technician with the collected details.")
                reply = {
                    "en": "I don't want to guess and walk you through steps that don't fit your "
                          "situation. Instead, let me put together ticket-ready information, "
                          "details you can copy into your official support request so IT can help "
                          "you directly with everything you've told me so far.",
                    "es": "No quiero adivinar y guiarle por pasos que no correspondan a su "
                          "situación. Mejor permítame preparar la información lista para su "
                          "solicitud de soporte. Detalles que puede copiar para que TI le ayude "
                          "directamente con todo lo que me ha dado.",
                }[lang]
                log.append({"kind": "escalation_reason", "detail": reason})
                state["memory"]["escalation_needed"] = True
                turn = Turn(reply=reply, phase="escalation_offer",
                            status_label={"en": "Support summary available", "es": "Resumen disponible"}[lang],
                            issue_summary=tr(fallback["issue"], lang),
                            progress_current=_total(state), progress_total=_total(state),
                            est_minutes_remaining=2, confidence=10, quick_replies=[],
                            log_entries=log, escalation_reason=reason)
                return turn, state
            self._select(state, entry, log)
            mem["software_or_system"] = entry["product"]
            return self._after_match(state, intake, lang, log), state

        entry = _entry(state) or kb.BY_ID["general_it"]

        if stage == "questions":
            # Bounds guard: if idx is past the list (e.g. after a re-route), move on.
            if state["idx"] >= len(entry["questions"]):
                return self._serve_current(state, intake, lang, log), state
            node = entry["questions"][state["idx"]]
            # A clarification request on a question: re-ask, don't consume it as an answer.
            if HELP.search(user_text) and not (node.get("quick")
                                               and user_text.strip() in [tr(q, lang) for q in node["quick"]]):
                log.append({"kind": "finding", "detail": f"User asked for clarification on: {node['label']}"})
                return self._q_turn(tr(node["q"], lang), [tr(q, lang) for q in node.get("quick", [])],
                                    tr(node["status"], lang), state, lang, log,
                                    prefix=tr(GENERIC_HELP, lang)), state
            log.append({"kind": "info_collected", "detail": f"{node['label']}: {user_text}"})
            if "error" in node["label"].lower() and user_text.strip().lower() not in (
                    "none", "ninguno", "no", "n/a", ""):
                mem["error_message"] = user_text
            # Confirm what was recorded so the user never wonders if a free-text
            # answer was understood (audit finding: answers were consumed silently).
            ans = user_text.strip().rstrip(".!?")
            if ans and len(ans) <= 60:
                state["pending_prefix"] = {"en": f"Thanks — noted: {ans}.",
                                           "es": f"Gracias — anotado: {ans}."}[lang]
            state["idx"] += 1
            return self._serve_current(state, intake, lang, log), state

        if stage == "steps":
            # Bounds guard: no step at this index -> transition (never IndexError).
            if state["idx"] >= len(entry["steps"]):
                return self._serve_current(state, intake, lang, log), state
            step = entry["steps"][state["idx"]]
            title = tr(step["title"], "en")
            # Clarification request: re-explain the SAME step; do not advance and do
            # not record it as a failed result (audit fix).
            if HELP.search(user_text):
                log.append({"kind": "finding", "detail": f"User asked for help understanding step: {title}"})
                help_text = tr(step.get("help", GENERIC_HELP), lang)
                return self._step_turn(step, state, lang, log, prefix=help_text), state
            log.append({"kind": "step_attempted", "detail": title})
            log.append({"kind": "step_result", "detail": user_text})
            mem["troubleshooting_steps_attempted"].append({"step": title, "result": user_text})
            if WORKED.search(user_text) and not NEGATIVE.search(user_text):
                log.append({"kind": "finding", "detail": "Issue confirmed resolved by user."})
                mem["resolved_status"] = True
                state["done"] = True
                state["stage"] = "resolved"
                return self._resolved_turn(entry, intake, lang, log, resolving_step=title), state
            state["failed_steps"].append(title)
            state["idx"] += 1
            return self._serve_current(state, intake, lang, log), state

        if stage == "scope":
            state["answers"]["scope"] = user_text
            log.append({"kind": "info_collected", "detail": f"Scope of impact: {user_text}"})
            if MULTI.search(user_text) or triage.is_multiuser(user_text):
                state["multiuser"] = True
                mem["multiuser_confirmed"] = True
                log.append({"kind": "finding",
                            "detail": "Multiple users affected. Escalation threshold met."})
            return self._go_escalate(entry, state, lang, log), state

        # Any other stage falls through to a safe escalation offer.
        return self._go_escalate(entry, state, lang, log), state

    # ----------------------------------------------------------- helpers
    def _select(self, state: dict, entry: dict, log: list):
        state["entry_id"] = entry["id"]
        state["stage"] = "questions" if entry["questions"] else "steps"
        state["idx"] = 0
        log.append({"kind": "finding",
                    "detail": f"Matched knowledge base entry '{entry['id']}' (product: {entry['product']})."})

    def _restate(self, entry: dict, lang: str) -> str:
        """One-line plain-language confirmation of the problem, shown once."""
        phrase = tr(entry["restate"], lang) if entry.get("restate") else tr(entry["issue"], lang).lower()
        return {"en": f"Got it, {phrase}.", "es": f"Entendido, {phrase}."}[lang]

    def _after_match(self, state: dict, intake: dict, lang: str, log: list) -> Turn:
        """After matching: restate the problem, then ask device ONLY when the entry
        has no targeted question of its own and the device is genuinely unknown."""
        entry = _entry(state)
        mem = state["memory"]
        # Plain-language restatement is queued to lead the next message (every issue).
        state["pending_prefix"] = self._restate(entry, lang)
        needs_device = (mem.get("device_type") is None and not state.get("asked_device")
                        and _device_relevant(entry) and not entry["questions"])
        if needs_device:
            state["asked_device"] = True
            state["stage"] = "device"
            quick = [v[0 if lang == "en" else 1] for v in config.DEVICE_TYPES.values()]
            return self._q_turn(tr(DEVICE_Q, lang), quick,
                                {"en": "Identifying device", "es": "Identificando equipo"}[lang],
                                state, lang, log)
        return self._serve_current(state, intake, lang, log)

    def _match_device(self, user_text: str) -> str:
        t = user_text.strip().lower()
        for key, labels in config.DEVICE_TYPES.items():
            for lbl in labels:
                if t == lbl.lower() or lbl.lower() in t:
                    return config.DEVICE_TYPES[key][0]
        dev = infer_device(user_text)
        return dev or config.DEVICE_TYPES["other"][0]

    def _answer_in_description(self, node: dict, desc: str) -> str | None:
        """If the user's original description clearly answers this diagnostic
        question, return that answer. Conservative: only auto-answer when the
        full option phrase appears, or a single distinctive one-word option
        matches. This deliberately still asks nuanced questions (e.g. black vs
        dim vs external monitor) rather than guessing from one shared word."""
        if not desc:
            return None
        d = f" {desc.lower()} "
        for opt in node.get("quick", []):
            en = tr(opt, "en").lower().strip()
            if f" {en} " in d:                      # exact option phrase present
                return tr(opt, "en")
            words = [w for w in re.findall(r"[a-z']+", en) if len(w) > 3]
            if len(words) == 1 and f" {words[0]} " in d:   # single distinctive keyword
                return tr(opt, "en")
        return None

    def _match_candidate(self, state: dict, user_text: str) -> dict:
        text = user_text.lower()
        best, best_score = None, -1.0
        for cid in state["candidates"]:
            e = kb.BY_ID[cid]
            score = 0.0
            for probe in (tr(e["issue"], "en").lower(), tr(e["issue"], "es").lower(), e["product"].lower()):
                if probe == text or probe in text:
                    score += 5.0
            score += sum(2.0 for kw in e["symptoms"] if kb.kw_in(text, kw))
            if score > best_score:  # ties keep earlier (higher-ranked) candidate
                best, best_score = e, score
        return best if best_score > 0 else None  # None -> caller escalates, never guesses

    def _serve_current(self, state: dict, intake: dict, lang: str, log: list) -> Turn:
        entry = _entry(state) or kb.BY_ID["general_it"]
        mem = state["memory"]
        if state["stage"] == "questions":
            # Skip any diagnostic question the user already answered in their description.
            while state["idx"] < len(entry["questions"]):
                node = entry["questions"][state["idx"]]
                known = self._answer_in_description(node, mem.get("issue_summary") or "")
                if known:
                    log.append({"kind": "info_collected",
                                "detail": f"{node['label']}: {known} (from initial description)"})
                    state["idx"] += 1
                    continue
                return self._q_turn(tr(node["q"], lang), [tr(q, lang) for q in node.get("quick", [])],
                                    tr(node["status"], lang), state, lang, log)
            state["stage"], state["idx"] = "steps", 0
        if state["stage"] == "steps":
            # Multi-user / outage: allow at most ONE quick diagnostic step to
            # distinguish a local fault from a wider outage, then escalate. Don't
            # make an affected user grind through the full single-device sequence.
            step_cap = 1 if state.get("multiuser") else len(entry["steps"])
            if state["idx"] < min(len(entry["steps"]), step_cap):
                return self._step_turn(entry["steps"][state["idx"]], state, lang, log)
            if entry.get("security_fast") or state.get("multiuser"):
                return self._go_escalate(entry, state, lang, log)
            state["stage"] = "scope"
        if state["stage"] == "scope":
            return self._q_turn(tr(SCOPE_Q, lang), [tr(q, lang) for q in SCOPE_QUICK],
                                {"en": "Checking who's affected", "es": "Verificando a quién afecta"}[lang],
                                state, lang, log)
        return self._go_escalate(entry, state, lang, log)

    def _go_escalate(self, entry, state, lang, log) -> Turn:
        """Single exit into escalation; marks the session terminal so a follow-up
        message can never index a step/question again (audit crash fix)."""
        state["memory"]["escalation_needed"] = True
        state["memory"]["resolved_status"] = False
        state["done"] = True
        state["stage"] = "escalated"
        return self._escalation_turn(entry, state, lang, log)

    # ------------------------------------------------------- corrections
    def _reroute_for_device(self, state: dict, device: str, log: list) -> dict | None:
        """When an OS-specific workstation flow is active and the user names a
        different OS, return the matching flow so we don't give wrong-OS steps."""
        cur = state.get("entry_id")
        target_id = WORKSTATION_BY_DEVICE.get(device)
        if cur in OS_SPECIFIC_ENTRIES and target_id and target_id != cur:
            log.append({"kind": "finding",
                        "detail": f"Re-routed from '{cur}' to '{target_id}' after device corrected to {device}."})
            return kb.BY_ID[target_id]
        return None

    def _apply_correction(self, state: dict, user_text: str, intake: dict,
                          lang: str, log: list) -> Turn | None:
        """Detect and apply a user correction. Returns a Turn when the correction
        changes the flow, else None to let normal processing continue."""
        mem = state["memory"]
        c = triage.detect_correction(user_text, mem)
        if not c:
            return None

        if c["kind"] == "device":
            old = mem.get("device_type")
            mem["device_type"] = c["value"]
            mem.setdefault("corrections", []).append(f"device: {old or 'unknown'} -> {c['value']}")
            log.append({"kind": "finding",
                        "detail": f"Correction: device updated from {old or 'unknown'} to {c['value']}."})
            rerouted = self._reroute_for_device(state, c["value"], log)
            if rerouted is not None:
                self._select(state, rerouted, log)
                mem["software_or_system"] = rerouted["product"]
                state["pending_prefix"] = self._restate(rerouted, lang)
                ack = {"en": f"Thanks for clarifying. Let me switch to the {c['value']} steps.",
                       "es": f"Gracias por aclararlo. Cambiaré a los pasos para {c['value']}."}[lang]
                state["pending_prefix"] = ack + " " + state["pending_prefix"]
                return self._serve_current(state, intake, lang, log)
            # Same flow: just acknowledge and re-serve the current step/question.
            state["pending_prefix"] = {
                "en": f"Got it, this is a {c['value']}. Let's continue.",
                "es": f"Entendido, es un {c['value']}. Continuemos."}[lang]
            return self._serve_current(state, intake, lang, log)

        if c["kind"] == "scope":
            state["multiuser"] = True
            mem["multiuser_confirmed"] = True
            mem.setdefault("corrections", []).append("scope: single -> multiple users")
            log.append({"kind": "finding",
                        "detail": "Correction: issue now reported as affecting multiple users; "
                                  "priority raised and routing to escalation."})
            entry = _entry(state) or kb.BY_ID["general_it"]
            return self._go_escalate(entry, state, lang, log)

        if c["kind"] == "location":
            mem["location_context"] = c["value"]
            mem.setdefault("corrections", []).append(f"location: {c['value']}")
            log.append({"kind": "info_collected",
                        "detail": f"Location context corrected to: "
                                  f"{'working remotely' if c['value'] == 'remote' else 'on campus'}."})
            return self._serve_current(state, intake, lang, log)

        if c["kind"] == "error":
            mem["error_message"] = None
            log.append({"kind": "finding", "detail": "User reports the error message changed."})
            state["pending_prefix"] = {
                "en": "Thanks, I'll note the error changed.",
                "es": "Gracias, anotaré que el error cambió."}[lang]
            return self._serve_current(state, intake, lang, log)

        if c["kind"] == "issue":
            mem.setdefault("corrections", []).append("issue: user says the matched problem was wrong")
            log.append({"kind": "finding",
                        "detail": "Correction: user says we matched the wrong issue; re-describing."})
            state["stage"] = "describe"
            state["entry_id"] = None
            state["idx"] = 0
            reask = {"en": "Sorry about that. In a sentence or two, what's the actual problem?",
                     "es": "Disculpe. En una o dos oraciones, ¿cuál es el problema real?"}[lang]
            return Turn(reply=reask, phase="intake",
                        status_label={"en": "Listening", "es": "Escuchando"}[lang],
                        issue_summary="", progress_current=1, progress_total=_total(state),
                        est_minutes_remaining=6, confidence=50, quick_replies=[], log_entries=log)
        return None

    def _reoffer_turn(self, entry, state, lang) -> Turn:
        """Re-show the escalation offer without adding new log entries (safe repeat)."""
        reply = {
            "en": "Your support summary is ready below. You can copy it into your official "
                  "support request, or start a new session any time.",
            "es": "Su resumen de soporte está listo abajo. Puede copiarlo en su solicitud oficial "
                  "de soporte, o iniciar una nueva sesión cuando quiera.",
        }[lang]
        return Turn(reply=reply, phase="escalation_offer",
                    status_label={"en": "Support summary available", "es": "Resumen disponible"}[lang],
                    issue_summary=tr(entry["issue"], lang),
                    progress_current=_total(state), progress_total=_total(state),
                    est_minutes_remaining=1, confidence=15, quick_replies=[], log_entries=[],
                    escalation_reason=tr(entry.get("esc_reason", ""), "en") if entry.get("esc_reason") else "")

    def _closed_turn(self, entry, lang) -> Turn:
        reply = {"en": "Glad that's sorted. If anything else comes up, start a new session any time.",
                 "es": "Me alegra que esté resuelto. Si surge algo más, inicie una nueva sesión cuando quiera."}[lang]
        return Turn(reply=reply, phase="resolved",
                    status_label={"en": "Resolved", "es": "Resuelto"}[lang],
                    issue_summary=tr(entry["issue"], lang) if entry else "",
                    progress_current=1, progress_total=1,
                    est_minutes_remaining=0, confidence=95, quick_replies=[], log_entries=[])

    def _base(self, state: dict, lang: str) -> dict:
        entry = _entry(state)
        return {
            "issue_summary": tr(entry["issue"], lang) if entry else "",
            "progress_current": _pos(state), "progress_total": _total(state),
            "est_minutes_remaining": max((_total(state) - _pos(state)) * 2, 1),
        }

    def _q_turn(self, question: str, quick: list, status: str, state, lang, log,
                confidence: int | None = None, prefix: str = "") -> Turn:
        b = self._base(state, lang)
        lead = prefix or state.get("pending_prefix") or ""
        if lead:
            question = f"{lead}\n\n{question}"
            state["pending_prefix"] = ""
        return Turn(reply=question, phase="diagnosis" if state["stage"] != "describe" else "intake",
                    status_label=status, quick_replies=quick, log_entries=log,
                    confidence=confidence if confidence is not None else max(70 - _pos(state) * 5, 30), **b)

    STEP_INTROS = {
        "en": ["Let's try this first.", "Okay, let's try this.", "Here's the next thing to try.",
               "Let's give this a go.", "Next, let's try this."],
        "es": ["Probemos esto primero.", "Bien, intentemos esto.", "Aquí está lo siguiente que probar.",
               "Vamos a intentar esto.", "Ahora, probemos esto."],
    }

    def _step_turn(self, step: dict, state, lang, log, prefix: str = "") -> Turn:
        b = self._base(state, lang)
        s = {"title": tr(step["title"], lang), "what": tr(step["what"], lang),
             "why": tr(step["why"], lang), "expected": tr(step["expected"], lang),
             "difficulty": step["difficulty"], "visual": step.get("visual")}
        given = state["memory"]["troubleshooting_steps_given"]
        if not prefix:
            if state.get("failed_steps"):
                # Acknowledge the failed attempt before offering the next step.
                intro = {"en": "Okay, that one didn't do it — thanks for trying. "
                               "Here's the next thing to try.",
                         "es": "Bien, eso no lo resolvió — gracias por intentarlo. "
                               "Aquí está lo siguiente que probar."}[lang]
            else:
                intro = self.STEP_INTROS[lang][len(given) % len(self.STEP_INTROS[lang])]
                # Naturally confirm the inferred device on the first step, if we have it.
                dev = state["memory"].get("device_type")
                if not given and dev and dev != config.DEVICE_TYPES["other"][0]:
                    intro = ({"en": f"Since this is a {dev}, ", "es": f"Como es un {dev}, "}[lang]
                             + intro[0].lower() + intro[1:])
            prefix = intro
        # A queued restatement leads the message on the first step (if no question preceded it).
        queued = state.get("pending_prefix") or ""
        if queued:
            prefix = f"{queued} {prefix}"
            state["pending_prefix"] = ""
        title_en = tr(step["title"], "en")
        if title_en not in given:
            given.append(title_en)
        return Turn(
            reply=prefix,
            phase="troubleshooting",
            status_label={"en": "Trying safe fixes", "es": "Probando soluciones seguras"}[lang],
            step=s,
            quick_replies=[L("it_worked", lang), L("still_not_working", lang), L("need_help_step", lang)],
            confidence=max(65 - _pos(state) * 5, 25), log_entries=log, **b)

    def _resolved_turn(self, entry, intake, lang, log, resolving_step: str = "") -> Turn:
        name = (intake.get("name") or "").split()[0] if intake.get("name") else ""
        reply = {
            "en": f"Excellent{', ' + name if name else ''}. Glad that fixed it! No ticket needed. "
                  "If it comes back, start a new session any time.",
            "es": f"Excelente{', ' + name if name else ''}. ¡me alegra que se haya arreglado! "
                  "No se necesita ticket. Si vuelve a ocurrir, inicie una nueva sesión cuando quiera.",
        }[lang]
        if resolving_step:
            log = log + [{"kind": "finding", "detail": f"Resolved by step: {resolving_step}"}]
        return Turn(reply=reply, phase="resolved",
                    status_label={"en": "Resolved", "es": "Resuelto"}[lang],
                    issue_summary=tr(entry["issue"], lang),
                    progress_current=1, progress_total=1,
                    est_minutes_remaining=0, confidence=95, quick_replies=[], log_entries=log)

    def _boundary_turn(self, state: dict, user_text: str, lang: str) -> Turn:
        """Polite refusal + safe alternative for bypass/restriction requests."""
        state["entry_id"] = "restricted_request"
        state["done"] = True
        state["stage"] = "boundary"
        reason = ("User requested help circumventing a district protection (filter, monitoring, "
                  "device management, or account access). Refused per policy; converted to a "
                  "restriction-review request for the responsible team.")
        log = [
            {"kind": "finding",
             "detail": f"Policy boundary triggered by request: {user_text[:200]}"},
            {"kind": "escalation_reason", "detail": reason},
        ]
        return Turn(
            reply=L("boundary_reply", lang),
            phase="escalation_offer",
            status_label={"en": "Policy review", "es": "Revisión de política"}[lang],
            issue_summary=tr(kb.BY_ID["restricted_request"]["issue"], lang),
            progress_current=_total(state), progress_total=_total(state),
            est_minutes_remaining=2, confidence=10, quick_replies=[],
            log_entries=log, escalation_reason=reason)

    def _escalation_turn(self, entry, state, lang, log) -> Turn:
        # The KB esc_reason is technician language ("reimage may be required");
        # it goes to the log and ticket, never to the user (audit finding).
        reason_en = tr(entry["esc_reason"], "en")
        tried = len(state.get("failed_steps") or [])
        opener = {
            "en": ("Thanks for trying those steps with me. The issue is still there, and the "
                   "remaining checks need a technician's tools and access — so I won't keep "
                   "you trying things that won't help." if tried else
                   "This one is best handled directly by a technician."),
            "es": ("Gracias por intentar esos pasos conmigo. El problema sigue, y las "
                   "verificaciones restantes requieren las herramientas y los accesos de un "
                   "técnico — así que no le haré seguir intentando cosas que no ayudarán."
                   if tried else
                   "Esto es mejor que lo atienda directamente un técnico."),
        }[lang]
        reply = {
            "en": f"{opener} I can put together a support-ready summary of everything we "
                  "covered — details you can copy into your official support request so you "
                  "don't have to explain it twice.",
            "es": f"{opener} Puedo preparar un resumen listo para soporte con todo lo que "
                  "revisamos — detalles que puede copiar en su solicitud oficial para que no "
                  "tenga que explicarlo dos veces.",
        }[lang]
        if entry.get("esc_extra"):
            reply += "\n\n" + tr(entry["esc_extra"], lang)
        log = log + [{"kind": "escalation_reason", "detail": reason_en}]
        state["memory"]["escalation_needed"] = True
        return Turn(reply=reply, phase="escalation_offer",
                    status_label={"en": "Support summary available", "es": "Resumen disponible"}[lang],
                    issue_summary=tr(entry["issue"], lang),
                    progress_current=_total(state), progress_total=_total(state),
                    est_minutes_remaining=2, confidence=15, quick_replies=[],
                    log_entries=log, escalation_reason=reason_en)


# ---------------------------------------------------------------------------
# Offline ticket generation (from KB routing + session log + intake)
# ---------------------------------------------------------------------------
def _impact_phrase(issue_text: str) -> str:
    """Best-effort operational-impact label from the issue text. Never invented
    beyond what the words support; defaults to a neutral statement."""
    t = issue_text or ""
    if any(k in t for k in ("testing", "exam", "assessment", "examen", "prueba")):
        return "may affect active testing"
    if any(k in t for k in ("attendance", "asistencia")):
        return "may affect attendance"
    if any(k in t for k in ("payroll", "nómina", "nomina")):
        return "may affect payroll/business operations"
    if any(k in t for k in ("teach", "class", "lesson", "instruction", "clase", "enseñ")):
        return "may affect instruction"
    if any(k in t for k in ("safety", "seguridad", "emergency", "emergencia")):
        return "may affect safety systems"
    return "not specified by the user"


def demo_ticket(intake: dict, log: list[dict], state: dict, reason: str, lang: str) -> dict:
    entry = kb.BY_ID.get(state.get("entry_id") or "general_it", kb.BY_ID["general_it"])
    r = entry["routing"]

    info = [e["detail"] for e in log if e["kind"] == "info_collected"]
    steps, results = [], []
    for e in log:
        if e["kind"] == "step_attempted":
            steps.append(e["detail"])
            results.append("Not recorded")
        elif e["kind"] == "step_result" and results:
            results[-1] = e["detail"]
    performed = [{"step": s, "result": x} for s, x in zip(steps, results)] or \
                [{"step": "Guided intake only", "result": "No troubleshooting steps before escalation"}]

    mem = state.get("memory", {})
    issue_text = (mem.get("issue_summary") or "").lower()

    # Priority + risk from a single consistent rule set (never contradictory).
    priority, risk, pr_rationale = triage.normalize_priority_risk(
        r["priority"], r["risk"], issue_text=issue_text,
        multiuser=bool(state.get("multiuser")), sitewide=bool(state.get("sitewide")),
        security=bool(entry.get("security_fast")),
        has_workaround=False,
    )

    # Device + OS. Prefer the confirmed device; fall back to intake; never invent.
    device = mem.get("device_type") or intake.get("device") or "Not provided"
    os_known = device if device in ("Windows laptop/desktop", "Mac", "Chromebook",
                                     "iPad / tablet") else "Not provided"

    scope = state.get("answers", {}).get("scope")
    if state.get("sitewide"):
        scope_txt = "Site-wide / outage reported"
    elif state.get("multiuser"):
        scope_txt = "Multiple users affected"
    elif scope:
        scope_txt = scope
    else:
        scope_txt = "Single user (scope not explicitly confirmed)"
    impact_txt = f"{scope_txt}. Operational impact: {_impact_phrase(issue_text)}."

    # Human-readable location. Never emit bare number fragments like
    # "Lincoln Elementary, 2, 114" (audit finding: mangled location in ticket).
    loc_bits = []
    if intake.get("campus") and intake["campus"] != "Not provided":
        loc_bits.append(intake["campus"])
    bldg = str(intake.get("building") or "").strip()
    if bldg:
        loc_bits.append(bldg if bldg.lower().startswith(("building", "edificio"))
                        else f"Building {bldg}")
    room = str(intake.get("room") or "").strip()
    if room and room not in loc_bits:
        loc_bits.append(f"Room {room}" if re.fullmatch(r"[0-9]{1,5}[a-z]?", room.lower())
                        else room)
    location = ", ".join(loc_bits) or "Not provided"
    errors = [d.split(":", 1)[1].strip() for d in info
              if "error" in d.split(":", 1)[0].lower()
              and d.split(":", 1)[1].strip().lower() not in ("none", "ninguno", "no", "n/a", "")]
    if mem.get("error_message") and mem["error_message"] not in errors:
        errors.append(mem["error_message"])

    # What a technician would still need to ask (based on what's missing).
    tech_needs = []
    if not errors:
        tech_needs.append("Exact on-screen error message (not observed during self-service).")
    if os_known == "Not provided":
        tech_needs.append("Operating system / exact device model (not confirmed by the user).")
    if not state.get("multiuser") and not scope:
        tech_needs.append("Whether any other users are affected (single-user assumed).")
    tech_needs.append(f"Confirm current state after: {tr(r['path'], 'en')}")
    if mem.get("corrections"):
        tech_needs.append("Note: user corrected earlier details mid-session (see corrections).")

    description = (
        f"Reported by {intake.get('name', 'user')} ({intake.get('role', 'unknown role')}) at {location}. "
        f"Device: {device}. Product: {entry['product']}. "
        + ("Information collected: " + "; ".join(info) + ". " if info else "")
        + f"Escalated because: {reason}"
    )

    # The user's own words always travel with the ticket, so a routing mistake
    # can never erase what was actually reported (audit finding).
    reported = (mem.get("issue_summary") or "").strip()

    return {
        "title": f"{tr(entry['issue'], 'en')}, {intake.get('campus', '')}".rstrip(", "),
        "user_reported": reported,
        "executive_summary": (
            f"{tr(entry['issue'], 'en')} affecting "
            f"{'multiple users' if state.get('multiuser') else 'one user'} "
            f"at {intake.get('campus') or 'an unspecified site'}. Guided self-service troubleshooting "
            f"{'was completed without resolution' if steps else 'was not applicable'}; technician action required."
        ),
        "detailed_description": description,
        "symptoms": info or [tr(entry["issue"], "en")],
        "environment": f"District-managed environment. Operating system: {os_known}. "
                       f"KB entry: {entry['id']}. "
                       f"Session language: {'Spanish' if lang == 'es' else 'English'}. "
                       f"Data sensitivity: {intake.get('data_type', 'unknown')}.",
        "device_information": device,
        "operating_system": os_known,
        "user_location": location,
        "user_name": intake.get("name", "Not provided"),
        "user_email": intake.get("email", "Not provided"),
        "user_role": intake.get("role", "Not provided"),
        "applications_involved": [entry["product"]],
        "error_messages": errors or ["Not observed"],
        "business_impact": impact_txt,
        "impact": impact_txt,
        "affected_scope": scope_txt,
        "troubleshooting_performed": performed,
        "technician_needs": tech_needs,
        "corrections": mem.get("corrections", []),
        "assignment_group": r["group"],
        "assignment_rationale": tr(r["rationale"], "en"),
        "category": r["category"],
        "subcategory": r["sub"],
        "priority": priority,
        "priority_rationale": pr_rationale,
        "risk_level": risk,
        "suggested_resolution_path": tr(r["path"], "en"),
        # Rule-based routing confidence = heuristic KB match strength (0-100).
        # NOT model certainty, ticket accuracy, or production performance.
        "routing_confidence": int(round(float(state.get("match_confidence") or 0.0) * 100)),
        "routing_confidence_basis": "Heuristic knowledge-base match strength (deterministic). "
                                    "Not model certainty or ticket accuracy.",
        "estimated_technician_effort": r["effort"],
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "ticket_ref": "DRAFT-" + datetime.now().strftime("%Y%m%d-%H%M%S"),
    }
