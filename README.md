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
| `settings` | `profiles` (profile passwords, added HPE-email profiles, their caricature photos and personal themes), `set` (preferences, SLA minutes per station, notification options, simulation user/clock, export and import setup), `rules` (alert rules), `staff` (7-day shift schedule: SUN–SAT time / OFF / PTO per person), `simev` (simulation timeline), `theme` (shared theme), `ann` (important communications), `acks` (who acknowledged what) |
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

## Profiles, passwords and access
* The 8 staff profiles on the login picture all have **admin (all) access**. Each can set a password in *Settings → Profile*; once set, it is asked every time that profile is selected.
* **There are only two ways to get a profile.** (1) Vendor management team: the profile is created automatically when the person is added to the shift schedule. (2) Everyone else: **Sign up** on the login page (full name, HPE email `@hpe.com`, password; no admin approval), then sign in with *Sign in with HPE email*. A name that is on the shift schedule cannot sign up; those people click their photo. Admins no longer add profiles by hand.
* Added profiles can view and edit cases, acknowledge alerts, alarms and announcements, respond to messages and mentions, and change their own theme (stored per profile). They do not see Settings tabs other than *Theme* and *Profile*, cannot publish announcements, import/export, run the simulation or touch alert rules, SLA or the shift schedule, and are not automatically part of the shift schedule (adding a person to the shift schedule is what gives a matching profile its photo slot).
* **Photo slots are only for the vendor management team = the people on the shift schedule.** As soon as someone is added to the shift schedule (Settings → Shift schedule, Excel import, or `/api/staff`), a profile is created for them and their photo slot appears on the login picture straight away (first 6 slots on the picture). Clicking a photo (a slot or one of the 8 staff faces) asks whether you want to set a password; once a password is set, clicking that photo shows a password verification modal before the profile opens. After that the person is asked to add a photo (caricature, original not stored). The password can be changed in *Settings → Profile*. Renaming someone in the schedule renames their profile; removing someone removes the slot (an unclaimed auto-created profile is removed too, a profile with a password is kept).
* **Existing accounts:** an admin can make an already-created profile part of the vendor management team in *Settings → Profile → Profiles → Make vendor team member*. This adds the person to the shift schedule (default 09:00–17:00, rest SAT/SUN, editable) and gives them a photo slot. Profiles not on the schedule have no photo slot.
* On phones the picture itself is the menu: tap a face or a slot to open that profile. The profile list under the picture was removed; *Sign in with HPE email* and *Sign up* stay.
* The signed-in profile name is shown beside the gear button.
* Passwords are stored as salted, iterated SHA-256 hashes inside the `profiles` setting and are checked in the browser. This keeps casual access out, but it is not server-side authentication: anyone who can read the database or `/api/state` can read the hashes.
* Each browser now keeps its own signed-in identity (it is no longer overwritten by other browsers).
