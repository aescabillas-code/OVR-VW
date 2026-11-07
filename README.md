# HPE OVR-VW Operations Workspace — Streamlit + MongoDB + REST API

The workspace (`frontend/index.html`, your *new sound* HTML plus a small save layer) is hosted inside Streamlit.
Everything entered in it is written to MongoDB and restored on reload. A FastAPI REST API reads and writes the
same collections, and its changes show up live in every open browser.

## Database connection (Streamlit secrets)

Put the connection in `.streamlit/secrets.toml` (or the Secrets box on Streamlit Community Cloud). Secrets win over environment variables:

```toml
MONGO_URI = "mongodb+srv://<user>:<password>@<cluster-host>/?retryWrites=true&w=majority"
MONGO_DB  = "ovrvw"          # optional: a database name in the URI path is used if this is missing
```
A `[mongo]` / `[mongodb]` section works too (`uri`, `connection_string`, or `host` + `username` + `password`, and `database` / `db_name`).
The sidebar shows which source was used. If the database cannot be reached, a banner appears in the workspace
(`⚠ MongoDB not connected — data is NOT saved`) instead of silently keeping data in memory.

## Run

```bash
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"      # default; add credentials / Atlas URI as needed
export MONGO_DB="ovrvw"                           # default
streamlit run app.py                              # UI on :8501, REST API on :8000 (docs at /docs)
```
MongoDB quick start: `docker run -d -p 27017:27017 -v ovrvw:/data/db mongo:7`

If MongoDB cannot be reached, the app still opens but shows a warning and keeps data in memory only
(nothing is saved). The “Saved to MongoDB” badge is hidden; the bottom-left badge only appears when something is wrong (`⚠ Not connected` or `❌ Storage error`) so unsaved work is never silent.

| Env var | Purpose | Default |
|---|---|---|
| `MONGO_URI`, `MONGO_DB` | database | `mongodb://localhost:27017`, `ovrvw` |
| `API_PORT`, `API_HOST` | where the API listens | `8000`, `0.0.0.0` |
| `API_PUBLIC_URL` | link shown in the sidebar | `http://localhost:8000` |
| `OVRVW_API_KEY` | if set, every `/api/*` call needs header `X-API-Key` | off |
| `API_CORS_ORIGINS` | comma-separated origins | `*` |

Run the API on its own (no Streamlit): `uvicorn api:app_factory --factory --host 0.0.0.0 --port 8000`

## What is stored (MongoDB database `ovrvw`)

| Collection | Content |
|---|---|
| `cases` | one document per case, active and closed (`bucket`), with checklist and reminders, case history, collaboration, station history, attachment metadata |
| `attachments` | file contents (data URLs) keyed `<caseId>\|<attachmentId>` — files over 8 MB are refused |
| `settings` | `set` (preferences, SLA minutes per station, notification options, simulation user/clock, export and import setup), `rules` (alert rules), `staff` (7-day shift schedule: SUN–SAT time / OFF / PTO per person), `simev` (simulation timeline), `theme` (shared theme), `ann` (important communications), `acks` (who acknowledged what) |
| `tombstones`, `meta` | deletions and the revision counter that powers live sync |

Every write carries `_rev`, `_ts` and `_cid`. Each browser sends its changes (about once a second), the server
writes them, and returns anything new from other browsers or the REST API.

## REST API (`/docs` has the full interactive reference)

| Area | Endpoints |
|---|---|
| system | `GET /health`, `GET /api/state`, `PUT /api/state` (restore), `GET /api/stats`, `GET /api/history?case=` |
| cases | `GET/POST /api/cases`, `GET/PUT/PATCH/DELETE /api/cases/{id}`, `POST …/close`, `POST …/transfer` |
| history, notes | `GET/POST /api/cases/{id}/history`, `POST …/notes` |
| collaboration | `GET/POST /api/cases/{id}/collaboration` |
| checklist, reminders | `GET/POST …/checklist`, `PATCH/DELETE …/checklist/{item}`, `PUT …/checklist/{item}/reminder`, `POST …/reminder/complete` |
| attachments | `GET/POST …/attachments`, `GET …/attachments/{id}`, `GET …/download`, `DELETE …/attachments/{id}` |
| settings | `GET/PUT /api/settings` |
| alert rules | `GET/PUT/POST /api/rules`, `PUT/DELETE /api/rules/{id}` |
| shift schedule | `GET/PUT/POST /api/staff`, `DELETE /api/staff/{name}` |
| simulation | `GET/PUT /api/simulation` |

```bash
curl -X POST localhost:8000/api/cases -H 'Content-Type: application/json' \
  -d '{"subject":"Store network outage","account":"H&M","station":"CARE","assignee":"June John Cruz","isHighPriority":true}'
```

## Tests
`python -m unittest discover -s tests -v` — sync protocol and REST service logic against the in-memory store (no MongoDB needed).

## Notes
* Running cases tick in each browser; the database stores the last known elapsed seconds plus the write time, and
  a reloading browser adds the time that passed. Alerts that came due while nobody had the page open are marked
  as seen instead of popping all at once.
* Live simulation timers (the scripted events) are not stored; the cases and alerts they produced are.
* Several people can use the app at once; concurrent edits to the *same case* are last-write-wins.
