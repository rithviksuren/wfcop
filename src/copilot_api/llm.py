from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from copy import deepcopy
from typing import Any, Callable

from .catalog import NODE_CATALOG, catalog_for_prompt
from .models import Workflow, WorkflowEdge, WorkflowNode
from .validation import repair_workflow, validate_workflow


logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLMAuthenticationError(LLMError):
    pass


class LLMRateLimitError(LLMError):
    pass


class LLMTimeoutError(LLMError):
    pass


class LLMResponseError(LLMError):
    pass


class LLMProvider(ABC):
    name: str

    def bind_repository(self, repository: Any) -> None:
        """Optionally provide read-only application data to provider tools."""
        return None

    @abstractmethod
    def generate(self, task: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class OpenAIProvider(LLMProvider):
    name = "openai"

    API_URL = "https://api.openai.com/v1/responses"
    TRANSIENT_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-5.5",
        *,
        timeout_seconds: float = 45,
        max_retries: int = 2,
        max_tool_rounds: int = 2,
        reasoning_effort: str = "low",
        transport: Callable[[dict[str, Any]], tuple[dict[str, Any], dict[str, str]]] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        repository: Any | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.max_tool_rounds = max_tool_rounds
        self.reasoning_effort = reasoning_effort
        self.transport = transport
        self.sleeper = sleeper
        self.repository = repository
        self.last_request_id: str | None = None
        self.last_response_id: str | None = None
        self.last_usage: dict[str, Any] = {}
        self.last_tool_calls: list[str] = []

    def generate(self, task: str, payload: dict[str, Any]) -> dict[str, Any]:
        prompt_payload = deepcopy(payload)
        tool_context = prompt_payload.pop("_tool_context", {})
        request_payload = self._base_request(task, prompt_payload)
        response = self._request_with_retries(request_payload)
        tool_rounds = 0

        while True:
            self._record_response(response)
            tool_calls = [
                item
                for item in response.get("output", [])
                if item.get("type") == "function_call"
            ]
            if not tool_calls:
                return self._parse_structured_response(response)
            if tool_rounds >= self.max_tool_rounds:
                raise LLMResponseError(
                    "OpenAI exceeded the allowed workflow-planning tool rounds."
                )

            outputs = []
            for call in tool_calls:
                name = str(call.get("name", ""))
                self.last_tool_calls.append(name)
                try:
                    arguments = json.loads(call.get("arguments") or "{}")
                except json.JSONDecodeError as exc:
                    raise LLMResponseError(
                        f"OpenAI returned invalid arguments for tool {name}."
                    ) from exc
                result = self._execute_tool(
                    name,
                    arguments,
                    tool_context=tool_context,
                )
                outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.get("call_id"),
                        "output": json.dumps(result, default=str),
                    }
                )

            request_payload = {
                **self._base_request(task, prompt_payload),
                "previous_response_id": response.get("id"),
                "input": outputs,
            }
            response = self._request_with_retries(request_payload)
            tool_rounds += 1

    def bind_repository(self, repository: Any) -> None:
        self.repository = repository

    def _base_request(
        self,
        task: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "model": self.model,
            "instructions": self._instructions(task),
            "input": json.dumps(
                {
                    "task": task,
                    "request": payload,
                },
                default=str,
            ),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "workflow_copilot_result",
                    "strict": True,
                    "schema": self._result_schema(),
                },
                "verbosity": "low",
            },
            "reasoning": {"effort": self.reasoning_effort},
            "tools": self._tools(),
            "tool_choice": "auto",
            "max_output_tokens": 5000,
            "store": False,
        }

    def _instructions(self, task: str) -> str:
        return (
            "You are FlowMind's workflow planning engine. "
            f"Complete the {task} operation and return the exact structured result. "
            "Success means every explicit user requirement is represented, unrelated "
            "steps are preserved for modify/fix operations, node configuration is "
            "specific, and the graph is executable. Never invent extra actions. "
            "Use search_nodes when node capabilities or defaults are uncertain. "
            "Use get_workflow when a persisted workflow is referenced by id or prior "
            "state is needed. "
            "Use validate_workflow before finalizing when the candidate graph may be "
            "invalid. Email text requirements need both Gmail search_text and a matching "
            "filter_condition. Interpret category wording such as 'job-related emails' "
            "as email text containing 'job'; do not use the whole category phrase as the "
            "literal search value. Preserve a multiword literal only when the user quotes "
            "it or explicitly calls it a phrase. Scheduled language must set mode, trigger_schedule, and "
            "active status. Prefer event fields such as {{subject}}, {{from}}, and "
            "{{body}} in action templates. Do not include secrets or credentials. "
            "If validation feedback is supplied, repair every listed error."
        )

    def _request_with_retries(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            try:
                body, headers = (
                    self.transport(payload)
                    if self.transport
                    else self._http_post(payload)
                )
                self.last_request_id = (
                    headers.get("x-request-id")
                    or headers.get("X-Request-Id")
                    or self.last_request_id
                )
                return body
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    logger.warning("OpenAI authentication failed.")
                    raise LLMAuthenticationError(
                        "OpenAI authentication failed. Check OPENAI_API_KEY."
                    ) from exc
                if exc.code in self.TRANSIENT_STATUS_CODES and attempt < self.max_retries:
                    logger.warning(
                        "Retrying transient OpenAI HTTP %s (attempt %s).",
                        exc.code,
                        attempt + 1,
                    )
                    self.sleeper(self._retry_delay(attempt, exc.headers))
                    continue
                if exc.code == 429:
                    raise LLMRateLimitError(
                        "OpenAI is rate limited or out of quota."
                    ) from exc
                raise LLMError(
                    f"OpenAI request failed with HTTP {exc.code}."
                ) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < self.max_retries:
                    logger.warning(
                        "Retrying OpenAI transport failure (attempt %s).",
                        attempt + 1,
                    )
                    self.sleeper(self._retry_delay(attempt))
                    continue
                raise LLMTimeoutError(
                    "OpenAI could not be reached before the retry limit."
                ) from exc
            except json.JSONDecodeError as exc:
                raise LLMResponseError(
                    "OpenAI returned a response that was not valid JSON."
                ) from exc
        raise LLMError("OpenAI request failed.")

    def _http_post(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        request = urllib.request.Request(
            self.API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(
            request,
            timeout=self.timeout_seconds,
        ) as response:
            body = json.loads(response.read().decode("utf-8"))
            return body, dict(response.headers.items())

    def _parse_structured_response(
        self,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        if body.get("error"):
            error = body["error"]
            raise LLMResponseError(
                f"OpenAI response failed: {error.get('message', 'unknown error')}"
            )
        if body.get("status") == "incomplete":
            reason = (body.get("incomplete_details") or {}).get("reason", "unknown")
            raise LLMResponseError(f"OpenAI response was incomplete: {reason}.")

        output_text: str | None = body.get("output_text")
        refusal: str | None = None
        for item in body.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    output_text = content.get("text")
                elif content.get("type") == "refusal":
                    refusal = content.get("refusal")
        if refusal:
            raise LLMResponseError(f"OpenAI refused the workflow request: {refusal}")
        if not output_text:
            raise LLMResponseError(
                "OpenAI returned no structured workflow output."
            )
        try:
            parsed = json.loads(output_text)
            parsed["workflow"] = self._normalize_workflow(
                parsed["workflow"]
            )
            Workflow.model_validate(parsed["workflow"])
        except (KeyError, json.JSONDecodeError, ValueError) as exc:
            raise LLMResponseError(
                "OpenAI returned structured output that did not match the workflow contract."
            ) from exc
        parsed["provider"] = self.name
        return parsed

    def _execute_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        tool_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if name == "search_nodes":
            return self._search_nodes(
                str(arguments.get("query", "")),
                arguments.get("roles") or [],
                int(arguments.get("limit", 5)),
            )
        if name == "validate_workflow":
            try:
                workflow = Workflow.model_validate(
                    self._normalize_workflow(arguments["workflow"])
                )
            except (KeyError, ValueError) as exc:
                return {
                    "valid": False,
                    "errors": [
                        {
                            "code": "schema_error",
                            "message": str(exc),
                        }
                    ],
                }
            return validate_workflow(workflow).model_dump()
        if name == "get_workflow":
            return self._get_workflow(
                str(arguments.get("workflow_id", "")),
                tool_context or {},
            )
        raise LLMResponseError(f"OpenAI requested unsupported tool {name}.")

    def _tools(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": "search_nodes",
                "description": (
                    "Search supported workflow nodes by capability, app, action, or "
                    "trigger. Returns required configuration, role, and safe defaults."
                ),
                "strict": True,
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Capability query such as 'email trigger', "
                                "'send Teams notification', or 'create task'."
                            ),
                        },
                        "roles": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["trigger", "condition", "action"],
                            },
                            "description": "Optional roles to include.",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 20,
                        },
                    },
                    "required": ["query", "roles", "limit"],
                },
            },
            {
                "type": "function",
                "name": "validate_workflow",
                "description": (
                    "Validate a candidate workflow for required configuration and graph "
                    "integrity before returning the final result."
                ),
                "strict": True,
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "workflow": self._workflow_schema(),
                    },
                    "required": ["workflow"],
                },
            },
            {
                "type": "function",
                "name": "get_workflow",
                "description": (
                    "Retrieve a persisted workflow by id when the current user is "
                    "allowed to access it. Returns the graph without credentials."
                ),
                "strict": True,
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "workflow_id": {
                            "type": "string",
                            "description": "Persisted workflow id.",
                        }
                    },
                    "required": ["workflow_id"],
                },
            },
        ]

    def _search_nodes(
        self,
        query: str,
        roles: list[str],
        limit: int,
    ) -> dict[str, Any]:
        query_tokens = set(re.findall(r"[a-z0-9]+", query.lower()))
        requested_roles = set(roles)
        ranked = []
        for item in catalog_for_prompt():
            if requested_roles and item["role"] not in requested_roles:
                continue
            text = " ".join(
                [
                    item["type"].replace("_", " "),
                    item["description"],
                    item["role"],
                    *item["required_config"],
                    *item["defaults"].keys(),
                ]
            ).lower()
            tokens = set(re.findall(r"[a-z0-9]+", text))
            overlap = len(query_tokens & tokens)
            phrase_bonus = 2 if query.lower() in text and query else 0
            ranked.append((overlap + phrase_bonus, item["type"], item))
        ranked.sort(key=lambda entry: (-entry[0], entry[1]))
        matched = [
            item
            for score, _, item in ranked
            if score > 0 or not query_tokens
        ][: max(1, min(limit, 20))]
        return {"query": query, "nodes": matched}

    def _get_workflow(
        self,
        workflow_id: str,
        tool_context: dict[str, Any],
    ) -> dict[str, Any]:
        if self.repository is None:
            return {
                "found": False,
                "error": "Workflow repository is unavailable.",
            }
        user_id = tool_context.get("user_id")
        if not user_id:
            return {
                "found": False,
                "error": "Authenticated workflow context is required.",
            }
        workflow = self.repository.get(workflow_id)
        if workflow is None:
            return {"found": False}
        member = self.repository.get_member(user_id)
        permission = self.repository.permission_for_user(
            workflow,
            user_id,
            member.role if member else None,
        )
        if permission is None:
            return {"found": False}
        payload = workflow.model_dump(mode="json", by_alias=True)
        for node in payload["nodes"]:
            node["config"] = self._sanitize_tool_config(node["config"])
        return {
            "found": True,
            "permission": permission,
            "workflow": payload,
        }

    def _sanitize_tool_config(
        self,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        sensitive_fragments = (
            "token",
            "secret",
            "password",
            "webhook_url",
            "service_account",
            "api_key",
            "access_key",
        )
        return {
            key: value
            for key, value in config.items()
            if not any(fragment in key.lower() for fragment in sensitive_fragments)
        }

    def _result_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "workflow": self._workflow_schema(),
                "explanation": {"type": "string"},
            },
            "required": ["workflow", "explanation"],
        }

    def _workflow_schema(self) -> dict[str, Any]:
        config_keys = sorted(
            {
                key
                for definition in NODE_CATALOG.values()
                for key in (
                    *definition.required_config,
                    *definition.defaults.keys(),
                )
            }
        )
        config_properties = {
            key: {
                "anyOf": [
                    {"type": "string"},
                    {"type": "integer"},
                    {"type": "number"},
                    {"type": "boolean"},
                    {"type": "null"},
                ]
            }
            for key in config_keys
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["draft", "active", "paused"],
                },
                "visibility": {
                    "type": "string",
                    "enum": ["private", "team", "restricted"],
                },
                "mode": {
                    "type": "string",
                    "enum": ["manual", "scheduled"],
                },
                "trigger_schedule": {
                    "anyOf": [{"type": "string"}, {"type": "null"}]
                },
                "nodes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "id": {"type": "string"},
                            "type": {
                                "type": "string",
                                "enum": sorted(NODE_CATALOG),
                            },
                            "role": {
                                "type": "string",
                                "enum": ["trigger", "condition", "action"],
                            },
                            "label": {"type": "string"},
                            "description": {"type": "string"},
                            "config": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": config_properties,
                                "required": config_keys,
                            },
                        },
                        "required": [
                            "id",
                            "type",
                            "role",
                            "label",
                            "description",
                            "config",
                        ],
                    },
                },
                "edges": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "from": {"type": "string"},
                            "to": {"type": "string"},
                        },
                        "required": ["from", "to"],
                    },
                },
            },
            "required": [
                "name",
                "status",
                "visibility",
                "mode",
                "trigger_schedule",
                "nodes",
                "edges",
            ],
        }

    def _normalize_workflow(
        self,
        workflow: dict[str, Any],
    ) -> dict[str, Any]:
        normalized = deepcopy(workflow)
        for node in normalized.get("nodes", []):
            config = node.get("config") or {}
            node["config"] = {
                key: value
                for key, value in config.items()
                if value is not None
            }
        return normalized

    def _record_response(self, response: dict[str, Any]) -> None:
        self.last_response_id = response.get("id")
        self.last_usage = response.get("usage") or {}
        logger.info(
            "OpenAI response completed response_id=%s request_id=%s total_tokens=%s",
            self.last_response_id,
            self.last_request_id,
            self.last_usage.get("total_tokens"),
        )

    def _retry_delay(
        self,
        attempt: int,
        headers: Any | None = None,
    ) -> float:
        if headers:
            retry_after = headers.get("retry-after")
            if retry_after:
                try:
                    return min(float(retry_after), 10.0)
                except ValueError:
                    pass
        return min(0.5 * (2**attempt), 4.0)


class FallbackProvider(LLMProvider):
    def __init__(self, primary: LLMProvider, fallback: LLMProvider) -> None:
        self.primary = primary
        self.fallback = fallback
        self.name = f"{primary.name}-with-{fallback.name}"
        self.last_error: str | None = None

    def bind_repository(self, repository: Any) -> None:
        self.primary.bind_repository(repository)
        self.fallback.bind_repository(repository)

    def generate(self, task: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            self.last_error = None
            result = self.primary.generate(task, payload)
            result.setdefault("provider", self.primary.name)
            return result
        except LLMError as exc:
            self.last_error = str(exc)
            result = self.fallback.generate(task, payload)
            explanation = result.get("explanation") or ""
            result["explanation"] = (
                f"{explanation}\n\nGenerated locally because the OpenAI provider was unavailable: {self.last_error}"
            ).strip()
            result["provider"] = self.fallback.name
            return result


class HeuristicProvider(LLMProvider):
    name = "heuristic-fallback"

    def generate(self, task: str, payload: dict[str, Any]) -> dict[str, Any]:
        if task == "create":
            workflow = self._create(payload["instruction"])
        elif task == "modify":
            workflow = self._modify(Workflow.model_validate(payload["workflow"]), payload["instruction"])
        elif task == "fix":
            workflow = self._fix(Workflow.model_validate(payload["workflow"]), payload.get("validation_errors", []))
        elif task == "explain":
            workflow = Workflow.model_validate(payload["workflow"])
        else:
            raise LLMError(f"Unsupported task {task}.")
        return {"workflow": workflow.model_dump(by_alias=True), "explanation": self._explain(workflow)}

    def _create(self, instruction: str) -> Workflow:
        nodes = self._nodes_for_instruction(instruction)
        if not nodes:
            nodes = [self._node("webhook", "node_1")]
        workflow = self._linear_workflow("Generated workflow", nodes)
        from .intent import enforce_workflow_intent

        return enforce_workflow_intent(workflow, instruction)

    def _modify(self, workflow: Workflow, instruction: str) -> Workflow:
        updated = deepcopy(workflow)
        existing_types = {node.type for node in updated.nodes}
        nodes_to_add = [node for node in self._nodes_for_instruction(instruction) if node.type not in existing_types]
        for node in nodes_to_add:
            node.id = f"node_{len(updated.nodes) + 1}"
            updated.nodes.append(node)
            if updated.nodes:
                trigger = updated.nodes[0]
                updated.edges.append(WorkflowEdge(from_=trigger.id, to=node.id))
        updated.name = self._name_for_workflow(updated)
        return updated

    def _fix(self, workflow: Workflow, validation_errors: list[dict[str, Any]]) -> Workflow:
        fixed = deepcopy(workflow)
        errors = validation_errors or [error.model_dump() for error in validate_workflow(fixed).errors]
        node_map = {node.id: node for node in fixed.nodes}

        for error in errors:
            node_id = error.get("node_id")
            node_type = error.get("node_type")
            field = error.get("field")
            node = node_map.get(node_id or "") or next(
                (item for item in fixed.nodes if item.type == node_type),
                None,
            )
            if node and field:
                definition = NODE_CATALOG.get(node.type)
                if definition and field in definition.defaults:
                    node.config[field] = definition.defaults[field]

        existing = {node.id for node in fixed.nodes}
        fixed.edges = [edge for edge in fixed.edges if edge.from_ in existing and edge.to in existing and edge.from_ != edge.to]
        if len(fixed.nodes) > 1 and not fixed.edges:
            root = fixed.nodes[0]
            fixed.edges = [WorkflowEdge(from_=root.id, to=node.id) for node in fixed.nodes[1:]]
        fixed.name = self._name_for_workflow(fixed)
        return repair_workflow(fixed)

    def _nodes_for_instruction(self, instruction: str) -> list[WorkflowNode]:
        text = instruction.lower()
        nodes: list[WorkflowNode] = []
        creates_calendar_event = bool(
            ("calendar" in text or "event" in text)
            and re.search(r"\b(?:create|add|schedule|make|set)\b", text)
            and "reminder" not in text
        )
        if ("calendar" in text or "event" in text) and not creates_calendar_event:
            nodes.append(self._node("calendar_event_trigger", f"node_{len(nodes) + 1}"))
        if any(term in text for term in ("email", "gmail", "stripe")):
            sender = "Stripe" if "stripe" in text else "any sender"
            nodes.append(self._node("gmail_trigger", f"node_{len(nodes) + 1}", {"from_contains": sender}))
        if any(
            term in text
            for term in ("urgent", "tagged", "only if", "when tag", "with word", "containing", "contains")
        ) or re.search(r"""["'][^"']+["']""", instruction):
            nodes.append(self._node("filter_condition", f"node_{len(nodes) + 1}"))
        if "webhook" in text or "http" in text:
            nodes.append(self._node("webhook", f"node_{len(nodes) + 1}"))
        if "task" in text or "task list" in text:
            nodes.append(self._node("task_create", f"node_{len(nodes) + 1}"))
        if "reminder" in text or "remind" in text:
            nodes.append(self._node("reminder_create", f"node_{len(nodes) + 1}"))
        if creates_calendar_event:
            nodes.append(self._node("calendar_event_create", f"node_{len(nodes) + 1}"))
        if "slack" in text or "finance team" in text:
            channel = self._channel_from_text(text)
            nodes.append(
                self._node(
                    "slack_message",
                    f"node_{len(nodes) + 1}",
                    {"channel_id": channel, "message_template": "New matching email received."},
                )
            )
        elif "notification" in text and any(
            term in text for term in ("email", "gmail", "inbox")
        ):
            nodes.append(
                self._node(
                    "slack_message",
                    f"node_{len(nodes) + 1}",
                    {
                        "channel_id": "general",
                        "message_template": "New email from {{from}}: {{subject}}",
                    },
                )
            )
        if "notion" in text or "page" in text:
            nodes.append(self._node("notion_create_page", f"node_{len(nodes) + 1}"))
        if "teams" in text:
            nodes.append(self._node("teams_message", f"node_{len(nodes) + 1}"))
        return nodes

    def _node(self, node_type: str, node_id: str, config: dict[str, Any] | None = None) -> WorkflowNode:
        definition = NODE_CATALOG[node_type]
        merged = dict(definition.defaults)
        if config:
            merged.update(config)
        return WorkflowNode(
            id=node_id,
            type=node_type,
            role=definition.role,
            config=merged,
        )

    def _linear_workflow(self, name: str, nodes: list[WorkflowNode]) -> Workflow:
        edges = [WorkflowEdge(from_=nodes[index].id, to=nodes[index + 1].id) for index in range(len(nodes) - 1)]
        workflow = Workflow(name=name, nodes=nodes, edges=edges)
        workflow.name = self._name_for_workflow(workflow)
        return workflow

    def _channel_from_text(self, text: str) -> str:
        match = re.search(r"to the ([a-z0-9_-]+) team", text)
        if match:
            return match.group(1)
        return "general"

    def _name_for_workflow(self, workflow: Workflow) -> str:
        types = [node.type.replace("_", " ") for node in workflow.nodes]
        return " -> ".join(types).title() if types else "Untitled workflow"

    def _explain(self, workflow: Workflow) -> str:
        if not workflow.nodes:
            return "This workflow has no steps yet."
        node_by_id = {node.id: node for node in workflow.nodes}
        steps = []
        for node in workflow.nodes:
            description = NODE_CATALOG.get(node.type)
            label = description.description if description else node.type
            details = ", ".join(f"{key}: {value}" for key, value in sorted(node.config.items()))
            steps.append(f"{label}" + (f" ({details})" if details else ""))
        connections = [f"{node_by_id.get(edge.from_, edge).type} to {node_by_id.get(edge.to, edge).type}" for edge in workflow.edges]
        explanation = "This workflow runs these steps: " + " Then ".join(steps) + "."
        if connections:
            explanation += " Connections: " + "; ".join(connections) + "."
        return explanation


def build_provider() -> LLMProvider:
    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        return FallbackProvider(
            primary=OpenAIProvider(
                api_key=api_key,
                model=os.getenv("OPENAI_MODEL", "gpt-5.5"),
                timeout_seconds=float(
                    os.getenv("OPENAI_TIMEOUT_SECONDS", "45")
                ),
                max_retries=int(os.getenv("OPENAI_MAX_RETRIES", "2")),
                max_tool_rounds=int(
                    os.getenv("OPENAI_MAX_TOOL_ROUNDS", "2")
                ),
                reasoning_effort=os.getenv(
                    "OPENAI_REASONING_EFFORT",
                    "low",
                ),
            ),
            fallback=HeuristicProvider(),
        )
    return HeuristicProvider()
