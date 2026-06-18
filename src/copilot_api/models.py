from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


WorkflowStatus = Literal["draft", "active", "paused"]
WorkflowVisibility = Literal["private", "team", "restricted"]
WorkflowMode = Literal["manual", "scheduled"]
NodeRole = Literal["trigger", "action", "condition"]
NodeRunStatus = Literal["idle", "pending", "running", "success", "failed"]
TeamRole = Literal["admin", "member"]
WorkflowPermission = Literal["run", "edit_run"]
RunStatus = Literal["pending", "running", "success", "failed"]
RunTriggerType = Literal["manual", "scheduled"]


class WorkflowNode(BaseModel):
    id: str = Field(default_factory=lambda: f"node_{uuid4().hex[:8]}")
    type: str
    role: NodeRole = "action"
    label: str | None = None
    description: str | None = None
    status: NodeRunStatus = "idle"
    config: dict[str, Any] = Field(default_factory=dict)


class WorkflowEdge(BaseModel):
    from_: str = Field(alias="from")
    to: str

    model_config = {"populate_by_name": True}


class Workflow(BaseModel):
    id: str = Field(default_factory=lambda: f"wf_{uuid4().hex[:12]}")
    name: str = "Untitled workflow"
    status: WorkflowStatus = "draft"
    visibility: WorkflowVisibility = "private"
    owner_id: str | None = None
    created_by: str | None = None
    updated_by: str | None = None
    mode: WorkflowMode = "manual"
    trigger_schedule: str | None = None
    nodes: list[WorkflowNode] = Field(default_factory=list)
    edges: list[WorkflowEdge] = Field(default_factory=list)
    version: int = 1
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ValidationErrorDetail(BaseModel):
    code: str
    message: str
    node_id: str | None = None
    edge_index: int | None = None
    field: str | None = None


class ValidationResult(BaseModel):
    valid: bool
    errors: list[ValidationErrorDetail] = Field(default_factory=list)


class WorkflowOperation(BaseModel):
    op: Literal["add_node", "update_node", "remove_node", "connect_nodes", "disconnect_nodes"]
    node: WorkflowNode | None = None
    node_id: str | None = None
    edge: WorkflowEdge | None = None
    config: dict[str, Any] | None = None
    reason: str | None = None


class CopilotResponse(BaseModel):
    workflow: Workflow
    validation: ValidationResult
    operations: list[WorkflowOperation] = Field(default_factory=list)
    explanation: str | None = None
    provider: str


class CreateWorkflowRequest(BaseModel):
    instruction: str
    context: dict[str, Any] = Field(default_factory=dict)


class ModifyWorkflowRequest(BaseModel):
    workflow: Workflow
    instruction: str
    context: dict[str, Any] = Field(default_factory=dict)


class FixWorkflowRequest(BaseModel):
    workflow: Workflow
    instruction: str = "Fix the workflow."
    validation_errors: list[ValidationErrorDetail] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)


class ExplainWorkflowRequest(BaseModel):
    workflow: Workflow
    instruction: str = "Explain this workflow."
    context: dict[str, Any] = Field(default_factory=dict)


class SaveWorkflowRequest(BaseModel):
    workflow: Workflow


class UpdateWorkflowRequest(BaseModel):
    name: str | None = None
    status: WorkflowStatus | None = None
    visibility: WorkflowVisibility | None = None
    mode: WorkflowMode | None = None
    trigger_schedule: str | None = None
    nodes: list[WorkflowNode] | None = None
    edges: list[WorkflowEdge] | None = None


class WorkflowSummary(BaseModel):
    id: str
    name: str
    status: WorkflowStatus
    visibility: WorkflowVisibility
    mode: WorkflowMode
    trigger_schedule: str | None = None
    owner_id: str | None = None
    permission: WorkflowPermission
    version: int
    node_count: int
    last_run_status: RunStatus | None = None
    updated_at: datetime


class TeamMember(BaseModel):
    id: str = Field(default_factory=lambda: f"user_{uuid4().hex[:10]}")
    email: str
    name: str | None = None
    role: TeamRole = "member"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class InviteMemberRequest(BaseModel):
    id: str | None = None
    email: str
    role: TeamRole = "member"
    name: str | None = None


class UpdateMemberRoleRequest(BaseModel):
    role: TeamRole


class WorkflowPermissionGrant(BaseModel):
    user_id: str
    permission: WorkflowPermission


class ShareWorkflowRequest(BaseModel):
    visibility: WorkflowVisibility = "team"
    team_permission: WorkflowPermission = "run"
    members: list[WorkflowPermissionGrant] = Field(default_factory=list)


class WorkflowPermissionSummary(BaseModel):
    workflow_id: str
    visibility: WorkflowVisibility
    team_permission: WorkflowPermission | None = None
    members: list[WorkflowPermissionGrant] = Field(default_factory=list)


class RunWorkflowRequest(BaseModel):
    trigger_type: RunTriggerType = "manual"
    input: dict[str, Any] = Field(default_factory=dict)


class WorkflowRunStep(BaseModel):
    step_id: str
    label: str
    status: RunStatus
    started_at: datetime
    completed_at: datetime
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class WorkflowRun(BaseModel):
    id: str = Field(default_factory=lambda: f"run_{uuid4().hex[:12]}")
    workflow_id: str
    trigger_type: RunTriggerType
    status: RunStatus = "pending"
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    duration_ms: int | None = None
    summary: str | None = None
    steps: list[WorkflowRunStep] = Field(default_factory=list)
