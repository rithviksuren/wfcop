from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import (
    TeamMember,
    TeamRole,
    Workflow,
    WorkflowPermission,
    WorkflowPermissionGrant,
    WorkflowPermissionSummary,
    WorkflowSummary,
    WorkflowVisibility,
    WorkflowRun,
)


class WorkflowRepository:
    def __init__(self, db_path: str = "data/workflows.sqlite3") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS workflows (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS team_members (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    name TEXT,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_team_shares (
                    workflow_id TEXT PRIMARY KEY,
                    permission TEXT NOT NULL,
                    FOREIGN KEY(workflow_id) REFERENCES workflows(id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_member_permissions (
                    workflow_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    permission TEXT NOT NULL,
                    PRIMARY KEY(workflow_id, user_id),
                    FOREIGN KEY(workflow_id) REFERENCES workflows(id),
                    FOREIGN KEY(user_id) REFERENCES team_members(id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    trigger_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    FOREIGN KEY(workflow_id) REFERENCES workflows(id)
                )
                """
            )

    def save(self, workflow: Workflow) -> Workflow:
        payload = workflow.model_dump_json(by_alias=True)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO workflows (id, name, version, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    version = excluded.version,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    workflow.id,
                    workflow.name,
                    workflow.version,
                    payload,
                    workflow.created_at.isoformat(),
                    workflow.updated_at.isoformat(),
                ),
            )
        return workflow

    def get(self, workflow_id: str) -> Workflow | None:
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
        if row is None:
            return None
        return Workflow.model_validate_json(row["payload"])

    def list(self, user_id: str | None = None) -> list[WorkflowSummary]:
        with self._connect() as connection:
            rows = connection.execute("SELECT payload FROM workflows ORDER BY updated_at DESC").fetchall()
            member = self.get_member(user_id) if user_id else None
        summaries: list[WorkflowSummary] = []
        for row in rows:
            workflow = Workflow.model_validate_json(row["payload"])
            permission = self.permission_for_user(workflow, user_id, member.role if member else None)
            if user_id and permission is None:
                continue
            last_run = self.latest_run_for_workflow(workflow.id)
            summaries.append(
                WorkflowSummary(
                    id=workflow.id,
                    name=workflow.name,
                    status=workflow.status,
                    visibility=workflow.visibility,
                    mode=workflow.mode,
                    trigger_schedule=workflow.trigger_schedule,
                    owner_id=workflow.owner_id,
                    permission=permission or "edit_run",
                    version=workflow.version,
                    node_count=len(workflow.nodes),
                    last_run_status=last_run.status if last_run else None,
                    updated_at=workflow.updated_at,
                )
            )
        return summaries

    def save_member(self, member: TeamMember) -> TeamMember:
        with self._connect() as connection:
            existing = connection.execute("SELECT payload FROM team_members WHERE email = ?", (member.email,)).fetchone()
            if existing:
                current = TeamMember.model_validate_json(existing["payload"])
                current.name = member.name
                current.role = member.role
                member = current
            connection.execute(
                """
                INSERT INTO team_members (id, email, name, role, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    name = excluded.name,
                    role = excluded.role,
                    payload = excluded.payload
                """,
                (
                    member.id,
                    member.email,
                    member.name,
                    member.role,
                    member.model_dump_json(),
                    member.created_at.isoformat(),
                ),
            )
            row = connection.execute("SELECT payload FROM team_members WHERE email = ?", (member.email,)).fetchone()
        return TeamMember.model_validate_json(row["payload"])

    def list_members(self) -> list[TeamMember]:
        with self._connect() as connection:
            rows = connection.execute("SELECT payload FROM team_members ORDER BY created_at ASC").fetchall()
        return [TeamMember.model_validate_json(row["payload"]) for row in rows]

    def get_member(self, user_id: str | None) -> TeamMember | None:
        if user_id is None:
            return None
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM team_members WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            return None
        return TeamMember.model_validate_json(row["payload"])

    def update_member_role(self, user_id: str, role: TeamRole) -> TeamMember | None:
        member = self.get_member(user_id)
        if member is None:
            return None
        member.role = role
        with self._connect() as connection:
            connection.execute(
                "UPDATE team_members SET role = ?, payload = ? WHERE id = ?",
                (member.role, member.model_dump_json(), member.id),
            )
        return member

    def share_workflow(
        self,
        workflow: Workflow,
        visibility: WorkflowVisibility,
        team_permission: WorkflowPermission,
        members: list[WorkflowPermissionGrant],
    ) -> WorkflowPermissionSummary:
        workflow.visibility = visibility
        self.save(workflow)
        with self._connect() as connection:
            if visibility == "team":
                connection.execute(
                    """
                    INSERT INTO workflow_team_shares (workflow_id, permission)
                    VALUES (?, ?)
                    ON CONFLICT(workflow_id) DO UPDATE SET permission = excluded.permission
                    """,
                    (workflow.id, team_permission),
                )
            else:
                connection.execute("DELETE FROM workflow_team_shares WHERE workflow_id = ?", (workflow.id,))
            for grant in members:
                connection.execute(
                    """
                    INSERT INTO workflow_member_permissions (workflow_id, user_id, permission)
                    VALUES (?, ?, ?)
                    ON CONFLICT(workflow_id, user_id) DO UPDATE SET permission = excluded.permission
                    """,
                    (workflow.id, grant.user_id, grant.permission),
                )
        return self.permissions_for_workflow(workflow.id, workflow.visibility)

    def permissions_for_workflow(
        self, workflow_id: str, visibility: WorkflowVisibility | None = None
    ) -> WorkflowPermissionSummary:
        workflow = self.get(workflow_id)
        effective_visibility = visibility or (workflow.visibility if workflow else "private")
        with self._connect() as connection:
            team_row = connection.execute(
                "SELECT permission FROM workflow_team_shares WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
            rows = connection.execute(
                "SELECT user_id, permission FROM workflow_member_permissions WHERE workflow_id = ? ORDER BY user_id",
                (workflow_id,),
            ).fetchall()
        return WorkflowPermissionSummary(
            workflow_id=workflow_id,
            visibility=effective_visibility,
            team_permission=team_row["permission"] if team_row else None,
            members=[WorkflowPermissionGrant(user_id=row["user_id"], permission=row["permission"]) for row in rows],
        )

    def permission_for_user(
        self, workflow: Workflow, user_id: str | None, role: TeamRole | None = None
    ) -> WorkflowPermission | None:
        if user_id is None:
            return "edit_run"
        if role == "admin" or workflow.owner_id == user_id:
            return "edit_run"
        with self._connect() as connection:
            member_row = connection.execute(
                "SELECT permission FROM workflow_member_permissions WHERE workflow_id = ? AND user_id = ?",
                (workflow.id, user_id),
            ).fetchone()
            if member_row:
                return member_row["permission"]
            team_row = connection.execute(
                "SELECT permission FROM workflow_team_shares WHERE workflow_id = ?", (workflow.id,)
            ).fetchone()
        if workflow.visibility == "team" and team_row:
            return team_row["permission"]
        return None

    def save_run(self, run: WorkflowRun) -> WorkflowRun:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO workflow_runs (id, workflow_id, status, trigger_type, payload, started_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    payload = excluded.payload,
                    completed_at = excluded.completed_at
                """,
                (
                    run.id,
                    run.workflow_id,
                    run.status,
                    run.trigger_type,
                    run.model_dump_json(),
                    run.started_at.isoformat(),
                    run.completed_at.isoformat() if run.completed_at else None,
                ),
            )
        return run

    def list_runs(self, workflow_id: str) -> list[WorkflowRun]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM workflow_runs WHERE workflow_id = ? ORDER BY started_at DESC", (workflow_id,)
            ).fetchall()
        return [WorkflowRun.model_validate_json(row["payload"]) for row in rows]

    def get_run(self, workflow_id: str, run_id: str) -> WorkflowRun | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM workflow_runs WHERE workflow_id = ? AND id = ?", (workflow_id, run_id)
            ).fetchone()
        if row is None:
            return None
        return WorkflowRun.model_validate_json(row["payload"])

    def latest_run_for_workflow(self, workflow_id: str) -> WorkflowRun | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM workflow_runs WHERE workflow_id = ? ORDER BY started_at DESC LIMIT 1",
                (workflow_id,),
            ).fetchone()
        if row is None:
            return None
        return WorkflowRun.model_validate_json(row["payload"])
