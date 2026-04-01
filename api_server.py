"""
Bot Management API Server - Production Ready

Complete REST API for managing bots, guidelines, journeys, and chat sessions.
All operations are fully CRUD compliant and testable via Postman.

Architecture:
┌─────────────────┐     ┌─────────────────┐     ┌──────────────┐
│   Web Frontend  │ ──► │  API Server     │ ──► │   Parlant    │
│   (port 3000)   │     │  (port 8801)    │     │  (port 8800) │
└─────────────────┘     └─────────────────┘     └──────────────┘
                                                       │
                                                       ▼
                                               ┌──────────────┐
                                               │   MongoDB    │
                                               │ (persistent) │
                                               └──────────────┘

Endpoints:
    Health:
        GET  /                         - Health check
        GET  /health                   - Detailed health check
    
    Agents/Bots:
        GET    /bots                   - List all bots
        POST   /bots                   - Create a new bot
        GET    /bots/{bot_id}          - Get a specific bot
        PATCH  /bots/{bot_id}          - Update a bot
        DELETE /bots/{bot_id}          - Delete a bot
    
    Guidelines:
        GET    /guidelines             - List all guidelines
        POST   /guidelines             - Create a guideline
        GET    /guidelines/{id}        - Get a guideline
        PATCH  /guidelines/{id}        - Update a guideline
        DELETE /guidelines/{id}        - Delete a guideline
        POST   /bots/{id}/guidelines   - Add guideline to bot
    
    Journeys:
        GET    /journeys               - List all journeys
        POST   /journeys               - Create a journey
        GET    /journeys/{id}          - Get a journey
        PATCH  /journeys/{id}          - Update a journey
        DELETE /journeys/{id}          - Delete a journey
        POST   /bots/{id}/journeys     - Add journey to bot
    
    Sessions/Chat:
        POST   /bots/{id}/sessions     - Create chat session
        GET    /sessions/{id}          - Get session
        DELETE /sessions/{id}          - Delete session
        GET    /sessions/{id}/events   - Get events/messages
        POST   /sessions/{id}/events   - Send event/message
        POST   /sessions/{id}/messages - Send message (alias)
        GET    /sessions/{id}/messages - Get messages (alias)
"""

import os
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException, status, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

# Import domain persistence for MongoDB mirroring
try:
    from domain_persistence import get_persistence, initialize_persistence, shutdown_persistence
    PERSISTENCE_AVAILABLE = True
except ImportError:
    PERSISTENCE_AVAILABLE = False
    def get_persistence():
        return None
    async def initialize_persistence(uri):
        return False, "Not available"
    async def shutdown_persistence():
        pass

# =============================================================================
# Configuration
# =============================================================================

PARLANT_API_BASE_URL = os.getenv("PARLANT_API_BASE_URL", "http://localhost:8800")
PARLANT_API_TIMEOUT = int(os.getenv("PARLANT_API_TIMEOUT", "30"))
PARLANT_API_TOKEN = os.getenv("PARLANT_API_TOKEN")
API_PORT = int(os.getenv("API_PORT", "8801"))
API_HOST = os.getenv("API_HOST", "0.0.0.0")

# MongoDB configuration
MONGODB_URI = os.getenv("MONGODB_URI")

# =============================================================================
# Parlant API Client
# =============================================================================

class ParlantClient:
    """HTTP client for Parlant REST API."""
    
    def __init__(self, base_url: str, timeout: int, token: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = token
    
    def _headers(self) -> dict:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers
    
    async def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> tuple[bool, Any]:
        """Make HTTP request to Parlant API."""
        url = f"{self.base_url}{endpoint}"
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                kwargs = {"headers": self._headers()}
                if params:
                    kwargs["params"] = params
                if data is not None:
                    kwargs["json"] = data
                
                if method == "GET":
                    response = await client.get(url, **kwargs)
                elif method == "POST":
                    response = await client.post(url, **kwargs)
                elif method == "PATCH":
                    response = await client.patch(url, **kwargs)
                elif method == "DELETE":
                    response = await client.delete(url, **kwargs)
                else:
                    return False, {"error": f"Unsupported method: {method}"}
                
                response.raise_for_status()
                
                # Handle empty responses
                if response.status_code == 204 or not response.content:
                    return True, {}
                
                return True, response.json()
                
        except httpx.TimeoutException:
            return False, {"error": f"Request timeout after {self.timeout}s"}
        except httpx.HTTPStatusError as e:
            error_detail = e.response.text[:500] if e.response.text else str(e)
            return False, {
                "error": f"HTTP {e.response.status_code}",
                "details": error_detail,
                "status_code": e.response.status_code
            }
        except httpx.RequestError as e:
            return False, {"error": f"Connection failed: {str(e)}"}
        except Exception as e:
            return False, {"error": f"Unexpected error: {str(e)}"}
    
    # -------------------------------------------------------------------------
    # Agent/Bot Operations
    # -------------------------------------------------------------------------
    
    async def list_agents(self) -> tuple[bool, Any]:
        return await self._request("GET", "/agents")
    
    async def get_agent(self, agent_id: str) -> tuple[bool, Any]:
        return await self._request("GET", f"/agents/{agent_id}")
    
    async def create_agent(self, data: dict) -> tuple[bool, Any]:
        return await self._request("POST", "/agents", data)
    
    async def update_agent(self, agent_id: str, data: dict) -> tuple[bool, Any]:
        return await self._request("PATCH", f"/agents/{agent_id}", data)
    
    async def delete_agent(self, agent_id: str) -> tuple[bool, Any]:
        return await self._request("DELETE", f"/agents/{agent_id}")
    
    # -------------------------------------------------------------------------
    # Guideline Operations
    # -------------------------------------------------------------------------
    
    async def list_guidelines(self, tag: Optional[str] = None) -> tuple[bool, Any]:
        params = {"tag": tag} if tag else None
        return await self._request("GET", "/guidelines", params=params)
    
    async def get_guideline(self, guideline_id: str) -> tuple[bool, Any]:
        return await self._request("GET", f"/guidelines/{guideline_id}")
    
    async def create_guideline(self, data: dict) -> tuple[bool, Any]:
        return await self._request("POST", "/guidelines", data)
    
    async def update_guideline(self, guideline_id: str, data: dict) -> tuple[bool, Any]:
        return await self._request("PATCH", f"/guidelines/{guideline_id}", data)
    
    async def delete_guideline(self, guideline_id: str) -> tuple[bool, Any]:
        return await self._request("DELETE", f"/guidelines/{guideline_id}")
    
    # -------------------------------------------------------------------------
    # Journey Operations
    # -------------------------------------------------------------------------
    
    async def list_journeys(self, tag: Optional[str] = None) -> tuple[bool, Any]:
        params = {"tag": tag} if tag else None
        return await self._request("GET", "/journeys", params=params)
    
    async def get_journey(self, journey_id: str) -> tuple[bool, Any]:
        return await self._request("GET", f"/journeys/{journey_id}")
    
    async def create_journey(self, data: dict) -> tuple[bool, Any]:
        return await self._request("POST", "/journeys", data)
    
    async def update_journey(self, journey_id: str, data: dict) -> tuple[bool, Any]:
        return await self._request("PATCH", f"/journeys/{journey_id}", data)
    
    async def delete_journey(self, journey_id: str) -> tuple[bool, Any]:
        return await self._request("DELETE", f"/journeys/{journey_id}")
    
    # -------------------------------------------------------------------------
    # Session Operations
    # -------------------------------------------------------------------------
    
    async def create_session(self, agent_id: str, customer_id: Optional[str] = None) -> tuple[bool, Any]:
        # Parlant 3.x: POST /sessions with agent_id in body (not /agents/{id}/sessions)
        data: dict[str, Any] = {"agent_id": agent_id}
        if customer_id:
            data["customer_id"] = customer_id
        return await self._request("POST", "/sessions", data)
    
    async def get_session(self, session_id: str) -> tuple[bool, Any]:
        return await self._request("GET", f"/sessions/{session_id}")
    
    async def delete_session(self, session_id: str) -> tuple[bool, Any]:
        return await self._request("DELETE", f"/sessions/{session_id}")
    
    async def get_events(self, session_id: str) -> tuple[bool, Any]:
        return await self._request("GET", f"/sessions/{session_id}/events")
    
    async def send_event(self, session_id: str, data: dict) -> tuple[bool, Any]:
        return await self._request("POST", f"/sessions/{session_id}/events", data)


# Global client instance
_client: Optional[ParlantClient] = None


# =============================================================================
# Pydantic Models
# =============================================================================

class GuidelineCreate(BaseModel):
    """Create guideline request."""
    condition: str = Field(..., description="Trigger condition")
    action: Optional[str] = Field(None, description="Action to take")
    description: Optional[str] = Field(None, description="Description")
    criticality: Optional[str] = Field("medium", description="low, medium, or high")
    tags: Optional[list[str]] = Field(default_factory=list, description="Tags including agent:id")

    model_config = {
        "json_schema_extra": {
            "example": {
                "condition": "When user asks about orders",
                "action": "Provide order tracking information",
                "criticality": "high",
                "tags": ["agent:abc123"]
            }
        }
    }


class GuidelineUpdate(BaseModel):
    """Update guideline request."""
    condition: Optional[str] = Field(None, description="Trigger condition")
    action: Optional[str] = Field(None, description="Action to take")
    description: Optional[str] = Field(None, description="Description")
    criticality: Optional[str] = Field(None, description="low, medium, or high")


class JourneyCreate(BaseModel):
    """Create journey request."""
    title: str = Field(..., description="Journey title")
    description: str = Field(..., description="Journey description")
    conditions: list[str] = Field(..., description="Trigger conditions")
    tags: Optional[list[str]] = Field(default_factory=list, description="Tags including agent:id")

    model_config = {
        "json_schema_extra": {
            "example": {
                "title": "Order Support",
                "description": "Help customers with their orders",
                "conditions": ["When customer mentions order", "When customer needs help"],
                "tags": ["agent:abc123"]
            }
        }
    }


class JourneyUpdate(BaseModel):
    """Update journey request."""
    title: Optional[str] = Field(None, description="Journey title")
    description: Optional[str] = Field(None, description="Journey description")
    conditions: Optional[list[str]] = Field(None, description="Trigger conditions")


class BotCreateRequest(BaseModel):
    """Create bot request with guidelines and journeys."""
    name: str = Field(..., description="Bot name")
    purpose: str = Field(..., description="Bot's primary purpose")
    scope: str = Field(..., description="What the bot handles")
    target_users: str = Field(..., description="Who will use the bot")
    use_cases: list[str] = Field(..., description="List of use cases")
    tone: str = Field(..., description="Communication tone")
    personality: str = Field(..., description="Bot personality traits")
    tools: list[str] = Field(default=["none"], description="Required tools")
    constraints: list[str] = Field(..., description="Business rules")
    guardrails: list[str] = Field(..., description="Safety measures")
    guidelines: list[GuidelineCreate] = Field(..., description="Behavior rules")
    journeys: list[JourneyCreate] = Field(..., description="Conversation flows")
    composition_mode: Optional[str] = Field("FLUID", description="FLUID, COMPOSITED, or STRICT")
    max_engine_iterations: Optional[int] = Field(3, description="Max iterations")


class BotUpdate(BaseModel):
    """Update bot request."""
    name: Optional[str] = Field(None, description="Bot name")
    description: Optional[str] = Field(None, description="Bot description")
    composition_mode: Optional[str] = Field(None, description="Composition mode")
    max_engine_iterations: Optional[int] = Field(None, description="Max iterations")


class MessageInput(BaseModel):
    """Send message request."""
    message: str = Field(..., description="Message content")


class EventInput(BaseModel):
    """Send event request."""
    kind: str = Field("message", description="Event kind")
    source: str = Field("customer", description="Event source")
    message: Optional[str] = Field(None, description="Message content")
    data: Optional[dict] = Field(None, description="Additional event data")


# =============================================================================
# Application Lifecycle
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown."""
    global _client
    
    print("=" * 60)
    print("🚀 Bot Management API v2.0 - Starting...")
    print("=" * 60)
    print(f"📡 Parlant API: {PARLANT_API_BASE_URL}")
    print(f"⏱️  Timeout: {PARLANT_API_TIMEOUT}s")
    
    _client = ParlantClient(
        base_url=PARLANT_API_BASE_URL,
        timeout=PARLANT_API_TIMEOUT,
        token=PARLANT_API_TOKEN,
    )
    
    # Verify Parlant connectivity
    success, _ = await _client.list_agents()
    if success:
        print("✅ Connected to Parlant API")
    else:
        print("⚠️  Parlant API not available - will retry on requests")
    
    # Initialize MongoDB persistence for mirroring
    if PERSISTENCE_AVAILABLE and MONGODB_URI:
        print(f"🗄️  Initializing MongoDB persistence...")
        success, message = await initialize_persistence(MONGODB_URI)
        if success:
            print(f"✅ {message}")
            print("📝 CRUD operations will be mirrored to MongoDB")
        else:
            print(f"⚠️  {message}")
            print("📝 MongoDB mirroring disabled")
    else:
        print("📝 MongoDB mirroring disabled (no MONGODB_URI)")
    
    print("-" * 60)
    print(f"🌐 API Server ready on http://{API_HOST}:{API_PORT}")
    print(f"📖 API Docs: http://localhost:{API_PORT}/docs")
    print("-" * 60)
    
    yield
    
    # Shutdown
    print("👋 Shutting down Bot Management API...")
    if PERSISTENCE_AVAILABLE:
        await shutdown_persistence()
        print("🗄️  MongoDB connection closed")


# =============================================================================
# FastAPI Application
# =============================================================================

app = FastAPI(
    title="Bot Management API",
    description="Complete REST API for managing AI bots with Parlant - Full CRUD for bots, guidelines, journeys, and chat sessions",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================================================================
# Helper Functions
# =============================================================================

def _map_criticality(crit: str | None) -> str:
    """Map criticality to Parlant API format."""
    if not crit:
        return "medium"
    crit_lower = crit.lower()
    return crit_lower if crit_lower in ["low", "medium", "high"] else "medium"


def _map_composition_mode(mode: str | None) -> str:
    """Map composition mode to Parlant API format."""
    return {"COMPOSITED": "composited_canned", "STRICT": "strict_canned"}.get(mode or "", "fluid")


def _build_description(spec: dict) -> str:
    """Build agent description from spec."""
    return "\n".join([
        f"Purpose: {spec.get('purpose', '')}",
        f"Scope: {spec.get('scope', '')}",
        f"Target users: {spec.get('target_users', '')}",
        f"Tone: {spec.get('tone', '')}",
        f"Personality: {spec.get('personality', '')}",
        f"Use cases: {'; '.join(spec.get('use_cases', []))}",
        f"Tools: {', '.join(spec.get('tools', ['none']))}",
        f"Constraints: {'; '.join(spec.get('constraints', []))}",
        f"Guardrails: {'; '.join(spec.get('guardrails', []))}",
    ])


def _normalize_list(response: Any) -> list:
    """Normalize API response to list."""
    if isinstance(response, list):
        return response
    if isinstance(response, dict):
        return response.get("items", response.get("data", []))
    return []


async def _get_bot_with_details(agent: dict) -> dict:
    """Get a bot with its guidelines and journeys."""
    agent_id = agent.get("id")
    agent_tag = f"agent:{agent_id}"
    
    # Fetch guidelines for this agent
    success, guidelines_response = await _client.list_guidelines(agent_tag)
    guidelines = []
    if success:
        all_guidelines = _normalize_list(guidelines_response)
        guidelines = [
            {
                "id": g.get("id"),
                "condition": g.get("condition"),
                "action": g.get("action"),
                "description": g.get("description"),
                "criticality": g.get("criticality"),
                "tags": g.get("tags", []),
            }
            for g in all_guidelines
            if agent_tag in g.get("tags", [])
        ]
    
    # Fetch journeys for this agent
    success, journeys_response = await _client.list_journeys(agent_tag)
    journeys = []
    if success:
        all_journeys = _normalize_list(journeys_response)
        journeys = [
            {
                "id": j.get("id"),
                "title": j.get("title"),
                "description": j.get("description"),
                "conditions": j.get("conditions", []),
                "tags": j.get("tags", []),
            }
            for j in all_journeys
            if agent_tag in j.get("tags", [])
        ]
    
    return {
        "id": agent_id,
        "name": agent.get("name"),
        "description": agent.get("description"),
        "composition_mode": agent.get("composition_mode"),
        "max_engine_iterations": agent.get("max_engine_iterations"),
        "created_at": agent.get("creation_utc"),
        "status": "CREATED",
        "guidelines": guidelines,
        "journeys": journeys,
    }


# =============================================================================
# Health Endpoints
# =============================================================================

@app.get("/", tags=["Health"])
async def root():
    """Health check endpoint."""
    parlant_status = "unknown"
    if _client:
        success, _ = await _client.list_agents()
        parlant_status = "connected" if success else "disconnected"
    
    return {
        "service": "Bot Management API",
        "version": "2.0.0",
        "status": "running",
        "parlant_api": PARLANT_API_BASE_URL,
        "parlant_status": parlant_status,
    }


@app.get("/health", tags=["Health"])
async def health():
    """Detailed health check."""
    return await root()


# =============================================================================
# Bot/Agent Endpoints
# =============================================================================

@app.get("/bots", tags=["Bots"])
async def list_bots():
    """List all bots (excludes system agents like Otto)."""
    if not _client:
        raise HTTPException(503, "Service not initialized")
    
    success, response = await _client.list_agents()
    if not success:
        raise HTTPException(502, f"Parlant API error: {response.get('error')}")
    
    agents = _normalize_list(response)
    
    # Filter out Otto and get details
    bots = []
    for agent in agents:
        if agent.get("name") == "Otto":
            continue
        bot = await _get_bot_with_details(agent)
        bots.append(bot)
    
    return {"bots": bots, "count": len(bots)}


@app.get("/bots/{bot_id}", tags=["Bots"])
async def get_bot(bot_id: str):
    """Get a specific bot with its guidelines and journeys."""
    if not _client:
        raise HTTPException(503, "Service not initialized")
    
    success, agent = await _client.get_agent(bot_id)
    if not success:
        status_code = agent.get("status_code", 404)
        raise HTTPException(status_code, f"Bot not found: {bot_id}")
    
    return await _get_bot_with_details(agent)


@app.post("/bots", tags=["Bots"], status_code=201)
async def create_bot(request: BotCreateRequest):
    """Create a new bot with guidelines and journeys."""
    if not _client:
        raise HTTPException(503, "Service not initialized")
    
    spec = request.model_dump()
    
    # Create agent
    agent_payload = {
        "name": spec["name"],
        "description": _build_description(spec),
        "composition_mode": _map_composition_mode(spec.get("composition_mode")),
        "max_engine_iterations": spec.get("max_engine_iterations", 3),
    }
    
    success, agent_response = await _client.create_agent(agent_payload)
    if not success:
        raise HTTPException(502, f"Failed to create agent: {agent_response.get('error')}")
    
    agent_id = agent_response.get("id")
    agent_tag = f"agent:{agent_id}"
    
    # Create guidelines and track their IDs for MongoDB persistence
    guidelines_created = 0
    created_guidelines = []
    for guideline in spec.get("guidelines", []):
        payload = {
            "condition": guideline.get("condition"),
            "action": guideline.get("action"),
            "description": guideline.get("description"),
            "criticality": _map_criticality(guideline.get("criticality")),
            "tags": [agent_tag],
        }
        success, guideline_response = await _client.create_guideline(payload)
        if success:
            guidelines_created += 1
            created_guidelines.append({
                "id": guideline_response.get("id"),
                "condition": guideline.get("condition"),
                "action": guideline.get("action"),
                "description": guideline.get("description"),
                "criticality": guideline.get("criticality"),
            })
    
    # Create journeys and track their IDs for MongoDB persistence
    journeys_created = 0
    created_journeys = []
    for journey in spec.get("journeys", []):
        payload = {
            "title": journey.get("title"),
            "description": journey.get("description"),
            "conditions": journey.get("conditions"),
            "tags": [agent_tag],
        }
        success, journey_response = await _client.create_journey(payload)
        if success:
            journeys_created += 1
            created_journeys.append({
                "id": journey_response.get("id"),
                "title": journey.get("title"),
                "description": journey.get("description"),
                "conditions": journey.get("conditions"),
            })
    
    # Mirror CREATE to MongoDB for persistence (bot, guidelines, and journeys)
    if PERSISTENCE_AVAILABLE:
        try:
            persistence = get_persistence()
            if persistence and persistence.enabled:
                # Persist bot
                await persistence.persist_bot(
                    bot_id=agent_id,
                    name=spec["name"],
                    description=_build_description(spec),
                    composition_mode=_map_composition_mode(spec.get("composition_mode")),
                    max_engine_iterations=spec.get("max_engine_iterations", 3),
                    metadata={
                        "purpose": spec.get("purpose"),
                        "scope": spec.get("scope"),
                        "target_users": spec.get("target_users"),
                        "tone": spec.get("tone"),
                        "personality": spec.get("personality"),
                        "use_cases": spec.get("use_cases"),
                        "tools": spec.get("tools"),
                        "constraints": spec.get("constraints"),
                        "guardrails": spec.get("guardrails"),
                    }
                )
                
                # Persist guidelines
                for g in created_guidelines:
                    await persistence.persist_guideline(
                        guideline_id=g["id"],
                        bot_id=agent_id,
                        condition=g["condition"],
                        action=g["action"],
                        description=g["description"],
                        criticality=_map_criticality(g["criticality"]),
                    )
                
                # Persist journeys
                for j in created_journeys:
                    await persistence.persist_journey(
                        journey_id=j["id"],
                        bot_id=agent_id,
                        title=j["title"],
                        description=j["description"],
                        conditions=j["conditions"],
                    )
                
                print(f"💾 MongoDB mirrored: CREATE bot {agent_id} ({spec['name']}) with {guidelines_created} guidelines, {journeys_created} journeys")
        except Exception as e:
            print(f"⚠️  MongoDB mirror failed for bot create: {e}")
    
    return {
        "success": True,
        "status": "CREATED",
        "bot_id": agent_id,
        "bot_name": spec["name"],
        "guidelines_created": guidelines_created,
        "journeys_created": journeys_created,
    }


@app.patch("/bots/{bot_id}", tags=["Bots"])
async def update_bot(bot_id: str, request: BotUpdate):
    """Update a bot's properties."""
    if not _client:
        raise HTTPException(503, "Service not initialized")
    
    # Build update payload with only non-None values
    update_data = {}
    if request.name is not None:
        update_data["name"] = request.name
    if request.description is not None:
        update_data["description"] = request.description
    if request.composition_mode is not None:
        update_data["composition_mode"] = _map_composition_mode(request.composition_mode)
    if request.max_engine_iterations is not None:
        update_data["max_engine_iterations"] = request.max_engine_iterations
    
    if not update_data:
        raise HTTPException(400, "No update fields provided")
    
    success, response = await _client.update_agent(bot_id, update_data)
    if not success:
        status_code = response.get("status_code", 400)
        raise HTTPException(status_code, f"Failed to update bot: {response.get('error')}")
    
    # Mirror UPDATE to MongoDB
    if PERSISTENCE_AVAILABLE:
        try:
            persistence = get_persistence()
            if persistence and persistence.enabled:
                await persistence.update_bot(
                    bot_id=bot_id,
                    name=request.name,
                    description=request.description,
                    composition_mode=_map_composition_mode(request.composition_mode) if request.composition_mode else None,
                    max_engine_iterations=request.max_engine_iterations,
                )
                print(f"📝 MongoDB mirrored: UPDATE bot {bot_id}")
        except Exception as e:
            print(f"⚠️  MongoDB mirror failed for bot update: {e}")
    
    return {"status": "updated", "bot_id": bot_id}


@app.delete("/bots/{bot_id}", tags=["Bots"])
async def delete_bot(bot_id: str):
    """Delete a bot and its associated guidelines and journeys."""
    if not _client:
        raise HTTPException(503, "Service not initialized")
    
    agent_tag = f"agent:{bot_id}"
    
    # Delete associated guidelines
    success, guidelines_response = await _client.list_guidelines(agent_tag)
    if success:
        guidelines = _normalize_list(guidelines_response)
        for g in guidelines:
            if agent_tag in g.get("tags", []):
                await _client.delete_guideline(g["id"])
    
    # Delete associated journeys
    success, journeys_response = await _client.list_journeys(agent_tag)
    if success:
        journeys = _normalize_list(journeys_response)
        for j in journeys:
            if agent_tag in j.get("tags", []):
                await _client.delete_journey(j["id"])
    
    # Delete agent
    success, response = await _client.delete_agent(bot_id)
    if not success:
        status_code = response.get("status_code", 404)
        raise HTTPException(status_code, f"Bot not found: {bot_id}")
    
    # Mirror DELETE to MongoDB (deletes bot and all related data)
    if PERSISTENCE_AVAILABLE:
        try:
            persistence = get_persistence()
            if persistence and persistence.enabled:
                await persistence.delete_bot(bot_id)
                print(f"🗑️  MongoDB mirrored: DELETE bot {bot_id}")
        except Exception as e:
            print(f"⚠️  MongoDB mirror failed for bot delete: {e}")
    
    return {"status": "deleted", "bot_id": bot_id}


# =============================================================================
# Guideline Endpoints
# =============================================================================

@app.get("/guidelines", tags=["Guidelines"])
async def list_guidelines(tag: Optional[str] = Query(None, description="Filter by tag (e.g., agent:abc123)")):
    """List all guidelines, optionally filtered by tag."""
    if not _client:
        raise HTTPException(503, "Service not initialized")
    
    success, response = await _client.list_guidelines(tag)
    if not success:
        raise HTTPException(502, f"Failed to list guidelines: {response.get('error')}")
    
    guidelines = _normalize_list(response)
    return {"guidelines": guidelines, "count": len(guidelines)}


@app.get("/guidelines/{guideline_id}", tags=["Guidelines"])
async def get_guideline(guideline_id: str):
    """Get a specific guideline."""
    if not _client:
        raise HTTPException(503, "Service not initialized")
    
    success, response = await _client.get_guideline(guideline_id)
    if not success:
        status_code = response.get("status_code", 404)
        raise HTTPException(status_code, f"Guideline not found: {guideline_id}")
    
    return response


@app.post("/guidelines", tags=["Guidelines"], status_code=201)
async def create_guideline(request: GuidelineCreate):
    """Create a new guideline."""
    if not _client:
        raise HTTPException(503, "Service not initialized")
    
    payload = {
        "condition": request.condition,
        "action": request.action,
        "description": request.description,
        "criticality": _map_criticality(request.criticality),
        "tags": request.tags or [],
    }
    
    success, response = await _client.create_guideline(payload)
    if not success:
        raise HTTPException(400, f"Failed to create guideline: {response.get('error')}")
    
    # Mirror CREATE to MongoDB
    if PERSISTENCE_AVAILABLE:
        try:
            persistence = get_persistence()
            if persistence and persistence.enabled:
                # Extract bot_id from tags
                bot_id = None
                for tag in (request.tags or []):
                    if tag.startswith("agent:"):
                        bot_id = tag.replace("agent:", "")
                        break
                if bot_id:
                    await persistence.persist_guideline(
                        guideline_id=response.get("id"),
                        bot_id=bot_id,
                        condition=request.condition,
                        action=request.action,
                        description=request.description,
                        criticality=_map_criticality(request.criticality),
                    )
        except Exception as e:
            print(f"⚠️  MongoDB mirror failed for guideline create: {e}")
    
    return {
        "status": "created",
        "guideline_id": response.get("id"),
        "guideline": response,
    }


@app.patch("/guidelines/{guideline_id}", tags=["Guidelines"])
async def update_guideline(guideline_id: str, request: GuidelineUpdate):
    """Update a guideline."""
    if not _client:
        raise HTTPException(503, "Service not initialized")
    
    # Build update payload
    update_data = {}
    if request.condition is not None:
        update_data["condition"] = request.condition
    if request.action is not None:
        update_data["action"] = request.action
    if request.description is not None:
        update_data["description"] = request.description
    if request.criticality is not None:
        update_data["criticality"] = _map_criticality(request.criticality)
    
    if not update_data:
        raise HTTPException(400, "No update fields provided")
    
    success, response = await _client.update_guideline(guideline_id, update_data)
    if not success:
        status_code = response.get("status_code", 400)
        raise HTTPException(status_code, f"Failed to update guideline: {response.get('error')}")
    
    # Mirror UPDATE to MongoDB
    if PERSISTENCE_AVAILABLE:
        try:
            persistence = get_persistence()
            if persistence and persistence.enabled:
                await persistence.update_guideline(
                    guideline_id=guideline_id,
                    condition=request.condition,
                    action=request.action,
                    description=request.description,
                    criticality=_map_criticality(request.criticality) if request.criticality else None,
                )
                print(f"📝 MongoDB mirrored: UPDATE guideline {guideline_id}")
        except Exception as e:
            print(f"⚠️  MongoDB mirror failed for guideline update: {e}")
    
    return {"status": "updated", "guideline_id": guideline_id}


@app.delete("/guidelines/{guideline_id}", tags=["Guidelines"])
async def delete_guideline(guideline_id: str):
    """Delete a guideline."""
    if not _client:
        raise HTTPException(503, "Service not initialized")
    
    success, response = await _client.delete_guideline(guideline_id)
    if not success:
        status_code = response.get("status_code", 404)
        raise HTTPException(status_code, f"Guideline not found: {guideline_id}")
    
    # Mirror DELETE to MongoDB
    if PERSISTENCE_AVAILABLE:
        try:
            persistence = get_persistence()
            if persistence and persistence.enabled:
                await persistence.delete_guideline(guideline_id)
                print(f"🗑️  MongoDB mirrored: DELETE guideline {guideline_id}")
        except Exception as e:
            print(f"⚠️  MongoDB mirror failed for guideline delete: {e}")
    
    return {"status": "deleted", "guideline_id": guideline_id}


@app.post("/bots/{bot_id}/guidelines", tags=["Guidelines"], status_code=201)
async def add_guideline_to_bot(bot_id: str, request: GuidelineCreate):
    """Add a guideline to a specific bot."""
    if not _client:
        raise HTTPException(503, "Service not initialized")
    
    # Ensure bot exists
    success, _ = await _client.get_agent(bot_id)
    if not success:
        raise HTTPException(404, f"Bot not found: {bot_id}")
    
    payload = {
        "condition": request.condition,
        "action": request.action,
        "description": request.description,
        "criticality": _map_criticality(request.criticality),
        "tags": [f"agent:{bot_id}"] + (request.tags or []),
    }
    
    success, response = await _client.create_guideline(payload)
    if not success:
        raise HTTPException(400, f"Failed to create guideline: {response.get('error')}")
    
    # Mirror CREATE to MongoDB
    if PERSISTENCE_AVAILABLE:
        try:
            persistence = get_persistence()
            if persistence and persistence.enabled:
                await persistence.persist_guideline(
                    guideline_id=response.get("id"),
                    bot_id=bot_id,
                    condition=request.condition,
                    action=request.action,
                    description=request.description,
                    criticality=_map_criticality(request.criticality),
                )
        except Exception as e:
            print(f"⚠️  MongoDB mirror failed for guideline create: {e}")
    
    return {
        "status": "created",
        "guideline_id": response.get("id"),
        "bot_id": bot_id,
    }


# =============================================================================
# Journey Endpoints
# =============================================================================

@app.get("/journeys", tags=["Journeys"])
async def list_journeys(tag: Optional[str] = Query(None, description="Filter by tag (e.g., agent:abc123)")):
    """List all journeys, optionally filtered by tag."""
    if not _client:
        raise HTTPException(503, "Service not initialized")
    
    success, response = await _client.list_journeys(tag)
    if not success:
        raise HTTPException(502, f"Failed to list journeys: {response.get('error')}")
    
    journeys = _normalize_list(response)
    return {"journeys": journeys, "count": len(journeys)}


@app.get("/journeys/{journey_id}", tags=["Journeys"])
async def get_journey(journey_id: str):
    """Get a specific journey."""
    if not _client:
        raise HTTPException(503, "Service not initialized")
    
    success, response = await _client.get_journey(journey_id)
    if not success:
        status_code = response.get("status_code", 404)
        raise HTTPException(status_code, f"Journey not found: {journey_id}")
    
    return response


@app.post("/journeys", tags=["Journeys"], status_code=201)
async def create_journey(request: JourneyCreate):
    """Create a new journey."""
    if not _client:
        raise HTTPException(503, "Service not initialized")
    
    payload = {
        "title": request.title,
        "description": request.description,
        "conditions": request.conditions,
        "tags": request.tags or [],
    }
    
    success, response = await _client.create_journey(payload)
    if not success:
        raise HTTPException(400, f"Failed to create journey: {response.get('error')}")
    
    # Mirror CREATE to MongoDB
    if PERSISTENCE_AVAILABLE:
        try:
            persistence = get_persistence()
            if persistence and persistence.enabled:
                # Extract bot_id from tags
                bot_id = None
                for tag in (request.tags or []):
                    if tag.startswith("agent:"):
                        bot_id = tag.replace("agent:", "")
                        break
                if bot_id:
                    await persistence.persist_journey(
                        journey_id=response.get("id"),
                        bot_id=bot_id,
                        title=request.title,
                        description=request.description,
                        conditions=request.conditions,
                    )
        except Exception as e:
            print(f"⚠️  MongoDB mirror failed for journey create: {e}")
    
    return {
        "status": "created",
        "journey_id": response.get("id"),
        "journey": response,
    }


@app.patch("/journeys/{journey_id}", tags=["Journeys"])
async def update_journey(journey_id: str, request: JourneyUpdate):
    """Update a journey."""
    if not _client:
        raise HTTPException(503, "Service not initialized")
    
    # Build update payload
    update_data = {}
    if request.title is not None:
        update_data["title"] = request.title
    if request.description is not None:
        update_data["description"] = request.description
    if request.conditions is not None:
        update_data["conditions"] = request.conditions
    
    if not update_data:
        raise HTTPException(400, "No update fields provided")
    
    success, response = await _client.update_journey(journey_id, update_data)
    if not success:
        status_code = response.get("status_code", 400)
        raise HTTPException(status_code, f"Failed to update journey: {response.get('error')}")
    
    # Mirror UPDATE to MongoDB
    if PERSISTENCE_AVAILABLE:
        try:
            persistence = get_persistence()
            if persistence and persistence.enabled:
                await persistence.update_journey(
                    journey_id=journey_id,
                    title=request.title,
                    description=request.description,
                    conditions=request.conditions,
                )
                print(f"📝 MongoDB mirrored: UPDATE journey {journey_id}")
        except Exception as e:
            print(f"⚠️  MongoDB mirror failed for journey update: {e}")
    
    return {"status": "updated", "journey_id": journey_id}


@app.delete("/journeys/{journey_id}", tags=["Journeys"])
async def delete_journey(journey_id: str):
    """Delete a journey."""
    if not _client:
        raise HTTPException(503, "Service not initialized")
    
    success, response = await _client.delete_journey(journey_id)
    if not success:
        status_code = response.get("status_code", 404)
        raise HTTPException(status_code, f"Journey not found: {journey_id}")
    
    # Mirror DELETE to MongoDB
    if PERSISTENCE_AVAILABLE:
        try:
            persistence = get_persistence()
            if persistence and persistence.enabled:
                await persistence.delete_journey(journey_id)
                print(f"🗑️  MongoDB mirrored: DELETE journey {journey_id}")
        except Exception as e:
            print(f"⚠️  MongoDB mirror failed for journey delete: {e}")
    
    return {"status": "deleted", "journey_id": journey_id}


@app.post("/bots/{bot_id}/journeys", tags=["Journeys"], status_code=201)
async def add_journey_to_bot(bot_id: str, request: JourneyCreate):
    """Add a journey to a specific bot."""
    if not _client:
        raise HTTPException(503, "Service not initialized")
    
    # Ensure bot exists
    success, _ = await _client.get_agent(bot_id)
    if not success:
        raise HTTPException(404, f"Bot not found: {bot_id}")
    
    payload = {
        "title": request.title,
        "description": request.description,
        "conditions": request.conditions,
        "tags": [f"agent:{bot_id}"] + (request.tags or []),
    }
    
    success, response = await _client.create_journey(payload)
    if not success:
        raise HTTPException(400, f"Failed to create journey: {response.get('error')}")
    
    # Mirror CREATE to MongoDB
    if PERSISTENCE_AVAILABLE:
        try:
            persistence = get_persistence()
            if persistence and persistence.enabled:
                await persistence.persist_journey(
                    journey_id=response.get("id"),
                    bot_id=bot_id,
                    title=request.title,
                    description=request.description,
                    conditions=request.conditions,
                )
        except Exception as e:
            print(f"⚠️  MongoDB mirror failed for journey create: {e}")
    
    return {
        "status": "created",
        "journey_id": response.get("id"),
        "bot_id": bot_id,
    }


# =============================================================================
# Session/Chat Endpoints
# =============================================================================

@app.post("/bots/{bot_id}/sessions", tags=["Sessions"], status_code=201)
async def create_session(bot_id: str, customer_id: Optional[str] = None):
    """Create a new chat session with a bot."""
    if not _client:
        raise HTTPException(503, "Service not initialized")
    
    success, response = await _client.create_session(bot_id, customer_id)
    if not success:
        status_code = response.get("status_code", 400)
        raise HTTPException(status_code, f"Failed to create session: {response.get('error')}")
    
    return {
        "status": "created",
        "session_id": response.get("id"),
        "bot_id": bot_id,
    }


@app.get("/sessions/{session_id}", tags=["Sessions"])
async def get_session(session_id: str):
    """Get session details."""
    if not _client:
        raise HTTPException(503, "Service not initialized")
    
    success, response = await _client.get_session(session_id)
    if not success:
        status_code = response.get("status_code", 404)
        raise HTTPException(status_code, f"Session not found: {session_id}")
    
    return response


@app.delete("/sessions/{session_id}", tags=["Sessions"])
async def delete_session(session_id: str):
    """Delete a session."""
    if not _client:
        raise HTTPException(503, "Service not initialized")
    
    success, response = await _client.delete_session(session_id)
    if not success:
        status_code = response.get("status_code", 404)
        raise HTTPException(status_code, f"Session not found: {session_id}")
    
    return {"status": "deleted", "session_id": session_id}


@app.get("/sessions/{session_id}/events", tags=["Sessions"])
async def get_events(session_id: str):
    """Get all events from a session."""
    if not _client:
        raise HTTPException(503, "Service not initialized")
    
    success, response = await _client.get_events(session_id)
    if not success:
        status_code = response.get("status_code", 400)
        raise HTTPException(status_code, f"Failed to get events: {response.get('error')}")
    
    events = _normalize_list(response)
    return {"session_id": session_id, "events": events}


@app.post("/sessions/{session_id}/events", tags=["Sessions"])
async def send_event(session_id: str, request: EventInput):
    """Send an event to a session."""
    if not _client:
        raise HTTPException(503, "Service not initialized")
    
    event_data = {
        "kind": request.kind,
        "source": request.source,
    }
    if request.message:
        event_data["message"] = request.message
    if request.data:
        event_data["data"] = request.data
    
    success, response = await _client.send_event(session_id, event_data)
    if not success:
        raise HTTPException(400, f"Failed to send event: {response.get('error')}")
    
    return {"status": "sent", "session_id": session_id}


# Message aliases (for simpler API)
@app.post("/sessions/{session_id}/messages", tags=["Sessions"])
async def send_message(session_id: str, body: MessageInput):
    """Send a message to a session (alias for events)."""
    if not _client:
        raise HTTPException(503, "Service not initialized")
    
    event_data = {
        "kind": "message",
        "source": "customer",
        "message": body.message,
    }
    
    success, response = await _client.send_event(session_id, event_data)
    if not success:
        raise HTTPException(400, f"Failed to send message: {response.get('error')}")
    
    return {"status": "sent", "session_id": session_id}


@app.get("/sessions/{session_id}/messages", tags=["Sessions"])
async def get_messages(session_id: str):
    """Get messages from a session (alias for events)."""
    if not _client:
        raise HTTPException(503, "Service not initialized")
    
    success, response = await _client.get_events(session_id)
    if not success:
        raise HTTPException(400, f"Failed to get messages: {response.get('error')}")
    
    events = _normalize_list(response)
    # Filter to only message events
    messages = [e for e in events if e.get("kind") == "message" or e.get("event_kind") == "message"]
    
    return {"session_id": session_id, "messages": messages}


# =============================================================================
# Run Server
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "api_server:app",
        host=API_HOST,
        port=API_PORT,
        reload=True,
    )
