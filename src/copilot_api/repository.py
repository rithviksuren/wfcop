from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
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
    WorkflowTask,
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_tasks (
                    id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(workflow_id) REFERENCES workflows(id),
                    FOREIGN KEY(run_id) REFERENCES workflow_runs(id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS integrations (
                    provider TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES team_members(id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS oauth_states (
                    state TEXT PRIMARY KEY,
                    nonce TEXT NOT NULL,
                    expires_at TEXT NOT NULL
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

    def delete(self, workflow_id: str) -> bool:
        with self._connect() as connection:
            exists = connection.execute("SELECT 1 FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
            if exists is None:
                return False
            connection.execute("DELETE FROM workflow_tasks WHERE workflow_id = ?", (workflow_id,))
            connection.execute("DELETE FROM workflow_runs WHERE workflow_id = ?", (workflow_id,))
            connection.execute("DELETE FROM workflow_member_permissions WHERE workflow_id = ?", (workflow_id,))
            connection.execute("DELETE FROM workflow_team_shares WHERE workflow_id = ?", (workflow_id,))
            connection.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))
        return True

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
                current.picture = member.picture
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

    def get_member_by_email(self, email: str) -> TeamMember | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM team_members WHERE email = ?", (email,)
            ).fetchone()
        return TeamMember.model_validate_json(row["payload"]) if row else None

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

    def save_task(self, task: WorkflowTask) -> WorkflowTask:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO workflow_tasks (id, workflow_id, run_id, status, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    task.id,
                    task.workflow_id,
                    task.run_id,
                    task.status,
                    task.model_dump_json(),
                    task.created_at.isoformat(),
                ),
            )
        return task

    def list_tasks(self, workflow_id: str | None = None) -> list[WorkflowTask]:
        query = "SELECT payload FROM workflow_tasks"
        params: tuple[str, ...] = ()
        if workflow_id:
            query += " WHERE workflow_id = ?"
            params = (workflow_id,)
        query += " ORDER BY created_at DESC"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [WorkflowTask.model_validate_json(row["payload"]) for row in rows]

    def save_integration(self, provider: str, config: dict[str, str]) -> datetime:
        updated_at = datetime.now(timezone.utc)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO integrations (provider, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (provider, json.dumps(config), updated_at.isoformat()),
            )
        return updated_at

    def get_integration(self, provider: str) -> dict[str, str]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM integrations WHERE provider = ?", (provider,)
            ).fetchone()
        if row is None:
            return {}
        payload = json.loads(row["payload"])
        return {str(key): str(value) for key, value in payload.items()}

    def integration_updated_at(self, provider: str) -> datetime | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT updated_at FROM integrations WHERE provider = ?", (provider,)
            ).fetchone()
        return datetime.fromisoformat(row["updated_at"]) if row else None

    def delete_integration(self, provider: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM integrations WHERE provider = ?", (provider,))
        return cursor.rowcount > 0

    def save_oauth_state(self, state: str, nonce: str, ttl_minutes: int = 10) -> None:
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO oauth_states (state, nonce, expires_at) VALUES (?, ?, ?)",
                (state, nonce, expires_at.isoformat()),
            )

    def consume_oauth_state(self, state: str) -> str | None:
        now = datetime.now(timezone.utc)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT nonce, expires_at FROM oauth_states WHERE state = ?", (state,)
            ).fetchone()
            connection.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
        if row is None or datetime.fromisoformat(row["expires_at"]) <= now:
            return None
        return str(row["nonce"])

    def create_session(self, user_id: str, ttl_days: int = 14) -> str:
        token = secrets.token_urlsafe(48)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=ttl_days)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO auth_sessions (token, user_id, expires_at, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (token, user_id, expires_at.isoformat(), now.isoformat()),
            )
        return token

    def user_for_session(self, token: str | None) -> TeamMember | None:
        if not token:
            return None
        now = datetime.now(timezone.utc)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT user_id, expires_at FROM auth_sessions WHERE token = ?", (token,)
            ).fetchone()
            if row and datetime.fromisoformat(row["expires_at"]) <= now:
                connection.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))
                row = None
        return self.get_member(row["user_id"]) if row else None

    def delete_session(self, token: str | None) -> None:
        if not token:
            return
        with self._connect() as connection:
            connection.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))
