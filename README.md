# FlowMind

AI workflow copilot with a FastAPI backend and a plain JavaScript frontend.

## Run

For the simplest setup, run:

```bash
python main.py
```

Then open `http://127.0.0.1:8000`. The backend serves the current frontend
directly, so a second terminal is not required.

If you prefer the development proxy on port 3000, open two terminals in the
project folder.

Terminal 1 - backend:

```bash
python main.py
```

The development launcher enables backend auto-reload by default. Use
`python main.py --check` to verify that the complete Copilot API is loaded,
or `python main.py --no-reload` when running it under another process manager.

Terminal 2 - frontend:

```bash
npm start
```

Then open `http://127.0.0.1:3000`.

There is no virtual environment to activate manually. `main.py` automatically
uses the project's `.venv` when it exists. The frontend has no npm packages to
install.

If Windows PowerShell blocks `npm.ps1`, use the equivalent command:

```powershell
npm.cmd start
```

## Configuration

The backend reads `.env` automatically. An API key is optional:

```env
OPENAI_API_KEY=your-key
```

Without a key, the app uses its deterministic local AI provider. See
`.env.example` for optional host, port, and model settings.

### Google sign-in

Create an OAuth 2.0 Client ID with application type **Web application** in
Google Cloud, configure the consent screen with the app name **FlowMind**, and
add this authorized redirect URI:

```text
http://127.0.0.1:3000/auth/google/callback
```

Then add the credentials to `.env`:

```env
GOOGLE_OAUTH_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_OAUTH_CLIENT_SECRET=your-client-secret
APP_BASE_URL=http://127.0.0.1:3000
```

For production, use your HTTPS application URL and set
`APP_COOKIE_SECURE=true`.

Workflow runs execute real node behavior. Open **Integrations** in the app to
add Gmail, Slack, Microsoft Teams, or Notion connection details. Saved secrets
are masked in API responses and are never sent back to the browser. Environment
variables remain available as a fallback. A manual run can also supply an
`email`, `event`, or webhook payload through the API. Tasks and in-app reminders
are persisted locally and are available from `GET /tasks`.

## Workflow intelligence architecture

FlowMind does not create a workflow immediately from the first prompt. The
creation experience uses a reviewable pipeline:

1. **Natural-language extraction** converts the prompt into a trigger, ordered
   tasks, and business goal.
2. **Intent understanding** classifies industry, workflow type, priority, and
   required applications.
3. **RAG retrieval** searches the built-in `flowmind-proven-workflows`
   knowledge base and returns ranked, proven workflow recommendations.
4. **Workflow planning** creates a proposed node graph using the extracted
   requirements and retrieved guidance.
5. **Human approval** is required before `POST /copilot/build` persists the
   workflow.

`POST /copilot/analyze` performs the first four read-only stages. Its response
includes the structured extraction, intent profile, recommendations, retrieval
context, proposed workflow, and missing app connections.

## Copilot API

All Copilot routes require an authenticated session. During local automated
testing, `X-User-Id` can be enabled with `AUTH_ALLOW_DEV_HEADER=1`.

- `POST /copilot/create` — create, validate, own, and persist a workflow.
- `POST /copilot/modify` — update an existing workflow and return graph operations.
- `POST /copilot/fix` — repair validation errors using workflow context.
- `POST /copilot/explain` — return a safe, graph-aware explanation.

Create request:

```json
{
  "instruction": "When I receive an email from Stripe, send a Slack message to the finance team.",
  "context": {}
}
```

Modify and explain requests use:

```json
{
  "workflow": {"name": "Existing workflow", "nodes": [], "edges": []},
  "instruction": "Also create a Notion page whenever an email arrives.",
  "context": {}
}
```

Fix accepts canonical validation errors as well as the concise external shape:

```json
{
  "workflow": {"name": "Existing workflow", "nodes": [], "edges": []},
  "instruction": "Fix the workflow.",
  "validation_errors": [
    {"node": "slack_message", "error": "channel_id missing"}
  ]
}
```

Successful create requests return `201`; modify, fix, and explain return `200`.
Invalid requests return `422`, unauthenticated requests return `401`, and an
unavailable AI provider returns `502`. Interactive OpenAPI documentation is at
`http://127.0.0.1:8000/docs`.

## Workflow validation

Every workflow is validated before it is persisted or executed. The validation
layer checks:

- required node configuration, including Gmail sender criteria and Slack
  channel/message fields;
- known node types and correct trigger, condition, or action roles;
- existing edge endpoints, unique connections, and no self-connections;
- exactly one trigger, reachability from that trigger, and no graph cycles;
- required schedules and supported configuration values.

Validation errors contain stable codes plus node, field, or edge context. The
Copilot sends these errors back through its repair pass. Safe deterministic
repairs fill catalog defaults, remove invalid connections, restore node roles,
and reconnect orphaned actions without inventing new workflow steps.

## Persistence

FlowMind stores workflows in SQLite by default at `data/workflows.sqlite3`.
Set `FLOWMIND_DATABASE_PATH` to use a different file:

```env
FLOWMIND_DATABASE_PATH=data/workflows.sqlite3
```

The database is initialized automatically and uses foreign-key enforcement,
write-ahead logging, busy timeouts, transactions, and indexes for workflow,
run, task, and session retrieval. Workflow payloads retain the complete typed
graph while indexed columns support ordered listing.

Persistence APIs include:

- `POST /copilot/create` or `POST /workflows` to store workflows;
- `GET /workflows` to list visible workflows;
- `GET /workflows/{workflow_id}` to retrieve one workflow;
- `PATCH /workflows/{workflow_id}` to update it;
- `DELETE /workflows/{workflow_id}` to remove it and related run data.

Visibility and workflow permissions are enforced during retrieval and mutation.

## AI integration

When `OPENAI_API_KEY` is configured, FlowMind uses OpenAI as its primary LLM
provider and retains the deterministic planner only as a resilience fallback.
The integration uses the Responses API with a strict JSON Schema contract.

```env
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5.5
OPENAI_REASONING_EFFORT=low
OPENAI_TIMEOUT_SECONDS=45
OPENAI_MAX_RETRIES=2
OPENAI_MAX_TOOL_ROUNDS=2
```

The LLM architecture includes:

- **Outcome-first prompt design:** static product rules and success criteria are
  sent as developer instructions; the dynamic operation and workflow context
  are sent separately for prompt-cache friendliness.
- **Structured outputs:** the model must return a schema-constrained workflow
  and explanation. Unknown node types, invalid enums, extra response fields,
  and malformed JSON are rejected.
- **Tool usage:** the model can call `search_nodes` to inspect supported nodes,
  `validate_workflow` to test a candidate graph, and `get_workflow` to retrieve
  authorized persisted context. All tools are read-only, strict, and executed
  by the application.
- **Validation loops:** generated workflows pass through the application
  validator. Errors are returned to the provider in a bounded repair attempt,
  followed by deterministic safe repair and final validation.
- **Error handling:** authentication, rate limiting, transport timeouts,
  incomplete responses, refusals, malformed output, and excessive tool rounds
  have typed failures. Transient failures use bounded exponential retries.
- **Reliability:** requests have timeouts and output-token limits, tool rounds
  are bounded, API keys are never placed in prompts, responses are not stored
  by OpenAI, and the local fallback keeps core workflow creation available.
- **Observability:** request IDs, response IDs, tool names, and token usage are
  retained on the provider and logged without prompt contents or credentials.

The implementation follows OpenAI's current guidance for the
[Responses API](https://developers.openai.com/api/reference/resources/responses/methods/create),
[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs),
and [function calling](https://developers.openai.com/api/docs/guides/function-calling).

### Copilot tools

The OpenAI request uses `tool_choice: auto`, allowing the model to decide
whether it needs no tools, one tool, or several tools before returning its
structured workflow:

- `search_nodes(query, roles, limit)` — semantically ranks supported triggers,
  conditions, and actions and returns their roles, required configuration, and
  defaults;
- `validate_workflow(workflow)` — runs the same application validation layer
  used before persistence and returns stable error codes with node/field/edge
  context;
- `get_workflow(workflow_id)` — retrieves a persisted workflow only when the
  current authenticated user has access.

All tools are strict, read-only, and application-executed. Tool calls may be
parallel within a round and may continue for a bounded number of rounds.
`get_workflow` receives authorization through request-scoped server context;
that context is removed before the prompt is sent to OpenAI. Tool results also
filter token, secret, password, webhook, service-account, and API-key fields.

## Workflow diffing

FlowMind supports compact, reversible workflow patches. Existing full-workflow
responses remain available for compatibility, while operation-first clients can
use:

- `POST /copilot/modify/operations` — apply a natural-language modification,
  persist it, and return only operations and version metadata;
- `POST /workflows/diff` — preview operations between two workflow payloads
  without persisting them;
- `PATCH /workflows/{workflow_id}/operations` — apply operations with an
  `expected_version` optimistic-concurrency check.

Example response:

```json
{
  "workflow_id": "wf_123",
  "base_version": 1,
  "target_version": 2,
  "persisted": true,
  "operations": [
    {
      "op": "add_node",
      "node": {
        "id": "node_3",
        "type": "notion_create_page"
      }
    },
    {
      "op": "connect_nodes",
      "edge": {
        "from": "node_1",
        "to": "node_3"
      }
    }
  ]
}
```

Supported operations are `update_workflow`, `add_node`, `update_node`,
`remove_node`, `connect_nodes`, and `disconnect_nodes`. Updates carry complete
typed node state, making patches deterministic and round-trip testable.
Conflicting versions return `409` instead of overwriting newer changes.

## Conversation memory

Workflow conversations persist prior instructions and validated workflow
snapshots in SQLite. A follow-up message therefore needs only the new
instruction; clients do not need to resend the current workflow.

- `POST /copilot/conversations` — start a conversation and create its workflow;
- `POST /copilot/conversations/{id}/messages` — continue from the latest
  persisted workflow state;
- `GET /copilot/conversations` — list the current user's conversations;
- `GET /copilot/conversations/{id}` — retrieve ordered turns and snapshots.

Example:

```text
Build an email notification workflow.
Actually send notifications to Teams instead.
```

The second turn replaces the existing Slack notification with Microsoft Teams
while preserving the email trigger, routing, and message context. Replacement
language such as `instead`, `replace`, `switch`, and `rather than` is grounded
against the prior graph; additive language remains additive.

Memory is:

- durable across application restarts;
- scoped to the authenticated owner;
- stored as ordered turns containing the instruction, workflow snapshot,
  operations, explanation, and provider;
- bounded before LLM calls to the six most recent compact turn summaries;
- independent of OpenAI-side response storage.

## Multi-step planning and streaming

Broad outcome requests use a durable planning state machine instead of jumping
straight to a guessed workflow:

1. understand the requested outcome;
2. identify decisions that materially change the graph;
3. ask focused clarifying questions;
4. compile answers into a concrete instruction;
5. generate and validate the workflow analysis.

For example, `Build a lead capture system.` asks how leads enter, where they
should be stored, and how the team should be notified. It does not invent those
choices.

Planning APIs:

- `POST /copilot/plans` — start a plan;
- `POST /copilot/plans/{id}/answers` — provide clarification answers;
- `GET /copilot/plans/{id}` — retrieve the durable plan state;
- `POST /copilot/plans/stream` — stream initial planning progress;
- `POST /copilot/plans/{id}/answers/stream` — stream generation after answers.

Streaming uses Server-Sent Events (`text/event-stream`) with named events:
`accepted`, `planning`, `clarification`, `analysis`, `validation`, `complete`,
and `error`. Every event contains JSON with a user-facing `message` and typed
`data`. Responses disable proxy buffering and caching so clients can render
progress as it occurs.

Planning sessions are owner-scoped, persist in SQLite, survive restarts, and
return proposed workflows that already pass the application validation layer.

## URLs

- App: `http://127.0.0.1:3000`
- Backend: `http://127.0.0.1:8000`
- API docs: `http://127.0.0.1:8000/docs`
- Health check: `http://127.0.0.1:8000/health`

The health response includes the API version and loaded feature flags. This
makes it easy to detect an older backend process after adding new capabilities.

## Tests

After the backend has completed its first-run package installation:

```bash
python main.py --test
```

The backend can still serve the frontend directly at
`http://127.0.0.1:8000`, but `npm start` is the intended frontend command.
