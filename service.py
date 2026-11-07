"""Domain/service layer used by the REST API (pure Python, no web framework)."""
import base64
import re
import time
from typing import Any, Dict, List, Optional

WORKFLOW = ["CARE", "ARCH", "PET", "SUPPLY CHAIN", "FULFILLMENT"]
DEFAULT_SLA = {"CARE": 15, "ARCH": 30, "PET": 45, "SUPPLY CHAIN": 30, "FULFILLMENT": 120}
PRIORITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
CHECKLIST = [
    "Confirm the HPE/Aruba product, model and serial number.",
    "Capture the exact alert, error message or customer symptom.",
    "Complete the applicable remote/basic troubleshooting checks.",
    "Verify support, entitlement or contract context when applicable.",
    "Record customer contact, site and required reference information.",
    "Document the assessment and recommended next action.",
]


class NotFound(KeyError):
    pass


class Invalid(ValueError):
    pass


def stamp(ms: Optional[int] = None) -> str:
    t = time.localtime((ms or int(time.time() * 1000)) / 1000)
    return time.strftime("%b %d, %Y %I:%M %p", t).replace(" 0", " ")


def fmt_dur(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def uid() -> str:
    return "i" + base64.b32encode(str(time.time_ns()).encode()).decode()[-7:].lower()


def station_sla(store, station: str) -> int:
    sla = ((store.get_setting("set") or {}).get("sla")) or {}
    return int(sla.get(station) or DEFAULT_SLA.get(station, 15))


def history_entry(action: str, by: str, details: str, status: str = "") -> Dict[str, str]:
    return {"date": stamp(), "action": action, "status": status, "performedBy": by, "details": details}


def add_history(case: Dict, action: str, by: str, details: str) -> None:
    case.setdefault("caseHistoryLog", []).insert(0, history_entry(action, by, details, case.get("status", "")))


def with_live_sla(case: Dict) -> Dict:
    """Active cases tick in the browser; expose the live value computed from the last write time."""
    out = dict(case)
    if out.get("bucket") == "active":
        age = max(0, int(time.time() * 1000) - int(out.get("_ts") or 0)) / 1000
        out["slaSecondsNow"] = int((out.get("slaSeconds") or 0) + age)
        out["durationNow"] = fmt_dur(out["slaSecondsNow"])
    return out


def get_case(store, case_id: str) -> Dict:
    c = store.get_case(case_id)
    if not c:
        raise NotFound(case_id)
    return c


def save(store, case: Dict, cid: str = "api") -> Dict:
    case.pop("_rev", None)
    case.pop("_ts", None)
    store.put_case(case, cid)
    return with_live_sla(get_case(store, case["id"]))


def next_case_id(store) -> str:
    n = 9000
    for c in store.all_cases():
        m = re.search(r"(\d+)$", c.get("id", ""))
        if m:
            n = max(n, int(m.group(1)))
    return f"CS-{time.strftime('%Y')}-{n + 1}"


def list_cases(store, bucket=None, station=None, assignee=None, priority=None, q=None, skip=0, limit=200) -> Dict:
    items = []
    for c in store.all_cases(bucket):
        if station and c.get("station") != station:
            continue
        if assignee and c.get("assignee") != assignee:
            continue
        if priority and (c.get("priority") or "").upper() != priority.upper():
            continue
        if q and q.lower() not in " ".join(str(c.get(k, "")) for k in ("id", "subject", "account", "assignee")).lower():
            continue
        items.append(with_live_sla(c))
    items.sort(key=lambda c: (c.get("bucket") != "active", -(c.get("_ts") or 0)))
    return {"total": len(items), "items": items[skip: skip + limit]}


def create_case(store, p: Dict[str, Any], cid: str = "api") -> Dict:
    station = p.get("station") or "CARE"
    if station not in WORKFLOW:
        raise Invalid(f"station must be one of {WORKFLOW}")
    if not p.get("subject"):
        raise Invalid("subject is required")
    cid_ = (p.get("id") or next_case_id(store)).strip()
    if store.get_case(cid_):
        raise Invalid(f"case {cid_} already exists")
    pri = (p.get("priority") or "MEDIUM").upper()
    hp = bool(p.get("isHighPriority")) or pri == "CRITICAL"
    sla_min = station_sla(store, station)
    by = p.get("createdBy") or "API"
    idx = WORKFLOW.index(station)
    hist = [{"station": s, "enteredAt": stamp(), "exitedAt": "-" if i == idx else stamp(),
             "duration": "00:00:00" if i == idx else fmt_dur(station_sla(store, s) * 60),
             "slaLimit": f"{station_sla(store, s)} minutes", "status": "In Progress" if i == idx else "Completed"}
            for i, s in enumerate(WORKFLOW[: idx + 1])]
    case = {
        "id": cid_, "subject": p["subject"], "account": p.get("account", ""), "station": station,
        "assignee": p.get("assignee", ""), "priority": "CRITICAL" if hp else (pri if pri in PRIORITIES else "MEDIUM"),
        "status": p.get("status") or "New", "duration": "00:00:00", "slaSeconds": int(p.get("slaSeconds") or 0),
        "isHighPriority": hp, "triggeredAlert60": False, "triggeredAlert80": False, "triggeredAlertBreach": False,
        "stationHistory": hist, "collaboration": p.get("collaboration"), "bucket": "active",
        "vendor": p.get("vendor", ""), "issue": p.get("issue", ""), "department": p.get("department", ""),
        "description": p.get("description", ""), "source": p.get("source") or "REST API", "createdAt": stamp(), "createdTs": int(time.time() * 1000), "updatedAt": stamp(),
        "caseHistoryLog": [history_entry("Case Created", by, "Case created through the REST API.", "New")], "simCase": False,
        "checklist": [{"id": uid() + str(i), "text": t, "completed": False} for i, t in enumerate(CHECKLIST)],
    }
    case["duration"] = fmt_dur(case["slaSeconds"])
    return save(store, case, cid)


def update_case(store, case_id: str, patch: Dict[str, Any], replace: bool = False, cid: str = "api") -> Dict:
    cur = get_case(store, case_id)
    protected = {"id", "_rev", "_ts"}
    if replace:
        new = {k: v for k, v in patch.items() if k not in protected}
        new["id"] = case_id
        new.setdefault("bucket", cur.get("bucket", "active"))
        add_history(new, "Case Updated", patch.get("updatedBy") or "API", "Case replaced through the REST API.")
    else:
        new = dict(cur)
        for k, v in patch.items():
            if k not in protected:
                new[k] = v
        add_history(new, "Case Updated", patch.get("updatedBy") or "API", "Fields updated: " + ", ".join(sorted(k for k in patch if k not in protected)))
    return save(store, new, cid)


def close_case(store, case_id: str, summary: str = "", by: str = "API") -> Dict:
    c = get_case(store, case_id)
    if c.get("bucket") == "closed":
        raise Invalid("case is already closed")
    c.update(status="CLOSED", bucket="closed", date=stamp())
    add_history(c, "Case Closed", by, f"Resolution: {summary or 'Case closed and verified.'}")
    return save(store, c)


def transfer_case(store, case_id: str, station: str, by: str = "API") -> Dict:
    c = get_case(store, case_id)
    if station not in WORKFLOW:
        raise Invalid(f"station must be one of {WORKFLOW}")
    prev = c.get("station")
    for h in c.get("stationHistory", []):
        if h.get("exitedAt") == "-":
            h.update(exitedAt=stamp(), status="Completed", duration=c.get("duration", "00:00:00"))
    c.setdefault("stationHistory", []).append({"station": station, "enteredAt": stamp(), "exitedAt": "-", "duration": "00:00:00",
                                              "slaLimit": f"{station_sla(store, station)} minutes", "status": "In Progress"})
    c.update(station=station, slaSeconds=0, duration="00:00:00", triggeredAlert60=False, triggeredAlert80=False, triggeredAlertBreach=False)
    add_history(c, "Case Transferred", by, f"Case transferred from {prev} to {station}.")
    return save(store, c)


def add_note(store, case_id: str, text: str, by: str = "API") -> Dict:
    c = get_case(store, case_id)
    add_history(c, "Note Added", by, f"Added note: {text}")
    return save(store, c)


def add_collab(store, case_id: str, user: str, text: str, role: str = "Support") -> Dict:
    c = get_case(store, case_id)
    c["collaboration"] = c.get("collaboration") or []
    initials = "".join(w[0] for w in user.split()[:1] + user.split()[-1:]).upper() or "?"
    c["collaboration"].insert(0, {"user": user, "role": role, "time": stamp(), "text": text,
                                  "badgeBg": "bg-blue-100 text-[#0066cc]", "avatarBg": "bg-blue-600", "initials": initials})
    add_history(c, "Collaboration Message", user, text[:120])
    return save(store, c)


# ---- checklist
def checklist_add(store, case_id: str, text: str, by: str = "API") -> Dict:
    c = get_case(store, case_id)
    c.setdefault("checklist", []).append({"id": uid(), "text": text, "completed": False})
    add_history(c, "Checklist Item Added", by, f"Added: {text[:60]}")
    return save(store, c)


def _item(c: Dict, item_id: str) -> Dict:
    for it in c.get("checklist", []):
        if it.get("id") == item_id:
            return it
    raise NotFound(item_id)


def checklist_patch(store, case_id: str, item_id: str, patch: Dict[str, Any], by: str = "API") -> Dict:
    c = get_case(store, case_id)
    it = _item(c, item_id)
    for k in ("text", "completed"):
        if k in patch:
            it[k] = patch[k]
    add_history(c, "Checklist Item Updated", by, f"{it.get('text', '')[:50]} → completed={it.get('completed')}")
    return save(store, c)


def checklist_delete(store, case_id: str, item_id: str, by: str = "API") -> Dict:
    c = get_case(store, case_id)
    it = _item(c, item_id)
    c["checklist"].remove(it)
    add_history(c, "Checklist Item Deleted", by, f"Deleted: {it.get('text', '')[:60]}")
    return save(store, c)


def reminder_set(store, case_id: str, item_id: str, eta_ms: int, reason: str, by: str = "API", lead_ms: int = 300000) -> Dict:
    c = get_case(store, case_id)
    it = _item(c, item_id)
    it["alarm"] = {"at": int(eta_ms) - lead_ms, "eta": int(eta_ms), "reason": reason, "fired": False, "by": by}
    add_history(c, "Reminder Set", by, f"{it.get('text', '')[:40]} · ETA {stamp(eta_ms)} · {reason}")
    return save(store, c)


def reminder_complete(store, case_id: str, item_id: str, documentation: str, by: str = "API") -> Dict:
    c = get_case(store, case_id)
    it = _item(c, item_id)
    if not it.get("alarm"):
        raise Invalid("item has no reminder")
    if not documentation.strip():
        raise Invalid("documentation is required to complete a reminder")
    it["alarm"]["done"] = {"text": documentation, "by": by, "at": int(time.time() * 1000)}
    add_history(c, "Reminder Completed", by, documentation)
    return save(store, c)


# ---- attachments (metadata in the case, payload in the attachments collection)
def att_key(case_id: str, att_id) -> str:
    return f"{case_id}|{att_id}"


def attachment_add(store, case_id: str, name: str, data: str, by: str = "API", desc: str = "") -> Dict:
    c = get_case(store, case_id)
    raw = data.split(",", 1)[1] if data.startswith("data:") else data
    try:
        size = len(base64.b64decode(raw, validate=True))
    except Exception:  # noqa: BLE001
        raise Invalid("data must be base64 or a data URL")
    ext = (name.rsplit(".", 1)[-1] if "." in name else "").lower()
    typ, cat = "File", "others"
    if ext in ("png", "jpg", "jpeg", "gif", "webp", "svg", "bmp"):
        typ, cat = "Image", "images"
    elif ext in ("txt", "log"):
        typ, cat = "Text", "logs"
    elif ext == "pdf":
        typ, cat = "PDF", "documents"
    elif ext in ("xls", "xlsx", "csv"):
        typ, cat = "Excel", "documents"
    elif ext in ("msg", "eml"):
        typ, cat = "Email", "others"
    elif ext == "zip":
        typ, cat = "ZIP", "others"
    atts = c.setdefault("attachments", [])
    aid = max([a.get("id", 0) for a in atts if isinstance(a.get("id"), int)] + [0]) + 1
    sz = f"{max(1, round(size / 1024))} KB" if size < 1048576 else f"{size / 1048576:.1f} MB"
    atts.insert(0, {"id": aid, "name": name, "type": typ, "cat": cat, "size": sz, "by": by, "date": stamp(), "desc": desc})
    if not data.startswith("data:"):
        data = f"data:application/octet-stream;base64,{raw}"
    store.put_att(att_key(case_id, aid), data, "api")
    add_history(c, "File Attached", by, f"Attached file: {name}")
    out = save(store, c)
    out["attachmentId"] = aid
    return out


def attachment_get(store, case_id: str, att_id: int):
    c = get_case(store, case_id)
    meta = next((a for a in c.get("attachments", []) if str(a.get("id")) == str(att_id)), None)
    if not meta:
        raise NotFound(att_id)
    return meta, store.get_att(att_key(case_id, att_id))


def attachment_delete(store, case_id: str, att_id: int, by: str = "API") -> Dict:
    c = get_case(store, case_id)
    meta = next((a for a in c.get("attachments", []) if str(a.get("id")) == str(att_id)), None)
    if not meta:
        raise NotFound(att_id)
    c["attachments"] = [a for a in c["attachments"] if str(a.get("id")) != str(att_id)]
    store.del_att(att_key(case_id, att_id), "api")
    add_history(c, "File Deleted", by, f"Deleted attachment: {meta.get('name')}")
    return save(store, c)


# ---- settings / rules / staff / simulation
def get_set(store, key: str, default: Any) -> Any:
    v = store.get_setting(key)
    return default if v is None else v


def history_all(store, case: Optional[str] = None, limit: int = 200) -> List[Dict]:
    rows: List[Dict] = []
    for c in store.all_cases():
        if case and c["id"] != case:
            continue
        for h in c.get("caseHistoryLog", []):
            rows.append({"case": c["id"], **h})
    return rows[:limit] if case else rows[:limit]


def stats(store) -> Dict:
    cases = store.all_cases()
    by: Dict[str, Dict[str, int]] = {"bucket": {}, "station": {}, "priority": {}, "assignee": {}}
    for c in cases:
        for k, f in (("bucket", "bucket"), ("station", "station"), ("priority", "priority"), ("assignee", "assignee")):
            v = str(c.get(f) or "—")
            by[k][v] = by[k].get(v, 0) + 1
    return {"total": len(cases), **by, "storage": store.kind, "counts": store.counts()}
