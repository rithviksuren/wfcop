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
