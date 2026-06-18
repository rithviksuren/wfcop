# FlowMind

AI workflow copilot with a FastAPI backend and a plain JavaScript frontend.

## Run

Open two terminals in the project folder.

Terminal 1 - backend:

```bash
python main.py
```

Terminal 2 - frontend:

```bash
npm start
```

Then open `http://127.0.0.1:3000`.

That is the normal startup flow. There is no virtual environment to activate,
and the frontend has no npm packages to install. On its first run, `main.py`
installs missing Python packages into `.python_packages` inside this project.

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

## URLs

- App: `http://127.0.0.1:3000`
- Backend: `http://127.0.0.1:8000`
- API docs: `http://127.0.0.1:8000/docs`
- Health check: `http://127.0.0.1:8000/health`

## Tests

After the backend has completed its first-run package installation:

```bash
python main.py --test
```

The backend can still serve the frontend directly at
`http://127.0.0.1:8000`, but `npm start` is the intended frontend command.
