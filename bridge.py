"""Server side of the browser <-> MongoDB sync protocol (framework independent, unit-testable).

Browser -> server (Streamlit component value):
    {cid, tick, have, out:[{k, i, v, s}]}      v=None deletes; s = per-client sequence number
Server -> browser (component args "reply"):
    {synced, rev, ents:[{k,i,v,r,t}], ack, now, ok, err, store}
"""
import threading
from collections import deque
from typing import Any, Dict, Optional

from store import SETTING_KEYS, now_ms


class Bridge:
    def __init__(self, store) -> None:
        self.store = store
        self.ack: Dict[str, int] = {}          # per browser: highest sequence number that is safely stored
        self.applied: Dict[str, int] = {}      # per browser: highest sequence number already applied (dedupe)
        self.pending: Dict[str, deque] = {}    # per browser: (sequence number, store write ticket) not yet stored
        self.resend: Dict[str, Dict[str, int]] = {}  # per browser: setting -> seq of its unused default (sent back until acked)
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
        """Apply a browser's changes. They reach the in-memory mirror (and so every other browser) immediately;
        MongoDB is written in the background and the change is acknowledged once it is stored there."""
        cid = msg.get("cid") or "?"
        with self.lock:
            last = self.applied.get(cid, self.ack.get(cid, 0))
            pend = self.pending.setdefault(cid, deque())
            for e in sorted(msg.get("out") or [], key=lambda x: x.get("s", 0)):
                s = e.get("s", 0)
                if s <= last:
                    continue
                if e.get("seed") and e.get("k") in SETTING_KEYS and self.store.has_setting(e.get("k")):
                    # A screen's built-in default for a setting that is already stored: keep the stored value (another
                    # device may have changed it moments ago) and send it to this screen instead of overwriting it.
                    self.resend.setdefault(cid, {})[e.get("k")] = s
                    pend.append((s, 0))
                    last = max(last, s)
                    continue
                try:
                    self.apply(e, cid)
                    self.err = None
                    pend.append((s, self.store.ticket()))
                except Exception as ex:  # noqa: BLE001  keep going, report to the browser
                    self.err = f"{ex.__class__.__name__}: {str(ex)[:200]}"
                    pend.append((s, 0))
                last = max(last, s)
            self.applied[cid] = last
            self._settle(cid)

    def _settle(self, cid: str) -> int:
        pend = self.pending.get(cid)
        while pend and self.store.durable(pend[0][1]):
            self.ack[cid] = max(self.ack.get(cid, 0), pend.popleft()[0])
        return self.ack.get(cid, 0)

    def reply(self, msg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        err = self.err or self.store.error()
        base = {"now": now_ms(), "ok": err is None, "err": err, "store": self.store.kind}
        if not msg:
            return {**base, "synced": False, "rev": 0, "ents": [], "ack": 0}
        have = int(msg.get("have") or 0)
        rev, ents = self.store.feed(have)
        with self.lock:
            ack = self._settle(msg.get("cid"))
            waiting = self.resend.get(msg.get("cid")) or {}
            again = set(waiting)                  # send them all; once acked, this reply (which carries the ack) is the last
            for k in [k for k, s in waiting.items() if s <= ack]:
                waiting.pop(k)
        if again:
            have_keys = {(e["k"], e["i"]) for e in ents}
            ents = ents + [x for x in (self.store.setting_ent(k) for k in sorted(again))
                           if x and (x["k"], x["i"]) not in have_keys]
        return {**base, "synced": True, "rev": rev, "ents": ents, "ack": ack}
