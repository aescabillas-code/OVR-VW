"""Server side of the browser <-> MongoDB sync protocol (framework independent, unit-testable).

Browser -> server (Streamlit component value):
    {cid, tick, have, out:[{k, i, v, s}]}      v=None deletes; s = per-client sequence number
Server -> browser (component args "reply"):
    {synced, rev, ents:[{k,i,v,r,t}], ack, now, ok, err, store}
"""
import threading
from typing import Any, Dict, Optional

from store import SETTING_KEYS, now_ms


class Bridge:
    def __init__(self, store) -> None:
        self.store = store
        self.ack: Dict[str, int] = {}
        self.err: Optional[str] = None
        self.lock = threading.Lock()

    def apply(self, e: Dict[str, Any], cid: str) -> None:
        k, i, v = e.get("k"), e.get("i"), e.get("v")
        if k == "case":
            if v is None:
                self.store.del_case(i, cid)
            else:
                v = dict(v)
                v["id"] = i
                self.store.put_case(v, cid)
        elif k in SETTING_KEYS:
            self.store.put_setting(k, v, cid)
        elif k == "att":
            if v is None:
                self.store.del_att(i, cid)
            else:
                self.store.put_att(i, v.get("data", ""), cid)

    def handle(self, msg: Dict[str, Any]) -> None:
        cid = msg.get("cid") or "?"
        with self.lock:
            last = self.ack.get(cid, 0)
            for e in sorted(msg.get("out") or [], key=lambda x: x.get("s", 0)):
                s = e.get("s", 0)
                if s <= last:
                    continue
                try:
                    self.apply(e, cid)
                    self.err = None
                except Exception as ex:  # noqa: BLE001  keep going, report to the browser
                    self.err = f"{ex.__class__.__name__}: {str(ex)[:200]}"
                last = max(last, s)
            self.ack[cid] = last

    def reply(self, msg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        base = {"now": now_ms(), "ok": self.err is None, "err": self.err, "store": self.store.kind}
        if not msg:
            return {**base, "synced": False, "rev": 0, "ents": [], "ack": 0}
        have = int(msg.get("have") or 0)
        return {**base, "synced": True, "rev": self.store.current_rev(),
                "ents": self.store.changes_since(have), "ack": self.ack.get(msg.get("cid"), 0)}
