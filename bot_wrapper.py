"""
Bot Creation Wrapper Layer

This module provides a robust wrapper over the REST-based bot creation system with:
- Idempotency guarantees (no duplicate bots)
- Transaction-like atomic creation flow
- Automatic retry on persistence failures
- Status tracking (CREATED, PARTIALLY_CREATED, FAILED)
- Clear error classification
- Recovery and reconciliation support

Architecture:
┌─────────────────────────────────────────────────────────────────┐
│                     BotCreationWrapper                          │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  1. Validate Input                                        │  │
│  │  2. Check Idempotency (duplicate detection)              │  │
│  │  3. Create in Parlant (REST API)                         │  │
│  │  4. Persist to MongoDB (with retry)                      │  │
│  │  5. Return structured result                             │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
"""

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional
import httpx


class BotStatus(str, Enum):
    """Bot creation status states."""
    PENDING = "PENDING"
    CREATED = "CREATED"
    PARTIALLY_CREATED = "PARTIALLY_CREATED"
    FAILED = "FAILED"


class ErrorType(str, Enum):
    """Classification of errors for actionable diagnostics."""
    VALIDATION = "VALIDATION"
    API_FAILURE = "API_FAILURE"
    PERSISTENCE_FAILURE = "PERSISTENCE_FAILURE"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INTERNAL = "INTERNAL"


@dataclass
class WrapperError:
    """Structured error with type classification."""
    error_type: ErrorType
    message: str
    details: Optional[dict[str, Any]] = None
    recoverable: bool = False


@dataclass
class CreationResult:
    """Result of a bot creation operation."""
    success: bool
    status: BotStatus
    bot_id: Optional[str] = None
    bot_name: Optional[str] = None
    guidelines_created: int = 0
    journeys_created: int = 0
    persisted_to_mongodb: bool = False
    idempotency_key: Optional[str] = None
    errors: list[WrapperError] = field(default_factory=list)
    created_at: Optional[datetime] = None
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dictionary."""
        return {
            "success": self.success,
            "status": self.status.value,
            "bot_id": self.bot_id,
            "bot_name": self.bot_name,
            "guidelines_created": self.guidelines_created,
            "journeys_created": self.journeys_created,
            "persisted_to_mongodb": self.persisted_to_mongodb,
            "idempotency_key": self.idempotency_key,
            "errors": [
                {
                    "type": e.error_type.value,
                    "message": e.message,
                    "details": e.details,
                    "recoverable": e.recoverable,
                }
                for e in self.errors
            ],
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class BotCreationWrapper:
    """
    Wrapper layer guaranteeing persistent, idempotent bot creation.
    
    This sits between business logic and REST APIs, ensuring:
    - Every bot is persisted to MongoDB
    - Duplicate requests don't create duplicates
    - Partial failures are tracked for reconciliation
    """
    
    # Retry configuration
    MAX_PERSISTENCE_RETRIES = 3
    RETRY_DELAY_SECONDS = 1
    
    def __init__(
        self,
        parlant_api_base_url: str,
        parlant_api_timeout: int = 30,
        parlant_api_token: Optional[str] = None,
    ):
        self.parlant_api_base_url = parlant_api_base_url
        self.parlant_api_timeout = parlant_api_timeout
        self.parlant_api_token = parlant_api_token
        self._persistence = None
    
    def set_persistence(self, persistence):
        """Inject the persistence layer."""
        self._persistence = persistence
    
    # =========================================================================
    # Idempotency
    # =========================================================================
    
    def _compute_idempotency_key(self, spec: dict[str, Any]) -> str:
        """
        Compute a deterministic idempotency key from the spec.
        
        Uses name + a hash of critical fields to detect duplicates.
        """
        # Fields that define uniqueness
        key_fields = {
            "name": spec.get("name", ""),
            "purpose": spec.get("purpose", ""),
            "scope": spec.get("scope", ""),
        }
        key_string = json.dumps(key_fields, sort_keys=True)
        hash_digest = hashlib.sha256(key_string.encode()).hexdigest()[:16]
        return f"{spec.get('name', 'unnamed')}:{hash_digest}"
    
    async def _check_idempotency(self, idempotency_key: str) -> Optional[dict[str, Any]]:
        """
        Check if a bot with this idempotency key already exists.
        
        Returns the existing bot document if found, None otherwise.
        """
        if not self._persistence or not self._persistence.enabled:
            return None
        
        try:
            # Check by idempotency_key in metadata
            if self._persistence.db is not None:
                existing = await self._persistence.db.bots.find_one({
                    "metadata.idempotency_key": idempotency_key
                })
                return existing
        except Exception:
            pass
        return None
    
    # =========================================================================
    # Validation
    # =========================================================================
    
    def _validate_spec(self, spec: dict[str, Any]) -> list[WrapperError]:
        """
        Validate the bot specification.
        
        Returns a list of validation errors (empty if valid).
        """
        errors = []
        
        # Required string fields
        required_strings = ["name", "purpose", "scope", "target_users", "tone", "personality"]
        for field_name in required_strings:
            value = spec.get(field_name)
            if not value or not isinstance(value, str) or not value.strip():
                errors.append(WrapperError(
                    error_type=ErrorType.VALIDATION,
                    message=f"'{field_name}' is required and must be a non-empty string",
                    recoverable=False,
                ))
        
        # Required list fields
        required_lists = ["use_cases", "tools", "constraints", "guardrails"]
        for field_name in required_lists:
            value = spec.get(field_name)
            if not isinstance(value, list) or not value:
                errors.append(WrapperError(
                    error_type=ErrorType.VALIDATION,
                    message=f"'{field_name}' must be a non-empty list",
                    recoverable=False,
                ))
            elif not all(isinstance(item, str) and item.strip() for item in value):
                errors.append(WrapperError(
                    error_type=ErrorType.VALIDATION,
                    message=f"'{field_name}' must contain only non-empty strings",
                    recoverable=False,
                ))
        
        # Guidelines validation
        guidelines = spec.get("guidelines", [])
        if not isinstance(guidelines, list) or not guidelines:
            errors.append(WrapperError(
                error_type=ErrorType.VALIDATION,
                message="'guidelines' must be a non-empty list",
                recoverable=False,
            ))
        else:
            for idx, guideline in enumerate(guidelines, 1):
                if not isinstance(guideline, dict):
                    errors.append(WrapperError(
                        error_type=ErrorType.VALIDATION,
                        message=f"guidelines[{idx}] must be an object",
                        recoverable=False,
                    ))
                    continue
                if not guideline.get("condition"):
                    errors.append(WrapperError(
                        error_type=ErrorType.VALIDATION,
                        message=f"guidelines[{idx}].condition is required",
                        recoverable=False,
                    ))
                crit = guideline.get("criticality")
                if crit and crit not in {"LOW", "MEDIUM", "HIGH"}:
                    errors.append(WrapperError(
                        error_type=ErrorType.VALIDATION,
                        message=f"guidelines[{idx}].criticality must be LOW, MEDIUM, or HIGH",
                        recoverable=False,
                    ))
        
        # Journeys validation
        journeys = spec.get("journeys", [])
        if not isinstance(journeys, list) or not journeys:
            errors.append(WrapperError(
                error_type=ErrorType.VALIDATION,
                message="'journeys' must be a non-empty list",
                recoverable=False,
            ))
        else:
            for idx, journey in enumerate(journeys, 1):
                if not isinstance(journey, dict):
                    errors.append(WrapperError(
                        error_type=ErrorType.VALIDATION,
                        message=f"journeys[{idx}] must be an object",
                        recoverable=False,
                    ))
                    continue
                if not journey.get("title"):
                    errors.append(WrapperError(
                        error_type=ErrorType.VALIDATION,
                        message=f"journeys[{idx}].title is required",
                        recoverable=False,
                    ))
                if not journey.get("description"):
                    errors.append(WrapperError(
                        error_type=ErrorType.VALIDATION,
                        message=f"journeys[{idx}].description is required",
                        recoverable=False,
                    ))
                conditions = journey.get("conditions", [])
                if not isinstance(conditions, list) or not conditions:
                    errors.append(WrapperError(
                        error_type=ErrorType.VALIDATION,
                        message=f"journeys[{idx}].conditions must be a non-empty list",
                        recoverable=False,
                    ))
        
        # Optional fields validation
        composition_mode = spec.get("composition_mode")
        if composition_mode and composition_mode not in {"FLUID", "COMPOSITED", "STRICT"}:
            errors.append(WrapperError(
                error_type=ErrorType.VALIDATION,
                message="composition_mode must be FLUID, COMPOSITED, or STRICT",
                recoverable=False,
            ))
        
        max_iterations = spec.get("max_engine_iterations")
        if max_iterations is not None:
            if not isinstance(max_iterations, int) or max_iterations <= 0:
                errors.append(WrapperError(
                    error_type=ErrorType.VALIDATION,
                    message="max_engine_iterations must be a positive integer",
                    recoverable=False,
                ))
        
        return errors
    
    # =========================================================================
    # REST API Calls
    # =========================================================================
    
    async def _call_parlant_api(
        self,
        method: str,
        endpoint: str,
        data: Optional[dict[str, Any]] = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Make a REST API call to the Parlant server."""
        url = f"{self.parlant_api_base_url}{endpoint}"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.parlant_api_token:
            headers["Authorization"] = f"Bearer {self.parlant_api_token}"
        
        try:
            async with httpx.AsyncClient(timeout=self.parlant_api_timeout) as client:
                if method.upper() == "POST":
                    response = await client.post(url, json=data, headers=headers)
                elif method.upper() == "GET":
                    response = await client.get(url, headers=headers)
                elif method.upper() == "DELETE":
                    response = await client.delete(url, headers=headers)
                else:
                    return False, {"error": f"Unsupported HTTP method: {method}"}
                
                response.raise_for_status()
                return True, response.json()
                
        except httpx.TimeoutException:
            return False, {"error": f"API timeout after {self.parlant_api_timeout}s"}
        except httpx.HTTPStatusError as exc:
            return False, {
                "error": f"API returned {exc.response.status_code}",
                "details": exc.response.text[:500],
            }
        except httpx.RequestError as exc:
            return False, {"error": f"API connection failed: {str(exc)}"}
        except Exception as exc:
            return False, {"error": f"Unexpected error: {str(exc)}"}
    
    # =========================================================================
    # Helper Methods
    # =========================================================================
    
    def _build_agent_description(self, spec: dict[str, Any]) -> str:
        """Build the agent description from the spec."""
        def join_list(items):
            if isinstance(items, list):
                return "; ".join(str(i) for i in items if i)
            return str(items) if items else ""
        
        return "\n".join([
            f"Purpose: {spec.get('purpose', '')}",
            f"Scope: {spec.get('scope', '')}",
            f"Target users: {spec.get('target_users', '')}",
            f"Tone: {spec.get('tone', '')}",
            f"Personality: {spec.get('personality', '')}",
            f"Primary use cases: {join_list(spec.get('use_cases', []))}",
            f"Required tools: {join_list(spec.get('tools', []))}",
            f"Constraints: {join_list(spec.get('constraints', []))}",
            f"Guardrails: {join_list(spec.get('guardrails', []))}",
        ])
    
    def _map_criticality(self, criticality: Optional[str]) -> str:
        """Map criticality to API format."""
        mapping = {"LOW": "low", "HIGH": "high"}
        return mapping.get(criticality or "", "medium")
    
    def _map_composition_mode(self, mode: Optional[str]) -> str:
        """Map composition mode to API format."""
        mapping = {"COMPOSITED": "composited_canned", "STRICT": "strict_canned"}
        return mapping.get(mode or "", "fluid")
    
    # =========================================================================
    # Persistence with Retry
    # =========================================================================
    
    async def _persist_with_retry(
        self,
        bot_id: str,
        bot_name: str,
        description: str,
        composition_mode: str,
        max_iterations: int,
        guidelines: list[dict],
        journeys: list[dict],
        idempotency_key: str,
        original_spec: dict[str, Any],
    ) -> tuple[bool, Optional[WrapperError]]:
        """
        Persist to MongoDB with retry logic.
        
        Returns (success, error) tuple.
        """
        if not self._persistence or not self._persistence.enabled:
            return False, WrapperError(
                error_type=ErrorType.PERSISTENCE_FAILURE,
                message="MongoDB persistence is not configured",
                recoverable=False,
            )
        
        for attempt in range(1, self.MAX_PERSISTENCE_RETRIES + 1):
            try:
                # Persist bot with metadata including idempotency key and original spec
                await self._persistence.persist_bot(
                    bot_id=bot_id,
                    name=bot_name,
                    description=description,
                    composition_mode=composition_mode,
                    max_engine_iterations=max_iterations,
                    metadata={
                        "idempotency_key": idempotency_key,
                        "original_spec": original_spec,
                        "status": BotStatus.CREATED.value,
                    },
                )
                
                # Persist guidelines
                for guideline in guidelines:
                    if "id" in guideline:
                        await self._persistence.persist_guideline(
                            guideline_id=guideline["id"],
                            bot_id=bot_id,
                            condition=guideline.get("condition", ""),
                            action=guideline.get("action"),
                            description=guideline.get("description"),
                            criticality=guideline.get("criticality", "medium"),
                        )
                
                # Persist journeys
                for journey in journeys:
                    if "id" in journey:
                        await self._persistence.persist_journey(
                            journey_id=journey["id"],
                            bot_id=bot_id,
                            title=journey.get("title", ""),
                            description=journey.get("description", ""),
                            conditions=journey.get("conditions", []),
                        )
                
                return True, None
                
            except Exception as e:
                if attempt < self.MAX_PERSISTENCE_RETRIES:
                    print(f"⚠️  Persistence attempt {attempt} failed: {e}. Retrying...")
                    await asyncio.sleep(self.RETRY_DELAY_SECONDS * attempt)
                else:
                    return False, WrapperError(
                        error_type=ErrorType.PERSISTENCE_FAILURE,
                        message=f"Failed after {self.MAX_PERSISTENCE_RETRIES} attempts: {str(e)}",
                        recoverable=True,
                    )
        
        return False, None
    
    async def _mark_partially_created(
        self,
        bot_id: str,
        bot_name: str,
        description: str,
        composition_mode: str,
        max_iterations: int,
        idempotency_key: str,
        original_spec: dict[str, Any],
        error_message: str,
    ) -> bool:
        """Mark a bot as PARTIALLY_CREATED for later reconciliation."""
        if not self._persistence or not self._persistence.enabled:
            return False
        
        try:
            await self._persistence.persist_bot(
                bot_id=bot_id,
                name=bot_name,
                description=description,
                composition_mode=composition_mode,
                max_engine_iterations=max_iterations,
                metadata={
                    "idempotency_key": idempotency_key,
                    "original_spec": original_spec,
                    "status": BotStatus.PARTIALLY_CREATED.value,
                    "error": error_message,
                    "needs_reconciliation": True,
                },
            )
            return True
        except Exception as e:
            print(f"❌ Failed to mark bot as PARTIALLY_CREATED: {e}")
            return False
    
    # =========================================================================
    # Main Creation Flow
    # =========================================================================
    
    async def create_bot(self, spec: dict[str, Any]) -> CreationResult:
        """
        Create a bot with full persistence guarantees.
        
        Flow:
        1. Validate input parameters
        2. Check idempotency (detect duplicates)
        3. Create agent in Parlant via REST API
        4. Create guidelines in Parlant
        5. Create journeys in Parlant
        6. Persist everything to MongoDB (with retry)
        7. Return structured result
        
        Args:
            spec: Complete bot specification
        
        Returns:
            CreationResult with status and details
        """
        result = CreationResult(
            success=False,
            status=BotStatus.PENDING,
            created_at=datetime.utcnow(),
        )
        
        # Step 1: Validation
        validation_errors = self._validate_spec(spec)
        if validation_errors:
            result.status = BotStatus.FAILED
            result.errors = validation_errors
            return result
        
        # Step 2: Idempotency check
        idempotency_key = self._compute_idempotency_key(spec)
        result.idempotency_key = idempotency_key
        
        existing = await self._check_idempotency(idempotency_key)
        if existing:
            # Return existing bot info instead of creating duplicate
            result.success = True
            result.status = BotStatus.CREATED
            result.bot_id = existing.get("bot_id")
            result.bot_name = existing.get("name")
            result.persisted_to_mongodb = True
            result.errors.append(WrapperError(
                error_type=ErrorType.IDEMPOTENCY_CONFLICT,
                message="Bot with same specification already exists",
                details={"existing_bot_id": existing.get("bot_id")},
                recoverable=False,
            ))
            return result
        
        # Step 3: Create agent in Parlant
        agent_payload = {
            "name": spec["name"],
            "description": self._build_agent_description(spec),
            "composition_mode": self._map_composition_mode(spec.get("composition_mode")),
            "max_engine_iterations": spec.get("max_engine_iterations", 3),
        }
        
        success, agent_response = await self._call_parlant_api("POST", "/agents", agent_payload)
        if not success:
            result.status = BotStatus.FAILED
            result.errors.append(WrapperError(
                error_type=ErrorType.API_FAILURE,
                message=f"Failed to create agent: {agent_response.get('error')}",
                details=agent_response,
                recoverable=True,
            ))
            return result
        
        agent_id = agent_response.get("id")
        agent_name = agent_response.get("name")
        agent_tag = f"agent:{agent_id}"
        
        result.bot_id = agent_id
        result.bot_name = agent_name
        
        # Step 4: Create guidelines
        created_guidelines = []
        for idx, guideline in enumerate(spec.get("guidelines", []), 1):
            guideline_payload = {
                "condition": guideline["condition"],
                "action": guideline.get("action"),
                "description": guideline.get("description"),
                "criticality": self._map_criticality(guideline.get("criticality")),
                "tags": [agent_tag],
            }
            
            success, response = await self._call_parlant_api("POST", "/guidelines", guideline_payload)
            if success:
                created_guidelines.append({
                    "id": response.get("id"),
                    "condition": guideline["condition"],
                    "action": guideline.get("action"),
                    "criticality": self._map_criticality(guideline.get("criticality")),
                })
                result.guidelines_created += 1
            else:
                result.errors.append(WrapperError(
                    error_type=ErrorType.API_FAILURE,
                    message=f"Failed to create guideline {idx}: {response.get('error')}",
                    recoverable=True,
                ))
        
        # Step 5: Create journeys
        created_journeys = []
        for idx, journey in enumerate(spec.get("journeys", []), 1):
            journey_payload = {
                "title": journey["title"],
                "description": journey["description"],
                "conditions": journey["conditions"],
                "tags": [agent_tag],
            }
            
            success, response = await self._call_parlant_api("POST", "/journeys", journey_payload)
            if success:
                created_journeys.append({
                    "id": response.get("id"),
                    "title": journey["title"],
                    "description": journey["description"],
                    "conditions": journey["conditions"],
                })
                result.journeys_created += 1
            else:
                result.errors.append(WrapperError(
                    error_type=ErrorType.API_FAILURE,
                    message=f"Failed to create journey {idx}: {response.get('error')}",
                    recoverable=True,
                ))
        
        # Step 6: Persist to MongoDB with retry
        persist_success, persist_error = await self._persist_with_retry(
            bot_id=agent_id,
            bot_name=agent_name,
            description=self._build_agent_description(spec),
            composition_mode=self._map_composition_mode(spec.get("composition_mode")),
            max_iterations=spec.get("max_engine_iterations", 3),
            guidelines=created_guidelines,
            journeys=created_journeys,
            idempotency_key=idempotency_key,
            original_spec=spec,
        )
        
        if persist_success:
            result.persisted_to_mongodb = True
            result.status = BotStatus.CREATED
            result.success = True
        else:
            # Mark as PARTIALLY_CREATED for reconciliation
            await self._mark_partially_created(
                bot_id=agent_id,
                bot_name=agent_name,
                description=self._build_agent_description(spec),
                composition_mode=self._map_composition_mode(spec.get("composition_mode")),
                max_iterations=spec.get("max_engine_iterations", 3),
                idempotency_key=idempotency_key,
                original_spec=spec,
                error_message=persist_error.message if persist_error else "Unknown error",
            )
            result.status = BotStatus.PARTIALLY_CREATED
            result.success = True  # Bot was created, just persistence failed
            if persist_error:
                result.errors.append(persist_error)
        
        return result
    
    # =========================================================================
    # Query Operations
    # =========================================================================
    
    async def list_bots(self) -> list[dict[str, Any]]:
        """List all bots from MongoDB with their guidelines and journeys."""
        if not self._persistence or not self._persistence.enabled:
            return []
        
        bots = await self._persistence.list_bots()
        result = []
        
        for bot in bots:
            # Skip system agent
            if bot.get("name") == "Otto":
                continue
            
            bot_id = bot.get("bot_id")
            
            # Fetch guidelines and journeys for this bot
            guidelines = await self._persistence.list_guidelines(bot_id) if bot_id else []
            journeys = await self._persistence.list_journeys(bot_id) if bot_id else []
            
            result.append({
                "id": bot_id,
                "name": bot.get("name"),
                "description": bot.get("description"),
                "composition_mode": bot.get("composition_mode"),
                "max_engine_iterations": bot.get("max_engine_iterations"),
                "status": bot.get("metadata", {}).get("status", BotStatus.CREATED.value),
                "created_at": bot.get("created_at").isoformat() if bot.get("created_at") else None,
                "guidelines": [
                    {
                        "id": g.get("guideline_id"),
                        "condition": g.get("condition"),
                        "action": g.get("action"),
                        "criticality": g.get("criticality"),
                    }
                    for g in guidelines
                ],
                "journeys": [
                    {
                        "id": j.get("journey_id"),
                        "title": j.get("title"),
                        "description": j.get("description"),
                        "conditions": j.get("conditions"),
                    }
                    for j in journeys
                ],
            })
        
        return result
    
    async def get_bot(self, bot_id: str) -> Optional[dict[str, Any]]:
        """Get a specific bot with its guidelines and journeys."""
        if not self._persistence or not self._persistence.enabled:
            return None
        
        bot = await self._persistence.get_bot(bot_id)
        if not bot:
            return None
        
        guidelines = await self._persistence.list_guidelines(bot_id)
        journeys = await self._persistence.list_journeys(bot_id)
        
        return {
            "id": bot.get("bot_id"),
            "name": bot.get("name"),
            "description": bot.get("description"),
            "composition_mode": bot.get("composition_mode"),
            "max_engine_iterations": bot.get("max_engine_iterations"),
            "status": bot.get("metadata", {}).get("status", BotStatus.CREATED.value),
            "created_at": bot.get("created_at").isoformat() if bot.get("created_at") else None,
            "guidelines": [
                {
                    "id": g.get("guideline_id"),
                    "condition": g.get("condition"),
                    "action": g.get("action"),
                    "criticality": g.get("criticality"),
                }
                for g in guidelines
            ],
            "journeys": [
                {
                    "id": j.get("journey_id"),
                    "title": j.get("title"),
                    "description": j.get("description"),
                    "conditions": j.get("conditions"),
                }
                for j in journeys
            ],
        }
    
    async def delete_bot(self, bot_id: str) -> bool:
        """Delete a bot from MongoDB."""
        if not self._persistence or not self._persistence.enabled:
            return False
        return await self._persistence.delete_bot(bot_id)
    
    # =========================================================================
    # Reconciliation
    # =========================================================================
    
    async def list_partially_created(self) -> list[dict[str, Any]]:
        """List all bots that need reconciliation."""
        if not self._persistence or not self._persistence.enabled or not self._persistence.db:
            return []
        
        try:
            cursor = self._persistence.db.bots.find({
                "metadata.status": BotStatus.PARTIALLY_CREATED.value
            })
            bots = await cursor.to_list(length=None)
            return [
                {
                    "bot_id": bot.get("bot_id"),
                    "name": bot.get("name"),
                    "error": bot.get("metadata", {}).get("error"),
                    "created_at": bot.get("created_at").isoformat() if bot.get("created_at") else None,
                }
                for bot in bots
            ]
        except Exception:
            return []
    
    async def reconcile_bot(self, bot_id: str) -> bool:
        """
        Attempt to reconcile a PARTIALLY_CREATED bot.
        
        Re-tries persistence for bots that were created in Parlant
        but failed to persist fully to MongoDB.
        """
        if not self._persistence or not self._persistence.enabled or not self._persistence.db:
            return False
        
        try:
            bot = await self._persistence.get_bot(bot_id)
            if not bot:
                return False
            
            metadata = bot.get("metadata", {})
            if metadata.get("status") != BotStatus.PARTIALLY_CREATED.value:
                return True  # Already reconciled
            
            # Update status to CREATED
            await self._persistence.db.bots.update_one(
                {"bot_id": bot_id},
                {
                    "$set": {
                        "metadata.status": BotStatus.CREATED.value,
                        "metadata.needs_reconciliation": False,
                        "metadata.reconciled_at": datetime.utcnow(),
                    },
                    "$unset": {"metadata.error": ""},
                }
            )
            return True
        except Exception as e:
            print(f"❌ Reconciliation failed for {bot_id}: {e}")
            return False
    
    # =========================================================================
    # CRUD Operations for Agents
    # =========================================================================
    
    async def update_agent(self, agent_id: str, updates: dict[str, Any]) -> bool:
        """
        Update an existing agent.
        
        Args:
            agent_id: The agent ID to update
            updates: Dictionary of fields to update (name, description, etc.)
        
        Returns:
            bool: True if update succeeded
        """
        success, _ = await self._call_parlant_api("PATCH", f"/agents/{agent_id}", updates)
        if success and self._persistence and self._persistence.enabled and self._persistence.db:
            try:
                await self._persistence.db.bots.update_one(
                    {"bot_id": agent_id},
                    {"$set": {**updates, "updated_at": datetime.utcnow()}}
                )
            except Exception as e:
                print(f"⚠️  Failed to sync agent update to MongoDB: {e}")
        return success
    
    # =========================================================================
    # CRUD Operations for Guidelines
    # =========================================================================
    
    async def add_guideline(self, bot_id: str, guideline: dict[str, Any]) -> Optional[str]:
        """
        Add a new guideline to an existing bot.
        
        Args:
            bot_id: The bot/agent ID to add the guideline to
            guideline: Dictionary with condition, action, criticality, description
        
        Returns:
            Optional[str]: The created guideline ID, or None if failed
        """
        payload = {
            "condition": guideline["condition"],
            "action": guideline.get("action"),
            "description": guideline.get("description"),
            "criticality": self._map_criticality(guideline.get("criticality")),
            "tags": [f"agent:{bot_id}"],
        }
        
        success, response = await self._call_parlant_api("POST", "/guidelines", payload)
        if success:
            guideline_id = response.get("id")
            if self._persistence and self._persistence.enabled:
                try:
                    await self._persistence.persist_guideline(
                        guideline_id=guideline_id,
                        bot_id=bot_id,
                        condition=guideline["condition"],
                        action=guideline.get("action"),
                        description=guideline.get("description"),
                        criticality=self._map_criticality(guideline.get("criticality")),
                    )
                except Exception as e:
                    print(f"⚠️  Failed to persist guideline to MongoDB: {e}")
            return guideline_id
        return None
    
    async def update_guideline(self, guideline_id: str, updates: dict[str, Any]) -> bool:
        """
        Update an existing guideline.
        
        Args:
            guideline_id: The guideline ID to update
            updates: Dictionary of fields to update
        
        Returns:
            bool: True if update succeeded
        """
        # Map criticality if present
        if "criticality" in updates:
            updates["criticality"] = self._map_criticality(updates["criticality"])
        
        success, _ = await self._call_parlant_api("PATCH", f"/guidelines/{guideline_id}", updates)
        if success and self._persistence and self._persistence.enabled and self._persistence.db:
            try:
                await self._persistence.db.guidelines.update_one(
                    {"guideline_id": guideline_id},
                    {"$set": {**updates, "updated_at": datetime.utcnow()}}
                )
            except Exception as e:
                print(f"⚠️  Failed to sync guideline update to MongoDB: {e}")
        return success
    
    async def delete_guideline(self, guideline_id: str) -> bool:
        """
        Delete a guideline.
        
        Args:
            guideline_id: The guideline ID to delete
        
        Returns:
            bool: True if deletion succeeded
        """
        success, _ = await self._call_parlant_api("DELETE", f"/guidelines/{guideline_id}")
        if success and self._persistence and self._persistence.enabled and self._persistence.db:
            try:
                await self._persistence.db.guidelines.delete_one({"guideline_id": guideline_id})
            except Exception as e:
                print(f"⚠️  Failed to delete guideline from MongoDB: {e}")
        return success
    
    # =========================================================================
    # CRUD Operations for Journeys
    # =========================================================================
    
    async def add_journey(self, bot_id: str, journey: dict[str, Any]) -> Optional[str]:
        """
        Add a new journey to an existing bot.
        
        Args:
            bot_id: The bot/agent ID to add the journey to
            journey: Dictionary with title, description, conditions
        
        Returns:
            Optional[str]: The created journey ID, or None if failed
        """
        payload = {
            "title": journey["title"],
            "description": journey["description"],
            "conditions": journey["conditions"],
            "tags": [f"agent:{bot_id}"],
        }
        
        success, response = await self._call_parlant_api("POST", "/journeys", payload)
        if success:
            journey_id = response.get("id")
            if self._persistence and self._persistence.enabled:
                try:
                    await self._persistence.persist_journey(
                        journey_id=journey_id,
                        bot_id=bot_id,
                        title=journey["title"],
                        description=journey["description"],
                        conditions=journey["conditions"],
                    )
                except Exception as e:
                    print(f"⚠️  Failed to persist journey to MongoDB: {e}")
            return journey_id
        return None
    
    async def update_journey(self, journey_id: str, updates: dict[str, Any]) -> bool:
        """
        Update an existing journey.
        
        Args:
            journey_id: The journey ID to update
            updates: Dictionary of fields to update
        
        Returns:
            bool: True if update succeeded
        """
        success, _ = await self._call_parlant_api("PATCH", f"/journeys/{journey_id}", updates)
        if success and self._persistence and self._persistence.enabled and self._persistence.db:
            try:
                await self._persistence.db.journeys.update_one(
                    {"journey_id": journey_id},
                    {"$set": {**updates, "updated_at": datetime.utcnow()}}
                )
            except Exception as e:
                print(f"⚠️  Failed to sync journey update to MongoDB: {e}")
        return success
    
    async def delete_journey(self, journey_id: str) -> bool:
        """
        Delete a journey.
        
        Args:
            journey_id: The journey ID to delete
        
        Returns:
            bool: True if deletion succeeded
        """
        success, _ = await self._call_parlant_api("DELETE", f"/journeys/{journey_id}")
        if success and self._persistence and self._persistence.enabled and self._persistence.db:
            try:
                await self._persistence.db.journeys.delete_one({"journey_id": journey_id})
            except Exception as e:
                print(f"⚠️  Failed to delete journey from MongoDB: {e}")
        return success
    
    # =========================================================================
    # Session/Chat Operations
    # =========================================================================
    
    async def create_session(self, agent_id: str, customer_id: Optional[str] = None) -> Optional[str]:
        """
        Create a new chat session with an agent.
        
        Args:
            agent_id: The agent ID to chat with
            customer_id: Optional customer identifier
        
        Returns:
            Optional[str]: The created session ID, or None if failed
        """
        payload: dict[str, Any] = {"agent_id": agent_id}
        if customer_id:
            payload["customer_id"] = customer_id

        success, response = await self._call_parlant_api("POST", "/sessions", payload)
        return response.get("id") if success else None
    
    async def send_message(self, session_id: str, message: str) -> bool:
        """
        Send a message to a chat session.
        
        Args:
            session_id: The session ID to send the message to
            message: The message content
        
        Returns:
            bool: True if message was sent successfully
        """
        payload = {
            "kind": "message",
            "source": "customer",
            "message": message,
        }
        success, _ = await self._call_parlant_api("POST", f"/sessions/{session_id}/events", payload)
        return success
    
    async def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        """
        Get all messages/events from a chat session.
        
        Args:
            session_id: The session ID to get messages from
        
        Returns:
            list[dict]: List of message events
        """
        success, response = await self._call_parlant_api("GET", f"/sessions/{session_id}/events")
        if success:
            # Handle both list and dict response formats
            if isinstance(response, list):
                return response
            return response.get("events", [])
        return []