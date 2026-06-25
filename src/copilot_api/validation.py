from __future__ import annotations

from copy import deepcopy
from typing import Any

from .catalog import NODE_CATALOG, NodeDefinition
from .models import (
    ValidationErrorDetail,
    ValidationResult,
    Workflow,
    WorkflowEdge,
)


ALLOWED_FILTER_OPERATORS = {
    "contains",
    "equals",
    "eq",
    "==",
    "not_equals",
    "neq",
    "!=",
    "exists",
    "greater_than",
    "gt",
    ">",
    "less_than",
    "lt",
    "<",
}


def validate_workflow(workflow: Workflow) -> ValidationResult:
    errors: list[ValidationErrorDetail] = []
    if not workflow.nodes:
        errors.append(
            ValidationErrorDetail(
                code="workflow_empty",
                message="Workflow requires at least one node.",
            )
        )
        return ValidationResult(valid=False, errors=errors)

    node_ids: set[str] = set()
    definitions: dict[str, NodeDefinition] = {}
    for node in workflow.nodes:
        if not node.id.strip():
            errors.append(
                ValidationErrorDetail(
                    code="node_id_missing",
                    node_id=node.id,
                    message="Every workflow node requires an id.",
                )
            )
        elif node.id in node_ids:
            errors.append(
                ValidationErrorDetail(
                    code="duplicate_node_id",
                    node_id=node.id,
                    message=f"Node id {node.id} is duplicated.",
                )
            )
        node_ids.add(node.id)

        definition = NODE_CATALOG.get(node.type)
        if definition is None:
            errors.append(
                ValidationErrorDetail(
                    code="unknown_node_type",
                    node_id=node.id,
                    node_type=node.type,
                    message=f"Unknown node type {node.type}.",
                )
            )
            continue
        definitions[node.id] = definition
        if node.role != definition.role:
            errors.append(
                ValidationErrorDetail(
                    code="invalid_node_role",
                    node_id=node.id,
                    node_type=node.type,
                    field="role",
                    message=(
                        f"{node.type} must use role {definition.role}, "
                        f"not {node.role}."
                    ),
                )
            )
        errors.extend(_validate_node_config(node.id, node.type, node.config, definition))

    adjacency: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    incoming: dict[str, int] = {node_id: 0 for node_id in node_ids}
    edge_pairs: set[tuple[str, str]] = set()
    for index, edge in enumerate(workflow.edges):
        pair = (edge.from_, edge.to)
        if pair in edge_pairs:
            errors.append(
                ValidationErrorDetail(
                    code="duplicate_edge",
                    edge_index=index,
                    message=f"Connection {edge.from_} to {edge.to} is duplicated.",
                )
            )
        edge_pairs.add(pair)

        source_exists = edge.from_ in node_ids
        target_exists = edge.to in node_ids
        if not source_exists:
            errors.append(
                ValidationErrorDetail(
                    code="edge_from_missing",
                    edge_index=index,
                    message=f"Edge source {edge.from_} does not exist.",
                )
            )
        if not target_exists:
            errors.append(
                ValidationErrorDetail(
                    code="edge_to_missing",
                    edge_index=index,
                    message=f"Edge target {edge.to} does not exist.",
                )
            )
        if edge.from_ == edge.to:
            errors.append(
                ValidationErrorDetail(
                    code="self_edge",
                    edge_index=index,
                    message="Workflow edges cannot connect a node to itself.",
                )
            )
        if source_exists and target_exists and edge.from_ != edge.to:
            adjacency[edge.from_].append(edge.to)
            incoming[edge.to] += 1

    trigger_ids = [
        node.id
        for node in workflow.nodes
        if definitions.get(node.id) and definitions[node.id].role == "trigger"
    ]
    if not trigger_ids:
        errors.append(
            ValidationErrorDetail(
                code="trigger_missing",
                message="Workflow requires one trigger node.",
            )
        )
    elif len(trigger_ids) > 1:
        errors.append(
            ValidationErrorDetail(
                code="multiple_triggers",
                message="Workflow can contain only one trigger node.",
            )
        )

    for trigger_id in trigger_ids:
        if incoming.get(trigger_id, 0):
            errors.append(
                ValidationErrorDetail(
                    code="trigger_has_incoming_edge",
                    node_id=trigger_id,
                    message=f"Trigger node {trigger_id} cannot have an incoming connection.",
                )
            )

    if len(workflow.nodes) > 1 and not workflow.edges:
        errors.append(
            ValidationErrorDetail(
                code="workflow_disconnected",
                message="Workflow has multiple nodes but no connections.",
            )
        )

    if trigger_ids:
        reachable = _reachable_nodes(trigger_ids[0], adjacency)
        for node in workflow.nodes:
            if node.id not in reachable:
                errors.append(
                    ValidationErrorDetail(
                        code="node_unreachable",
                        node_id=node.id,
                        node_type=node.type,
                        message=f"Node {node.id} is not reachable from the workflow trigger.",
                    )
                )
            elif node.id != trigger_ids[0] and incoming.get(node.id, 0) == 0:
                errors.append(
                    ValidationErrorDetail(
                        code="node_missing_incoming_edge",
                        node_id=node.id,
                        node_type=node.type,
                        message=f"Node {node.id} requires an incoming connection.",
                    )
                )

    if _has_cycle(adjacency):
        errors.append(
            ValidationErrorDetail(
                code="workflow_cycle",
                message="Workflow connections must not contain a cycle.",
            )
        )

    if workflow.mode == "scheduled" and not _has_value(workflow.trigger_schedule):
        errors.append(
            ValidationErrorDetail(
                code="schedule_missing",
                field="trigger_schedule",
                message="Scheduled workflows require trigger_schedule.",
            )
        )

    return ValidationResult(valid=not errors, errors=errors)


def repair_workflow(workflow: Workflow) -> Workflow:
    """Apply safe structural and default-based repairs without inventing nodes."""
    repaired = deepcopy(workflow)
    seen_ids: set[str] = set()
    unique_nodes = []
    for node in repaired.nodes:
        if not node.id or node.id in seen_ids:
            node.id = _next_node_id(seen_ids)
        seen_ids.add(node.id)
        definition = NODE_CATALOG.get(node.type)
        if definition:
            node.role = definition.role
            for field in definition.required_config:
                if not _has_value(node.config.get(field)) and _has_value(
                    definition.defaults.get(field)
                ):
                    node.config[field] = deepcopy(definition.defaults[field])
            _repair_invalid_config(node.type, node.config, definition)
        unique_nodes.append(node)
    repaired.nodes = unique_nodes

    valid_ids = {node.id for node in repaired.nodes}
    trigger = next(
        (
            node
            for node in repaired.nodes
            if NODE_CATALOG.get(node.type)
            and NODE_CATALOG[node.type].role == "trigger"
        ),
        None,
    )
    edges: list[WorkflowEdge] = []
    seen_edges: set[tuple[str, str]] = set()
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in valid_ids}
    incoming: dict[str, int] = {node_id: 0 for node_id in valid_ids}
    for edge in repaired.edges:
        pair = (edge.from_, edge.to)
        if (
            edge.from_ not in valid_ids
            or edge.to not in valid_ids
            or edge.from_ == edge.to
            or (trigger is not None and edge.to == trigger.id)
            or pair in seen_edges
            or _path_exists(edge.to, edge.from_, adjacency)
        ):
            continue
        seen_edges.add(pair)
        edges.append(edge)
        adjacency[edge.from_].append(edge.to)
        incoming[edge.to] += 1

    if trigger:
        for node in repaired.nodes:
            if node.id == trigger.id:
                continue
            if incoming[node.id] == 0:
                edge = WorkflowEdge(from_=trigger.id, to=node.id)
                edges.append(edge)
                adjacency[trigger.id].append(node.id)
                incoming[node.id] += 1

    repaired.edges = edges
    return repaired


def _validate_node_config(
    node_id: str,
    node_type: str,
    config: dict[str, Any],
    definition: NodeDefinition,
) -> list[ValidationErrorDetail]:
    errors: list[ValidationErrorDetail] = []
    for field in definition.required_config:
        if not _has_value(config.get(field)):
            errors.append(
                ValidationErrorDetail(
                    code="missing_required_config",
                    node_id=node_id,
                    node_type=node_type,
                    field=field,
                    message=f"{node_type} requires {field}.",
                )
            )

    if node_type == "filter_condition":
        operator = str(config.get("operator", "")).lower()
        if operator and operator not in ALLOWED_FILTER_OPERATORS:
            errors.append(
                ValidationErrorDetail(
                    code="invalid_config_value",
                    node_id=node_id,
                    node_type=node_type,
                    field="operator",
                    message=f"filter_condition does not support operator {operator}.",
                )
            )
    elif node_type == "calendar_event_trigger":
        lookahead = config.get("lookahead_minutes")
        if _has_value(lookahead) and (
            not isinstance(lookahead, int)
            or isinstance(lookahead, bool)
            or lookahead <= 0
        ):
            errors.append(
                ValidationErrorDetail(
                    code="invalid_config_value",
                    node_id=node_id,
                    node_type=node_type,
                    field="lookahead_minutes",
                    message="calendar_event_trigger lookahead_minutes must be a positive integer.",
                )
            )
    elif node_type == "webhook":
        path = config.get("path")
        if _has_value(path) and (
            not isinstance(path, str) or not path.startswith("/")
        ):
            errors.append(
                ValidationErrorDetail(
                    code="invalid_config_value",
                    node_id=node_id,
                    node_type=node_type,
                    field="path",
                    message="webhook path must start with '/'.",
                )
            )
    return errors


def _repair_invalid_config(
    node_type: str,
    config: dict[str, Any],
    definition: NodeDefinition,
) -> None:
    if node_type == "filter_condition":
        operator = str(config.get("operator", "")).lower()
        if operator not in ALLOWED_FILTER_OPERATORS:
            config["operator"] = definition.defaults["operator"]
    elif node_type == "calendar_event_trigger":
        lookahead = config.get("lookahead_minutes")
        if (
            not isinstance(lookahead, int)
            or isinstance(lookahead, bool)
            or lookahead <= 0
        ):
            config["lookahead_minutes"] = definition.defaults["lookahead_minutes"]
    elif node_type == "webhook":
        path = config.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            config["path"] = definition.defaults["path"]


def _has_value(value: Any) -> bool:
    return value is not None and (
        not isinstance(value, str) or bool(value.strip())
    )


def _reachable_nodes(start: str, adjacency: dict[str, list[str]]) -> set[str]:
    visited: set[str] = set()
    pending = [start]
    while pending:
        node_id = pending.pop()
        if node_id in visited:
            continue
        visited.add(node_id)
        pending.extend(adjacency.get(node_id, []))
    return visited


def _has_cycle(adjacency: dict[str, list[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> bool:
        if node_id in visiting:
            return True
        if node_id in visited:
            return False
        visiting.add(node_id)
        if any(visit(target) for target in adjacency.get(node_id, [])):
            return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False

    return any(visit(node_id) for node_id in adjacency)


def _path_exists(
    start: str,
    target: str,
    adjacency: dict[str, list[str]],
) -> bool:
    return target in _reachable_nodes(start, adjacency)


def _next_node_id(existing: set[str]) -> str:
    index = 1
    while f"node_{index}" in existing:
        index += 1
    return f"node_{index}"
