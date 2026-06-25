from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, StringConstraints, model_validator


WorkflowStatus = Literal["draft", "active", "paused"]
WorkflowVisibility = Literal["private", "team", "restricted"]
WorkflowMode = Literal["manual", "scheduled"]
NodeRole = Literal["trigger", "action", "condition"]
NodeRunStatus = Literal["idle", "pending", "running", "success", "failed"]
TeamRole = Literal["admin", "member"]
WorkflowPermission = Literal["run", "edit_run"]
RunStatus = Literal["pending", "running", "success", "failed"]
RunTriggerType = Literal["manual", "scheduled"]
IntegrationProvider = Literal[
    "gmail",
    "slack",
    "teams",
    "notion",
    "jira",
    "hubspot",
    "google_calendar",
    "google_drive",
    "google_sheets",
    "github",
    "discord",
    "airtable",
    "stripe",
    "salesforce",
]
WorkflowPriority = Literal["low", "medium", "high"]
ConversationTurnKind = Literal["create", "modify"]
PlanningStatus = Literal["awaiting_clarification", "ready", "completed"]
InstructionText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


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
    code: str = "validation_error"
    message: str = "Workflow validation failed."
    node_id: str | None = None
    node_type: str | None = None
    edge_index: int | None = None
    field: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_external_error(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        raw_node = normalized.pop("node", None)
        raw_error = normalized.pop("error", None)
        if raw_node and not normalized.get("node_id") and not normalized.get("node_type"):
            node_value = str(raw_node).strip()
            if re.match(r"^(?:node[_-]|[a-f0-9]{8,}$)", node_value, re.I):
                normalized["node_id"] = node_value
            else:
                normalized["node_type"] = node_value
        if raw_error and not normalized.get("message"):
            normalized["message"] = str(raw_error)
        message = str(normalized.get("message") or raw_error or "")
        if not normalized.get("field"):
            missing_match = re.search(
                r"\b([a-z][a-z0-9_]*)\s+(?:is\s+)?missing\b",
                message,
                re.I,
            )
            if missing_match:
                normalized["field"] = missing_match.group(1)
        if not normalized.get("code"):
            normalized["code"] = (
                "missing_required_config"
                if normalized.get("field") and "missing" in message.lower()
                else "validation_error"
            )
        return normalized


class ValidationResult(BaseModel):
    valid: bool
    errors: list[ValidationErrorDetail] = Field(default_factory=list)


class WorkflowOperation(BaseModel):
    op: Literal[
        "add_node",
        "update_node",
        "remove_node",
        "connect_nodes",
        "disconnect_nodes",
        "update_workflow",
    ]
    node: WorkflowNode | None = None
    node_id: str | None = None
    edge: WorkflowEdge | None = None
    config: dict[str, Any] | None = None
    changes: dict[str, Any] | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def validate_operation_payload(self) -> "WorkflowOperation":
        if self.op == "add_node" and self.node is None:
            raise ValueError("add_node requires node.")
        if self.op == "update_node" and (
            self.node_id is None or self.node is None
        ):
            raise ValueError("update_node requires node_id and node.")
        if self.op == "remove_node" and self.node_id is None:
            raise ValueError("remove_node requires node_id.")
        if self.op in {"connect_nodes", "disconnect_nodes"} and self.edge is None:
            raise ValueError(f"{self.op} requires edge.")
        if self.op == "update_workflow" and not self.changes:
            raise ValueError("update_workflow requires changes.")
        return self


class WorkflowDiffRequest(BaseModel):
    before: Workflow
    after: Workflow


class ApplyWorkflowOperationsRequest(BaseModel):
    expected_version: int = Field(ge=1)
    operations: list[WorkflowOperation] = Field(min_length=1)


class WorkflowOperationsResponse(BaseModel):
    workflow_id: str
    base_version: int
    target_version: int
    operations: list[WorkflowOperation]
    validation: ValidationResult
    persisted: bool
    provider: str | None = None
    explanation: str | None = None


class CopilotResponse(BaseModel):
    workflow: Workflow = Field(
        description="The complete workflow after the requested Copilot operation."
    )
    validation: ValidationResult = Field(
        description="Validation status for the returned workflow."
    )
    operations: list[WorkflowOperation] = Field(default_factory=list)
    explanation: str | None = Field(
        default=None,
        description="Human-readable explanation when the operation provides one.",
    )
    provider: str = Field(
        description="Provider that generated or assisted with the result."
    )


class ConversationTurn(BaseModel):
    id: str = Field(default_factory=lambda: f"turn_{uuid4().hex[:12]}")
    conversation_id: str
    sequence: int = Field(ge=1)
    kind: ConversationTurnKind
    instruction: str
    workflow: Workflow
    operations: list[WorkflowOperation] = Field(default_factory=list)
    explanation: str | None = None
    provider: str
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class Conversation(BaseModel):
    id: str = Field(default_factory=lambda: f"conv_{uuid4().hex[:12]}")
    owner_id: str
    title: str
    workflow_id: str | None = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class ConversationMessageRequest(BaseModel):
    instruction: InstructionText
    context: dict[str, Any] = Field(default_factory=dict)


class ConversationResponse(BaseModel):
    conversation: Conversation
    turn: ConversationTurn
    validation: ValidationResult


class ConversationDetail(BaseModel):
    conversation: Conversation
    turns: list[ConversationTurn]


class CreateWorkflowRequest(BaseModel):
    instruction: InstructionText = Field(
        description="Natural-language description of the workflow to create.",
        examples=[
            "When I receive an email from Stripe, send a Slack message to the finance team."
        ],
    )
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional business or application context.",
    )


class ExtractedWorkflowRequest(BaseModel):
    trigger: str
    tasks: list[str] = Field(default_factory=list)
    goal: str


class WorkflowIntentProfile(BaseModel):
    industry: str
    workflow_type: str
    priority: WorkflowPriority = "medium"
    apps: list[str] = Field(default_factory=list)


class WorkflowRecommendation(BaseModel):
    id: str
    name: str
    description: str
    match_score: float
    reason: str
    apps: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)


class RetrievalContext(BaseModel):
    query: str
    knowledge_base: str = "flowmind-proven-workflows"
    retrieved_template_ids: list[str] = Field(default_factory=list)
    guidance: list[str] = Field(default_factory=list)


class WorkflowAnalysisResponse(BaseModel):
    instruction: str
    extracted: ExtractedWorkflowRequest
    intent: WorkflowIntentProfile
    recommendations: list[WorkflowRecommendation] = Field(default_factory=list)
    retrieval: RetrievalContext
    proposed_workflow: Workflow
    required_apps: list[str] = Field(default_factory=list)
    missing_integrations: list[str] = Field(default_factory=list)
    unsupported_tasks: list[str] = Field(default_factory=list)
    planning_warnings: list[str] = Field(default_factory=list)
    provider: str = "hybrid-rag"


class PlanningStep(BaseModel):
    id: str
    label: str
    description: str
    status: Literal["pending", "in_progress", "completed"]


class ClarifyingQuestion(BaseModel):
    id: str
    question: str
    reason: str
    choices: list[str] = Field(default_factory=list)
    required: bool = True


class WorkflowPlanSession(BaseModel):
    id: str = Field(default_factory=lambda: f"plan_{uuid4().hex[:12]}")
    owner_id: str
    instruction: str
    resolved_instruction: str | None = None
    status: PlanningStatus
    steps: list[PlanningStep]
    questions: list[ClarifyingQuestion] = Field(default_factory=list)
    answers: dict[str, str] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class WorkflowPlanResponse(BaseModel):
    session: WorkflowPlanSession
    analysis: WorkflowAnalysisResponse | None = None


class ContinueWorkflowPlanRequest(BaseModel):
    answers: dict[str, InstructionText]


class WorkflowPlanEvent(BaseModel):
    event: Literal[
        "accepted",
        "planning",
        "clarification",
        "analysis",
        "validation",
        "complete",
        "error",
    ]
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class BuildWorkflowRequest(BaseModel):
    instruction: InstructionText
    workflow: Workflow
    selected_recommendation_id: str | None = None


class ModifyWorkflowRequest(BaseModel):
    workflow: Workflow = Field(description="Existing workflow to modify.")
    instruction: InstructionText = Field(
        description="Natural-language description of the requested modification.",
        examples=["Also create a Notion page whenever an email arrives."],
    )
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional context used to interpret the modification.",
    )


class FixWorkflowRequest(BaseModel):
    workflow: Workflow = Field(description="Invalid workflow to repair.")
    instruction: InstructionText = Field(
        default="Fix the workflow.",
        description="Repair instruction.",
    )
    validation_errors: list[ValidationErrorDetail] = Field(
        default_factory=list,
        description=(
            "Known validation errors. Both the canonical shape and "
            '{"node":"slack_message","error":"channel_id missing"} are accepted.'
        ),
    )
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional context used to infer missing values.",
    )


class ExplainWorkflowRequest(BaseModel):
    workflow: Workflow = Field(description="Workflow to explain.")
    instruction: InstructionText = Field(
        default="Explain this workflow.",
        description="Explanation request.",
    )
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional audience or business context.",
    )


class APIErrorResponse(BaseModel):
    detail: str | list[dict[str, Any]]


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
    picture: str | None = None
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


class WorkflowTask(BaseModel):
    id: str = Field(default_factory=lambda: f"task_{uuid4().hex[:12]}")
    workflow_id: str
    run_id: str
    list_id: str = "default"
    title: str
    status: Literal["open", "completed"] = "open"
    source: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WorkflowTaskSummary(BaseModel):
    id: str
    workflow_id: str
    run_id: str
    list_id: str
    title: str
    status: Literal["open", "completed"]
    created_at: datetime


class AuthUser(BaseModel):
    id: str
    email: str
    name: str | None = None
    picture: str | None = None
    role: TeamRole


class AuthStatus(BaseModel):
    authenticated: bool
    google_configured: bool
    user: AuthUser | None = None
    setup_message: str | None = None
    oauth_redirect_uri: str | None = None


class IntegrationUpdate(BaseModel):
    config: dict[str, str] = Field(default_factory=dict)


class IntegrationSummary(BaseModel):
    provider: IntegrationProvider
    name: str
    connected: bool
    values: dict[str, str] = Field(default_factory=dict)
    configured_fields: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None
