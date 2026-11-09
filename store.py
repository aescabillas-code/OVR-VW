"""Storage layer: MongoDB (primary) with an in-memory fallback for development.

Every document carries bookkeeping fields:
  _rev  monotonically increasing revision (used for change feeds / live sync)
  _ts   server write time in ms
  _cid  id of the writer (browser session or "api")
"""
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

SETTING_KEYS = ("set", "rules", "staff", "simev", "theme", "ann", "acks", "profiles", "simrun")
META_KEYS = ("_id", "_rev", "_cid", "_ts")


def now_ms() -> int:
    return int(time.time() * 1000)


def clean(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in doc.items() if k not in META_KEYS}


def ent(k: str, i: str, v: Any, d: Dict[str, Any]) -> Dict[str, Any]:
    """Entity as delivered to the browser."""
    return {"k": k, "i": i, "v": v, "r": d["_rev"], "t": d["_ts"]}


class MemStore:
    """Process-local store (data is lost on restart). Used only if MongoDB is unreachable."""

    kind = "memory"
    ok = True

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.rev = 0
        self.cases: Dict[str, Dict] = {}
        self.settings: Dict[str, Dict] = {}
        self.atts: Dict[str, Dict] = {}
        self.tomb: Dict[tuple, Dict] = {}

    def describe(self) -> str:
        return "in-memory (NOT persistent)"

    def current_rev(self) -> int:
        return self.rev

    def _next(self) -> int:
        self.rev += 1
        return self.rev

    # ---- cases
    def put_case(self, doc: Dict, cid: Optional[str] = None) -> int:
        with self.lock:
            r = self._next()
            d = dict(doc)
            d.update(_id=doc["id"], _rev=r, _cid=cid, _ts=now_ms())
            self.cases[doc["id"]] = d
            self.tomb.pop(("case", doc["id"]), None)
            return r

    def del_case(self, cid_: str, cid: Optional[str] = None) -> bool:
        with self.lock:
            existed = self.cases.pop(cid_, None) is not None
            for k in [k for k in self.atts if k.split("|")[0] == cid_]:
                self.del_att(k, cid)
            self.tomb[("case", cid_)] = {"_rev": self._next(), "_ts": now_ms()}
            return existed

    def get_case(self, case_id: str) -> Optional[Dict]:
        d = self.cases.get(case_id)
        return clean(d) | {"_rev": d["_rev"], "_ts": d["_ts"]} if d else None

    def all_cases(self, bucket: Optional[str] = None) -> List[Dict]:
        out = []
        for d in list(self.cases.values()):
            if bucket and d.get("bucket") != bucket:
                continue
            out.append(clean(d) | {"_rev": d["_rev"], "_ts": d["_ts"]})
        return out

    # ---- settings
    def put_setting(self, key: str, value: Any, cid: Optional[str] = None) -> int:
        with self.lock:
            r = self._next()
            self.settings[key] = {"_id": key, "v": value, "_rev": r, "_cid": cid, "_ts": now_ms()}
            return r

    def get_setting(self, key: str) -> Any:
        d = self.settings.get(key)
        return d["v"] if d else None

    # ---- attachment payloads (data URLs), keyed "<caseId>|<attId>"
    def put_att(self, key: str, data: str, cid: Optional[str] = None) -> int:
        with self.lock:
            r = self._next()
            self.atts[key] = {"_id": key, "data": data, "_rev": r, "_cid": cid, "_ts": now_ms()}
            self.tomb.pop(("att", key), None)
            return r

    def get_att(self, key: str) -> Optional[str]:
        d = self.atts.get(key)
        return d["data"] if d else None

    def del_att(self, key: str, cid: Optional[str] = None) -> None:
        with self.lock:
            self.atts.pop(key, None)
            self.tomb[("att", key)] = {"_rev": self._next(), "_ts": now_ms()}

    # ---- change feed
    def changes_since(self, have: int, grace_ms: int = 3000) -> List[Dict]:
        cut = now_ms() - grace_ms
        want = lambda d: d["_rev"] > have or d["_ts"] > cut  # noqa: E731
        out: List[Dict] = []
        with self.lock:
            out += [ent("case", d["_id"], clean(d), d) for d in self.cases.values() if want(d)]
            out += [ent(k, "main", d["v"], d) for k, d in self.settings.items() if want(d)]
            out += [ent("att", k, {"data": d["data"]}, d) for k, d in self.atts.items() if want(d)]
            out += [ent(k, i, None, d) for (k, i), d in self.tomb.items() if want(d)]
        out.sort(key=lambda e: e["r"])
        return out

    def wipe(self) -> None:
        with self.lock:
            self.cases.clear(); self.settings.clear(); self.atts.clear(); self.tomb.clear()

    def counts(self) -> Dict[str, int]:
        return {"cases": len(self.cases), "attachments": len(self.atts), "settings": len(self.settings)}


class MongoStore(MemStore):  # type: ignore[misc]
    """MongoDB-backed store. Collections: cases, settings, attachments, tombstones, meta."""

    kind = "mongodb"

    def __init__(self, uri: str, dbname: str) -> None:
        from pymongo import ASCENDING, MongoClient, ReturnDocument

        self._RD = ReturnDocument
        self.lock = threading.RLock()
        self.uri, self.dbname = uri, dbname
        self.client = MongoClient(uri, serverSelectionTimeoutMS=4000)
        self.client.admin.command("ping")  # raises if unreachable
        self.db = self.client[dbname]
        self.c_cases, self.c_set = self.db["cases"], self.db["settings"]
        self.c_att, self.c_tomb, self.c_meta = self.db["attachments"], self.db["tombstones"], self.db["meta"]
        self.c_cases.create_index([("bucket", ASCENDING)])
        self.c_cases.create_index([("station", ASCENDING)])
        self.c_cases.create_index([("assignee", ASCENDING)])
        for c in (self.c_cases, self.c_set, self.c_att, self.c_tomb):
            c.create_index([("_rev", ASCENDING)])
            c.create_index([("_ts", ASCENDING)])

    def describe(self) -> str:
        host = self.uri.split("@")[-1].split("/")[0].split("?")[0]
        return f"MongoDB · {host} · db '{self.dbname}'" + (f" · from {self.source}" if getattr(self, "source", "") else "")

    def current_rev(self) -> int:
        d = self.c_meta.find_one({"_id": "rev"})
        return int(d["v"]) if d else 0

    def _next(self) -> int:
        d = self.c_meta.find_one_and_update({"_id": "rev"}, {"$inc": {"v": 1}}, upsert=True,
                                            return_document=self._RD.AFTER)
        return int(d["v"])

    def put_case(self, doc, cid=None):
        r = self._next()
        d = dict(doc)
        d.update(_id=doc["id"], _rev=r, _cid=cid, _ts=now_ms())
        self.c_cases.replace_one({"_id": doc["id"]}, d, upsert=True)
        self.c_tomb.delete_one({"_id": "case|" + doc["id"]})
        return r

    def del_case(self, case_id, cid=None):
        res = self.c_cases.delete_one({"_id": case_id})
        for a in list(self.c_att.find({"_id": {"$regex": "^" + re.escape(case_id) + r"\|"}}, {"_id": 1})):
            self.del_att(a["_id"], cid)
        self.c_tomb.replace_one({"_id": "case|" + case_id},
                                {"_id": "case|" + case_id, "k": "case", "i": case_id, "_rev": self._next(), "_ts": now_ms()},
                                upsert=True)
        return res.deleted_count > 0

    def get_case(self, case_id):
        d = self.c_cases.find_one({"_id": case_id})
        return clean(d) | {"_rev": d["_rev"], "_ts": d["_ts"]} if d else None

    def all_cases(self, bucket=None):
        q = {"bucket": bucket} if bucket else {}
        return [clean(d) | {"_rev": d["_rev"], "_ts": d["_ts"]} for d in self.c_cases.find(q)]

    def put_setting(self, key, value, cid=None):
        r = self._next()
        self.c_set.replace_one({"_id": key}, {"_id": key, "v": value, "_rev": r, "_cid": cid, "_ts": now_ms()}, upsert=True)
        return r

    def get_setting(self, key):
        d = self.c_set.find_one({"_id": key})
        return d["v"] if d else None

    def put_att(self, key, data, cid=None):
        r = self._next()
        self.c_att.replace_one({"_id": key}, {"_id": key, "data": data, "_rev": r, "_cid": cid, "_ts": now_ms()}, upsert=True)
        self.c_tomb.delete_one({"_id": "att|" + key})
        return r

    def get_att(self, key):
        d = self.c_att.find_one({"_id": key})
        return d["data"] if d else None

    def del_att(self, key, cid=None):
        self.c_att.delete_one({"_id": key})
        self.c_tomb.replace_one({"_id": "att|" + key}, {"_id": "att|" + key, "k": "att", "i": key,
                                                         "_rev": self._next(), "_ts": now_ms()}, upsert=True)

    def changes_since(self, have, grace_ms=3000):
        q = {"$or": [{"_rev": {"$gt": have}}, {"_ts": {"$gt": now_ms() - grace_ms}}]}
        out: List[Dict] = []
        out += [ent("case", d["_id"], clean(d), d) for d in self.c_cases.find(q)]
        out += [ent(d["_id"], "main", d["v"], d) for d in self.c_set.find(q)]
        out += [ent("att", d["_id"], {"data": d["data"]}, d) for d in self.c_att.find(q)]
        out += [ent(d["k"], d["i"], None, d) for d in self.c_tomb.find(q)]
        out.sort(key=lambda e: e["r"])
        return out

    def wipe(self):
        for c in (self.c_cases, self.c_set, self.c_att, self.c_tomb):
            c.delete_many({})

    def counts(self):
        return {"cases": self.c_cases.estimated_document_count(),
                "attachments": self.c_att.estimated_document_count(),
                "settings": self.c_set.estimated_document_count()}


def _flat(d: Any, prefix: str = "", out: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Flatten nested secrets: {"mongo": {"uri": "x"}} -> {"mongo_uri": "x"} (keys lower-cased)."""
    out = {} if out is None else out
    items = d.items() if hasattr(d, "items") else []
    for k, v in items:
        key = (prefix + "_" + str(k) if prefix else str(k)).lower()
        if hasattr(v, "items"):
            _flat(v, key, out)
        else:
            out[key] = v
    return out


_URI_KEYS = ("mongo_uri", "mongodb_uri", "mongo_url", "mongodb_url", "mongo_connection_string", "mongodb_connection_string",
             "mongo_connection_uri", "mongodb_connection_uri", "mongo_conn_str", "mongodb_conn_str", "connection_string", "uri", "url",
             "mongo_mongo_uri", "mongodb_mongodb_uri", "database_uri", "database_url", "db_uri", "db_url")
_DB_KEYS = ("mongo_db", "mongodb_db", "mongo_database", "mongodb_database", "mongo_dbname", "mongodb_dbname", "mongo_db_name",
            "mongodb_db_name", "mongo_name", "mongodb_name", "db_name", "dbname", "database_name", "database", "db",
            "mongo_mongo_db", "mongodb_mongodb_db")


def resolve_mongo_config(secrets: Optional[Dict[str, Any]] = None, env: Optional[Dict[str, str]] = None):
    """Work out (uri, database, source). Streamlit secrets win over environment variables, then the defaults.

    Accepted in secrets.toml (any of these spellings, flat or inside a [mongo] / [mongodb] / [database] section):
        MONGO_URI / MONGODB_URI / uri / url / connection_string        and        MONGO_DB / database / db_name / dbname
    or the parts host (+ port, username, password, srv).  A database name inside the URI path is used when none is given.
    """
    from urllib.parse import quote_plus

    env = dict(os.environ if env is None else env)
    flat = _flat(secrets or {})
    uri = next((str(flat[k]).strip() for k in _URI_KEYS if flat.get(k)), "")
    if not uri:  # any value that looks like a MongoDB connection string
        uri = next((str(v).strip() for v in flat.values() if isinstance(v, str) and v.strip().lower().startswith(("mongodb://", "mongodb+srv://"))), "")
    if not uri:
        host = next((str(flat[k]).strip() for k in ("mongo_host", "mongodb_host", "host") if flat.get(k)), "")
        if host:
            user = next((str(flat[k]) for k in ("mongo_username", "mongodb_username", "mongo_user", "mongodb_user", "username", "user") if flat.get(k)), "")
            pw = next((str(flat[k]) for k in ("mongo_password", "mongodb_password", "password") if flat.get(k)), "")
            port = next((str(flat[k]) for k in ("mongo_port", "mongodb_port", "port") if flat.get(k)), "")
            srv = str(next((flat[k] for k in ("mongo_srv", "mongodb_srv", "srv") if k in flat), host.endswith("mongodb.net"))).lower() in ("1", "true", "yes")
            auth = f"{quote_plus(user)}:{quote_plus(pw)}@" if user else ""
            uri = ("mongodb+srv://" if srv else "mongodb://") + auth + host + ("" if srv or not port else f":{port}")
    name = next((str(flat[k]).strip() for k in _DB_KEYS if flat.get(k)), "")
    source = "Streamlit secrets" if uri or name else ""
    if not uri:
        uri = env.get("MONGO_URI") or env.get("MONGODB_URI") or ""
        source = "environment" if uri else ""
    if not name:
        name = env.get("MONGO_DB") or env.get("MONGODB_DB") or ""
    if not name and uri:
        try:
            from pymongo.uri_parser import parse_uri
            name = parse_uri(uri).get("database") or ""
        except Exception:  # noqa: BLE001
            name = ""
    return uri or "mongodb://localhost:27017", name or "ovrvw", source or "defaults"


def load_secrets_file() -> Dict[str, Any]:
    """Read .streamlit/secrets.toml directly (used when the API runs without Streamlit)."""
    try:
        import tomllib  # Python 3.11+
    except Exception:  # noqa: BLE001
        try:
            import tomli as tomllib  # type: ignore
        except Exception:  # noqa: BLE001
            return {}
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (os.getcwd(), here, os.path.expanduser("~")):
        f = os.path.join(base, ".streamlit", "secrets.toml")
        if os.path.isfile(f):
            try:
                with open(f, "rb") as fh:
                    return tomllib.load(fh)
            except Exception:  # noqa: BLE001
                return {}
    return {}


def make_store(secrets: Optional[Dict[str, Any]] = None):
    """Return (store, error). Falls back to MemStore when MongoDB cannot be reached."""
    uri, name, source = resolve_mongo_config(secrets if secrets else load_secrets_file())
    try:
        st = MongoStore(uri, name)
        st.source = source
        return st, None
    except Exception as ex:  # noqa: BLE001
        return MemStore(), (f"MongoDB not reachable using {source} ({ex.__class__.__name__}: {str(ex)[:160]}). "
                            "Using in-memory storage — data will NOT be saved. Check MONGO_URI / MONGO_DB in .streamlit/secrets.toml.")
