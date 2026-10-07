"""Run: python test_remind.py  (no dependencies)"""
from datetime import date

from remind import UN, build, compose_rep, is_workday, pct, rank, tier

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
]}
REP_OF = {("org", "a"): "Ann", ("org", "b"): "Bob"}
FOLLOW = {("org", "I7"): {"Status": "Dead"},
          ("org", "I8"): {"Status": "Promised", "Promised pay date": "2026-10-10"},
          ("org", "I9"): {"Status": "Promised", "Promised pay date": "2026-10-02"}}


def run():
    return build(INVS, REP_OF, FOLLOW, TODAY)


def test_tiers():
    assert [tier(x) for x in (0, 6, 7, 13, 14, 20, 21)] == ["coach", "coach", "firm", "firm", "blunt", "blunt", "decision"]


def test_workday():
    assert is_workday(date(2026, 10, 6)) and not is_workday(date(2026, 10, 10)) and not is_workday(date(2026, 10, 11))


def test_items_and_followups():
    items, *_ = run()
    got = {i["number"]: i for i in items}
    assert set(got) == {"I2", "I3", "I5", "I9", "I10"}           # I4 grace, I6 void, I7 dead, I8 promised
    assert got["I2"]["tier"] == "blunt" and got["I5"]["tier"] == "coach"
    assert got["I9"]["broken"] and got["I9"]["tier"] == "blunt"  # broken promise bumps to blunt
    assert got["I3"]["kind"] == "draft" and got["I10"]["rep"] == UN


def test_rates():
    _, stats, outst, oldest = run()
    assert pct(stats["Ann"]) == 1000 / (1000 + 1000) * 100       # I1 paid, I2 unpaid (drafts excluded)
    assert pct(stats["__team__"]) is not None
    assert outst["Ann"] == 1000 and oldest["Ann"] == 14


def test_rank_needs_min_invoices():
    _, stats, *_ = run()
    assert rank(stats, "Ann") is None                            # only 2 invoices in window


def test_message_has_stats_and_tone():
    items, stats, outst, oldest = run()
    subj, body, escalate = compose_rep("Ann Obi", [i for i in items if i["rep"] == "Ann"], stats, outst, oldest)
    assert escalate and "Chase it today" in body and "collection rate" in body and "never sent" in body.lower()


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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("all tests passed")
