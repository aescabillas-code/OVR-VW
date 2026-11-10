"""Storage layer: MongoDB (primary) with an in-memory fallback for development.

Every document carries bookkeeping fields:
  _rev  monotonically increasing revision (used for change feeds / live sync)
  _ts   server write time in ms
  _cid  id of the writer (browser session or "api")

Speed: MongoStore keeps a full in-memory mirror of the database inside the server process. Every read and the live
change feed that browsers poll about once a second are answered from that mirror, so a heartbeat never waits for a
network round trip to MongoDB. Writes update the mirror at once (other browsers see them on their next heartbeat)
and a background writer persists them to MongoDB in batches; a browser's change is acknowledged only after it is
stored in MongoDB. Changes written by another process (for example the REST API started on its own) are picked up
by a light poll of the revision counter.
"""
import copy
import os
import re
import threading
import time
from collections import OrderedDict, deque
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
    """Process-local store (data is lost on restart). Used only if MongoDB is unreachable.

    It is also the in-memory mirror that MongoStore builds on: MongoStore only adds persistence (_persist)."""

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

    # ---- persistence hooks (no-ops in memory; MongoStore queues the write for MongoDB)
    def _persist(self, kind: str, key: str, doc: Optional[Dict], untomb: bool = False) -> None:
        return None

    def ticket(self) -> int:
        """Id of the most recently queued write (0 = nothing is ever queued)."""
        return 0

    def durable(self, ticket: int) -> bool:
        """True once the write with this ticket (and every earlier one) is stored."""
        return True

    def error(self) -> Optional[str]:
        return None

    # ---- cases
    def put_case(self, doc: Dict, cid: Optional[str] = None) -> int:
        with self.lock:
            r = self._next()
            d = dict(doc)
            d.update(_id=doc["id"], _rev=r, _cid=cid, _ts=now_ms())
            self.cases[doc["id"]] = d
            untomb = self.tomb.pop(("case", doc["id"]), None) is not None
            self._persist("case", doc["id"], d, untomb)
            return r

    def del_case(self, cid_: str, cid: Optional[str] = None) -> bool:
        with self.lock:
            existed = self.cases.pop(cid_, None) is not None
            for k in [k for k in self.atts if k.split("|")[0] == cid_]:
                self.del_att(k, cid)
            self.tomb[("case", cid_)] = {"_rev": self._next(), "_ts": now_ms()}
            self._persist("case", cid_, None)
            return existed

    def get_case(self, case_id: str) -> Optional[Dict]:
        with self.lock:
            d = self.cases.get(case_id)
            return copy.deepcopy(clean(d)) | {"_rev": d["_rev"], "_ts": d["_ts"]} if d else None

    def all_cases(self, bucket: Optional[str] = None) -> List[Dict]:
        out = []
        with self.lock:
            for d in list(self.cases.values()):
                if bucket and d.get("bucket") != bucket:
                    continue
                out.append(copy.deepcopy(clean(d)) | {"_rev": d["_rev"], "_ts": d["_ts"]})
        return out

    # ---- settings
    def put_setting(self, key: str, value: Any, cid: Optional[str] = None) -> int:
        with self.lock:
            r = self._next()
            d = {"_id": key, "v": value, "_rev": r, "_cid": cid, "_ts": now_ms()}
            self.settings[key] = d
            self._persist("set", key, d)
            return r

    def has_setting(self, key: str) -> bool:
        return key in self.settings

    def setting_ent(self, key: str) -> Optional[Dict]:
        """The stored setting as a change-feed entity (sent to a browser whose default was not used)."""
        with self.lock:
            d = self.settings.get(key)
            return ent(key, "main", d["v"], d) if d else None

    def get_setting(self, key: str) -> Any:
        with self.lock:
            d = self.settings.get(key)
            return copy.deepcopy(d["v"]) if d else None

    # ---- attachment payloads (data URLs), keyed "<caseId>|<attId>"
    def put_att(self, key: str, data: str, cid: Optional[str] = None) -> int:
        with self.lock:
            r = self._next()
            d = {"_id": key, "data": data, "_rev": r, "_cid": cid, "_ts": now_ms()}
            self.atts[key] = d
            untomb = self.tomb.pop(("att", key), None) is not None
            self._persist("att", key, d, untomb)
            return r

    def get_att(self, key: str) -> Optional[str]:
        d = self.atts.get(key)
        return d["data"] if d else None

    def del_att(self, key: str, cid: Optional[str] = None) -> None:
        with self.lock:
            self.atts.pop(key, None)
            self.tomb[("att", key)] = {"_rev": self._next(), "_ts": now_ms()}
            self._persist("att", key, None)

    # ---- change feed
    def changes_since(self, have: int, grace_ms: int = 0) -> List[Dict]:
        """Everything newer than revision `have`. Revisions are assigned and stored under one lock, so nothing can
        slip in behind a reported revision and no grace window is needed (grace_ms is kept for compatibility)."""
        cut = now_ms() - grace_ms if grace_ms else None
        want = (lambda d: d["_rev"] > have or d["_ts"] > cut) if cut else (lambda d: d["_rev"] > have)  # noqa: E731
        out: List[Dict] = []
        with self.lock:
            out += [ent("case", d["_id"], clean(d), d) for d in self.cases.values() if want(d)]
            out += [ent(k, "main", d["v"], d) for k, d in self.settings.items() if want(d)]
            out += [ent("att", k, {"data": d["data"]}, d) for k, d in self.atts.items() if want(d)]
            out += [ent(k, i, None, d) for (k, i), d in self.tomb.items() if want(d)]
        out.sort(key=lambda e: e["r"])
        return out

    def feed(self, have: int):
        """(current revision, changes since `have`) taken together."""
        with self.lock:
            return self.rev, self.changes_since(have)

    def wipe(self) -> None:
        with self.lock:
            self.cases.clear(); self.settings.clear(); self.atts.clear(); self.tomb.clear()

    def counts(self) -> Dict[str, int]:
        return {"cases": len(self.cases), "attachments": len(self.atts), "settings": len(self.settings)}


_COLL = {"case": "c_cases", "set": "c_set", "att": "c_att"}


class MongoStore(MemStore):  # type: ignore[misc]
    """MongoDB-backed store. Collections: cases, settings, attachments, tombstones, meta.

    Reads come from the in-memory mirror (MemStore); writes are queued and persisted by a background thread."""

    kind = "mongodb"

    def __init__(self, uri: str, dbname: str, client: Any = None, poll_s: float = 1.0) -> None:
        MemStore.__init__(self)
        self.uri, self.dbname = uri, dbname
        if client is None:
            from pymongo import ASCENDING, MongoClient, ReturnDocument
            from pymongo.operations import DeleteOne, ReplaceOne
            self._RD_AFTER = ReturnDocument.AFTER
            client = MongoClient(uri, serverSelectionTimeoutMS=4000, maxIdleTimeMS=300000)
        else:  # tests: a fake client brings its own operation classes
            ASCENDING, ReplaceOne, DeleteOne = 1, client.ReplaceOne, client.DeleteOne
            self._RD_AFTER = True
        self._Replace, self._Delete = ReplaceOne, DeleteOne
        self.client = client
        self.client.admin.command("ping")  # raises if unreachable
        self.db = self.client[dbname]
        self.c_cases, self.c_set = self.db["cases"], self.db["settings"]
        self.c_att, self.c_tomb, self.c_meta = self.db["attachments"], self.db["tombstones"], self.db["meta"]
        for c, f in ((self.c_cases, "bucket"), (self.c_cases, "station"), (self.c_cases, "assignee")):
            c.create_index([(f, ASCENDING)])
        for c in (self.c_cases, self.c_set, self.c_att, self.c_tomb):
            c.create_index([("_rev", ASCENDING)])
            c.create_index([("_ts", ASCENDING)])
        # The live-sync revision handed to browsers is a per-process counter. It starts at the current time in ms, so
        # after a restart it is higher than anything a still-open browser has seen and that browser simply resyncs.
        self.rev = now_ms()
        self._dbrev: Dict[tuple, int] = {}          # (kind, key) -> MongoDB _rev of the stored version
        self._own = deque(maxlen=20000)              # MongoDB revisions written by this process
        self._own_set: set = set()
        self._q: "OrderedDict[tuple, list]" = OrderedDict()   # (kind, key) -> [ticket, doc|None, untomb]
        self._writing: Dict[tuple, int] = {}         # (kind, key) -> ticket, currently being written
        self._tk = 0
        self._err: Optional[str] = None
        self._poll_err: Optional[str] = None
        self._cv = threading.Condition(self.lock)
        self._recent_until = 0.0
        self._load()
        self._poll_s = poll_s
        threading.Thread(target=self._writer, name="ovrvw-mongo-writer", daemon=True).start()
        if poll_s:
            threading.Thread(target=self._poller, name="ovrvw-mongo-poll", daemon=True).start()

    def describe(self) -> str:
        host = self.uri.split("@")[-1].split("/")[0].split("?")[0]
        return f"MongoDB · {host} · db '{self.dbname}'" + (f" · from {self.source}" if getattr(self, "source", "") else "")

    # ---- initial load: one query per collection, once per process
    def _meta_rev(self) -> int:
        d = self.c_meta.find_one({"_id": "rev"})
        return int(d["v"]) if d else 0

    def _load(self) -> None:
        self._dbseen = self._meta_rev()   # read first: anything written while loading is picked up by the poll
        with self.lock:
            for d in self.c_cases.find({}):
                self._mirror("case", d["_id"], d)
            for d in self.c_set.find({}):
                self._mirror("set", d["_id"], d)
            for d in self.c_att.find({}):
                self._mirror("att", d["_id"], d)
            for d in self.c_tomb.find({}):
                if d.get("k") in ("case", "att") and d.get("i") is not None:
                    self._mirror(d["k"], d["i"], None, d)

    def _mirror(self, kind: str, key: str, d: Optional[Dict], tomb: Optional[Dict] = None) -> None:
        """Put a MongoDB document (or a deletion) into the mirror with a fresh live-sync revision."""
        src = d if d is not None else tomb
        self._dbrev[(kind, key)] = int(src.get("_rev") or 0)
        r, ts = self._next(), int(src.get("_ts") or now_ms())
        if d is None:
            {"case": self.cases, "att": self.atts}[kind].pop(key, None)
            self.tomb[(kind, key)] = {"_rev": r, "_ts": ts}
            return
        m = dict(d)
        m.update(_id=key, _rev=r, _ts=ts, _cid=d.get("_cid"))
        if kind == "case":
            m.setdefault("id", key)
            self.cases[key] = m
        elif kind == "set":
            self.settings[key] = m
        else:
            self.atts[key] = m
        self.tomb.pop((kind, key), None)

    # ---- write queue
    def _persist(self, kind, key, doc, untomb=False):
        with self._cv:
            self._tk += 1
            k = (kind, key)
            if k in self._q:   # coalesce: keep the oldest ticket (so acks stay correct), store the newest content
                e = self._q[k]
                e[1], e[2] = doc, e[2] or untomb
            else:
                self._q[k] = [self._tk, doc, untomb]
            self._cv.notify()

    def ticket(self) -> int:
        with self.lock:
            return self._tk

    def _min_pending(self) -> float:
        ts = [e[0] for e in self._q.values()] + list(self._writing.values())
        return min(ts) if ts else float("inf")

    def durable(self, ticket: int) -> bool:
        with self.lock:
            return ticket < self._min_pending()

    def error(self) -> Optional[str]:
        return self._err

    def flush(self, timeout: float = 10.0) -> bool:
        """Wait until every queued write is in MongoDB (used by tests and on wipe)."""
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                if not self._q and not self._writing:
                    return True
            time.sleep(0.01)
        return False

    def _writer(self) -> None:
        backoff = 0.5
        while True:
            with self._cv:
                while not self._q:
                    self._cv.wait()
                batch = []
                while self._q and len(batch) < 500:
                    k, e = self._q.popitem(last=False)
                    batch.append((k, e))
                    self._writing[k] = e[0]
            try:
                self._write_batch(batch)
                self._err, backoff = None, 0.5
            except Exception as ex:  # noqa: BLE001  keep the writes and retry; the browser shows the error meanwhile
                self._err = f"{ex.__class__.__name__}: {str(ex)[:200]}"
                with self._cv:
                    rest = list(self._q.items())
                    self._q.clear()
                    for k, e in batch:
                        newer = dict(rest).get(k)
                        if newer is not None:   # a newer version was queued meanwhile: write that, keep the older ticket
                            newer[0] = min(newer[0], e[0])
                            newer[2] = newer[2] or e[2]
                        else:
                            self._q[k] = e
                    for k, e in rest:
                        self._q[k] = e
                time.sleep(backoff)
                backoff = min(backoff * 2, 8)
            finally:
                with self.lock:
                    for k, _ in batch:
                        self._writing.pop(k, None)

    def _write_batch(self, batch) -> None:
        n = len(batch)
        d = self.c_meta.find_one_and_update({"_id": "rev"}, {"$inc": {"v": n}}, upsert=True, return_document=self._RD_AFTER)
        top = int(d["v"])
        first = top - n + 1
        ops: Dict[str, list] = {"c_cases": [], "c_set": [], "c_att": [], "c_tomb": []}
        for j, ((kind, key), (_tk, doc, untomb)) in enumerate(batch):
            r, coll = first + j, _COLL[kind]
            if doc is not None:
                x = dict(doc)
                x["_rev"] = r
                ops[coll].append(self._Replace({"_id": key}, x, upsert=True))
                if untomb:
                    ops["c_tomb"].append(self._Delete({"_id": kind + "|" + key}))
            else:
                ops[coll].append(self._Delete({"_id": key}))
                ops["c_tomb"].append(self._Replace({"_id": kind + "|" + key},
                                                   {"_id": kind + "|" + key, "k": kind, "i": key, "_rev": r, "_ts": now_ms()},
                                                   upsert=True))
        for coll, lst in ops.items():
            if lst:
                getattr(self, coll).bulk_write(lst, ordered=False)
        with self.lock:
            for j, ((kind, key), _e) in enumerate(batch):
                r = first + j
                self._dbrev[(kind, key)] = max(self._dbrev.get((kind, key), 0), r)
                if len(self._own) == self._own.maxlen:
                    self._own_set.discard(self._own[0])
                self._own.append(r)
                self._own_set.add(r)
            if first - 1 == self._dbseen:   # nobody else wrote in between: nothing to poll for
                self._dbseen = top

    # ---- changes made by other processes (e.g. the REST API started on its own)
    def _poller(self) -> None:
        while True:
            time.sleep(self._poll_s)
            try:
                self.poll_once()
                self._poll_err = None
            except Exception as ex:  # noqa: BLE001
                self._poll_err = f"{ex.__class__.__name__}: {str(ex)[:200]}"

    def poll_once(self) -> int:
        v = self._meta_rev()
        with self.lock:
            seen = self._dbseen
        if v <= seen and time.time() > self._recent_until:
            return 0
        # Revisions are reserved before the documents are written, so also look back over the last few seconds.
        q = {"$or": [{"_rev": {"$gt": seen}}, {"_ts": {"$gt": now_ms() - 10000}}]}
        found = [("case", d["_id"], d, None) for d in self.c_cases.find(q)]
        found += [("set", d["_id"], d, None) for d in self.c_set.find(q)]
        found += [("att", d["_id"], d, None) for d in self.c_att.find(q)]
        found += [(d.get("k"), d.get("i"), None, d) for d in self.c_tomb.find(q) if d.get("k") in ("case", "att")]
        n = 0
        with self.lock:
            for kind, key, d, tomb in found:
                src = d if d is not None else tomb
                r = int(src.get("_rev") or 0)
                k = (kind, key)
                if r in self._own_set or k in self._q or k in self._writing or self._dbrev.get(k, 0) >= r:
                    continue   # our own write, a newer local edit, or already known
                self._mirror(kind, key, d, tomb)
                n += 1
            self._dbseen = max(self._dbseen, v)
            if v > seen:
                self._recent_until = time.time() + 10
        return n

    def wipe(self):
        with self.lock:
            self._q.clear()
        self.flush()
        for c in (self.c_cases, self.c_set, self.c_att, self.c_tomb):
            c.delete_many({})
        MemStore.wipe(self)

    def counts(self):
        return MemStore.counts(self)


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
