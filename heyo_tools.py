"""
Heyo Support Bot — Parlant SDK Tools

Three tools from the PDF spec (sections 4 & 5):
  1. heyo_get_account_detail   — HTTP call to Heyo API (internal only)
  2. pause_for_human_handoff   — signal human specialist takeover
  3. analyze_image             — GPT-4o-mini vision for support screenshots

Import this module in server.py so all three tools are registered
in the `built-in` Parlant SDK service and available to the Riva agent.
"""

import base64
import io
import json
import os
from pathlib import Path
from typing import Annotated

import httpx
import parlant.sdk as p
from dotenv import dotenv_values

# ---------------------------------------------------------------------------
# Config (overridable via env vars or Parlant context variables)
# ---------------------------------------------------------------------------

_APP_DIR = Path(__file__).resolve().parent
_ENV_PATH = _APP_DIR / ".env"

HEYO_API_BASE = os.getenv(
    "HEYO_API_BASE", "https://support.myoperator.biz/apis"
)
HEYO_API_AUTH_TOKEN = os.getenv(
    "HEYO_API_AUTH_TOKEN",
    "d00e3706637d3047631add88fa645c01ec02deaf8b3bff54c42c3e65ba5eadef",
)


def _load_openai_key() -> str:
    """Read OPENAI_API_KEY from local .env first, fall back to environment."""
    env_vals = dotenv_values(str(_ENV_PATH)) if _ENV_PATH.exists() else {}
    key = str(env_vals.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY") or "")
    return key.strip().strip('"').strip("'")


# ---------------------------------------------------------------------------
# Tool 1: heyo_get_account_detail
# ---------------------------------------------------------------------------

@p.tool
async def heyo_get_account_detail(
    context: p.ToolContext,
    superadmin_number: Annotated[
        str,
        p.ToolParameterOptions(
            description=(
                "The 10-digit phone number of the account's primary admin (superadmin). "
                "Used to locate the correct Heyo account. Normalize by removing a leading 0 "
                "if 11 digits are provided."
            ),
            source="customer",
            significance=(
                "Required to verify and troubleshoot a customer's Heyo account. "
                "Do NOT call this tool unless the superadmin number has been confirmed."
            ),
            examples=["9123456789"],
        ),
    ],
    parameter_choice: Annotated[
        int,
        p.ToolParameterOptions(
            description=(
                "Controls which account data to fetch. "
                "0 = all data (default), 1 = active account, "
                "2 = previous last account, 3 = previous second-last account."
            ),
            source="context",
        ),
    ] = 0,
) -> p.ToolResult:
    """
    Fetch Heyo account details using the superadmin phone number.
    INTERNAL ONLY — never quote or display raw returned data to the customer.
    Summarize abstractly (e.g. 'I've checked your account').
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{HEYO_API_BASE}/heyo-bot-get-account-detail",
                json={
                    "superadmin_number": superadmin_number,
                    "parameter_choice": parameter_choice,
                },
                headers={
                    "authorization": HEYO_API_AUTH_TOKEN,
                    "Content-Type": "application/json",
                },
            )
            if response.status_code >= 400:
                return p.ToolResult({
                    "status": "error",
                    "http_status": response.status_code,
                    "message": f"Heyo API returned {response.status_code}",
                    "_internal": True,
                })
            data = response.json()
            return p.ToolResult({
                "status": "ok",
                "account_data": data,
                "_internal": True,
                "_note": (
                    "PRIVACY: Do NOT show, quote, or echo any raw values to the customer. "
                    "Summarize only (e.g. 'I can see your account details')."
                ),
            })
    except httpx.TimeoutException:
        return p.ToolResult({
            "status": "error",
            "message": "Heyo API request timed out.",
            "_internal": True,
        })
    except Exception as exc:
        return p.ToolResult({
            "status": "error",
            "message": str(exc),
            "_internal": True,
        })


# ---------------------------------------------------------------------------
# Tool 2: pause_for_human_handoff
# ---------------------------------------------------------------------------

@p.tool
async def pause_for_human_handoff(
    context: p.ToolContext,
    reason: Annotated[
        str,
        p.ToolParameterOptions(
            description=(
                "Internal reason code for the handoff (not shown to customer). "
                "Use one of: frustration_detected, billing_dispute, refund_request, "
                "account_suspended, backend_change_request, waba_setup_issue, "
                "escalation_requested, conversation_loop."
            ),
            source="context",
            examples=[
                "frustration_detected",
                "billing_dispute",
                "waba_setup_issue",
                "escalation_requested",
            ],
        ),
    ] = "escalation_requested",
) -> p.ToolResult:
    """
    Stop automated responses and hand off to a human specialist.
    Use when: customer is frustrated, billing/refund/suspension issues,
    backend change requests, WABA setup escalation, or conversation looping.
    Frame the handoff with human phrasing — NOT 'connecting you to a live agent'.
    """
    human_messages = {
        "frustration_detected": (
            "I want to make sure you get the best help possible. "
            "Let me bring in a specialist from my team right away."
        ),
        "billing_dispute": (
            "For billing-related matters, I'm going to involve a senior specialist "
            "who can look into this properly for you."
        ),
        "refund_request": (
            "Refund requests need a senior team member to review. "
            "I'll get someone from my team to follow up with you shortly."
        ),
        "account_suspended": (
            "Account suspension cases require immediate attention from my team. "
            "A specialist will be with you shortly."
        ),
        "backend_change_request": (
            "This type of change requires a senior team member. "
            "I'll get the right person on this right away."
        ),
        "waba_setup_issue": (
            "I'm going to bring in a WABA specialist from my team "
            "who can give you dedicated attention on this."
        ),
        "conversation_loop": (
            "Let me get a specialist from my team to take a fresh look at this."
        ),
        "escalation_requested": (
            "Of course. Let me bring in a specialist from my team to help you further."
        ),
    }

    message = human_messages.get(
        reason,
        "Let me bring in a specialist from my team who can help you better.",
    )

    return p.ToolResult({
        "status": "handoff_initiated",
        "reason": reason,
        "suggested_message_to_customer": message,
        "action": "PAUSE_AUTOMATION",
        "_note": (
            "Use suggested_message_to_customer as your reply. "
            "Do NOT send any further automated responses after this."
        ),
    })


# ---------------------------------------------------------------------------
# Tool 3: analyze_image
# ---------------------------------------------------------------------------

@p.tool
async def analyze_image(
    context: p.ToolContext,
    image_input: Annotated[
        str,
        p.ToolParameterOptions(
            description=(
                "The image to analyze. Accepts: "
                "(a) a public HTTPS URL to an image, "
                "(b) a base64 data-URL (data:image/jpeg;base64,...), "
                "or (c) a plain-text description if the customer described the image verbally."
            ),
            source="customer",
            significance=(
                "Use to extract visible text, error messages, phone numbers, "
                "and UI elements from a customer screenshot for support context."
            ),
            examples=[
                "https://i.imgur.com/example.png",
                "data:image/jpeg;base64,/9j/4AAQ...",
                "The screen shows an error: 'Account not found'",
            ],
        ),
    ],
) -> p.ToolResult:
    """
    Analyze a customer screenshot or image. Extracts visible text, errors,
    phone numbers, and UI labels useful for troubleshooting.
    Works with URLs, base64 data-URLs, or falls back to text if no image.
    """
    api_key = _load_openai_key()
    if not api_key:
        return p.ToolResult({
            "status": "error",
            "message": "OPENAI_API_KEY not configured. Cannot analyze image.",
            "fallback": "Ask the customer to describe what the screen shows in text.",
        })

    # If it looks like a plain text description rather than an image, return as-is
    is_url = image_input.startswith("https://") or image_input.startswith("http://")
    is_data_url = image_input.startswith("data:")
    if not is_url and not is_data_url:
        return p.ToolResult({
            "status": "ok",
            "description": image_input,
            "_note": "Treated as verbal description (no image URL detected).",
        })

    try:
        from openai import AsyncOpenAI

        # For data-URL images, downscale first to save tokens / latency
        if is_data_url:
            try:
                header, b64 = image_input.split(",", 1)
                raw_bytes = base64.b64decode(b64)
                from PIL import Image as PILImage

                with PILImage.open(io.BytesIO(raw_bytes)) as im:
                    im = im.convert("RGB")
                    im.thumbnail((960, 960))
                    out = io.BytesIO()
                    im.save(out, format="JPEG", quality=72, optimize=True)
                    compressed = out.getvalue()
                b64_compressed = base64.standard_b64encode(compressed).decode("ascii")
                image_input = f"data:image/jpeg;base64,{b64_compressed}"
            except Exception:
                pass  # If PIL not available, send original

        oai = AsyncOpenAI(api_key=api_key)
        resp = await oai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "You are assisting a customer-support agent (Riva). "
                                "Describe this screenshot for the agent: "
                                "visible text, error messages, phone numbers, UI labels, "
                                "and any details relevant for troubleshooting. "
                                "Be concise; use bullets if there are multiple items."
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": image_input, "detail": "low"},
                        },
                    ],
                }
            ],
            max_tokens=250,
        )
        description = (resp.choices[0].message.content or "").strip()
        return p.ToolResult({"status": "ok", "description": description})

    except Exception as exc:
        return p.ToolResult({
            "status": "error",
            "message": str(exc),
            "fallback": "Ask the customer to describe what the screen shows in text.",
        })


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

HEYO_TOOLS = [heyo_get_account_detail, pause_for_human_handoff, analyze_image]
