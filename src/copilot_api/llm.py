from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from copy import deepcopy
from typing import Any

from .catalog import NODE_CATALOG, catalog_for_prompt
from .models import Workflow, WorkflowEdge, WorkflowNode
from .validation import validate_workflow


class LLMError(RuntimeError):
    pass


class LLMProvider(ABC):
    name: str

    @abstractmethod
    def generate(self, task: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, api_key: str, model: str = "gpt-4.1-mini") -> None:
        self.api_key = api_key
        self.model = model

    def generate(self, task: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_payload = {
            "model": self.model,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an AI workflow copilot. Return only valid JSON. "
                        "Use the provided node catalog and produce workflows that pass validation. "
                        "The response shape must be {\"workflow\": {...}, \"explanation\": string}."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "task": task,
                            "node_catalog": catalog_for_prompt(),
                            "payload": payload,
                            "workflow_schema": {
                                "id": "string",
                                "name": "string",
                                "nodes": [{"id": "string", "type": "string", "config": {}}],
                                "edges": [{"from": "string", "to": "string"}],
                            },
                        },
                        default=str,
                    ),
                },
            ],
        }
        request = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(request_payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise LLMError("OpenAI is rate limited or out of quota.") from exc
            raise LLMError(f"OpenAI request failed with HTTP {exc.code}.") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LLMError(f"OpenAI request failed: {exc}") from exc

        try:
            content = body["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            raise LLMError("OpenAI returned an unexpected response shape.") from exc
        return parsed


class FallbackProvider(LLMProvider):
    def __init__(self, primary: LLMProvider, fallback: LLMProvider) -> None:
        self.primary = primary
        self.fallback = fallback
        self.name = f"{primary.name}-with-{fallback.name}"
        self.last_error: str | None = None

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
        return self._linear_workflow("Generated workflow", nodes)

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
            field = error.get("field")
            node = node_map.get(node_id or "")
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
        return fixed

    def _nodes_for_instruction(self, instruction: str) -> list[WorkflowNode]:
        text = instruction.lower()
        nodes: list[WorkflowNode] = []
        if "calendar" in text or "event" in text:
            nodes.append(self._node("calendar_event_trigger", f"node_{len(nodes) + 1}"))
        if any(term in text for term in ("email", "gmail", "stripe")):
            sender = "Stripe" if "stripe" in text else "any sender"
            nodes.append(self._node("gmail_trigger", f"node_{len(nodes) + 1}", {"from_contains": sender}))
        if any(term in text for term in ("urgent", "tagged", "only if", "when tag")):
            nodes.append(self._node("filter_condition", f"node_{len(nodes) + 1}"))
        if "webhook" in text or "http" in text:
            nodes.append(self._node("webhook", f"node_{len(nodes) + 1}"))
        if "task" in text or "task list" in text:
            nodes.append(self._node("task_create", f"node_{len(nodes) + 1}"))
        if "reminder" in text or "remind" in text:
            nodes.append(self._node("reminder_create", f"node_{len(nodes) + 1}"))
        if "slack" in text or "finance team" in text:
            channel = self._channel_from_text(text)
            nodes.append(
                self._node(
                    "slack_message",
                    f"node_{len(nodes) + 1}",
                    {"channel_id": channel, "message_template": "New matching email received."},
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
        return WorkflowNode(id=node_id, type=node_type, config=merged)

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
            primary=OpenAIProvider(api_key=api_key, model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini")),
            fallback=HeuristicProvider(),
        )
    return HeuristicProvider()
