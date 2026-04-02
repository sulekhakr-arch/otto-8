# Otto / Heyo — implementation summary

This document summarizes work added or changed in this codebase (Streamlit console, Heyo/Riva bot, tools, Docker, and UI tweaks).

---

## Heyo (Riva) support bot in Parlant

- **`data/heyo_bot_spec.json`** — Full spec aligned with the Heyo Parlant PDF: profile fields, **17 guidelines**, **5 journeys**, **18 glossary terms**, **14 context variables**, and **`tools_info`** (reference schemas for the three tools).
- **`create_heyo_bot.py`** — One-shot script (async HTTP to Parlant) that:
  - Creates the **Riva (Heyo Support)** agent if missing (skips if it already exists).
  - Creates guidelines, journeys, terms, and context variables from the JSON spec.
  - Associates **built-in** tools with selected guidelines when `server.py` has loaded `heyo_tools` (see below).
  - Adds an extra **image-analysis** guideline wired to **`analyze_image`**.
- Run **after** Parlant is up: `python create_heyo_bot.py`  
  Env: `PARLANT_API_BASE_URL` (default `http://localhost:8800`).

---

## Parlant SDK tools (`heyo_tools.py`)

Three tools registered on the **`built-in`** service (imported from **`server.py`**):

| Tool | Purpose |
|------|--------|
| **`heyo_get_account_detail`** | POST to Heyo account API (`HEYO_API_BASE`, `HEYO_API_AUTH_TOKEN`). Returns data marked internal-only; bot must summarize, not quote raw payloads. |
| **`pause_for_human_handoff`** | Returns handoff status and suggested **human** wording (specialist/team phrasing). Does not by itself integrate with a real queue. |
| **`analyze_image`** | Vision via **OpenAI** `gpt-4o-mini` on image URL or base64 data-URL; uses **`OPENAI_API_KEY`** from `.env` or environment. |

**Note:** Do **not** use `from __future__ import annotations` in `heyo_tools.py` — Parlant’s `@p.tool` decorator requires real `ToolContext` annotations, not stringified ones.

---

## Streamlit app (`streamlit_app.py`)

- **Tabs:** Bots & chat, Terms (glossary), Context variables, Create bot.
- **Bots & chat:** Select bot, refresh/delete, sub-tabs for profile + guideline/journey **forms**, **chat** (session + messages), **Guidelines (JSON)** and **Journeys (JSON)** with **file upload**, **download**, and apply-to-API sync, **Tools** (Parlant services + Heyo tool reference from spec).
- **Terms / context variables:** CRUD-style editing against Parlant directly (`PARLANT_BASE` / sidebar URL).
- **Create bot:** Paste JSON spec; **Load Heyo (Riva) template**; optional **seed terms + context variables** from spec on create.
- **Chat:** Optional **Analyze images** (vision in UI prepends image description to the message). Image analysis key for Streamlit vision path reads **`OPENAI_API_KEY`** from project **`.env`** via `dotenv_values`.
- **Fixed wait for assistant:** **`CHAT_WAIT_ASSISTANT_S = 6.0`** (seconds) after send — sidebar **Fast mode** and **Chat speed / wait slider** were **removed** per request.

---

## API layer (`api_server.py`)

- Parlant **session creation** uses **`POST /sessions`** with **`agent_id`** in the body (Parlant 3.x), not the older agent-scoped path.
- Full CRUD for bots, guidelines, journeys, sessions/messages as documented in the file header.

---

## Docker

- **`Dockerfile`**, **`docker-compose.yml`**, **`.dockerignore`**, **`DOCKER.md`**, **`env.example`** — Stack typically includes Mongo (optional), Parlant (`server.py`), API (`api_server.py`), Streamlit. See **`DOCKER.md`** for run instructions.

---

## Other fixes / notes

- **`composio_tools.py`:** Typo fix — `list_composio_tools` (was `list_compxosio_tools`).
- **`requirements.txt`:** Includes dependencies used by the stack (e.g. `streamlit`, `motor`, `composio` as applicable).
- **GitHub:** A separate remote repo was created under **`sulekhakr-arch/otto-8`** for pushes when upstream **`Elvis-Menezes/otto`** was not writable; your local **`origin`** may point there — verify with `git remote -v`.

---

## Typical local run order

1. `python server.py` — Parlant + Otto + Heyo tools on **~8800**  
2. `python api_server.py` — Bot API on **~8801**  
3. `streamlit run streamlit_app.py` — UI (often **8501**)  
4. If Riva is not set up: `python create_heyo_bot.py`

---

## Files touched or added (high level)

| Area | Files |
|------|--------|
| Heyo tools | `heyo_tools.py`, import in `server.py` |
| Heyo bootstrap | `create_heyo_bot.py`, `data/heyo_bot_spec.json` |
| UI | `streamlit_app.py` |
| API | `api_server.py`, `bot_wrapper.py` (session endpoint) |
| Docker / env | `Dockerfile`, `docker-compose.yml`, `DOCKER.md`, `env.example`, `.dockerignore` |
| Web (original) | `web/app.js`, `web/styles.css`, `web/index.html` (JSON editors, copy) |

---

*Last updated to reflect Streamlit sidebar: Fast mode and Chat speed controls removed; fixed 6s assistant wait.*
