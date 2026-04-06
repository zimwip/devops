# Platform Dashboard

Web UI + REST API for the microservices platform.

## Architecture

```
dashboard/
├── backend/
│   └── app.py          # FastAPI server (serves both API and built React frontend)
└── frontend/
    ├── src/App.jsx      # React single-page application
    ├── package.json
    └── vite.config.js
```

## Quick start (development)

**Terminal 1 — API backend:**
```bash
cd platform/
pip install -r scripts/requirements.txt

# From the backend directory (so uvicorn resolves imports correctly)
cd dashboard/backend
uvicorn app:app --reload --port 5173
# → API: http://localhost:5173
# → Swagger UI: http://localhost:5173/docs
# → ReDoc: http://localhost:5173/redoc
```

**Terminal 2 — React frontend (hot-reload):**
```bash
cd platform/dashboard/frontend
npm install
npm run dev
# → UI: http://localhost:5174  (proxies /api to :5173)
```

## Production build (serve everything from FastAPI)

```bash
cd platform/dashboard/frontend
npm run build          # → dist/

cd ../backend
uvicorn app:app --host 0.0.0.0 --port 5173
# → UI + API both on http://localhost:5173
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `5173` | Server port |
| `RELOAD` | `1` | Enable uvicorn hot-reload (`0` to disable in prod) |
| `GITHUB_TOKEN` | — | GitHub API access for service/release operations |
| `JENKINS_USER` | — | Jenkins API user |
| `JENKINS_TOKEN` | — | Jenkins API token |

## API reference

All endpoints are documented at `/docs` (Swagger UI) when the server is running.

| Method | Path | Description |
|---|---|---|
| GET | `/api/envs` | List all environments |
| GET | `/api/envs/{name}` | Environment details |
| POST | `/api/envs` | Create POC environment |
| DELETE | `/api/envs/{name}` | Destroy POC environment |
| GET | `/api/envs/{a}/diff/{b}` | Version diff between envs |
| GET | `/api/services` | List all services |
| GET | `/api/services/{name}` | Service version matrix |
| POST | `/api/services` | Bootstrap new service |
| POST | `/api/deploy` | Trigger deployment |
| GET | `/api/templates` | List scaffold templates |
