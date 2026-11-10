"""Run:  python -m unittest discover -s tests -v
MongoStore (in-memory mirror + background writer) against a small fake MongoDB client: no server needed."""
import copy
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bridge import Bridge  # noqa: E402
from store import MongoStore  # noqa: E402


class _Op:
    def __init__(self, flt, doc=None, upsert=False):
        self.flt, self.doc, self.upsert = flt, doc, upsert


class ReplaceOne(_Op):
    pass


class DeleteOne(_Op):
    pass


def _match(d, q):
    if not q:
        return True
    if "$or" in q:
        return any(_match(d, x) for x in q["$or"])
    for k, v in q.items():
        if isinstance(v, dict) and "$gt" in v:
            if not (d.get(k) is not None and d.get(k) > v["$gt"]):
                return False
        elif d.get(k) != v:
            return False
    return True


class FakeColl:
    def __init__(self, cl):
        self.cl, self.docs = cl, {}

    def _io(self):
        if self.cl.fail:
            raise ConnectionError("network down")
        self.cl.calls += 1
        if self.cl.latency:
            time.sleep(self.cl.latency)

    def create_index(self, *a, **k):
        pass

    def find(self, q=None):
        self._io()
        with self.cl.lock:
            return [copy.deepcopy(d) for d in self.docs.values() if _match(d, q or {})]

    def find_one(self, q):
        self._io()
        with self.cl.lock:
            d = self.docs.get(q["_id"])
            return copy.deepcopy(d) if d else None

    def find_one_and_update(self, q, upd, upsert=False, return_document=None):
        self._io()
        with self.cl.lock:
            d = self.docs.setdefault(q["_id"], {"_id": q["_id"], "v": 0})
            for k, n in upd["$inc"].items():
                d[k] = d.get(k, 0) + n
            return dict(d)

    def bulk_write(self, ops, ordered=True):
        self._io()
        with self.cl.lock:
            for op in ops:
                if isinstance(op, ReplaceOne):
                    self.docs[op.flt["_id"]] = copy.deepcopy(op.doc)
                else:
                    self.docs.pop(op.flt["_id"], None)

    def replace_one(self, flt, doc, upsert=False):
        self.bulk_write([ReplaceOne(flt, doc, upsert)])

    def delete_many(self, q):
        with self.cl.lock:
            self.docs.clear()


class FakeClient:
    ReplaceOne, DeleteOne = ReplaceOne, DeleteOne

    def __init__(self):
        self.lock = threading.RLock()
        self.cols, self.latency, self.fail, self.calls = {}, 0.0, False, 0
        self.admin = type("A", (), {"command": lambda _s, *a: {"ok": 1}})()

    def __getitem__(self, name):
        return _DB(self)  # one database

    def coll(self, name):
        return self.cols.setdefault(name, FakeColl(self))


class _DB:
    def __init__(self, cl):
        self.cl = cl

    def __getitem__(self, name):
        return self.cl.coll(name)


def case(i, **kw):
    return {"id": i, "subject": "s", "station": "CARE", "status": "New", "bucket": "active", "slaSeconds": 5, **kw}


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.cl = FakeClient()
        self.st = MongoStore("mongodb://fake", "t", client=self.cl, poll_s=0)
        self.br = Bridge(self.st)

    def test_heartbeat_does_not_touch_the_database(self):
        self.br.handle({"cid": "A", "out": [{"k": "case", "i": "C1", "v": case("C1"), "s": 1}]})
        self.st.flush()
        self.cl.latency = 0.3   # a slow, far-away cluster
        t = time.time()
        before = self.cl.calls
        for n in range(20):
            self.br.reply({"cid": "B", "have": 0, "tick": n})
            self.st.counts()
        self.assertEqual(self.cl.calls, before, "replies must be served from memory")
        self.assertLess(time.time() - t, 0.3)

    def test_writes_reach_other_browsers_before_mongodb_and_ack_after(self):
        self.cl.latency = 0.3
        self.br.handle({"cid": "A", "out": [{"k": "simrun", "i": "main", "v": {"run": {"id": "sim_1"}}, "s": 1}]})
        r = self.br.reply({"cid": "B", "have": 0})
        self.assertEqual({e["k"]: e["v"] for e in r["ents"]}["simrun"]["run"]["id"], "sim_1")
        self.assertEqual(self.br.reply({"cid": "A", "have": 0})["ack"], 0, "not acked before it is stored")
        self.assertTrue(self.st.flush())
        self.assertEqual(self.br.reply({"cid": "A", "have": 0})["ack"], 1)
        self.assertEqual(self.cl.coll("settings").docs["simrun"]["v"]["run"]["id"], "sim_1")

    def test_restart_reloads_everything_from_mongodb(self):
        self.br.handle({"cid": "A", "out": [{"k": "case", "i": "C1", "v": case("C1"), "s": 1},
                                             {"k": "case", "i": "C2", "v": case("C2"), "s": 2},
                                             {"k": "att", "i": "C1|1", "v": {"data": "data:x"}, "s": 3},
                                             {"k": "profiles", "i": "main", "v": {"p": {"pw": {}}}, "s": 4}]})
        self.br.handle({"cid": "A", "out": [{"k": "case", "i": "C2", "v": None, "s": 5}]})
        self.assertTrue(self.st.flush())
        old_rev = self.st.current_rev()
        time.sleep(0.005)
        st2 = MongoStore("mongodb://fake", "t", client=self.cl, poll_s=0)
        self.assertEqual(sorted(st2.cases), ["C1"])
        self.assertEqual(st2.get_att("C1|1"), "data:x")
        self.assertEqual(st2.get_setting("profiles"), {"p": {"pw": {}}})
        self.assertIn(("case", "C2"), st2.tomb)
        self.assertGreater(st2.current_rev(), old_rev, "an open browser resyncs after a restart")
        ents = Bridge(st2).reply({"cid": "B", "have": old_rev})["ents"]
        self.assertIn(("case", "C2", None), [(e["k"], e["i"], e["v"]) for e in ents])

    def test_burst_of_edits_is_coalesced(self):
        self.cl.latency = 0.05
        for n in range(1, 101):
            self.br.handle({"cid": "A", "out": [{"k": "set", "i": "main", "v": {"n": n}, "s": n}]})
        self.assertTrue(self.st.flush())
        self.assertLess(self.cl.calls, 20)
        self.assertEqual(self.cl.coll("settings").docs["set"]["v"], {"n": 100})
        self.assertEqual(self.br.reply({"cid": "A", "have": 0})["ack"], 100)

    def test_outage_is_reported_retried_and_not_acked(self):
        self.cl.fail = True
        self.br.handle({"cid": "A", "out": [{"k": "case", "i": "C1", "v": case("C1"), "s": 1}]})
        time.sleep(0.2)
        r = self.br.reply({"cid": "A", "have": 0})
        self.assertFalse(r["ok"])
        self.assertIn("network down", r["err"])
        self.assertEqual(r["ack"], 0)
        self.cl.fail = False
        self.assertTrue(self.st.flush(15))
        r = self.br.reply({"cid": "A", "have": 0})
        self.assertTrue(r["ok"])
        self.assertEqual(r["ack"], 1)
        self.assertIn("C1", self.cl.coll("cases").docs)

    def test_changes_from_another_process_are_picked_up(self):
        other = MongoStore("mongodb://fake", "t", client=self.cl, poll_s=0)   # e.g. the REST API started on its own
        have = self.st.current_rev()
        other.put_case(case("API1"), "api")
        other.put_setting("rules", {"list": [1]}, "api")
        self.assertTrue(other.flush())
        self.assertEqual(self.st.poll_once(), 2)
        ents = self.br.reply({"cid": "B", "have": have})["ents"]
        self.assertEqual(sorted(e["i"] for e in ents), ["API1", "main"])
        other.del_case("API1", "api")
        self.assertTrue(other.flush())
        self.st.poll_once()
        self.assertNotIn("API1", self.st.cases)
        self.assertEqual(self.st.poll_once(), 0, "own and already-known changes are not applied twice")

    def test_reads_are_copies(self):
        self.st.put_case(case("C1", checklist=[{"id": "a"}]))
        c = self.st.get_case("C1")
        c["checklist"].append({"id": "b"})
        self.assertEqual(len(self.st.get_case("C1")["checklist"]), 1)


class SeedTests(unittest.TestCase):
    """A screen's built-in defaults never overwrite a setting that is already stored (fresh-database race)."""

    def test_default_from_a_second_screen_does_not_overwrite_a_saved_password(self):
        from store import MemStore
        for st in (MemStore(), MongoStore("mongodb://fake", "t", client=FakeClient(), poll_s=0)):
            br = Bridge(st)
            br.handle({"cid": "A", "out": [{"k": "profiles", "i": "main", "v": {"p": {"pw": {}}}, "s": 1, "seed": 1},
                                           {"k": "profiles", "i": "main", "v": {"p": {"pw": {"karen": {"h": "x"}}}}, "s": 2}]})
            br.handle({"cid": "B", "out": [{"k": "profiles", "i": "main", "v": {"p": {"pw": {}}}, "s": 1, "seed": 1}]})
            self.assertEqual(st.get_setting("profiles")["p"]["pw"], {"karen": {"h": "x"}})
            if hasattr(st, "flush"):
                st.flush()
            r = br.reply({"cid": "B", "have": st.current_rev()})
            self.assertEqual(r["ack"], 1)
            self.assertEqual([e["v"]["p"]["pw"] for e in r["ents"] if e["k"] == "profiles"], [{"karen": {"h": "x"}}],
                             "B is sent the stored value instead")
            self.assertEqual(br.reply({"cid": "B", "have": st.current_rev()})["ents"], [], "sent once it is acked")


if __name__ == "__main__":
    unittest.main()
