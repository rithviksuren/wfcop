from __future__ import annotations

from .models import Workflow, WorkflowOperation


def diff_workflows(before: Workflow, after: Workflow) -> list[WorkflowOperation]:
    operations: list[WorkflowOperation] = []
    before_nodes = {node.id: node for node in before.nodes}
    after_nodes = {node.id: node for node in after.nodes}

    for node_id, node in after_nodes.items():
        if node_id not in before_nodes:
            operations.append(WorkflowOperation(op="add_node", node=node, reason="Node added."))
        elif before_nodes[node_id] != node:
            operations.append(
                WorkflowOperation(op="update_node", node_id=node_id, config=node.config, reason="Node configuration changed.")
            )

    for node_id in before_nodes:
        if node_id not in after_nodes:
            operations.append(WorkflowOperation(op="remove_node", node_id=node_id, reason="Node removed."))

    before_edges = {(edge.from_, edge.to) for edge in before.edges}
    after_edges = {(edge.from_, edge.to) for edge in after.edges}

    for edge in after.edges:
        if (edge.from_, edge.to) not in before_edges:
            operations.append(WorkflowOperation(op="connect_nodes", edge=edge, reason="Nodes connected."))

    for edge in before.edges:
        if (edge.from_, edge.to) not in after_edges:
            operations.append(WorkflowOperation(op="disconnect_nodes", edge=edge, reason="Connection removed."))

    return operations
