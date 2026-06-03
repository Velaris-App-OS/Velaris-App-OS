"""
Temporal Activities
====================

These are the ``@activity.defn`` functions that Temporal workers execute.
Activities are where all non-deterministic work happens:
  - HTTP calls to external services
  - Database reads/writes
  - AI model inference
  - Form rendering and submission
  - Script execution

Each activity receives an ``ActivityInput`` dict and returns an
``ActivityOutput`` dict.  Temporal handles serialisation, retries,
timeouts, and heartbeats automatically.

Activity ↔ BPMN task mapping::

    helix.task.service       →  execute_service_task
    helix.task.user          →  execute_user_task
    helix.task.script        →  execute_script_task
    helix.task.send          →  execute_send_task
    helix.task.receive       →  execute_receive_task
    helix.task.business_rule →  execute_business_rule_task
    helix.task.generic       →  execute_generic_task

Adding a new activity:
  1. Write the function with @activity.defn
  2. Add it to ACTIVITY_LIST at the bottom
  3. The worker auto-registers all activities in that list
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import structlog
from temporalio import activity

logger = structlog.get_logger()


# ═══════════════════════════════════════════════════════════════════════
#  Activity I/O — serialised over the wire by Temporal
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class ActivityInput:
    """
    Input payload sent to every activity.

    Temporal serialises this as JSON.  Keep it flat and simple —
    no IR model objects, just primitive types and dicts.
    """
    task_id: str
    task_type: str                       # "helix.task.service", etc.
    task_name: str | None = None
    implementation: str | None = None    # Service URI
    form_key: str | None = None          # User task form ref
    script_body: str | None = None       # Script content
    script_language: str | None = None   # "python", "javascript"
    decision_ref: str | None = None      # Business rule ref
    message_ref: str | None = None
    variables: dict[str, Any] = field(default_factory=dict)
    extensions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ActivityInput:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ActivityOutput:
    """Output payload returned from every activity."""
    variables: dict[str, Any] = field(default_factory=dict)
    resolved_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ═══════════════════════════════════════════════════════════════════════
#  Activity implementations
# ═══════════════════════════════════════════════════════════════════════


@activity.defn(name="helix.task.service")
async def execute_service_task(input_data: dict[str, Any]) -> dict[str, Any]:
    """
    Execute a service task.

    Uses the HttpTaskResolver for HTTP/helix:// URIs.
    Falls back to pass-through if no resolver handles the task.
    """
    inp = ActivityInput.from_dict(input_data)

    activity.logger.info(
        "Executing service task: %s (impl: %s)",
        inp.task_name, inp.implementation,
    )

    # Try the HTTP resolver for HTTP and helix:// URIs
    if inp.implementation and (
        inp.implementation.startswith("http://")
        or inp.implementation.startswith("https://")
        or inp.implementation.startswith("helix://")
    ):
        from helix_engine.plugins.http_resolver import HttpTaskResolver
        from helix_ir.models.process import ServiceTask

        # Reconstruct a minimal ServiceTask for the resolver
        task = ServiceTask(
            id=inp.task_id,
            name=inp.task_name,
            implementation=inp.implementation,
            extensions=inp.extensions,
        )

        resolver = HttpTaskResolver(
            service_registry=_get_service_registry(),
        )
        try:
            result_vars = await resolver.resolve(task, inp.variables)
            # Merge result into existing variables
            merged = dict(inp.variables)
            merged.update(result_vars)
            return ActivityOutput(
                variables=merged,
                resolved_by="http_resolver",
            ).to_dict()
        except Exception as e:
            activity.logger.error("HTTP resolver failed: %s", str(e))
            # Fall through to pass-through
        finally:
            await resolver.close()

    # Pass-through: no resolver handled the task
    return ActivityOutput(
        variables=inp.variables,
        resolved_by="passthrough",
    ).to_dict()


def _get_service_registry() -> dict[str, str]:
    """
    Build the service registry from environment or config.

    Maps helix service names to base URLs.  In production this would
    come from service discovery or helix.yaml configuration.

    Override with HELIX_SERVICE_REGISTRY env var (JSON) or extend this function.
    """
    import json
    import os

    # Check env var first
    registry_json = os.environ.get("HELIX_SERVICE_REGISTRY")
    if registry_json:
        try:
            return json.loads(registry_json)
        except json.JSONDecodeError:
            pass

    # Default development registry
    return {
        # Add your services here, e.g.:
        # "order-service": "http://localhost:3001",
        # "notification-service": "http://localhost:3002",
    }


@activity.defn(name="helix.task.user")
async def execute_user_task(input_data: dict[str, Any]) -> dict[str, Any]:
    """
    Execute a user task.

    In production, this will:
      1. Create a task in the task/form service.
      2. The workflow will use a signal to wait for form submission.
      3. This activity just creates the task record.

    For now: logs and returns.
    """
    inp = ActivityInput.from_dict(input_data)

    activity.logger.info(
        "Executing user task: %s (form: %s)",
        inp.task_name, inp.form_key,
    )

    # TODO: Create task assignment in form-service
    # TODO: Workflow will wait for signal "user_task_completed_{task_id}"

    return ActivityOutput(
        variables=inp.variables,
        resolved_by="passthrough",
    ).to_dict()


_ALLOWED_SCRIPT_LANGUAGES = frozenset({"python", "javascript", "groovy"})
_MAX_SCRIPT_BYTES  = 65_536   # 64 KB hard limit (matches compiler validator)
_WARN_SCRIPT_BYTES = 16_384   # 16 KB — logged as a size warning


@activity.defn(name="helix.task.script")
async def execute_script_task(input_data: dict[str, Any]) -> dict[str, Any]:
    """
    Execute an inline script task.

    Script execution is not yet implemented — this activity validates
    the language and size limits enforced at deploy time by the compiler,
    then returns current variables unchanged.

    ╔══════════════════════════════════════════════════════════════════╗
    ║  SECURITY REQUIREMENTS — must be upheld when execution ships     ║
    ╠══════════════════════════════════════════════════════════════════╣
    ║  1. Run in an isolated subprocess — NEVER exec()/eval() here.   ║
    ║  2. os.environ must be EMPTY in the child process — no API keys, ║
    ║     no DATABASE_URL, no ANTHROPIC_API_KEY, nothing.             ║
    ║  3. No filesystem access — mount only a read-only /tmp scratch.  ║
    ║  4. No network from within the script — block all sockets.       ║
    ║  5. Hard CPU time limit (e.g. 10 s) and memory cap (e.g. 128 MB)║
    ║  6. Use bwrap/firejail + seccomp on Linux, or a gVisor sandbox.  ║
    ║  7. Only the declared input variables are injected, not the full  ║
    ║     process context. Strip any key containing 'secret', 'key',   ║
    ║     'password', 'token', 'credential' before passing.            ║
    ╚══════════════════════════════════════════════════════════════════╝
    """
    inp = ActivityInput.from_dict(input_data)

    lang = (inp.script_language or "").strip().lower()
    if lang not in _ALLOWED_SCRIPT_LANGUAGES:
        raise ValueError(
            f"ScriptTask '{inp.task_name}': language '{inp.script_language}' is not allowed"
        )

    script_bytes = len((inp.script_body or "").encode("utf-8"))
    if script_bytes > _MAX_SCRIPT_BYTES:
        raise ValueError(
            f"ScriptTask '{inp.task_name}': script exceeds 64 KB hard limit ({script_bytes} bytes)"
        )

    if script_bytes > _WARN_SCRIPT_BYTES:
        activity.logger.warning(
            "Script task exceeds 16 KB soft limit — consider moving logic to a ServiceTask: "
            "%s (%d bytes)",
            inp.task_name, script_bytes,
        )

    activity.logger.info(
        "Script task received (execution not yet implemented): %s (lang: %s, %d bytes)",
        inp.task_name, inp.script_language, script_bytes,
    )

    return ActivityOutput(
        variables=inp.variables,
        resolved_by="passthrough",
    ).to_dict()


@activity.defn(name="helix.task.send")
async def execute_send_task(input_data: dict[str, Any]) -> dict[str, Any]:
    """Execute a send task (dispatch a message/notification)."""
    inp = ActivityInput.from_dict(input_data)

    activity.logger.info(
        "Executing send task: %s (impl: %s, msg: %s)",
        inp.task_name, inp.implementation, inp.message_ref,
    )

    # TODO: Dispatch via NotificationChannel protocol

    return ActivityOutput(
        variables=inp.variables,
        resolved_by="passthrough",
    ).to_dict()


@activity.defn(name="helix.task.receive")
async def execute_receive_task(input_data: dict[str, Any]) -> dict[str, Any]:
    """
    Execute a receive task (wait for external message).

    Similar to user tasks — the workflow uses a signal to wait.
    This activity sets up the message correlation.
    """
    inp = ActivityInput.from_dict(input_data)

    activity.logger.info(
        "Executing receive task: %s (msg: %s)",
        inp.task_name, inp.message_ref,
    )

    # TODO: Register message correlation in event bus

    return ActivityOutput(
        variables=inp.variables,
        resolved_by="passthrough",
    ).to_dict()


@activity.defn(name="helix.task.business_rule")
async def execute_business_rule_task(input_data: dict[str, Any]) -> dict[str, Any]:
    """Execute a business rule task (evaluate a stored business rule).

    Calls POST {CASE_SERVICE_URL}/api/v1/rules/{decision_ref}/evaluate with the
    current workflow variables as context.  Requires HELIX_SERVICE_TOKEN (a valid
    JWT minted against the same AUTH_SECRET as case-service) and CASE_SERVICE_URL
    (default http://localhost:8200).

    Outcome variables from decision_table rules and set_value actions are merged
    back into the workflow variables.

    To wire this up:
      1. Generate a long-lived service JWT on case-service startup using
         ``create_dev_token(user_id="helix-engine", roles=["superadmin"])``
         and expose it as HELIX_SERVICE_TOKEN in the engine's environment.
      2. Set CASE_SERVICE_URL if case-service is not on localhost:8200.
    """
    import os

    import httpx

    inp = ActivityInput.from_dict(input_data)

    activity.logger.info(
        "Executing business rule task: %s (decision: %s)",
        inp.task_name, inp.decision_ref,
    )

    if not inp.decision_ref:
        return ActivityOutput(variables=inp.variables, resolved_by="passthrough").to_dict()

    service_token = os.environ.get("HELIX_SERVICE_TOKEN", "")
    if not service_token:
        activity.logger.warning(
            "HELIX_SERVICE_TOKEN not set — business rule task '%s' running as passthrough",
            inp.task_name,
        )
        return ActivityOutput(variables=inp.variables, resolved_by="passthrough").to_dict()

    case_service_url = os.environ.get("CASE_SERVICE_URL", "http://localhost:8200")
    url = f"{case_service_url}/api/v1/rules/{inp.decision_ref}/evaluate"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                json={"context": inp.variables},
                headers={"Authorization": f"Bearer {service_token}"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        activity.logger.error("Business rule evaluation failed for '%s': %s", inp.decision_ref, exc)
        return ActivityOutput(variables=inp.variables, resolved_by="passthrough").to_dict()

    result = data.get("result", {})
    merged = dict(inp.variables)

    # Merge decision_table outcomes
    for key, value in result.get("outcomes", {}).items():
        merged[key] = value

    # Apply set_value action results
    for action in result.get("action_results") or []:
        if action.get("action") == "set_value" and action.get("target"):
            merged[action["target"]] = action.get("value")

    # Merge expression result
    if result.get("result_field_path") and result.get("result") is not None:
        merged[result["result_field_path"]] = result["result"]

    activity.logger.info(
        "Business rule '%s' evaluated: matched=%s", inp.decision_ref, result.get("matched")
    )
    return ActivityOutput(variables=merged, resolved_by="business_rule").to_dict()


@activity.defn(name="helix.task.generic")
async def execute_generic_task(input_data: dict[str, Any]) -> dict[str, Any]:
    """Execute a generic task (no specific type)."""
    inp = ActivityInput.from_dict(input_data)

    activity.logger.info("Executing generic task: %s", inp.task_name)

    return ActivityOutput(
        variables=inp.variables,
        resolved_by="passthrough",
    ).to_dict()


# ═══════════════════════════════════════════════════════════════════════
#  Activity registry — the worker registers all of these
# ═══════════════════════════════════════════════════════════════════════

ACTIVITY_LIST = [
    execute_service_task,
    execute_user_task,
    execute_script_task,
    execute_send_task,
    execute_receive_task,
    execute_business_rule_task,
    execute_generic_task,
]
"""All activity functions.  The worker registers these automatically."""
