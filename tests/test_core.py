"""Run:  python -m unittest discover -s tests -v   (uses the in-memory store; no MongoDB needed)"""
import base64
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import service as S  # noqa: E402
from bridge import Bridge  # noqa: E402
from store import MemStore  # noqa: E402


def case(i, **kw):
    return {"id": i, "subject": "s", "station": "CARE", "status": "New", "bucket": "active", "slaSeconds": 5, **kw}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.st = MemStore()
        self.br = Bridge(self.st)

    def test_browser_writes_are_saved_and_acked_once(self):
        msg = {"cid": "A", "tick": 1, "have": 0, "out": [{"k": "case", "i": "C1", "v": case("C1"), "s": 1}]}
        self.br.handle(msg)
        self.br.handle(msg)  # re-sent heartbeat: must be idempotent
        self.assertEqual(len(self.st.cases), 1)
        self.assertEqual(self.st.rev, 1)
        self.assertEqual(self.br.reply(msg)["ack"], 1)

    def test_other_session_sees_changes_and_deletes(self):
        self.br.handle({"cid": "A", "out": [{"k": "case", "i": "C1", "v": case("C1"), "s": 1},
                                             {"k": "rules", "i": "main", "v": {"list": [{"id": 1}]}, "s": 2},
                                             {"k": "att", "i": "C1|1", "v": {"data": "data:text/plain;base64,QQ=="}, "s": 3}]})
        r = self.br.reply({"cid": "B", "have": 0})
        self.assertEqual(sorted(e["k"] for e in r["ents"]), ["att", "case", "rules"])
        self.assertTrue(r["synced"])
        have = r["rev"]
        self.br.handle({"cid": "A", "out": [{"k": "case", "i": "C1", "v": None, "s": 4}]})
        r2 = self.br.reply({"cid": "B", "have": have})
        dele = [e for e in r2["ents"] if e["k"] == "case" and e["v"] is None]
        self.assertEqual(len(dele), 1)
        self.assertIsNone(self.st.get_att("C1|1"), "attachment payload removed with its case")
        time.sleep(0.01)

    def test_change_feed_is_incremental(self):
        self.br.handle({"cid": "A", "out": [{"k": "case", "i": "C1", "v": case("C1"), "s": 1}]})
        have = self.br.reply({"cid": "B", "have": 0})["rev"]
        old = self.st.changes_since(have, grace_ms=0)
        self.assertEqual(old, [])
        self.br.handle({"cid": "A", "out": [{"k": "case", "i": "C2", "v": case("C2"), "s": 2}]})
        self.assertEqual([e["i"] for e in self.st.changes_since(have, grace_ms=0)], ["C2"])

    def test_storage_errors_are_reported_not_raised(self):
        self.st.put_case = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full"))
        self.br.handle({"cid": "A", "out": [{"k": "case", "i": "C1", "v": case("C1"), "s": 1}]})
        r = self.br.reply({"cid": "A", "have": 0})
        self.assertFalse(r["ok"])
        self.assertIn("disk full", r["err"])
        self.assertEqual(r["ack"], 1)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.st = MemStore()

    def test_case_lifecycle(self):
        c = S.create_case(self.st, {"subject": "Switch down", "account": "ACME", "station": "ARCH", "isHighPriority": True})
        self.assertEqual(c["priority"], "CRITICAL")
        self.assertEqual(len(c["checklist"]), 6)
        self.assertEqual(c["stationHistory"][-1]["slaLimit"], "30 minutes")
        cid = c["id"]
        with self.assertRaises(S.Invalid):
            S.create_case(self.st, {"id": cid, "subject": "dup"})
        c = S.update_case(self.st, cid, {"assignee": "Karen Sabile"})
        self.assertEqual(c["assignee"], "Karen Sabile")
        c = S.transfer_case(self.st, cid, "PET", "Karen")
        self.assertEqual((c["station"], c["slaSeconds"]), ("PET", 0))
        c = S.add_note(self.st, cid, "called customer", "Karen")
        c = S.add_collab(self.st, cid, "Karen Sabile", "on it")
        self.assertEqual(c["collaboration"][0]["initials"], "KS")
        c = S.close_case(self.st, cid, "fixed", "Karen")
        self.assertEqual((c["bucket"], c["status"]), ("closed", "CLOSED"))
        actions = [h["action"] for h in c["caseHistoryLog"]]
        for a in ("Case Created", "Case Updated", "Case Transferred", "Note Added", "Collaboration Message", "Case Closed"):
            self.assertIn(a, actions)
        self.assertEqual(len(S.list_cases(self.st, bucket="closed")["items"]), 1)
        self.assertEqual(S.list_cases(self.st, bucket="active")["total"], 0)

    def test_checklist_reminder_alarm_5_minutes_before(self):
        cid = S.create_case(self.st, {"subject": "x"})["id"]
        item = S.get_case(self.st, cid)["checklist"][0]["id"]
        eta = int(time.time() * 1000) + 3600_000
        c = S.reminder_set(self.st, cid, item, eta, "Call the store", "June")
        al = c["checklist"][0]["alarm"]
        self.assertEqual(al["eta"] - al["at"], 300_000)
        with self.assertRaises(S.Invalid):
            S.reminder_complete(self.st, cid, item, "  ")
        c = S.reminder_complete(self.st, cid, item, "Store confirmed", "June")
        self.assertEqual(c["checklist"][0]["alarm"]["done"]["text"], "Store confirmed")
        c = S.checklist_patch(self.st, cid, item, {"completed": True})
        self.assertTrue(c["checklist"][0]["completed"])
        c = S.checklist_add(self.st, cid, "new item")
        self.assertEqual(len(c["checklist"]), 7)
        c = S.checklist_delete(self.st, cid, item)
        self.assertEqual(len(c["checklist"]), 6)
        with self.assertRaises(S.NotFound):
            S.checklist_delete(self.st, cid, "nope")

    def test_attachments(self):
        cid = S.create_case(self.st, {"subject": "x"})["id"]
        raw = base64.b64encode(b"hello").decode()
        out = S.attachment_add(self.st, cid, "log.txt", raw, "June")
        aid = out["attachmentId"]
        meta, data = S.attachment_get(self.st, cid, aid)
        self.assertEqual((meta["type"], meta["cat"]), ("Text", "logs"))
        self.assertEqual(base64.b64decode(data.split(",", 1)[1]), b"hello")
        with self.assertRaises(S.Invalid):
            S.attachment_add(self.st, cid, "bad.bin", "***not base64***")
        S.attachment_delete(self.st, cid, aid)
        self.assertIsNone(self.st.get_att(S.att_key(cid, aid)))
        self.assertEqual(S.get_case(self.st, cid)["attachments"], [])

    def test_rest_writes_reach_browsers_through_the_bridge(self):
        br = Bridge(self.st)
        have = br.reply({"cid": "UI", "have": 0})["rev"]
        c = S.create_case(self.st, {"subject": "from API", "station": "PET"})
        ents = br.reply({"cid": "UI", "have": have})["ents"]
        self.assertEqual([(e["k"], e["i"]) for e in ents if e["k"] == "case"], [("case", c["id"])])
        self.assertEqual(ents[0]["v"]["bucket"], "active")

    def test_station_sla_comes_from_saved_settings(self):
        self.st.put_setting("set", {"sla": {"CARE": 20}})
        self.assertEqual(S.station_sla(self.st, "CARE"), 20)
        self.assertEqual(S.station_sla(self.st, "ARCH"), 30)
        c = S.create_case(self.st, {"subject": "x", "station": "CARE"})
        self.assertEqual(c["stationHistory"][0]["slaLimit"], "20 minutes")

    def test_stats_and_history(self):
        S.create_case(self.st, {"subject": "a", "assignee": "X", "priority": "LOW"})
        S.create_case(self.st, {"subject": "b", "assignee": "X", "station": "PET"})
        st = S.stats(self.st)
        self.assertEqual((st["total"], st["assignee"]["X"], st["station"]["PET"]), (2, 2, 1))
        self.assertEqual(len(S.history_all(self.st)), 2)


if __name__ == "__main__":
    unittest.main()
