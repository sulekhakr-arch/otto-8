"""
Streamlit UI for Otto Bot Creator — all 6 Parlant ATOM components:
  1. Bot Profile        (name, description, mode, iterations)
  2. Guidelines         (condition / action / criticality)
  3. Journeys           (trigger conditions, nodes, flow)
  4. Terms (Glossary)   (name, definition, synonyms)
  5. Context Variables  (name, description, default value)
  6. Tools              (view registered services + Heyo tool schemas)

Run (venv active, api_server on 8801, Parlant on 8800):
    streamlit run streamlit_app.py
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import streamlit as st
from dotenv import dotenv_values, load_dotenv

load_dotenv()

_APP_DIR = Path(__file__).resolve().parent
HEYO_SPEC_PATH = _APP_DIR / "data" / "heyo_bot_spec.json"
ENV_PATH = _APP_DIR / ".env"

DEFAULT_API = os.getenv("OTTO_API_BASE", "http://localhost:8801").rstrip("/")
DEFAULT_PARLANT = os.getenv("PARLANT_BASE", "http://localhost:8800").rstrip("/")
# Seconds to poll for assistant reply after sending a chat message (no UI control).
CHAT_WAIT_ASSISTANT_S = 6.0

DEFAULT_CREATE_SPEC = """{
  "name": "Support Bot",
  "purpose": "Answer common customer questions.",
  "scope": "Order status, returns, and account help.",
  "target_users": "Registered customers",
  "use_cases": ["Check order status", "Start a return"],
  "tone": "Friendly and clear",
  "personality": "Patient and concise",
  "tools": ["none"],
  "constraints": ["Cannot change payment methods"],
  "guardrails": ["Verify identity before sharing order details"],
  "guidelines": [
    {
      "condition": "When the user asks about an order",
      "action": "Ask for order number if missing, then look up status.",
      "criticality": "high"
    }
  ],
  "journeys": [
    {
      "title": "Order lookup",
      "description": "Help the user find their order status.",
      "conditions": ["User mentions order or tracking", "User provides order number"]
    }
  ],
  "composition_mode": "FLUID",
  "max_engine_iterations": 3
}"""


# ── Utility helpers ────────────────────────────────────────────────────────────

def normalize_bot_list(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if isinstance(payload.get("bots"), list):
            return payload["bots"]
        if isinstance(payload.get("items"), list):
            return payload["items"]
    return []


def _detail_error(r: httpx.Response) -> str:
    try:
        data = r.json()
        d = data.get("detail")
        if isinstance(d, list):
            return "; ".join(str(x) for x in d)
        if isinstance(d, dict):
            return json.dumps(d)
        if d is not None:
            return str(d)
        return str(data.get("error", r.text))[:500]
    except Exception:
        return r.text[:500] or f"HTTP {r.status_code}"


def _composition_mode_index(raw: Any) -> int:
    opts = ["FLUID", "COMPOSITED", "STRICT"]
    cm = str(raw or "fluid").strip().upper()
    if cm == "COMPOSITED" or "COMPOSIT" in cm:
        return 1
    if cm == "STRICT":
        return 2
    return 0


# ── Guideline helpers ──────────────────────────────────────────────────────────

def guidelines_draft(bot: dict) -> list[dict]:
    out = []
    for g in bot.get("guidelines") or []:
        out.append(
            {
                "id": g.get("id"),
                "condition": g.get("condition") or "",
                "action": g.get("action"),
                "description": g.get("description"),
                "criticality": str(g.get("criticality") or "medium").lower(),
            }
        )
    return out


def apply_guidelines_sync(client: httpx.Client, api: str, bot_id: str, bot: dict, text: str) -> None:
    items = json.loads(text)
    if not isinstance(items, list):
        raise ValueError("Root must be a JSON array.")

    old_list = bot.get("guidelines") or []
    old_by_id = {g["id"]: g for g in old_list if g.get("id")}
    old_ids = {g["id"] for g in old_list if g.get("id")}
    new_ids = {i["id"] for i in items if isinstance(i, dict) and i.get("id")}

    for gid in old_ids:
        if gid not in new_ids:
            r = client.delete(f"{api}/guidelines/{gid}")
            if r.status_code >= 400:
                raise RuntimeError(_detail_error(r))

    for item in items:
        if not isinstance(item, dict):
            continue
        gid = item.get("id")
        if gid and gid in old_by_id:
            body = {k: item[k] for k in ("condition", "action", "description", "criticality") if k in item}
            if body:
                r = client.patch(f"{api}/guidelines/{gid}", json=body)
                if r.status_code >= 400:
                    raise RuntimeError(_detail_error(r))
        else:
            cond = (item.get("condition") or "").strip()
            if not cond:
                raise ValueError("Each new guideline needs a non-empty condition.")
            payload = {
                "condition": cond,
                "action": item.get("action"),
                "description": item.get("description"),
                "criticality": str(item.get("criticality") or "medium").lower(),
            }
            r = client.post(f"{api}/bots/{bot_id}/guidelines", json=payload)
            if r.status_code >= 400:
                raise RuntimeError(_detail_error(r))


# ── Journey helpers ────────────────────────────────────────────────────────────

def journeys_draft(bot: dict) -> list[dict]:
    out = []
    for j in bot.get("journeys") or []:
        cond = j.get("conditions") or []
        out.append(
            {
                "id": j.get("id"),
                "title": j.get("title") or "",
                "description": j.get("description") or "",
                "conditions": list(cond) if isinstance(cond, list) else [],
            }
        )
    return out


def apply_journeys_sync(client: httpx.Client, api: str, bot_id: str, bot: dict, text: str) -> None:
    items = json.loads(text)
    if not isinstance(items, list):
        raise ValueError("Root must be a JSON array.")

    old_list = bot.get("journeys") or []
    old_by_id = {j["id"]: j for j in old_list if j.get("id")}
    old_ids = {j["id"] for j in old_list if j.get("id")}
    new_ids = {i["id"] for i in items if isinstance(i, dict) and i.get("id")}

    for jid in old_ids:
        if jid not in new_ids:
            r = client.delete(f"{api}/journeys/{jid}")
            if r.status_code >= 400:
                raise RuntimeError(_detail_error(r))

    for item in items:
        if not isinstance(item, dict):
            continue
        jid = item.get("id")
        if jid and jid in old_by_id:
            body = {k: item[k] for k in ("title", "description", "conditions") if k in item}
            if body:
                r = client.patch(f"{api}/journeys/{jid}", json=body)
                if r.status_code >= 400:
                    raise RuntimeError(_detail_error(r))
        else:
            title = (item.get("title") or "").strip()
            desc = (item.get("description") or "").strip()
            conds = item.get("conditions")
            conditions = [str(c).strip() for c in conds if str(c).strip()] if isinstance(conds, list) else []
            if not title or not desc or not conditions:
                raise ValueError("Each new journey needs title, description, and a non-empty conditions array.")
            r = client.post(
                f"{api}/bots/{bot_id}/journeys",
                json={"title": title, "description": desc, "conditions": conditions},
            )
            if r.status_code >= 400:
                raise RuntimeError(_detail_error(r))


# ── Terms (Glossary) helpers ───────────────────────────────────────────────────

def fetch_terms(p_client: httpx.Client, parlant: str) -> list[dict]:
    r = p_client.get(f"{parlant}/terms")
    if r.status_code >= 400:
        return []
    return r.json() or []


def apply_terms_sync(p_client: httpx.Client, parlant: str, text: str) -> None:
    items = json.loads(text)
    if not isinstance(items, list):
        raise ValueError("Root must be a JSON array.")

    existing = fetch_terms(p_client, parlant)
    old_by_id = {t["id"]: t for t in existing if t.get("id")}
    old_ids = set(old_by_id.keys())
    new_ids = {i["id"] for i in items if isinstance(i, dict) and i.get("id")}

    for tid in old_ids:
        if tid not in new_ids:
            r = p_client.delete(f"{parlant}/terms/{tid}")
            if r.status_code >= 400:
                raise RuntimeError(f"Delete term {tid}: {r.text[:200]}")

    for item in items:
        if not isinstance(item, dict):
            continue
        tid = item.get("id")
        if tid and tid in old_by_id:
            body = {k: item[k] for k in ("name", "description", "synonyms") if k in item}
            if body:
                r = p_client.patch(f"{parlant}/terms/{tid}", json=body)
                if r.status_code >= 400:
                    raise RuntimeError(f"Update term: {r.text[:200]}")
        else:
            name = (item.get("name") or "").strip()
            if not name:
                raise ValueError("Each new term needs a non-empty name.")
            r = p_client.post(f"{parlant}/terms", json={
                "name": name,
                "description": item.get("description") or "",
                "synonyms": item.get("synonyms") or [],
            })
            if r.status_code >= 400:
                raise RuntimeError(f"Create term '{name}': {r.text[:200]}")


def render_terms_tab(p_client: httpx.Client, parlant: str) -> None:
    terms = fetch_terms(p_client, parlant)

    col_l, col_r = st.columns([3, 1])
    with col_l:
        st.subheader(f"Terms / Glossary  ({len(terms)} terms)")
    with col_r:
        if st.button("Refresh", key="refresh_terms"):
            st.rerun()

    st.caption(
        "Shared business vocabulary — standardizes how the agent interprets words "
        "like WABA, Superadmin Number, Escalation, etc. across every conversation."
    )

    # ── JSON bulk editor ───────────────────────────────────────────────────────
    with st.expander("Bulk edit as JSON", expanded=False):
        draft = [
            {"id": t.get("id"), "name": t.get("name") or "", "description": t.get("description") or "", "synonyms": t.get("synonyms") or []}
            for t in terms
        ]
        t_text = st.text_area("Terms JSON", value=json.dumps(draft, indent=2), height=320, key="terms_json")
        if st.button("Apply terms JSON", key="apply_terms_json"):
            try:
                apply_terms_sync(p_client, parlant, t_text)
                st.success("Terms saved.")
                st.rerun()
            except Exception as e:
                st.error(str(e))

    st.divider()

    # ── Per-term form editor ───────────────────────────────────────────────────
    if not terms:
        st.info("No terms defined yet. Add one below or load the Heyo template from the **Create bot** tab.")
    else:
        for i, t in enumerate(terms):
            tid = t.get("id")
            label = (t.get("name") or "(unnamed)")[:60]
            with st.expander(f"{i + 1}. **{label}**", expanded=False):
                with st.form(f"term_form_{tid}"):
                    name = st.text_input("Name", value=t.get("name") or "")
                    desc = st.text_area("Definition", value=t.get("description") or "", height=100)
                    syns = st.text_area(
                        "Synonyms (one per line)",
                        value="\n".join(t.get("synonyms") or []),
                        height=80,
                    )
                    c1, c2 = st.columns(2)
                    with c1:
                        save = st.form_submit_button("Save")
                    with c2:
                        delete = st.form_submit_button("Delete", type="primary")
                if save:
                    syn_list = [s.strip() for s in syns.splitlines() if s.strip()]
                    r = p_client.patch(f"{parlant}/terms/{tid}", json={
                        "name": name.strip(),
                        "description": desc.strip(),
                        "synonyms": syn_list,
                    })
                    if r.status_code >= 400:
                        st.error(r.text[:200])
                    else:
                        st.success("Term saved.")
                        st.rerun()
                if delete:
                    r = p_client.delete(f"{parlant}/terms/{tid}")
                    if r.status_code >= 400:
                        st.error(r.text[:200])
                    else:
                        st.success("Deleted.")
                        st.rerun()

    st.divider()
    st.markdown("**Add new term**")
    with st.form("add_term_form"):
        new_name = st.text_input("Name")
        new_desc = st.text_area("Definition", height=80)
        new_syns = st.text_area("Synonyms (one per line)", height=64)
        if st.form_submit_button("Add term"):
            if not new_name.strip():
                st.error("Name is required.")
            else:
                syn_list = [s.strip() for s in new_syns.splitlines() if s.strip()]
                r = p_client.post(f"{parlant}/terms", json={
                    "name": new_name.strip(),
                    "description": new_desc.strip(),
                    "synonyms": syn_list,
                })
                if r.status_code >= 400:
                    st.error(r.text[:200])
                else:
                    st.success(f"Added term: {new_name}")
                    st.rerun()


# ── Context Variables helpers ──────────────────────────────────────────────────

def fetch_context_vars(p_client: httpx.Client, parlant: str) -> list[dict]:
    r = p_client.get(f"{parlant}/context-variables")
    if r.status_code >= 400:
        return []
    return r.json() or []


def _fetch_cv_default(p_client: httpx.Client, parlant: str, vid: str) -> str:
    r = p_client.get(f"{parlant}/context-variables/{vid}/default")
    if r.status_code >= 400:
        return ""
    data = r.json()
    if data is None:
        return ""
    val = data.get("data") if isinstance(data, dict) else data
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    return json.dumps(val, indent=2)


def _set_cv_default(p_client: httpx.Client, parlant: str, vid: str, raw_text: str) -> None:
    raw = raw_text.strip()
    if not raw:
        return
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = raw
    r = p_client.put(f"{parlant}/context-variables/{vid}/default", json={"data": parsed})
    if r.status_code >= 400:
        raise RuntimeError(f"Set default value failed: {r.text[:200]}")


def apply_context_vars_sync(p_client: httpx.Client, parlant: str, text: str) -> None:
    items = json.loads(text)
    if not isinstance(items, list):
        raise ValueError("Root must be a JSON array.")

    existing = fetch_context_vars(p_client, parlant)
    old_by_id = {cv["id"]: cv for cv in existing if cv.get("id")}
    old_ids = set(old_by_id.keys())
    new_ids = {i["id"] for i in items if isinstance(i, dict) and i.get("id")}

    for vid in old_ids:
        if vid not in new_ids:
            r = p_client.delete(f"{parlant}/context-variables/{vid}")
            if r.status_code >= 400:
                raise RuntimeError(f"Delete variable {vid}: {r.text[:200]}")

    for item in items:
        if not isinstance(item, dict):
            continue
        vid = item.get("id")
        if vid and vid in old_by_id:
            body = {k: item[k] for k in ("name", "description") if k in item}
            if body:
                r = p_client.patch(f"{parlant}/context-variables/{vid}", json=body)
                if r.status_code >= 400:
                    raise RuntimeError(f"Update variable: {r.text[:200]}")
            if "default_value" in item:
                _set_cv_default(p_client, parlant, vid, json.dumps(item["default_value"]) if not isinstance(item["default_value"], str) else item["default_value"])
        else:
            name = (item.get("name") or "").strip()
            if not name:
                raise ValueError("Each new variable needs a non-empty name.")
            r = p_client.post(f"{parlant}/context-variables", json={
                "name": name,
                "description": item.get("description") or "",
            })
            if r.status_code >= 400:
                raise RuntimeError(f"Create variable '{name}': {r.text[:200]}")
            if "default_value" in item and item["default_value"] is not None:
                new_vid = r.json().get("id")
                if new_vid:
                    dv = item["default_value"]
                    _set_cv_default(p_client, parlant, new_vid, json.dumps(dv) if not isinstance(dv, str) else dv)


def render_context_vars_tab(p_client: httpx.Client, parlant: str) -> None:
    cvars = fetch_context_vars(p_client, parlant)

    col_l, col_r = st.columns([3, 1])
    with col_l:
        st.subheader(f"Context Variables  ({len(cvars)} variables)")
    with col_r:
        if st.button("Refresh", key="refresh_cvars"):
            st.rerun()

    st.caption(
        "Persistent state and configuration the agent carries across conversation turns. "
        "Holds identity, phone format rules, API settings, and privacy flags."
    )

    # ── JSON bulk editor ───────────────────────────────────────────────────────
    with st.expander("Bulk edit as JSON", expanded=False):
        draft = []
        for cv in cvars:
            vid = cv.get("id")
            dval_raw = _fetch_cv_default(p_client, parlant, vid) if vid else ""
            try:
                dval = json.loads(dval_raw) if dval_raw else None
            except json.JSONDecodeError:
                dval = dval_raw
            draft.append({
                "id": vid,
                "name": cv.get("name") or "",
                "description": cv.get("description") or "",
                "default_value": dval,
            })
        cv_text = st.text_area("Context Variables JSON", value=json.dumps(draft, indent=2), height=320, key="cvars_json")
        if st.button("Apply context variables JSON", key="apply_cvars_json"):
            try:
                apply_context_vars_sync(p_client, parlant, cv_text)
                st.success("Context variables saved.")
                st.rerun()
            except Exception as e:
                st.error(str(e))

    st.divider()

    # ── Per-variable form editor ───────────────────────────────────────────────
    if not cvars:
        st.info("No context variables defined yet. Add one below or load the Heyo template from the **Create bot** tab.")
    else:
        for i, cv in enumerate(cvars):
            vid = cv.get("id")
            label = (cv.get("name") or "(unnamed)")[:60]
            default_val = _fetch_cv_default(p_client, parlant, vid) if vid else ""
            with st.expander(f"{i + 1}. **{label}**", expanded=False):
                with st.form(f"cv_form_{vid}"):
                    name = st.text_input("Name", value=cv.get("name") or "")
                    desc = st.text_area("Description", value=cv.get("description") or "", height=100)
                    dval = st.text_area(
                        "Default value (string, number, JSON object/array)",
                        value=default_val,
                        height=80,
                        help="Stored at key 'default'. Leave blank to skip. Objects/arrays must be valid JSON.",
                    )
                    c1, c2 = st.columns(2)
                    with c1:
                        save = st.form_submit_button("Save")
                    with c2:
                        delete = st.form_submit_button("Delete", type="primary")
                if save:
                    r = p_client.patch(f"{parlant}/context-variables/{vid}", json={
                        "name": name.strip(),
                        "description": desc.strip(),
                    })
                    if r.status_code >= 400:
                        st.error(f"Save failed: {r.text[:200]}")
                    else:
                        if dval.strip():
                            try:
                                _set_cv_default(p_client, parlant, vid, dval.strip())
                            except RuntimeError as e:
                                st.warning(str(e))
                        st.success("Variable saved.")
                        st.rerun()
                if delete:
                    r = p_client.delete(f"{parlant}/context-variables/{vid}")
                    if r.status_code >= 400:
                        st.error(r.text[:200])
                    else:
                        st.success("Deleted.")
                        st.rerun()

    st.divider()
    st.markdown("**Add new context variable**")
    with st.form("add_cv_form"):
        new_name = st.text_input("Name")
        new_desc = st.text_area("Description", height=80)
        new_dval = st.text_area(
            "Default value (optional — string, number, or JSON object/array)",
            height=64,
        )
        if st.form_submit_button("Add variable"):
            if not new_name.strip():
                st.error("Name is required.")
            else:
                r = p_client.post(f"{parlant}/context-variables", json={
                    "name": new_name.strip(),
                    "description": new_desc.strip(),
                })
                if r.status_code >= 400:
                    st.error(r.text[:200])
                else:
                    new_vid = r.json().get("id")
                    if new_vid and new_dval.strip():
                        try:
                            _set_cv_default(p_client, parlant, new_vid, new_dval.strip())
                        except RuntimeError as e:
                            st.warning(str(e))
                    st.success(f"Added: {new_name}")
                    st.rerun()


# ── Tools view ─────────────────────────────────────────────────────────────────

def render_tools_tab(p_client: httpx.Client, parlant: str) -> None:
    st.subheader("Tools & Services")
    st.caption(
        "Tools allow the agent to perform internal actions (API calls, human handoffs) during conversations. "
        "Register HTTP or SDK services via Parlant to wire them."
    )

    r = p_client.get(f"{parlant}/services")
    services = r.json() if r.status_code < 400 else []

    st.markdown("**Registered Parlant services**")
    if services:
        for svc in services:
            tools = svc.get("tools") or []
            icon = "🔧"
            with st.expander(f"{icon} {svc.get('name')} · {svc.get('kind')} · {len(tools)} tool(s)", expanded=False):
                st.json({
                    "name": svc.get("name"),
                    "kind": svc.get("kind"),
                    "url": svc.get("url"),
                    "tools": tools,
                })
    else:
        st.info("No external services registered.")

    st.divider()
    st.markdown("**Heyo tool schemas (from PDF spec)**")
    st.caption(
        "Reference config for the 3 Heyo tools. "
        "To activate HTTP tools, register them as Parlant SDK services in `server.py`."
    )

    # Load tools_info from the spec file if available
    tools_info: list[dict] = []
    if HEYO_SPEC_PATH.is_file():
        try:
            spec = json.loads(HEYO_SPEC_PATH.read_text(encoding="utf-8"))
            tools_info = spec.get("tools_info") or []
        except Exception:
            pass

    if not tools_info:
        st.warning("Could not load tool schemas from heyo_bot_spec.json.")
        return

    for t in tools_info:
        enabled = t.get("enabled", True)
        status = "✅ enabled" if enabled else "⏸️ disabled (future use)"
        t_type = t.get("type", "?")
        with st.expander(f"**{t['name']}**  ·  {t_type}  ·  {status}", expanded=False):
            c1, c2 = st.columns([2, 1])
            with c1:
                st.markdown(f"**Description:** {t.get('description', '')}")
                if t.get("endpoint"):
                    st.markdown(f"**Endpoint:** `{t.get('method', 'POST')} {t['endpoint']}`")
                if t.get("response_visibility"):
                    st.markdown(f"**Response visibility:** `{t['response_visibility']}`")
                if t.get("response_mode"):
                    st.markdown(f"**Response mode:** `{t['response_mode']}`")
                if t.get("allowed_guidelines"):
                    st.markdown("**Allowed guidelines:** " + ", ".join(f"`{g}`" for g in t["allowed_guidelines"]))
            with c2:
                params = t.get("parameters")
                if params:
                    st.markdown("**Parameters:**")
                    for p in params:
                        req = " *(required)*" if p.get("required") else ""
                        st.markdown(f"- `{p['name']}` ({p.get('type', '?')}){req}")
                        if p.get("description"):
                            st.caption(p["description"])
            if t.get("auth_header"):
                st.info(
                    f"Auth header: `{t['auth_header']}` — token stored in `account_detail_api_auth_token` context variable."
                )


# ── Bot profile / guideline / journey form renderers ──────────────────────────

def render_bot_profile_form(client: httpx.Client, api: str, full_bot: dict) -> None:
    bot_id = full_bot["id"]
    st.subheader("Bot profile")
    with st.form("bot_profile_form"):
        name = st.text_input("Name", value=full_bot.get("name") or "")
        desc = st.text_area(
            "Description",
            value=full_bot.get("description") or "",
            height=180,
            help="Full Parlant agent description (purpose, scope, tone, glossary notes, etc.).",
        )
        opts = ["FLUID", "COMPOSITED", "STRICT"]
        cm = st.selectbox("Composition mode", opts, index=_composition_mode_index(full_bot.get("composition_mode")))
        mei = st.number_input(
            "Max engine iterations",
            min_value=1,
            max_value=30,
            value=int(full_bot.get("max_engine_iterations") or 3),
        )
        submitted = st.form_submit_button("Save bot profile")
        if submitted:
            r = client.patch(f"{api}/bots/{bot_id}", json={
                "name": name.strip(),
                "description": desc.strip(),
                "composition_mode": cm,
                "max_engine_iterations": int(mei),
            })
            if r.status_code >= 400:
                st.error(_detail_error(r))
            else:
                st.success("Bot profile saved.")
                st.rerun()


def render_guideline_forms(client: httpx.Client, api: str, full_bot: dict) -> None:
    st.subheader("Guidelines (form editor)")
    st.caption("Conditional behaviour rules — condition, action, criticality. Use JSON tab for bulk edits.")
    glist = full_bot.get("guidelines") or []
    if not glist:
        st.info("No guidelines on this bot.")
        return
    for i, g in enumerate(glist):
        gid = g.get("id")
        if not gid:
            continue
        label = (g.get("condition") or "(no condition)")[:72]
        with st.expander(f"{i + 1}. {label}", expanded=False):
            with st.form(f"guideline_form_{gid}"):
                cond = st.text_area("Condition", value=g.get("condition") or "", height=72)
                act = st.text_area("Action", value=g.get("action") or "", height=120)
                dscr = st.text_area("Description", value=g.get("description") or "", height=64)
                crit_opts = ["low", "medium", "high"]
                cr = str(g.get("criticality") or "medium").lower()
                crit_i = crit_opts.index(cr) if cr in crit_opts else 1
                crit = st.selectbox("Criticality", crit_opts, index=crit_i)
                if st.form_submit_button("Save this guideline"):
                    r = client.patch(f"{api}/guidelines/{gid}", json={
                        "condition": cond.strip(),
                        "action": act.strip() or None,
                        "description": dscr.strip() or None,
                        "criticality": crit,
                    })
                    if r.status_code >= 400:
                        st.error(_detail_error(r))
                    else:
                        st.success("Guideline saved.")
                        st.rerun()


def render_journey_forms(client: httpx.Client, api: str, full_bot: dict) -> None:
    st.subheader("Journeys (form editor)")
    st.caption("Conversation flows — trigger conditions, node descriptions, edges. One condition per line.")
    jlist = full_bot.get("journeys") or []
    if not jlist:
        st.info("No journeys on this bot.")
        return
    for i, j in enumerate(jlist):
        jid = j.get("id")
        if not jid:
            continue
        title0 = j.get("title") or f"Journey {i + 1}"
        with st.expander(f"{i + 1}. {title0}", expanded=False):
            with st.form(f"journey_form_{jid}"):
                title = st.text_input("Title", value=j.get("title") or "")
                desc = st.text_area("Description / nodes / flow", value=j.get("description") or "", height=160)
                conds = j.get("conditions") or []
                cond_text = st.text_area(
                    "Trigger conditions (one per line)",
                    value="\n".join(str(c) for c in conds),
                    height=100,
                )
                if st.form_submit_button("Save this journey"):
                    lines = [ln.strip() for ln in cond_text.splitlines() if ln.strip()]
                    if not lines:
                        st.error("Add at least one condition line.")
                    else:
                        r = client.patch(
                            f"{api}/journeys/{jid}",
                            json={"title": title.strip(), "description": desc.strip(), "conditions": lines},
                        )
                        if r.status_code >= 400:
                            st.error(_detail_error(r))
                        else:
                            st.success("Journey saved.")
                            st.rerun()


# ── Chat helpers ───────────────────────────────────────────────────────────────

def fetch_messages(client: httpx.Client, api: str, session_id: str) -> list[dict]:
    r = client.get(f"{api}/sessions/{session_id}/messages")
    if r.status_code >= 400:
        return []
    data = r.json()
    return data.get("messages") or []


def _message_source(m: dict) -> str:
    return str(m.get("source") or m.get("event_source") or "").strip().lower()


def _count_ai_messages(messages: list[dict]) -> int:
    return sum(1 for m in messages if _message_source(m) in ("ai_agent", "assistant", "agent"))


def wait_for_ai_reply(
    client: httpx.Client,
    api: str,
    session_id: str,
    baseline_ai_count: int,
    timeout_s: float = 10.0,
    poll_interval_s: float = 0.35,
) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(poll_interval_s)
        messages = fetch_messages(client, api, session_id)
        if _count_ai_messages(messages) > baseline_ai_count:
            return True
    return False


def render_chat_messages(messages: list[dict]) -> None:
    for m in messages:
        if m.get("kind") != "message" and m.get("event_kind") != "message":
            continue
        src = _message_source(m)
        raw = m.get("message")
        if raw is None and isinstance(m.get("data"), dict):
            raw = m["data"].get("message")
        content = (raw or "").strip()
        if not content:
            continue
        if src in ("customer", "user"):
            with st.chat_message("user"):
                st.write(content)
        elif src in ("ai_agent", "assistant", "agent"):
            with st.chat_message("assistant"):
                st.write(content)
        elif src == "system":
            with st.chat_message("assistant"):
                st.caption("system")
                st.write(content)


def ensure_session(client: httpx.Client, api: str, bot_id: str) -> str:
    key = f"parlant_session_{bot_id}"
    if key not in st.session_state:
        r = client.post(f"{api}/bots/{bot_id}/sessions")
        if r.status_code >= 400:
            raise RuntimeError(_detail_error(r))
        st.session_state[key] = r.json()["session_id"]
    return st.session_state[key]


# ── Image helpers ──────────────────────────────────────────────────────────────

def describe_image_for_support(image_bytes: bytes, mime: str) -> str:
    env_values = dotenv_values(str(ENV_PATH)) if ENV_PATH.exists() else {}
    api_key = str(env_values.get("OPENAI_API_KEY") or "").strip().strip('"').strip("'")
    if not api_key:
        return "[Image attached — set OPENAI_API_KEY in .env to auto-describe images]"
    mime = mime or "image/jpeg"
    try:
        from openai import OpenAI

        try:
            from PIL import Image

            with Image.open(io.BytesIO(image_bytes)) as im:
                im = im.convert("RGB")
                im.thumbnail((960, 960))
                out = io.BytesIO()
                im.save(out, format="JPEG", quality=72, optimize=True)
                image_bytes = out.getvalue()
                mime = "image/jpeg"
        except Exception:
            pass

        b64 = base64.standard_b64encode(image_bytes).decode("ascii")
        data_url = f"data:{mime};base64,{b64}"
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "You assist a customer-support bot. Describe the image for an agent: "
                                "visible text, errors, phone numbers, UI labels, and anything useful "
                                "for troubleshooting. Be concise; use bullets if many items."
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": data_url, "detail": "low"}},
                    ],
                }
            ],
            max_tokens=220,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        return f"[Image analysis failed: {exc}]"


def _image_placeholder_analysis_disabled() -> str:
    return (
        "The customer attached an image in the chat UI, but automatic image-to-text is turned off "
        "(Analyze images in the sidebar). You cannot see the pixels. "
        "Reply helpfully: briefly acknowledge the attachment, ask them to describe what it shows "
        "(error text, phone numbers, screen name) or to turn on 'Analyze images' in the UI and resend. "
        "Do not say you are unable to help with images in general—focus on this limitation and next steps."
    )


def describe_image_cached(image_bytes: bytes, mime: str) -> str:
    cache = st.session_state.setdefault("image_desc_cache", {})
    digest = hashlib.sha1(image_bytes).hexdigest()
    if digest in cache:
        return cache[digest]
    text = describe_image_for_support(image_bytes, mime)
    cache[digest] = text
    return text


def load_heyo_bot_template() -> str:
    if not HEYO_SPEC_PATH.is_file():
        raise FileNotFoundError(f"Missing {HEYO_SPEC_PATH}")
    return HEYO_SPEC_PATH.read_text(encoding="utf-8")


# ── Seed terms + context vars from spec ───────────────────────────────────────

def seed_terms_from_spec(p_client: httpx.Client, parlant: str, spec: dict) -> tuple[int, list[str]]:
    """Push all terms from spec into Parlant. Returns (count_added, errors)."""
    terms = spec.get("terms") or []
    errors: list[str] = []
    added = 0
    for t in terms:
        name = (t.get("name") or "").strip()
        if not name:
            continue
        r = p_client.post(f"{parlant}/terms", json={
            "name": name,
            "description": t.get("description") or "",
            "synonyms": t.get("synonyms") or [],
        })
        if r.status_code >= 400:
            errors.append(f"Term '{name}': {r.text[:100]}")
        else:
            added += 1
    return added, errors


def seed_context_vars_from_spec(p_client: httpx.Client, parlant: str, spec: dict) -> tuple[int, list[str]]:
    """Push all context variables from spec into Parlant. Returns (count_added, errors)."""
    cvars = spec.get("context_variables") or []
    errors: list[str] = []
    added = 0
    for cv in cvars:
        name = (cv.get("name") or "").strip()
        if not name:
            continue
        r = p_client.post(f"{parlant}/context-variables", json={
            "name": name,
            "description": cv.get("description") or "",
        })
        if r.status_code >= 400:
            errors.append(f"Var '{name}': {r.text[:100]}")
            continue
        added += 1
        dv = cv.get("default_value")
        if dv is not None:
            vid = r.json().get("id")
            if vid:
                try:
                    _set_cv_default(p_client, parlant, vid, json.dumps(dv) if not isinstance(dv, str) else dv)
                except RuntimeError as e:
                    errors.append(str(e))
    return added, errors


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    st.set_page_config(page_title="Bot console", layout="wide")

    st.sidebar.markdown("### API endpoints")
    api = st.sidebar.text_input("Otto API (bots / chat)", value=DEFAULT_API).rstrip("/")
    parlant = st.sidebar.text_input("Parlant (terms / context vars)", value=DEFAULT_PARLANT).rstrip("/")

    image_scan_enabled = st.sidebar.toggle(
        "Analyze images",
        value=False,
        help="Disable to avoid vision latency. Text messages remain fast.",
    )
    st.sidebar.caption("Run `python server.py` (8800) and `python api_server.py` (8801) first.")

    tab_bots, tab_terms, tab_cvars, tab_create = st.tabs([
        "🤖  Bots & Chat",
        "📖  Terms (Glossary)",
        "🔧  Context Variables",
        "➕  Create Bot",
    ])

    # ── TAB: Bots & Chat ───────────────────────────────────────────────────────
    with tab_bots:
        bots: list[dict] = []
        browse_err: str | None = None
        try:
            with httpx.Client(timeout=60.0) as client:
                r = client.get(f"{api}/bots")
                if r.status_code >= 400:
                    browse_err = _detail_error(r)
                else:
                    bots = normalize_bot_list(r.json())
        except httpx.RequestError as e:
            browse_err = str(e)

        if browse_err:
            st.error(f"Could not load bots: {browse_err}")
        elif not bots:
            st.info("No bots yet. Open the **Create Bot** tab to add one.")
        else:
            labels = [f"{b.get('name') or 'Untitled'} ({b.get('id', '')[:8]}…)" for b in bots]
            choice = st.selectbox("Select bot", range(len(bots)), format_func=lambda i: labels[i])
            bot = bots[choice]
            bot_id = bot["id"]

            c1, c2 = st.columns(2)
            with c1:
                if st.button("Refresh list", key="ref_bots"):
                    st.session_state.pop(f"parlant_session_{bot_id}", None)
                    st.rerun()
            with c2:
                if st.button("Delete this bot", type="primary"):
                    with httpx.Client(timeout=60.0) as client:
                        dr = client.delete(f"{api}/bots/{bot_id}")
                        if dr.status_code >= 400:
                            st.error(_detail_error(dr))
                        else:
                            st.session_state.pop(f"parlant_session_{bot_id}", None)
                            st.success("Deleted.")
                            st.rerun()

            with httpx.Client(timeout=60.0) as client:
                fr = client.get(f"{api}/bots/{bot_id}")
                if fr.status_code >= 400:
                    st.warning(f"Could not load bot details: {_detail_error(fr)}")
                    full_bot: dict = bot
                else:
                    full_bot = fr.json()

            st.caption(
                f"{len(full_bot.get('guidelines') or [])} guidelines · "
                f"{len(full_bot.get('journeys') or [])} journeys"
            )

            sub_edit, sub_chat, sub_g, sub_j, sub_tools = st.tabs([
                "Edit bot (forms)", "Chat", "Guidelines (JSON)", "Journeys (JSON)", "Tools",
            ])

            with sub_edit:
                with httpx.Client(timeout=60.0) as client:
                    fr2 = client.get(f"{api}/bots/{bot_id}")
                    if fr2.status_code >= 400:
                        st.error(_detail_error(fr2))
                    else:
                        fb = fr2.json()
                        render_bot_profile_form(client, api, fb)
                        st.divider()
                        render_guideline_forms(client, api, fb)
                        st.divider()
                        render_journey_forms(client, api, fb)

            with sub_chat:
                with httpx.Client(timeout=120.0) as client:
                    try:
                        sid = ensure_session(client, api, bot_id)
                    except RuntimeError as e:
                        st.error(str(e))
                        sid = None
                    if sid:
                        st.caption(f"Session `{sid[:12]}…` — use **Refresh list** to start a new session.")
                        msgs = fetch_messages(client, api, sid)
                        render_chat_messages(msgs)
                        up = st.file_uploader(
                            "Attach image (screenshot, error, etc.)",
                            type=["png", "jpg", "jpeg", "webp", "gif"],
                            key=f"chat_img_{bot_id}",
                        )
                        if st.button(
                            "Send image only",
                            key=f"img_only_{bot_id}",
                            help="Sends a vision summary to the bot.",
                            disabled=not up,
                        ):
                            ai_before = _count_ai_messages(msgs)
                            raw = up.getvalue()
                            mime = up.type or "image/jpeg"
                            if image_scan_enabled:
                                with st.spinner("Analyzing image…"):
                                    analysis = describe_image_cached(raw, mime)
                            else:
                                analysis = _image_placeholder_analysis_disabled()
                            ir = client.post(
                                f"{api}/sessions/{sid}/messages",
                                json={"message": f"[User sent an image]\n{analysis}"},
                            )
                            if ir.status_code >= 400:
                                st.error(_detail_error(ir))
                            else:
                                with st.spinner("Assistant is responding…"):
                                    wait_for_ai_reply(
                                        client, api, sid, ai_before, CHAT_WAIT_ASSISTANT_S, 0.3
                                    )
                                st.rerun()

                        if prompt := st.chat_input("Message"):
                            ai_before = _count_ai_messages(msgs)
                            user_msg = prompt.strip()
                            if up is not None:
                                with st.spinner("Analyzing image…"):
                                    img_part = (
                                        describe_image_cached(up.getvalue(), up.type or "image/jpeg")
                                        if image_scan_enabled
                                        else _image_placeholder_analysis_disabled()
                                    )
                                user_msg = f"{user_msg}\n\n[Image details for support]\n{img_part}".strip()
                            pr = client.post(f"{api}/sessions/{sid}/messages", json={"message": user_msg})
                            if pr.status_code >= 400:
                                st.error(_detail_error(pr))
                            else:
                                with st.spinner("Assistant is responding…"):
                                    wait_for_ai_reply(
                                        client, api, sid, ai_before, CHAT_WAIT_ASSISTANT_S, 0.3
                                    )
                                st.rerun()

            with sub_g:
                with httpx.Client(timeout=60.0) as client:
                    gr = client.get(f"{api}/bots/{bot_id}")
                    if gr.status_code >= 400:
                        st.error(_detail_error(gr))
                    else:
                        full = gr.json()

                        # ── File upload ──────────────────────────────────
                        uploaded_g = st.file_uploader(
                            "Upload guidelines JSON file  (replaces text area below)",
                            type=["json"],
                            key=f"g_upload_{bot_id}",
                        )
                        if uploaded_g is not None:
                            try:
                                g_file_text = uploaded_g.read().decode("utf-8")
                                json.loads(g_file_text)  # validate
                                st.session_state[f"gjson_val_{bot_id}"] = g_file_text
                                st.success(f"Loaded {uploaded_g.name} — click Apply below.")
                            except Exception as e:
                                st.error(f"Invalid JSON file: {e}")

                        default_g = st.session_state.get(
                            f"gjson_val_{bot_id}",
                            json.dumps(guidelines_draft(full), indent=2),
                        )
                        g_text = st.text_area(
                            "Guidelines JSON",
                            value=default_g,
                            height=320,
                            key=f"gjson_{bot_id}",
                        )
                        if st.button("Apply guidelines JSON", key="apply_g"):
                            try:
                                apply_guidelines_sync(client, api, bot_id, full, g_text)
                                st.session_state.pop(f"gjson_val_{bot_id}", None)
                                st.success("Saved.")
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))

                        # ── Download current ─────────────────────────────
                        st.download_button(
                            "Download guidelines JSON",
                            data=json.dumps(guidelines_draft(full), indent=2),
                            file_name="guidelines.json",
                            mime="application/json",
                            key=f"g_dl_{bot_id}",
                        )

            with sub_j:
                with httpx.Client(timeout=60.0) as client:
                    jr = client.get(f"{api}/bots/{bot_id}")
                    if jr.status_code >= 400:
                        st.error(_detail_error(jr))
                    else:
                        full = jr.json()

                        # ── File upload ──────────────────────────────────
                        uploaded_j = st.file_uploader(
                            "Upload journeys JSON file  (replaces text area below)",
                            type=["json"],
                            key=f"j_upload_{bot_id}",
                        )
                        if uploaded_j is not None:
                            try:
                                j_file_text = uploaded_j.read().decode("utf-8")
                                json.loads(j_file_text)  # validate
                                st.session_state[f"jjson_val_{bot_id}"] = j_file_text
                                st.success(f"Loaded {uploaded_j.name} — click Apply below.")
                            except Exception as e:
                                st.error(f"Invalid JSON file: {e}")

                        default_j = st.session_state.get(
                            f"jjson_val_{bot_id}",
                            json.dumps(journeys_draft(full), indent=2),
                        )
                        j_text = st.text_area(
                            "Journeys JSON",
                            value=default_j,
                            height=320,
                            key=f"jjson_{bot_id}",
                        )
                        if st.button("Apply journeys JSON", key="apply_j"):
                            try:
                                apply_journeys_sync(client, api, bot_id, full, j_text)
                                st.session_state.pop(f"jjson_val_{bot_id}", None)
                                st.success("Saved.")
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))

                        # ── Download current ─────────────────────────────
                        st.download_button(
                            "Download journeys JSON",
                            data=json.dumps(journeys_draft(full), indent=2),
                            file_name="journeys.json",
                            mime="application/json",
                            key=f"j_dl_{bot_id}",
                        )

            with sub_tools:
                with httpx.Client(timeout=30.0) as p_client:
                    render_tools_tab(p_client, parlant)

    # ── TAB: Terms (Glossary) ──────────────────────────────────────────────────
    with tab_terms:
        try:
            with httpx.Client(timeout=30.0) as p_client:
                render_terms_tab(p_client, parlant)
        except httpx.RequestError as e:
            st.error(f"Cannot reach Parlant at {parlant}: {e}")

    # ── TAB: Context Variables ─────────────────────────────────────────────────
    with tab_cvars:
        try:
            with httpx.Client(timeout=30.0) as p_client:
                render_context_vars_tab(p_client, parlant)
        except httpx.RequestError as e:
            st.error(f"Cannot reach Parlant at {parlant}: {e}")

    # ── TAB: Create Bot ────────────────────────────────────────────────────────
    with tab_create:
        st.markdown("Paste a full bot spec as JSON and click **Create bot**.")
        if "create_bot_spec_json" not in st.session_state:
            st.session_state.create_bot_spec_json = DEFAULT_CREATE_SPEC

        b1, b2 = st.columns(2)
        with b1:
            if st.button("Load Heyo (Riva) template"):
                try:
                    st.session_state.create_bot_spec_json = load_heyo_bot_template()
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))
        with b2:
            if st.button("Reset to minimal example"):
                st.session_state.create_bot_spec_json = DEFAULT_CREATE_SPEC
                st.rerun()

        st.text_area(
            "Bot spec (JSON)",
            height=420,
            key="create_bot_spec_json",
            help="Load Heyo (Riva) template for the full 6-component PDF spec.",
        )

        also_seed = st.checkbox(
            "Also seed Terms + Context Variables from spec",
            value=True,
            help="If the spec has 'terms' and 'context_variables' keys, push them to Parlant too.",
        )

        if st.button("Create bot", type="primary"):
            raw_spec = st.session_state.get("create_bot_spec_json") or "{}"
            try:
                body = json.loads(raw_spec)
            except json.JSONDecodeError as e:
                st.error(f"Invalid JSON: {e}")
            else:
                with httpx.Client(timeout=120.0) as client:
                    cr = client.post(f"{api}/bots", json=body)
                    if cr.status_code >= 400:
                        st.error(_detail_error(cr))
                    else:
                        out = cr.json()
                        st.success(f"Created bot: **{out.get('bot_name')}** (`{out.get('bot_id')}`)")

                        if also_seed and (body.get("terms") or body.get("context_variables")):
                            with httpx.Client(timeout=60.0) as p_client:
                                if body.get("terms"):
                                    t_added, t_errs = seed_terms_from_spec(p_client, parlant, body)
                                    if t_errs:
                                        st.warning(f"Terms seeded with errors: {'; '.join(t_errs)}")
                                    else:
                                        st.success(f"Seeded {t_added} terms into Parlant.")
                                if body.get("context_variables"):
                                    cv_added, cv_errs = seed_context_vars_from_spec(p_client, parlant, body)
                                    if cv_errs:
                                        st.warning(f"Context vars seeded with errors: {'; '.join(cv_errs)}")
                                    else:
                                        st.success(f"Seeded {cv_added} context variables into Parlant.")

                        st.rerun()


if __name__ in ("__main__", "__mp_main__"):
    main()
