"""REST API (FastAPI) for the OVR-VW Operations Workspace. All data lives in MongoDB (see store.py).

Run standalone:   uvicorn api:app_factory --factory --host 0.0.0.0 --port 8000
or let app.py start it in a background thread next to Streamlit.
Interactive docs: http://localhost:8000/docs
Optional auth:    set OVRVW_API_KEY and send the header  X-API-Key: <key>
"""
import base64
import os
import re
import socket
import threading
import time
from typing import Any, Dict, List, Optional

import service as S
from store import SETTING_KEYS


def create_app(store):
    from fastapi import APIRouter, Body, Depends, FastAPI, Header, HTTPException, Query, Response
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse

    app = FastAPI(title="OVR-VW Operations Workspace API", version="1.0.0",
                  description="Cases, checklists, reminders, attachments, settings, alert rules, shift schedule and history. "
                              "Writes are picked up live by every open Streamlit session.")
    origins = [o.strip() for o in os.getenv("API_CORS_ORIGINS", "*").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(S.NotFound)
    async def _nf(_, exc):  # noqa: ANN001
        return JSONResponse(status_code=404, content={"detail": f"not found: {exc.args[0] if exc.args else ''}"})

    @app.exception_handler(S.Invalid)
    async def _inv(_, exc):  # noqa: ANN001
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    def auth(x_api_key: Optional[str] = Header(None)) -> None:
        key = os.getenv("OVRVW_API_KEY")
        if key and x_api_key != key:
            raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")

    @app.get("/health", tags=["system"])
    def health() -> Dict[str, Any]:
        return {"status": "ok", "storage": store.kind, "detail": store.describe(), "rev": store.current_rev()}

    api = APIRouter(prefix="/api", dependencies=[Depends(auth)])
    B = lambda: Body(default_factory=dict)  # noqa: E731

    # ------------------------------------------------------------------ state
    @api.get("/state", tags=["system"])
    def get_state() -> Dict[str, Any]:
        return {"rev": store.current_rev(), "cases": [S.with_live_sla(c) for c in store.all_cases()],
                "settings": {k: store.get_setting(k) for k in SETTING_KEYS}}

    @api.put("/state", tags=["system"])
    def put_state(payload: Dict[str, Any] = B()) -> Dict[str, Any]:
        """Replace everything (restore a backup). Connected browsers update within ~2 seconds."""
        for c in store.all_cases():
            store.del_case(c["id"], "api")
        for c in payload.get("cases", []):
            c = {k: v for k, v in c.items() if k not in ("_rev", "_ts", "slaSecondsNow", "durationNow")}
            c.setdefault("bucket", "closed" if c.get("status") == "CLOSED" else "active")
            store.put_case(c, "api")
        for k, v in (payload.get("settings") or {}).items():
            if k in SETTING_KEYS and v is not None:
                store.put_setting(k, v, "api")
        return {"cases": len(payload.get("cases", [])), "rev": store.current_rev()}

    @api.get("/stats", tags=["system"])
    def get_stats() -> Dict[str, Any]:
        return S.stats(store)

    @api.get("/history", tags=["history"])
    def get_history(case: Optional[str] = None, limit: int = Query(200, le=2000)) -> List[Dict]:
        return S.history_all(store, case, limit)

    # ------------------------------------------------------------------ cases
    @api.get("/cases", tags=["cases"])
    def list_cases(bucket: Optional[str] = Query(None, pattern="^(active|closed)$"), station: Optional[str] = None,
                   assignee: Optional[str] = None, priority: Optional[str] = None, q: Optional[str] = None,
                   skip: int = 0, limit: int = Query(200, le=1000)) -> Dict[str, Any]:
        return S.list_cases(store, bucket, station, assignee, priority, q, skip, limit)

    @api.post("/cases", status_code=201, tags=["cases"])
    def create_case(payload: Dict[str, Any] = B()) -> Dict:
        """Body: subject (required), account, station, assignee, priority, isHighPriority, id (optional), createdBy."""
        return S.create_case(store, payload)

    @api.get("/cases/{case_id}", tags=["cases"])
    def get_case(case_id: str) -> Dict:
        return S.with_live_sla(S.get_case(store, case_id))

    @api.put("/cases/{case_id}", tags=["cases"])
    def replace_case(case_id: str, payload: Dict[str, Any] = B()) -> Dict:
        return S.update_case(store, case_id, payload, replace=True)

    @api.patch("/cases/{case_id}", tags=["cases"])
    def patch_case(case_id: str, payload: Dict[str, Any] = B()) -> Dict:
        return S.update_case(store, case_id, payload)

    @api.delete("/cases/{case_id}", status_code=204, tags=["cases"])
    def delete_case(case_id: str) -> Response:
        S.get_case(store, case_id)
        store.del_case(case_id, "api")
        return Response(status_code=204)

    @api.post("/cases/{case_id}/close", tags=["cases"])
    def close_case(case_id: str, payload: Dict[str, Any] = B()) -> Dict:
        return S.close_case(store, case_id, payload.get("summary", ""), payload.get("by", "API"))

    @api.post("/cases/{case_id}/transfer", tags=["cases"])
    def transfer_case(case_id: str, payload: Dict[str, Any] = B()) -> Dict:
        return S.transfer_case(store, case_id, payload.get("station", ""), payload.get("by", "API"))

    @api.get("/cases/{case_id}/history", tags=["history"])
    def case_history(case_id: str) -> List[Dict]:
        return S.get_case(store, case_id).get("caseHistoryLog", [])

    @api.post("/cases/{case_id}/history", status_code=201, tags=["history"])
    def add_history(case_id: str, payload: Dict[str, Any] = B()) -> Dict:
        c = S.get_case(store, case_id)
        S.add_history(c, payload.get("action", "Action"), payload.get("by", "API"), payload.get("details", ""))
        return S.save(store, c)

    @api.post("/cases/{case_id}/notes", status_code=201, tags=["history"])
    def add_note(case_id: str, payload: Dict[str, Any] = B()) -> Dict:
        if not payload.get("text"):
            raise S.Invalid("text is required")
        return S.add_note(store, case_id, payload["text"], payload.get("by", "API"))

    @api.get("/cases/{case_id}/collaboration", tags=["collaboration"])
    def get_collab(case_id: str) -> List[Dict]:
        return S.get_case(store, case_id).get("collaboration") or []

    @api.post("/cases/{case_id}/collaboration", status_code=201, tags=["collaboration"])
    def post_collab(case_id: str, payload: Dict[str, Any] = B()) -> Dict:
        if not payload.get("text") or not payload.get("user"):
            raise S.Invalid("user and text are required")
        return S.add_collab(store, case_id, payload["user"], payload["text"], payload.get("role", "Support"))

    # ------------------------------------------------------------------ checklist & reminders
    @api.get("/cases/{case_id}/checklist", tags=["checklist"])
    def get_checklist(case_id: str) -> List[Dict]:
        return S.get_case(store, case_id).get("checklist", [])

    @api.post("/cases/{case_id}/checklist", status_code=201, tags=["checklist"])
    def add_item(case_id: str, payload: Dict[str, Any] = B()) -> Dict:
        if not payload.get("text"):
            raise S.Invalid("text is required")
        return S.checklist_add(store, case_id, payload["text"], payload.get("by", "API"))

    @api.patch("/cases/{case_id}/checklist/{item_id}", tags=["checklist"])
    def patch_item(case_id: str, item_id: str, payload: Dict[str, Any] = B()) -> Dict:
        return S.checklist_patch(store, case_id, item_id, payload, payload.get("by", "API"))

    @api.delete("/cases/{case_id}/checklist/{item_id}", tags=["checklist"])
    def del_item(case_id: str, item_id: str, by: str = "API") -> Dict:
        return S.checklist_delete(store, case_id, item_id, by)

    @api.put("/cases/{case_id}/checklist/{item_id}/reminder", tags=["checklist"])
    def set_reminder(case_id: str, item_id: str, payload: Dict[str, Any] = B()) -> Dict:
        """Body: eta (epoch ms), reason, by. The alarm rings 5 minutes before eta."""
        if not payload.get("eta"):
            raise S.Invalid("eta (epoch milliseconds) is required")
        return S.reminder_set(store, case_id, item_id, int(payload["eta"]), payload.get("reason", ""), payload.get("by", "API"))

    @api.post("/cases/{case_id}/checklist/{item_id}/reminder/complete", tags=["checklist"])
    def complete_reminder(case_id: str, item_id: str, payload: Dict[str, Any] = B()) -> Dict:
        return S.reminder_complete(store, case_id, item_id, payload.get("documentation", ""), payload.get("by", "API"))

    # ------------------------------------------------------------------ attachments
    @api.get("/cases/{case_id}/attachments", tags=["attachments"])
    def list_atts(case_id: str) -> List[Dict]:
        return S.get_case(store, case_id).get("attachments", [])

    @api.post("/cases/{case_id}/attachments", status_code=201, tags=["attachments"])
    def add_att(case_id: str, payload: Dict[str, Any] = B()) -> Dict:
        """Body: name, data (base64 or data URL), by, desc."""
        if not payload.get("name") or not payload.get("data"):
            raise S.Invalid("name and data are required")
        return S.attachment_add(store, case_id, payload["name"], payload["data"], payload.get("by", "API"), payload.get("desc", ""))

    @api.get("/cases/{case_id}/attachments/{att_id}", tags=["attachments"])
    def att_meta(case_id: str, att_id: str) -> Dict:
        return S.attachment_get(store, case_id, att_id)[0]

    @api.get("/cases/{case_id}/attachments/{att_id}/download", tags=["attachments"])
    def att_download(case_id: str, att_id: str) -> Response:
        meta, data = S.attachment_get(store, case_id, att_id)
        if not data:
            raise S.NotFound("no file content stored for this attachment")
        head, raw = data.split(",", 1) if data.startswith("data:") else ("data:application/octet-stream;base64", data)
        mime = head[5:].split(";")[0] or "application/octet-stream"
        return Response(content=base64.b64decode(raw), media_type=mime,
                        headers={"Content-Disposition": f'attachment; filename="{meta["name"]}"'})

    @api.delete("/cases/{case_id}/attachments/{att_id}", tags=["attachments"])
    def att_delete(case_id: str, att_id: str, by: str = "API") -> Dict:
        return S.attachment_delete(store, case_id, att_id, by)

    # ------------------------------------------------------------------ settings, alert rules, shifts, simulation
    @api.get("/settings", tags=["settings"])
    def get_settings() -> Any:
        return S.get_set(store, "set", {})

    @api.put("/settings", tags=["settings"])
    def put_settings(payload: Dict[str, Any] = B()) -> Any:
        store.put_setting("set", payload, "api")
        return payload

    def _list(key: str) -> List[Dict]:
        return S.get_set(store, key, {"list": []}).get("list", [])

    @api.get("/rules", tags=["alert rules"])
    def get_rules() -> List[Dict]:
        return _list("rules")

    @api.put("/rules", tags=["alert rules"])
    def put_rules(payload: List[Dict[str, Any]] = Body(...)) -> List[Dict]:
        store.put_setting("rules", {"list": payload}, "api")
        return payload

    @api.post("/rules", status_code=201, tags=["alert rules"])
    def add_rule(payload: Dict[str, Any] = B()) -> Dict:
        rules = _list("rules")
        payload["id"] = max([r.get("id", 0) for r in rules] + [0]) + 1
        rules.append(payload)
        store.put_setting("rules", {"list": rules}, "api")
        return payload

    @api.put("/rules/{rule_id}", tags=["alert rules"])
    def put_rule(rule_id: int, payload: Dict[str, Any] = B()) -> Dict:
        rules = _list("rules")
        for i, r in enumerate(rules):
            if r.get("id") == rule_id:
                rules[i] = {**payload, "id": rule_id}
                store.put_setting("rules", {"list": rules}, "api")
                return rules[i]
        raise S.NotFound(rule_id)

    @api.delete("/rules/{rule_id}", status_code=204, tags=["alert rules"])
    def del_rule(rule_id: int) -> Response:
        rules = _list("rules")
        if not any(r.get("id") == rule_id for r in rules):
            raise S.NotFound(rule_id)
        store.put_setting("rules", {"list": [r for r in rules if r.get("id") != rule_id]}, "api")
        return Response(status_code=204)

    @api.get("/staff", tags=["shift schedule"])
    def get_staff() -> List[Dict]:
        return _list("staff")

    @api.put("/staff", tags=["shift schedule"])
    def put_staff(payload: List[Dict[str, Any]] = Body(...)) -> List[Dict]:
        """Replace the shift schedule. One entry per person with a 7-day grid: {name, role, sch:[SUN..SAT]} where each
        day is "HH:MM-HH:MM", "OFF" or "PTO". Entries that only have {start:"HH:MM", end:"HH:MM", rest:[0-6]} are still
        accepted; the browser converts them to the 7-day grid."""
        store.put_setting("staff", {"list": payload}, "api")
        return payload

    @api.post("/staff", status_code=201, tags=["shift schedule"])
    def add_staff(payload: Dict[str, Any] = B()) -> Dict:
        for k in ("name", "start", "end"):
            if not payload.get(k):
                raise S.Invalid(f"{k} is required")
        staff = [p for p in _list("staff") if p.get("name") != payload["name"]]
        payload.setdefault("role", "Support")
        staff.append(payload)
        store.put_setting("staff", {"list": staff}, "api")
        return payload

    @api.delete("/staff/{name}", status_code=204, tags=["shift schedule"])
    def del_staff(name: str) -> Response:
        staff = _list("staff")
        if not any(p.get("name") == name for p in staff):
            raise S.NotFound(name)
        store.put_setting("staff", {"list": [p for p in staff if p.get("name") != name]}, "api")
        return Response(status_code=204)

    # ------------------------------------------------------------------ theme and important communication
    @api.get("/theme", tags=["theme"])
    def get_theme() -> Any:
        """Shared theme shown on every screen: {"theme": {p, c, font, size, ...}} or {"theme": null} for the default."""
        return S.get_set(store, "theme", {"theme": None})

    @api.put("/theme", tags=["theme"])
    def put_theme(payload: Dict[str, Any] = B()) -> Any:
        store.put_setting("theme", {"theme": payload.get("theme", payload) or None}, "api")
        return S.get_set(store, "theme", {"theme": None})

    @api.get("/announcements", tags=["important communication"])
    def get_announcements() -> List[Dict]:
        return _list("ann")

    @api.post("/announcements", status_code=201, tags=["important communication"])
    def post_announcement(payload: Dict[str, Any] = B()) -> Dict:
        """Publish an important communication. It pops up (with sound) on the screens of the recipients until acknowledged.
        Body: {title, msg, by, to:"all"|[names], cases:[{id,no,type}]}"""
        for k in ("title", "by"):
            if not payload.get(k):
                raise S.Invalid(f"{k} is required")
        if not payload.get("msg"):
            raise S.Invalid("msg is required")
        to = payload.get("to") or "all"
        if to != "all" and not (isinstance(to, list) and to):
            raise S.Invalid('to must be "all" or a non-empty list of names')
        ts = int(time.time() * 1000)
        a = {"id": S.uid(), "title": payload["title"], "msg": payload["msg"], "to": to, "by": payload["by"],
             "byKey": re.sub(r"[^A-Za-z0-9_.-]", "_", "api:" + payload["by"]), "at": S.stamp(ts), "ts": ts,
             "mentions": payload.get("mentions") or [], "cases": payload.get("cases") or [], "imgs": []}
        store.put_setting("ann", {"list": ([a] + _list("ann"))[:60]}, "api")
        return a

    @api.get("/announcements/acknowledgements", tags=["important communication"])
    def get_acks() -> List[Dict]:
        return _list("acks")

    @api.get("/simulation", tags=["simulation"])
    def get_sim() -> List[Dict]:
        return _list("simev")

    @api.put("/simulation", tags=["simulation"])
    def put_sim(payload: List[Dict[str, Any]] = Body(...)) -> List[Dict]:
        store.put_setting("simev", {"list": payload}, "api")
        return payload

    app.include_router(api)
    return app


def app_factory():
    from store import make_store
    store, err = make_store()
    if err:
        print("WARNING:", err)
    return create_app(store)


def start_background(store, host: Optional[str] = None, port: Optional[int] = None) -> Dict[str, Any]:
    """Serve the API from a daemon thread inside the Streamlit process."""
    host = host or os.getenv("API_HOST", "0.0.0.0")
    port = int(port or os.getenv("API_PORT", "8000"))
    public = os.getenv("API_PUBLIC_URL", f"http://localhost:{port}")
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            return {"url": public, "started": False, "note": f"port {port} already in use — assuming the API is already running"}
    try:
        import uvicorn

        server = uvicorn.Server(uvicorn.Config(create_app(store), host=host, port=port, log_level="warning"))
        server.install_signal_handlers = lambda: None  # type: ignore[assignment]
        threading.Thread(target=server.run, daemon=True, name="ovrvw-api").start()
        return {"url": public, "started": True, "note": ""}
    except Exception as ex:  # noqa: BLE001
        return {"url": public, "started": False, "note": f"API not started: {ex}"}
