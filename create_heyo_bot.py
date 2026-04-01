"""
create_heyo_bot.py — One-click setup for the Riva (Heyo Support) bot in Parlant.

What this script does:
  1. Creates the Riva agent in Parlant (skips if already exists)
  2. Creates all 17 guidelines with tool associations:
       - heyo_get_account_detail  → account verification / escalation guidelines
       - pause_for_human_handoff  → escalation guidelines
       - analyze_image            → image-support guideline
  3. Creates all 5 journeys
  4. Creates all 18 terms (glossary)
  5. Seeds all 14 context variables with default values
  6. Prints a full summary

Usage:
  python create_heyo_bot.py                   # uses defaults (Parlant on :8800)
  PARLANT_API_BASE_URL=http://... python create_heyo_bot.py

Requires: server.py (with heyo_tools imported) to be RUNNING first,
          so the `built-in` service exposes the Heyo tools.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

_APP_DIR = Path(__file__).resolve().parent
SPEC_PATH = _APP_DIR / "data" / "heyo_bot_spec.json"

PARLANT = os.getenv("PARLANT_API_BASE_URL", "http://localhost:8800").rstrip("/")
TIMEOUT = int(os.getenv("PARLANT_API_TIMEOUT", "30"))

# Tool IDs once the heyo_tools are registered in the built-in service
TOOL_ACCOUNT_DETAIL = {"service_name": "built-in", "tool_name": "heyo_get_account_detail"}
TOOL_HANDOFF = {"service_name": "built-in", "tool_name": "pause_for_human_handoff"}
TOOL_IMAGE = {"service_name": "built-in", "tool_name": "analyze_image"}

# Maps guideline description keywords → tools to associate
# (matched against the guideline's `description` field)
GUIDELINE_TOOL_MAP: dict[str, list[dict]] = {
    "1.8": [TOOL_ACCOUNT_DETAIL],
    "1.9": [TOOL_ACCOUNT_DETAIL, TOOL_HANDOFF],
    "1.15": [TOOL_HANDOFF],
    "1.16": [TOOL_ACCOUNT_DETAIL, TOOL_HANDOFF],
    "image": [TOOL_IMAGE],
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

async def _get(client: httpx.AsyncClient, path: str) -> tuple[int, object]:
    r = await client.get(f"{PARLANT}{path}")
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text


async def _post(client: httpx.AsyncClient, path: str, body: dict) -> tuple[int, object]:
    r = await client.post(f"{PARLANT}{path}", json=body)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text


async def _patch(client: httpx.AsyncClient, path: str, body: dict) -> tuple[int, object]:
    r = await client.patch(f"{PARLANT}{path}", json=body)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text


async def _put(client: httpx.AsyncClient, path: str, body: dict) -> tuple[int, object]:
    r = await client.put(f"{PARLANT}{path}", json=body)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _crit(value: str | None) -> str:
    return {"high": "high", "medium": "medium", "low": "low"}.get(
        str(value or "medium").lower(), "medium"
    )


def _build_description(spec: dict) -> str:
    return "\n".join([
        f"Purpose: {spec.get('purpose', '')}",
        f"Scope: {spec.get('scope', '')}",
        f"Target users: {spec.get('target_users', '')}",
        f"Tone: {spec.get('tone', '')}",
        f"Personality: {spec.get('personality', '')}",
        f"Use cases: {'; '.join(spec.get('use_cases', []))}",
        f"Constraints: {'; '.join(spec.get('constraints', []))}",
        f"Guardrails: {'; '.join(spec.get('guardrails', []))}",
    ])


def _tools_for_guideline(g: dict) -> list[dict]:
    desc = (g.get("description") or "").lower()
    action = (g.get("action") or "").lower()
    combined = desc + " " + action
    tools: list[dict] = []
    for keyword, tool_list in GUIDELINE_TOOL_MAP.items():
        if keyword.lower() in combined:
            for t in tool_list:
                if t not in tools:
                    tools.append(t)
    return tools


def ok(status: int) -> bool:
    return 200 <= status < 300


def p_ok(label: str, status: int, body: object) -> None:
    if ok(status):
        print(f"  ✅ {label}")
    else:
        print(f"  ❌ {label} → HTTP {status}: {str(body)[:120]}")


# ---------------------------------------------------------------------------
# Main setup
# ---------------------------------------------------------------------------

async def main() -> None:
    print("=" * 60)
    print("  Riva (Heyo Support) Bot — Parlant Setup")
    print("=" * 60)
    print(f"  Parlant: {PARLANT}\n")

    # Load spec
    if not SPEC_PATH.is_file():
        sys.exit(f"❌ Spec not found: {SPEC_PATH}")
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:

        # ── 0. Verify Parlant is reachable ─────────────────────────────────
        status, _ = await _get(client, "/healthz")
        if not ok(status):
            sys.exit(f"❌ Parlant not reachable at {PARLANT} (HTTP {status})")
        print("  Parlant is reachable.")

        # ── 1. Check if Riva already exists ────────────────────────────────
        status, agents_raw = await _get(client, "/agents")
        if not ok(status):
            sys.exit(f"❌ Cannot list agents: {agents_raw}")
        agents = agents_raw if isinstance(agents_raw, list) else []
        existing = next((a for a in agents if a.get("name") == spec["name"]), None)
        if existing:
            agent_id = existing["id"]
            print(f"  Agent '{spec['name']}' already exists (ID: {agent_id}).")
            print("  To recreate: delete it first via the UI or API, then re-run.\n")
        else:
            # ── 2. Create agent ────────────────────────────────────────────
            print("\n[1/6] Creating Riva agent …")
            status, body = await _post(client, "/agents", {
                "name": spec["name"],
                "description": _build_description(spec),
                "composition_mode": "fluid",
                "max_engine_iterations": spec.get("max_engine_iterations", 6),
            })
            p_ok("Create agent", status, body)
            if not ok(status):
                sys.exit(f"❌ Cannot continue without agent: {body}")
            agent_id = body["id"]
            print(f"  Agent ID: {agent_id}")

        agent_tag = f"agent:{agent_id}"

        # ── 3. Verify tools are available in built-in service ──────────────
        print("\n[2/6] Verifying built-in tools …")
        status, svc = await _get(client, "/services/built-in")
        tools_available: list[str] = []
        if ok(status):
            tools_available = [t.get("name", "") for t in (svc.get("tools") or [])]
        heyo_tool_names = ["heyo_get_account_detail", "pause_for_human_handoff", "analyze_image"]
        missing_tools = [t for t in heyo_tool_names if t not in tools_available]
        if missing_tools:
            print(f"  ⚠️  Heyo tools NOT yet in built-in service: {missing_tools}")
            print("     → Restart server.py (which now imports heyo_tools.py) first.")
            print("     → Tool associations will be SKIPPED for now.")
            can_associate_tools = False
        else:
            print(f"  ✅ All 3 Heyo tools found in built-in service.")
            can_associate_tools = True

        # ── 4. Create guidelines ───────────────────────────────────────────
        print(f"\n[3/6] Creating {len(spec['guidelines'])} guidelines …")
        guidelines_ok = 0
        for i, g in enumerate(spec["guidelines"], 1):
            status, body = await _post(client, "/guidelines", {
                "condition": g["condition"],
                "action": g.get("action", ""),
                "description": g.get("description", ""),
                "criticality": _crit(g.get("criticality")),
                "tags": [agent_tag],
            })
            if ok(status):
                gid = body.get("id")
                guidelines_ok += 1

                # Associate tools with this guideline if available
                if can_associate_tools and gid:
                    tools = _tools_for_guideline(g)
                    if tools:
                        ts, tb = await _patch(client, f"/guidelines/{gid}", {
                            "tool_associations": {"add": tools}
                        })
                        tool_names = [t["tool_name"] for t in tools]
                        if ok(ts):
                            print(f"    {i:2d}. ✅ + tools {tool_names}")
                        else:
                            print(f"    {i:2d}. ✅  (tool assoc failed: {tb})")
                    else:
                        print(f"    {i:2d}. ✅")
            else:
                print(f"    {i:2d}. ❌ HTTP {status}: {str(body)[:80]}")
        print(f"  {guidelines_ok}/{len(spec['guidelines'])} guidelines created.")

        # ── 4b. Add image-analysis guideline ──────────────────────────────
        print("\n  + Adding image-analysis guideline …")
        img_g = {
            "condition": "The customer shares or mentions an image, screenshot, or photo",
            "action": (
                "Use the analyze_image tool to extract visible text, error messages, "
                "phone numbers, and UI labels from the image. Use the description internally "
                "to understand the customer's issue. Never mention the tool by name to the customer."
            ),
            "description": "image — enable analyze_image tool for screenshot support (PDF 4.3 extension)",
            "criticality": "medium",
        }
        status, body = await _post(client, "/guidelines", {
            **img_g,
            "tags": [agent_tag],
        })
        if ok(status):
            gid = body.get("id")
            if can_associate_tools and gid:
                ts, _ = await _patch(client, f"/guidelines/{gid}", {
                    "tool_associations": {"add": [TOOL_IMAGE]}
                })
                suffix = "+ analyze_image" if ok(ts) else "(tool assoc failed)"
                print(f"  ✅ Image guideline created {suffix}")
            else:
                print("  ✅ Image guideline created")
        else:
            print(f"  ❌ Image guideline: HTTP {status}")

        # ── 5. Create journeys ─────────────────────────────────────────────
        print(f"\n[4/6] Creating {len(spec['journeys'])} journeys …")
        journeys_ok = 0
        for i, j in enumerate(spec["journeys"], 1):
            status, body = await _post(client, "/journeys", {
                "title": j["title"],
                "description": j["description"],
                "conditions": j["conditions"],
                "tags": [agent_tag],
            })
            p_ok(f"{i:2d}. {j['title']}", status, body)
            if ok(status):
                journeys_ok += 1
        print(f"  {journeys_ok}/{len(spec['journeys'])} journeys created.")

        # ── 6. Seed terms ──────────────────────────────────────────────────
        print(f"\n[5/6] Seeding {len(spec.get('terms', []))} terms …")
        terms_ok = 0
        for t in spec.get("terms", []):
            name = (t.get("name") or "").strip()
            if not name:
                continue
            status, body = await _post(client, "/terms", {
                "name": name,
                "description": t.get("description") or "",
                "synonyms": t.get("synonyms") or [],
            })
            if ok(status):
                terms_ok += 1
            else:
                print(f"    ⚠️  Term '{name}': HTTP {status}")
        print(f"  {terms_ok}/{len(spec.get('terms', []))} terms seeded.")

        # ── 7. Seed context variables ──────────────────────────────────────
        cv_spec = spec.get("context_variables", [])
        print(f"\n[6/6] Seeding {len(cv_spec)} context variables …")
        cvars_ok = 0
        for cv in cv_spec:
            name = (cv.get("name") or "").strip()
            if not name:
                continue
            status, body = await _post(client, "/context-variables", {
                "name": name,
                "description": cv.get("description") or "",
            })
            if not ok(status):
                print(f"    ⚠️  Variable '{name}': HTTP {status}")
                continue
            vid = body.get("id")
            cvars_ok += 1
            dv = cv.get("default_value")
            if dv is not None and vid:
                raw = json.dumps(dv) if not isinstance(dv, str) else dv
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = raw
                sv, sb = await _put(client, f"/context-variables/{vid}/default", {"data": parsed})
                if not ok(sv):
                    print(f"    ⚠️  '{name}' default value: HTTP {sv}")
        print(f"  {cvars_ok}/{len(cv_spec)} context variables seeded.")

    # ── Summary ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  ✅ Riva (Heyo Support) bot setup complete!")
    print(f"  Agent ID : {agent_id}")
    print(f"  Parlant  : {PARLANT}")
    if not can_associate_tools:
        print()
        print("  ⚠️  IMPORTANT: Tool associations were skipped.")
        print("     Restart server.py (it now imports heyo_tools.py)")
        print("     then run this script again to wire the tools.")
    print()
    print("  Next steps:")
    print("  1. streamlit run streamlit_app.py  → open UI")
    print("  2. Select the Riva bot → Chat tab")
    print("  3. Upload a screenshot or type a message to test")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
