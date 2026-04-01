"""
Otto Bot Creator Server - Production Ready with MongoDB Persistence

This server uses MongoDB as Parlant's native backing store, ensuring:
- Agents persist across restarts (no rehydration needed)
- Guidelines and journeys persist automatically
- Sessions and events persist
- Single source of truth in MongoDB

Architecture:
┌──────────────────┐     ┌─────────────────────┐
│   Parlant SDK    │ ◄─► │      MongoDB        │
│   (server.py)    │     │   (backing store)   │
└──────────────────┘     └─────────────────────┘
         │
         ▼
┌──────────────────┐     ┌─────────────────────┐
│   API Server     │ ◄─► │    Web Frontend     │
│  (api_server.py) │     │   (web/app.js)      │
└──────────────────┘     └─────────────────────┘
"""
import asyncio
import json
import os
import logging
from typing import Any, Annotated
import httpx

# Import Composio tools from separate module
from composio_tools import (
    ALL_COMPOSIO_TOOLS,
    connect_composio_account,
    check_composio_connection,
    execute_composio_tool,
    list_composio_tools,
    github_create_issue,
    slack_send_message,
    gmail_send_email,
)
import parlant.sdk as p
from parlant.core.sessions import Event
from dotenv import load_dotenv

# Import domain persistence layer (event-based, not Parlant internals)
from domain_persistence import initialize_persistence, get_persistence, shutdown_persistence
from domain_rehydration import rehydrate_bots_from_persistence, persist_bot_creation

load_dotenv()
os.makedirs("logs", exist_ok=True)
os.environ['PARLANT_LOG_LEVEL'] = 'DEBUG'

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/openai.log'),
        logging.StreamHandler()
    ]
)
logging.getLogger("httpx").setLevel(logging.DEBUG)
logging.getLogger("openai").setLevel(logging.DEBUG)

# Dedicated logger for history trimming
history_logger = logging.getLogger("history")
history_logger.setLevel(logging.INFO)
history_file_handler = logging.FileHandler("logs/history.log")
history_file_handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
history_logger.addHandler(history_file_handler)
# Server configuration
PARLANT_API_BASE_URL = os.getenv("PARLANT_API_BASE_URL", "http://localhost:8800")
PARLANT_API_TIMEOUT = int(os.getenv("PARLANT_API_TIMEOUT", "30"))

REQUIRED_SPEC_FIELDS = {
    "name",
    "purpose",
    "scope",
    "target_users",
    "use_cases",
    "tone",
    "personality",
    "tools",
    "constraints",
    "guardrails",
    "guidelines",
    "journeys",
}
RELATIONSHIP_TYPES = {"ENTAILMENT", "PRIORITY", "DEPENDENCY", "DISAMBIGUATION"}
RELATIONSHIP_KINDS = {"guideline", "journey"}
COMPOSITION_MODES = {
    "FLUID": p.CompositionMode.FLUID,
    "COMPOSITED": p.CompositionMode.COMPOSITED,
    "STRICT": p.CompositionMode.STRICT,
}


def _trim_interaction_events(
    events: list[Event],
    max_messages: int,
) -> list[Event]:
    if max_messages <= 0:
        return list(events)

    message_indices = [
        index for index, event in enumerate(events) if event.kind == p.EventKind.MESSAGE
    ]
    if len(message_indices) <= max_messages:
        return list(events)

    cutoff_index = message_indices[-max_messages]
    return [
        event for event in events[cutoff_index:] if event.kind != p.EventKind.STATUS
    ]


def _format_message_event(event: Event) -> str:
    try:
        data = event.data
        participant = data.get("participant", {}) if isinstance(data, dict) else {}
        name = participant.get("display_name", "Unknown") if isinstance(participant, dict) else "Unknown"
        message = data.get("message", "") if isinstance(data, dict) else ""
        return f"{name}: {message.strip()}"
    except Exception:
        return "<unavailable message event>"


async def _configure_engine_hooks(hooks: p.EngineHooks) -> p.EngineHooks:
    async def _trim_history_hook(
        context: p.EngineContext,
        _payload: Any,
        _exc: Exception | None = None,
    ) -> p.EngineHookResult:
        events = list(context.interaction.events)
        message_count_before = sum(
            1 for event in events if event.kind == p.EventKind.MESSAGE
        )
        trimmed = _trim_interaction_events(events, PARLANT_HISTORY_MAX_MESSAGES)
        
        if len(trimmed) != len(events):
            # Trimming happened
            context.interaction = p.Interaction(events=trimmed)
            message_count_after = sum(
                1 for event in trimmed if event.kind == p.EventKind.MESSAGE
            )
            log_msg = (
                f"TRIMMED: {message_count_before} -> {message_count_after} messages "
                f"(limit: {PARLANT_HISTORY_MAX_MESSAGES})"
            )
            print(f"[History] {log_msg}")
            history_logger.info(log_msg)
            
            if PARLANT_HISTORY_LOG_MESSAGES:
                messages = [
                    _format_message_event(event)
                    for event in trimmed
                    if event.kind == p.EventKind.MESSAGE
                ]
                if messages:
                    print("[History] Messages sent to OpenAI:")
                    history_logger.info("Messages sent to OpenAI:")
                    for idx, msg in enumerate(messages, start=1):
                        print(f"  {idx}. {msg}")
                        history_logger.info(f"  {idx}. {msg}")
        else:
            # No trimming needed
            log_msg = (
                f"OK: {message_count_before} messages (limit: {PARLANT_HISTORY_MAX_MESSAGES})"
            )
            history_logger.info(log_msg)
            
        return p.EngineHookResult.CALL_NEXT

    hooks.on_preparing.append(_trim_history_hook)
    return hooks


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
        return [item.strip() for item in value]
    return []


def _validate_guidelines(guidelines: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(guidelines, list) or not guidelines:
        return ["guidelines must be a non-empty list"]

    for index, guideline in enumerate(guidelines, start=1):
        if not isinstance(guideline, dict):
            errors.append(f"guidelines[{index}] must be an object")
            continue
        condition = guideline.get("condition")
        action = guideline.get("action")
        if not isinstance(condition, str) or not condition.strip():
            errors.append(f"guidelines[{index}].condition is required")
        if action is not None and (not isinstance(action, str) or not action.strip()):
            errors.append(f"guidelines[{index}].action must be a non-empty string when provided")
        criticality = guideline.get("criticality")
        if criticality is not None and criticality not in {"LOW", "MEDIUM", "HIGH"}:
            errors.append(f"guidelines[{index}].criticality must be LOW, MEDIUM, or HIGH")
    return errors


def _validate_journeys(journeys: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(journeys, list) or not journeys:
        return ["journeys must be a non-empty list"]

    for index, journey in enumerate(journeys, start=1):
        if not isinstance(journey, dict):
            errors.append(f"journeys[{index}] must be an object")
            continue
        title = journey.get("title")
        description = journey.get("description")
        conditions = journey.get("conditions")
        if not isinstance(title, str) or not title.strip():
            errors.append(f"journeys[{index}].title is required")
        if not isinstance(description, str) or not description.strip():
            errors.append(f"journeys[{index}].description is required")
        if not _as_str_list(conditions):
            errors.append(f"journeys[{index}].conditions must be a non-empty list of strings")
    return errors


def _normalize_relationship_type(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip().upper()
    return None


def _relationship_ref_from_guideline(guideline: dict[str, Any]) -> str | None:
    ref = guideline.get("key") or guideline.get("condition")
    if isinstance(ref, str) and ref.strip():
        return ref.strip()
    return None


def _relationship_ref_from_journey(journey: dict[str, Any]) -> str | None:
    ref = journey.get("key") or journey.get("title")
    if isinstance(ref, str) and ref.strip():
        return ref.strip()
    return None


def _collect_relationship_refs(
    items: list[dict[str, Any]],
    ref_builder: callable,
    kind: str,
) -> tuple[dict[str, int], list[str]]:
    errors: list[str] = []
    refs: dict[str, int] = {}
    for index, item in enumerate(items, start=1):
        ref = ref_builder(item)
        if not ref:
            errors.append(f"{kind}[{index}] must include a non-empty key or reference field")
            continue
        if ref in refs:
            errors.append(f"{kind}[{index}] reference '{ref}' must be unique")
            continue
        refs[ref] = index
    return refs, errors


def _validate_relationships(
    relationships: Any,
    guideline_refs: set[str],
    journey_refs: set[str],
) -> list[str]:
    errors: list[str] = []
    if relationships is None:
        return errors
    if not isinstance(relationships, list):
        return ["relationships must be a list of objects"]
    if not relationships:
        return ["relationships must be a non-empty list when provided"]

    for index, relationship in enumerate(relationships, start=1):
        if not isinstance(relationship, dict):
            errors.append(f"relationships[{index}] must be an object")
            continue
        rel_type = _normalize_relationship_type(relationship.get("type"))
        if rel_type not in RELATIONSHIP_TYPES:
            errors.append(
                f"relationships[{index}].type must be one of {', '.join(sorted(RELATIONSHIP_TYPES))}"
            )
        source = relationship.get("source")
        if not isinstance(source, str) or not source.strip():
            errors.append(f"relationships[{index}].source must be a non-empty string")
        targets = relationship.get("targets")
        if targets is None:
            target = relationship.get("target")
            targets = [target] if target is not None else []
        if not isinstance(targets, list) or not targets or not all(
            isinstance(target, str) and target.strip() for target in targets
        ):
            errors.append(f"relationships[{index}].targets must be a non-empty list of strings")

        source_kind = relationship.get("source_kind", "guideline")
        target_kind = relationship.get("target_kind", source_kind)
        if source_kind not in RELATIONSHIP_KINDS:
            errors.append(
                f"relationships[{index}].source_kind must be guideline or journey when provided"
            )
        if target_kind not in RELATIONSHIP_KINDS:
            errors.append(
                f"relationships[{index}].target_kind must be guideline or journey when provided"
            )

        source_refs = guideline_refs if source_kind == "guideline" else journey_refs
        target_refs = guideline_refs if target_kind == "guideline" else journey_refs
        if isinstance(source, str) and source.strip() and source.strip() not in source_refs:
            errors.append(
                f"relationships[{index}].source '{source}' does not match any {source_kind} reference"
            )
        if isinstance(targets, list):
            for target in targets:
                if isinstance(target, str) and target.strip() and target.strip() not in target_refs:
                    errors.append(
                        f"relationships[{index}].target '{target}' does not match any {target_kind} reference"
                    )
            if rel_type != "DISAMBIGUATION" and len(targets) != 1:
                errors.append(
                    f"relationships[{index}].targets must contain exactly one target unless type is DISAMBIGUATION"
                )
            if rel_type == "DISAMBIGUATION" and len(targets) < 2:
                errors.append(
                    f"relationships[{index}].targets must contain at least two targets for DISAMBIGUATION"
                )
    return errors


def _validate_spec(spec: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    missing = REQUIRED_SPEC_FIELDS - spec.keys()
    if missing:
        errors.append(f"missing required fields: {', '.join(sorted(missing))}")

    for key in ("name", "purpose", "scope", "target_users", "tone", "personality"):
        value = spec.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{key} must be a non-empty string")

    if not _as_str_list(spec.get("use_cases")):
        errors.append("use_cases must be a non-empty list of strings")
    if not _as_str_list(spec.get("tools")):
        errors.append("tools must be a non-empty list of strings (use ['none'] if not needed)")
    if not _as_str_list(spec.get("constraints")):
        errors.append("constraints must be a non-empty list of strings")
    if not _as_str_list(spec.get("guardrails")):
        errors.append("guardrails must be a non-empty list of strings")

    errors.extend(_validate_guidelines(spec.get("guidelines")))
    errors.extend(_validate_journeys(spec.get("journeys")))

    guideline_refs, guideline_errors = _collect_relationship_refs(
        spec.get("guidelines", []),
        _relationship_ref_from_guideline,
        "guidelines",
    )
    journey_refs, journey_errors = _collect_relationship_refs(
        spec.get("journeys", []),
        _relationship_ref_from_journey,
        "journeys",
    )
    errors.extend(guideline_errors)
    errors.extend(journey_errors)
    errors.extend(
        _validate_relationships(
            spec.get("relationships"),
            set(guideline_refs.keys()),
            set(journey_refs.keys()),
        )
    )

    composition_mode = spec.get("composition_mode")
    if composition_mode is not None and composition_mode not in COMPOSITION_MODES:
        errors.append("composition_mode must be one of FLUID, COMPOSITED, STRICT")

    max_iterations = spec.get("max_engine_iterations")
    if max_iterations is not None:
        if not isinstance(max_iterations, int) or max_iterations <= 0:
            errors.append("max_engine_iterations must be a positive integer")

    return errors


def _build_agent_description(spec: dict[str, Any]) -> str:
    tools = ", ".join(_as_str_list(spec.get("tools")))
    use_cases = "; ".join(_as_str_list(spec.get("use_cases")))
    constraints = "; ".join(_as_str_list(spec.get("constraints")))
    guardrails = "; ".join(_as_str_list(spec.get("guardrails")))

    return "\n".join(
        [
            f"Purpose: {spec['purpose']}",
            f"Scope: {spec['scope']}",
            f"Target users: {spec['target_users']}",
            f"Tone: {spec['tone']}",
            f"Personality: {spec['personality']}",
            f"Primary use cases: {use_cases}",
            f"Required tools/integrations: {tools}",
            f"Constraints: {constraints}",
            f"Guardrails: {guardrails}",
        ]
    )


async def _call_parlant_api(
    method: str,
    endpoint: str,
    data: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    url = f"{PARLANT_API_BASE_URL}{endpoint}"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if PARLANT_API_TOKEN:
        headers["Authorization"] = f"Bearer {PARLANT_API_TOKEN}"

    try:
        async with httpx.AsyncClient(timeout=PARLANT_API_TIMEOUT) as client:
            if method.upper() == "POST":
                response = await client.post(url, json=data, headers=headers)
            elif method.upper() == "GET":
                response = await client.get(url, headers=headers)
            else:
                return False, {"error": f"Unsupported HTTP method: {method}"}

            response.raise_for_status()
            return True, response.json()

    except httpx.TimeoutException:
        return False, {"error": f"API request timeout after {PARLANT_API_TIMEOUT}s"}
    except httpx.HTTPStatusError as exc:
        return False, {
            "error": f"API returned {exc.response.status_code}",
            "details": exc.response.text[:200],
        }
    except httpx.RequestError as exc:
        return False, {"error": f"API connection failed: {str(exc)}"}
    except Exception as exc:
        return False, {"error": f"Unexpected error: {str(exc)}"}


def _map_criticality_to_api(criticality: str | None) -> str:
    if criticality == "LOW":
        return "low"
    elif criticality == "HIGH":
        return "high"
    else:
        return "medium"


def _map_composition_mode_to_api(mode: str | None) -> str:
    if mode == "COMPOSITED":
        return "composited_canned"
    elif mode == "STRICT":
        return "strict_canned"
    else:
        return "fluid"


@p.tool
async def create_parlant_bot(
    context: p.ToolContext,
    spec_json: Annotated[
        str,
        p.ToolParameterOptions(
            description=(
                "Complete JSON bot specification with all required fields: name, purpose, scope, "
                "target_users, use_cases, tone, personality, tools, constraints, guardrails, "
                "guidelines, and journeys. Otto must construct this from gathered requirements."
            ),
            source="context",
            significance="Required to create a validated, production-ready Parlant bot via REST API",
            examples=[
                json.dumps({
                    "name": "Reva Support Bot",
                    "purpose": "E-commerce customer support for order tracking and returns",
                    "scope": "Order status, cancellations, refunds, returns, shipping info",
                    "target_users": "Existing customers with placed orders",
                    "use_cases": ["Track order", "Cancel order", "Request refund"],
                    "tone": "Friendly, empathetic, efficient",
                    "personality": "Helpful customer service rep",
                    "tools": ["none"],
                    "constraints": ["30-day cancellation policy", "No refunds over $500"],
                    "guardrails": ["Verify order number before actions"],
                    "guidelines": [
                        {
                            "condition": "Customer asks about order status",
                            "action": "Verify order number and provide tracking info",
                            "criticality": "HIGH"
                        }
                    ],
                    "journeys": [
                        {
                            "title": "Order Tracking",
                            "description": "Help customer find order status",
                            "conditions": ["When customer asks about delivery"]
                        }
                    ],
                    "relationships": [
                        {
                            "type": "priority",
                            "source": "The customer is becoming upset",
                            "targets": ["The customer wants a coke"],
                            "source_kind": "guideline",
                            "target_kind": "guideline",
                        }
                    ],
                    "composition_mode": "FLUID",
                    "max_engine_iterations": 3
                })
            ],
        ),
    ],
) -> p.ToolResult:
    try:
        spec = json.loads(spec_json)
    except json.JSONDecodeError as exc:
        return p.ToolResult({"status": "error", "errors": [f"Invalid JSON: {exc.msg}"]})

    if not isinstance(spec, dict):
        return p.ToolResult({"status": "error", "errors": ["Spec must be a JSON object"]})

    errors = _validate_spec(spec)
    if errors:
        return p.ToolResult({"status": "error", "errors": errors})

    agent_payload = {
        "name": spec["name"],
        "description": _build_agent_description(spec),
        "composition_mode": _map_composition_mode_to_api(spec.get("composition_mode")),
        "max_engine_iterations": spec.get("max_engine_iterations", 3),
    }

    success, agent_response = await _call_parlant_api("POST", "/agents", agent_payload)
    if not success:
        return p.ToolResult({
            "status": "error",
            "errors": [f"Failed to create agent: {agent_response.get('error')}"],
            "details": agent_response.get("details"),
        })

    agent_id = agent_response.get("id")
    agent_name = agent_response.get("name")
    agent_tag = f"agent:{agent_id}"

    created_guidelines = []
    guideline_id_by_ref: dict[str, str] = {}
    for idx, guideline in enumerate(spec["guidelines"], 1):
        guideline_payload = {
            "condition": guideline["condition"],
            "action": guideline.get("action"),
            "description": guideline.get("description"),
            "criticality": _map_criticality_to_api(guideline.get("criticality")),
            "tags": [agent_tag],
        }

        success, guideline_response = await _call_parlant_api("POST", "/guidelines", guideline_payload)
        if success:
            created_guidelines.append({
                "id": guideline_response.get("id"),
                "condition": guideline["condition"]
            })
            guideline_ref = _relationship_ref_from_guideline(guideline)
            guideline_id = guideline_response.get("id")
            if guideline_ref and guideline_id:
                guideline_id_by_ref[guideline_ref] = guideline_id
        else:
            created_guidelines.append({
                "error": f"Guideline {idx} failed: {guideline_response.get('error')}"
            })

    created_journeys = []
    journey_id_by_ref: dict[str, str] = {}
    for idx, journey in enumerate(spec["journeys"], 1):
        journey_payload = {
            "title": journey["title"],
            "description": journey["description"],
            "conditions": journey["conditions"],
            "tags": [agent_tag],
        }

        success, journey_response = await _call_parlant_api("POST", "/journeys", journey_payload)
        if success:
            created_journeys.append({
                "id": journey_response.get("id"),
                "title": journey["title"],
                "description": journey["description"],
                "conditions": journey["conditions"],
            })
            journey_ref = _relationship_ref_from_journey(journey)
            journey_id = journey_response.get("id")
            if journey_ref and journey_id:
                journey_id_by_ref[journey_ref] = journey_id
        else:
            created_journeys.append({
                "error": f"Journey {idx} failed: {journey_response.get('error')}"
            })
    
    return p.ToolResult(
        {
            "status": "created",
            "agent_id": agent_id,
            "agent_name": agent_name,
            "guidelines_created": len([g for g in created_guidelines if "id" in g]),
            "journeys_created": len([j for j in created_journeys if "id" in j]),
            "relationships_created": len([r for r in created_relationships if "id" in r]),
            "guidelines": created_guidelines,
            "journeys": created_journeys,
            "relationships": created_relationships,
            "api_base_url": PARLANT_API_BASE_URL,
            "persisted_to_mongodb": persistence.enabled if 'persistence' in locals() else False,
        }
    )


async def _rehydrate_after_ready(server: p.Server, persistence: Any) -> None:
    await server.ready.wait()
    print("📥 Rehydrating bots from MongoDB...")
    try:
        rehydration_stats = await rehydrate_bots_from_persistence(server, persistence)
        if rehydration_stats.get("errors"):
            print(f"⚠️  {len(rehydration_stats['errors'])} error(s) during rehydration")
    except Exception as exc:
        print(f"⚠️  Failed to load bots from MongoDB: {exc}")


async def main():
    print("🚀 Starting Otto Bot Creator Server...")
    print(f"📡 Parlant API: {PARLANT_API_BASE_URL}")
    print(f"⏱️  API Timeout: {PARLANT_API_TIMEOUT}s")
    print("-" * 50)

    mongodb_uri = (os.getenv("MONGODB_URI") or "").strip()
    persistence_enabled = False
    if mongodb_uri:
        success, msg = await initialize_persistence(mongodb_uri)
        persistence_enabled = success
        print(f"✅ {msg}" if success else f"⚠️  {msg}")
    else:
        print("📝 MongoDB disabled (no MONGODB_URI) — domain rehydration skipped")

    try:
        async with p.Server(nlp_service=p.NLPServices.openai) as server:
            # Create Otto orchestrator agent
            agent = await server.create_agent(
                name="Otto",
                description=(
                    "Primary orchestrator for converting a business chatbot description into a fully "
                    "configured Parlant bot. Collect requirements, detect gaps, ask focused follow-ups, "
                    "produce a validated specification, and create the bot using Parlant REST APIs."
                ),
            )

            print(f"✅ Created Otto agent (ID: {agent.id})")

            await agent.create_guideline(
                condition="When a business user provides a bot description or asks to create a bot.",
                action=(
                    "Extract purpose, scope, target users, use cases, tone/personality, required tools, "
                    "constraints, and guardrails. Summarize extracted fields clearly before proceeding."
                ),
                description="Ensure requirements are captured explicitly from the start.",
                criticality=p.Criticality.HIGH,
            )

            await agent.create_guideline(
                condition="When any required parameter is missing, vague, or ambiguous.",
                action=(
                    "Ask ONE focused clarification question at a time and explain why that detail matters. "
                    "Guide the user step-by-step until all parameters are explicit and clear."
                ),
                description="Prevent assumptions and drive completion through structured questioning.",
                criticality=p.Criticality.HIGH,
            )

            await agent.create_guideline(
                condition="When preparing to build a bot specification from gathered requirements.",
                action=(
                    "Construct detailed Parlant guidelines and journeys that teach effective bot design, "
                    "enforce consistency, and apply safety/guardrails based on the user's requirements."
                ),
                description="Generate guidance and journeys from finalized requirements.",
                criticality=p.Criticality.MEDIUM,
            )

            # Guideline 4: Bot creation via REST API
            await agent.create_guideline(
                condition="When all required parameters are explicit, validated, and confirmed by the user.",
                action=(
                    "Assemble a complete JSON bot specification with all required fields, validate it "
                    "against the schema, and call create_parlant_bot to instantiate the bot via REST API. "
                    "Provide the user with the created bot details including agent ID and confirmation."
                ),
                description="Only create bots from a fully validated specification using REST API.",
                criticality=p.Criticality.HIGH,
                tools=[create_parlant_bot],
            )

            await agent.create_journey(
                title="Bot Intake & Clarification",
                description="Gather requirements, close gaps, validate specification, and create bot via API.",
                conditions=[
                    "Start when the user describes a bot or requests a new bot.",
                    "Continue asking clarifying questions until all required parameters are explicit.",
                    "Validate the complete specification before calling the REST API.",
                ],
            )

            if persistence_enabled:
                persistence = get_persistence()
                asyncio.create_task(_rehydrate_after_ready(server, persistence))
            else:
                print("📭 MongoDB disabled - no bots to load")

            print("✅ Configuration complete. Server will start now.")
    finally:
        await shutdown_persistence()


asyncio.run(main())