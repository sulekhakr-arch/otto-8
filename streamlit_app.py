"""
Simple Streamlit UI for the Bot Management API.

Run (with venv active, API on 8801, Parlant on 8800):
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
from dotenv import load_dotenv

load_dotenv()

_APP_DIR = Path(__file__).resolve().parent
HEYO_SPEC_PATH = _APP_DIR / "data" / "heyo_bot_spec.json"

# In Docker, set OTTO_API_BASE=http://api:8801 (see docker-compose.yml).
DEFAULT_API = os.getenv("OTTO_API_BASE", "http://localhost:8801").rstrip("/")

# Minimal valid POST /bots body (edit before create)
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
            body = {}
            for k in ("condition", "action", "description", "criticality"):
                if k in item:
                    body[k] = item[k]
            if not body:
                continue
            r = client.patch(f"{api}/guidelines/{gid}", json=body)
            if r.status_code >= 400:
                raise RuntimeError(_detail_error(r))
        else:
            cond = (item.get("condition") or "").strip() if isinstance(item.get("condition"), str) else ""
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
            body = {}
            for k in ("title", "description", "conditions"):
                if k in item:
                    body[k] = item[k]
            if not body:
                continue
            r = client.patch(f"{api}/journeys/{jid}", json=body)
            if r.status_code >= 400:
                raise RuntimeError(_detail_error(r))
        else:
            title = (item.get("title") or "").strip() if isinstance(item.get("title"), str) else ""
            desc = (item.get("description") or "").strip() if isinstance(item.get("description"), str) else ""
            conds = item.get("conditions")
            conditions = (
                [str(c).strip() for c in conds if str(c).strip()]
                if isinstance(conds, list)
                else []
            )
            if not title or not desc or not conditions:
                raise ValueError("Each new journey needs title, description, and a non-empty conditions array.")
            r = client.post(
                f"{api}/bots/{bot_id}/journeys",
                json={"title": title, "description": desc, "conditions": conditions},
            )
            if r.status_code >= 400:
                raise RuntimeError(_detail_error(r))


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
    """Poll briefly for a new assistant message after send."""
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


def load_heyo_bot_template() -> str:
    """Full Heyo / Riva spec from PDF (committed JSON, no API secrets)."""
    if not HEYO_SPEC_PATH.is_file():
        raise FileNotFoundError(f"Missing {HEYO_SPEC_PATH}")
    return HEYO_SPEC_PATH.read_text(encoding="utf-8")


def describe_image_for_support(image_bytes: bytes, mime: str) -> str:
    """Use OpenAI vision so the Parlant bot receives text about the screenshot."""
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return (
            "[Image attached — set OPENAI_API_KEY in your environment to auto-describe images]"
        )
    mime = mime or "image/jpeg"
    try:
        from openai import OpenAI

        # Downscale to keep vision requests lightweight/fast.
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
            # If Pillow is unavailable or conversion fails, send original bytes.
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


def describe_image_cached(image_bytes: bytes, mime: str) -> str:
    """Cache image descriptions so repeated sends are instant."""
    cache = st.session_state.setdefault("image_desc_cache", {})
    digest = hashlib.sha1(image_bytes).hexdigest()
    if digest in cache:
        return cache[digest]
    text = describe_image_for_support(image_bytes, mime)
    cache[digest] = text
    return text


def _composition_mode_index(raw: Any) -> int:
    opts = ["FLUID", "COMPOSITED", "STRICT"]
    cm = str(raw or "fluid").strip().upper()
    if cm == "COMPOSITED" or "COMPOSIT" in cm:
        return 1
    if cm == "STRICT":
        return 2
    return 0


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
        cm = st.selectbox(
            "Composition mode",
            opts,
            index=_composition_mode_index(full_bot.get("composition_mode")),
        )
        mei = st.number_input(
            "Max engine iterations",
            min_value=1,
            max_value=30,
            value=int(full_bot.get("max_engine_iterations") or 3),
        )
        submitted = st.form_submit_button("Save bot profile")
        if submitted:
            payload = {
                "name": name.strip(),
                "description": desc.strip(),
                "composition_mode": cm,
                "max_engine_iterations": int(mei),
            }
            r = client.patch(f"{api}/bots/{bot_id}", json=payload)
            if r.status_code >= 400:
                st.error(_detail_error(r))
            else:
                st.success("Bot profile saved.")
                st.rerun()


def render_guideline_forms(client: httpx.Client, api: str, full_bot: dict) -> None:
    st.subheader("Guidelines (form editor)")
    st.caption("Edit condition, action, description, and criticality. JSON tab is still available for bulk edits.")
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
                    body = {
                        "condition": cond.strip(),
                        "action": act.strip() or None,
                        "description": dscr.strip() or None,
                        "criticality": crit,
                    }
                    r = client.patch(f"{api}/guidelines/{gid}", json=body)
                    if r.status_code >= 400:
                        st.error(_detail_error(r))
                    else:
                        st.success("Guideline saved.")
                        st.rerun()


def render_journey_forms(client: httpx.Client, api: str, full_bot: dict) -> None:
    st.subheader("Journeys (form editor)")
    st.caption("One trigger condition per line. Save updates the journey in Parlant.")
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
                desc = st.text_area("Description", value=j.get("description") or "", height=140)
                conds = j.get("conditions") or []
                cond_text = st.text_area(
                    "Conditions (one per line)",
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
                            json={
                                "title": title.strip(),
                                "description": desc.strip(),
                                "conditions": lines,
                            },
                        )
                        if r.status_code >= 400:
                            st.error(_detail_error(r))
                        else:
                            st.success("Journey saved.")
                            st.rerun()


def main() -> None:
    st.set_page_config(page_title="Bot console", layout="wide")
    st.sidebar.markdown("### API")
    api = st.sidebar.text_input("Base URL", value=DEFAULT_API).rstrip("/")
    st.sidebar.markdown("### Chat speed")
    fast_mode = st.sidebar.toggle("Fast mode", value=True, help="Shorter waits for snappier UI.")
    wait_timeout_s = st.sidebar.slider(
        "Wait for assistant (seconds)",
        min_value=0.0,
        max_value=20.0,
        value=6.0 if fast_mode else 10.0,
        step=0.5,
        help="0 = do not block; messages appear on next refresh/send.",
    )
    image_scan_enabled = st.sidebar.toggle(
        "Analyze images",
        value=False,
        help="Turn off to avoid vision latency. You can still send text messages quickly.",
    )
    st.sidebar.caption("Run `python server.py` (8800) and `python api_server.py` (8801) first.")

    tab_browse, tab_create = st.tabs(["Bots & chat", "Create bot"])

    with tab_browse:
        # Do not `return` from this block — Streamlit must still run the Create bot tab below.
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
            st.info("No bots yet. Open the **Create bot** tab to add one.")
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
                    st.warning(f"Could not load bot details: {_detail_error(fr)} — using list data only.")
                    full_bot: dict = bot
                else:
                    full_bot = fr.json()

            st.caption(
                f"{len(full_bot.get('guidelines') or [])} guidelines · "
                f"{len(full_bot.get('journeys') or [])} journeys"
            )

            sub_forms, sub_chat, sub_g, sub_j = st.tabs(
                ["Edit bot (forms)", "Chat", "Guidelines (JSON)", "Journeys (JSON)"]
            )

            with sub_forms:
                with httpx.Client(timeout=60.0) as client:
                    fr = client.get(f"{api}/bots/{bot_id}")
                    if fr.status_code >= 400:
                        st.error(_detail_error(fr))
                    else:
                        fb = fr.json()
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
                        st.caption(
                            f"Session `{sid[:12]}…` — use **Refresh list** to start a new session."
                        )
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
                            help="Sends a vision summary to the bot (needs OPENAI_API_KEY).",
                            disabled=not up,
                        ):
                            ai_before = _count_ai_messages(msgs)
                            raw = up.getvalue()
                            mime = up.type or "image/jpeg"
                            if image_scan_enabled:
                                with st.spinner("Analyzing image…"):
                                    analysis = describe_image_cached(raw, mime)
                            else:
                                analysis = "[Image attached. Image analysis is disabled in sidebar.]"
                            payload = f"[User sent an image]\n{analysis}"
                            ir = client.post(
                                f"{api}/sessions/{sid}/messages",
                                json={"message": payload},
                            )
                            if ir.status_code >= 400:
                                st.error(_detail_error(ir))
                            else:
                                if wait_timeout_s > 0:
                                    with st.spinner("Assistant is responding…"):
                                        wait_for_ai_reply(
                                            client,
                                            api,
                                            sid,
                                            baseline_ai_count=ai_before,
                                            timeout_s=float(wait_timeout_s),
                                            poll_interval_s=0.25 if fast_mode else 0.35,
                                        )
                                st.rerun()

                        if prompt := st.chat_input("Message"):
                            ai_before = _count_ai_messages(msgs)
                            user_msg = prompt.strip()
                            if up is not None:
                                with st.spinner("Analyzing image…"):
                                    img_part = describe_image_cached(up.getvalue(), up.type or "image/jpeg") if image_scan_enabled else "[Image attached. Image analysis is disabled in sidebar.]"
                                    user_msg = (
                                        f"{user_msg}\n\n[Image details for support]\n"
                                        f"{img_part}"
                                    ).strip()
                            pr = client.post(
                                f"{api}/sessions/{sid}/messages",
                                json={"message": user_msg},
                            )
                            if pr.status_code >= 400:
                                st.error(_detail_error(pr))
                            else:
                                if wait_timeout_s > 0:
                                    with st.spinner("Assistant is responding…"):
                                        wait_for_ai_reply(
                                            client,
                                            api,
                                            sid,
                                            baseline_ai_count=ai_before,
                                            timeout_s=float(wait_timeout_s),
                                            poll_interval_s=0.25 if fast_mode else 0.35,
                                        )
                                st.rerun()

            with sub_g:
                with httpx.Client(timeout=60.0) as client:
                    gr = client.get(f"{api}/bots/{bot_id}")
                    if gr.status_code >= 400:
                        st.error(_detail_error(gr))
                    else:
                        full = gr.json()
                        g_text = st.text_area(
                            "Guidelines JSON",
                            value=json.dumps(guidelines_draft(full), indent=2),
                            height=320,
                            key=f"gjson_{bot_id}",
                        )
                        if st.button("Apply guidelines JSON", key="apply_g"):
                            try:
                                apply_guidelines_sync(client, api, bot_id, full, g_text)
                                st.success("Saved.")
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))

            with sub_j:
                with httpx.Client(timeout=60.0) as client:
                    jr = client.get(f"{api}/bots/{bot_id}")
                    if jr.status_code >= 400:
                        st.error(_detail_error(jr))
                    else:
                        full = jr.json()
                        j_text = st.text_area(
                            "Journeys JSON",
                            value=json.dumps(journeys_draft(full), indent=2),
                            height=320,
                            key=f"jjson_{bot_id}",
                        )
                        if st.button("Apply journeys JSON", key="apply_j"):
                            try:
                                apply_journeys_sync(client, api, bot_id, full, j_text)
                                st.success("Saved.")
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))

    with tab_create:
        st.markdown("Paste a full bot spec as JSON (`POST /bots`).")
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
            help="Use “Load Heyo (Riva) template” for the full PDF-aligned spec (data/heyo_bot_spec.json).",
        )
        if st.button("Create bot", type="primary"):
            try:
                body = json.loads(st.session_state.get("create_bot_spec_json") or "{}")
            except json.JSONDecodeError as e:
                st.error(f"Invalid JSON: {e}")
            else:
                with httpx.Client(timeout=120.0) as client:
                    cr = client.post(f"{api}/bots", json=body)
                    if cr.status_code >= 400:
                        st.error(_detail_error(cr))
                    else:
                        out = cr.json()
                        st.success(f"Created: {out.get('bot_name')} (`{out.get('bot_id')}`)")
                        st.rerun()


if __name__ in ("__main__", "__mp_main__"):
    main()
