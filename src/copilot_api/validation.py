from __future__ import annotations

from .catalog import NODE_CATALOG
from .models import ValidationErrorDetail, ValidationResult, Workflow


def validate_workflow(workflow: Workflow) -> ValidationResult:
    errors: list[ValidationErrorDetail] = []
    node_ids: set[str] = set()

    for node in workflow.nodes:
        if node.id in node_ids:
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
                    message=f"Unknown node type {node.type}.",
                )
            )
            continue

        for field in definition.required_config:
            value = node.config.get(field)
            if value is None or value == "":
                errors.append(
                    ValidationErrorDetail(
                        code="missing_required_config",
                        node_id=node.id,
                        field=field,
                        message=f"{node.type} requires {field}.",
                    )
                )

    for index, edge in enumerate(workflow.edges):
        if edge.from_ not in node_ids:
            errors.append(
                ValidationErrorDetail(
                    code="edge_from_missing",
                    edge_index=index,
                    message=f"Edge source {edge.from_} does not exist.",
                )
            )
        if edge.to not in node_ids:
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

    if workflow.nodes and not workflow.edges and len(workflow.nodes) > 1:
        errors.append(
            ValidationErrorDetail(
                code="workflow_disconnected",
                message="Workflow has multiple nodes but no connections.",
            )
        )

    return ValidationResult(valid=not errors, errors=errors)
