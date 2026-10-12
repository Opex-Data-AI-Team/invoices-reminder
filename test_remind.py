"""Run: python test_remind.py  (no dependencies)"""
from datetime import date

from remind import UN, all_clients, build, client_list, compose_rep, is_workday, pct, tier, html_rep

TODAY = date(2026, 10, 6)  # a Tuesday


def inv(num, client, status, total, balance, dt, due=None, created=None):
    return dict(invoice_number=num, customer_name=client, status=status, total=total, balance=balance,
                date=dt, due_date=due or dt, created_time=(created or dt) + "T09:00:00+0100")


INVS = {"Org": [
    inv("I1", "A", "paid", 1000, 0, "2026-09-20"),
    inv("I2", "A", "sent", 1000, 1000, "2026-09-22"),            # 14d late -> blunt
    inv("I3", "A", "draft", 500, 500, "2026-10-01"),             # 5d old draft -> flagged
    inv("I4", "A", "draft", 500, 500, "2026-10-05"),             # 1d old draft -> grace
    inv("I5", "B", "sent", 400, 400, "2026-10-06"),              # 0d -> coach
    inv("I6", "B", "void", 999, 999, "2026-10-01"),              # ignored
    inv("I7", "B", "sent", 300, 300, "2026-10-01"),              # dead via follow-up
    inv("I8", "B", "sent", 300, 300, "2026-10-01"),              # promised in future
    inv("I9", "B", "sent", 300, 300, "2026-10-01"),              # broken promise
    inv("I10", "Z", "sent", 100, 100, "2026-10-01"),             # unmapped client
    inv("I11", "Z", "sent", 100, 100, "2026-10-05", due="2026-10-20"),  # raised yesterday, not due -> early
    inv("I12", "Z", "sent", 100, 100, "2026-10-06", due="2026-10-20"),  # raised today -> tomorrow
]}
REP_OF = {("org", "a"): "Ann", ("org", "b"): "Bob"}
FOLLOW = {("org", "I7"): {"Status": "Dead"},
          ("org", "I8"): {"Status": "Promised", "Promised pay date": "2026-10-10"},
          ("org", "I9"): {"Status": "Promised", "Promised pay date": "2026-10-02"}}


def run():
    return build(INVS, REP_OF, FOLLOW, TODAY)


def test_tiers():
    assert [tier(x) for x in (-1, 0, 6, 7, 13, 14, 20, 21)] == ["early", "coach", "coach", "firm", "firm", "blunt", "blunt", "decision"]


def test_workday():
    assert is_workday(date(2026, 10, 6)) and not is_workday(date(2026, 10, 10)) and not is_workday(date(2026, 10, 11))


def test_items_and_followups():
    items, *_ = run()
    got = {i["number"]: i for i in items}
    assert set(got) == {"I2", "I3", "I5", "I8", "I9", "I10", "I11"}  # I4 grace, I6 void, I7 dead, I12 raised today
    assert got["I2"]["tier"] == "blunt" and got["I5"]["tier"] == "coach"
    assert got["I9"]["broken"] and got["I9"]["tier"] == "blunt"  # broken promise bumps to blunt
    assert got["I11"]["tier"] == "early" and got["I11"]["days"] < 0
    assert not got["I8"]["broken"]                               # promised in future: still reminded, not broken
    assert got["I3"]["kind"] == "draft" and got["I10"]["rep"] == UN


def test_salesperson_beats_clients_tab():
    a = inv("S1", "A", "sent", 10, 10, "2026-10-01")
    a["salesperson_name"] = "Cy"
    items, *_ = build({"Org": [a, inv("S2", "A", "sent", 10, 10, "2026-10-01")]}, REP_OF, {}, TODAY, ["Cy", "Ann"])
    assert {i["number"]: i["rep"] for i in items} == {"S1": "Cy", "S2": "Ann"}  # blank -> Clients tab


def test_unknown_salesperson_not_dropped():
    a, z = inv("U1", "A", "sent", 10, 10, "2026-10-01"), inv("U2", "Z", "sent", 10, 10, "2026-10-01")
    a["salesperson_name"] = z["salesperson_name"] = "Cy Smith"  # not on the Reps tab
    items, *_ = build({"Org": [a, z]}, REP_OF, {}, TODAY, ["Cy", "Ann"])
    assert {i["number"]: i["rep"] for i in items} == {"U1": "Ann", "U2": UN}  # Clients tab, else unassigned


def test_rates():
    _, stats, outst, oldest = run()
    assert pct(stats["Ann"]) == 1000 / 3000 * 100                # I1 paid; I2, I3, I4 unpaid (drafts count, void does not)
    assert pct(stats["__team__"]) is not None
    assert outst["Ann"] == 1000 and oldest["Ann"] == 14


def test_message_has_stats_and_tone():
    items, stats, outst, oldest = run()
    subj, body, escalate = compose_rep("Ann Obi", [i for i in items if i["rep"] == "Ann"], stats, outst, oldest)
    assert escalate and "Chase it today" in body and "collection rate" in body and "never sent" in body.lower()


def test_client_list_only_owing_clients_biggest_first():
    rows = client_list(INVS)
    assert rows == [("Org", "A", ""), ("Org", "B", ""), ("Org", "Z", "")]   # A owes 2000, B 1300, Z 100; void and paid ignored
    assert client_list({"Org": [inv("P", "Paid Co", "paid", 5, 0, "2026-09-01")]}) == []


def test_all_clients_includes_paid_and_void_sorted_case_insensitive():
    invs = {"Org": [inv("1", "beta Co", "paid", 1, 0, "2026-01-01"), inv("2", "Alpha Co ", "void", 1, 1, "2026-01-01"),
                    inv("3", "beta Co", "sent", 1, 1, "2026-01-01"), inv("4", "Zed", "draft", 1, 1, "2026-01-01")]}
    assert all_clients(invs) == ["Alpha Co", "beta Co", "Zed"]


def test_fetch_retries_timeouts_but_not_client_errors():
    import urllib.error
    import urllib.request

    import remind

    class Resp:
        def read(self):
            return b"ok"

    calls = []
    real_open, real_sleep = urllib.request.urlopen, remind.time.sleep
    remind.time.sleep = lambda s: None
    try:
        def flaky(req, timeout=0):
            calls.append(1)
            if len(calls) < 3:
                raise TimeoutError("slow")
            return Resp()
        urllib.request.urlopen = flaky
        assert remind.fetch("x") == b"ok" and len(calls) == 3        # 2 timeouts then success

        calls.clear()
        def denied(req, timeout=0):
            calls.append(1)
            raise urllib.error.HTTPError("x", 401, "no", {}, None)
        urllib.request.urlopen = denied
        try:
            remind.fetch("x")
            assert False
        except urllib.error.HTTPError:
            assert len(calls) == 1                                   # 401 is not retried
    finally:
        urllib.request.urlopen, remind.time.sleep = real_open, real_sleep


def test_html_escapes_and_renders():
    invs = {"Org": [inv("X1", "<script>", "sent", 100, 100, "2026-10-01")]}
    items, stats, outst, oldest = build(invs, {("org", "<script>"): "Ann"}, {}, TODAY)
    page = html_rep("Ann", items, stats, outst, oldest, TODAY)
    assert "<script>" not in page and "&lt;script&gt;" in page and "X1" in page


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("all tests passed")
