from __future__ import annotations

from copy import deepcopy
from typing import Any

from .models import Workflow, WorkflowEdge, WorkflowNode, WorkflowOperation


MUTABLE_WORKFLOW_FIELDS = (
    "name",
    "status",
    "visibility",
    "mode",
    "trigger_schedule",
)


def diff_workflows(
    before: Workflow,
    after: Workflow,
) -> list[WorkflowOperation]:
    """Return a deterministic semantic patch from before to after."""
    operations: list[WorkflowOperation] = []
    before_nodes = {node.id: node for node in before.nodes}
    after_nodes = {node.id: node for node in after.nodes}
    before_edges = {(edge.from_, edge.to): edge for edge in before.edges}
    after_edges = {(edge.from_, edge.to): edge for edge in after.edges}

    workflow_changes = {
        field: deepcopy(getattr(after, field))
        for field in MUTABLE_WORKFLOW_FIELDS
        if getattr(before, field) != getattr(after, field)
    }
    if workflow_changes:
        operations.append(
            WorkflowOperation(
                op="update_workflow",
                changes=workflow_changes,
                reason="Workflow properties changed.",
            )
        )

    for pair, edge in before_edges.items():
        if pair not in after_edges:
            operations.append(
                WorkflowOperation(
                    op="disconnect_nodes",
                    edge=deepcopy(edge),
                    reason="Connection removed.",
                )
            )

    for node in before.nodes:
        if node.id not in after_nodes:
            operations.append(
                WorkflowOperation(
                    op="remove_node",
                    node_id=node.id,
                    reason="Node removed.",
                )
            )

    for node in after.nodes:
        previous = before_nodes.get(node.id)
        if previous is None:
            operations.append(
                WorkflowOperation(
                    op="add_node",
                    node=deepcopy(node),
                    reason="Node added.",
                )
            )
        elif _node_semantics(previous) != _node_semantics(node):
            operations.append(
                WorkflowOperation(
                    op="update_node",
                    node_id=node.id,
                    node=deepcopy(node),
                    config=deepcopy(node.config),
                    reason="Node properties changed.",
                )
            )

    for pair, edge in after_edges.items():
        if pair not in before_edges:
            operations.append(
                WorkflowOperation(
                    op="connect_nodes",
                    edge=deepcopy(edge),
                    reason="Nodes connected.",
                )
            )

    return operations


def apply_workflow_operations(
    workflow: Workflow,
    operations: list[WorkflowOperation],
) -> Workflow:
    """Apply a semantic patch without mutating the source workflow."""
    patched = deepcopy(workflow)

    for operation in operations:
        if operation.op != "update_workflow":
            continue
        for field, value in (operation.changes or {}).items():
            if field not in MUTABLE_WORKFLOW_FIELDS:
                raise ValueError(f"Workflow field {field} cannot be patched.")
            setattr(patched, field, deepcopy(value))

    disconnected = {
        (operation.edge.from_, operation.edge.to)
        for operation in operations
        if operation.op == "disconnect_nodes" and operation.edge
    }
    if disconnected:
        patched.edges = [
            edge
            for edge in patched.edges
            if (edge.from_, edge.to) not in disconnected
        ]

    removed_ids = {
        operation.node_id
        for operation in operations
        if operation.op == "remove_node" and operation.node_id
    }
    if removed_ids:
        patched.nodes = [
            node for node in patched.nodes if node.id not in removed_ids
        ]
        patched.edges = [
            edge
            for edge in patched.edges
            if edge.from_ not in removed_ids and edge.to not in removed_ids
        ]

    node_positions = {
        node.id: index for index, node in enumerate(patched.nodes)
    }
    for operation in operations:
        if operation.op != "update_node" or operation.node is None:
            continue
        if operation.node_id not in node_positions:
            raise ValueError(
                f"Cannot update missing node {operation.node_id}."
            )
        if operation.node.id != operation.node_id:
            raise ValueError("update_node cannot change a node id.")
        index = node_positions[operation.node_id]
        patched.nodes[index] = deepcopy(operation.node)

    existing_ids = {node.id for node in patched.nodes}
    for operation in operations:
        if operation.op != "add_node" or operation.node is None:
            continue
        if operation.node.id in existing_ids:
            raise ValueError(f"Node {operation.node.id} already exists.")
        patched.nodes.append(deepcopy(operation.node))
        existing_ids.add(operation.node.id)

    edge_pairs = {(edge.from_, edge.to) for edge in patched.edges}
    for operation in operations:
        if operation.op != "connect_nodes" or operation.edge is None:
            continue
        edge = operation.edge
        if edge.from_ not in existing_ids or edge.to not in existing_ids:
            raise ValueError(
                f"Cannot connect missing nodes {edge.from_} and {edge.to}."
            )
        pair = (edge.from_, edge.to)
        if pair not in edge_pairs:
            patched.edges.append(deepcopy(edge))
            edge_pairs.add(pair)

    return patched


def workflows_semantically_equal(
    left: Workflow,
    right: Workflow,
) -> bool:
    return _workflow_semantics(left) == _workflow_semantics(right)


def _node_semantics(node: WorkflowNode) -> dict[str, Any]:
    return node.model_dump(exclude={"status"})


def _workflow_semantics(workflow: Workflow) -> dict[str, Any]:
    return {
        **{
            field: getattr(workflow, field)
            for field in MUTABLE_WORKFLOW_FIELDS
        },
        "nodes": [_node_semantics(node) for node in workflow.nodes],
        "edges": [
            edge.model_dump(by_alias=True)
            for edge in workflow.edges
        ],
    }
