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
    assert pct(stats["Ann"]) == 1000 / 2000 * 100                # I1 paid, I2 unpaid; drafts and void are not in the rate
    assert stats["__drafts__"][0] == 1000 and stats["__drafts__"][2] == 2   # I3 + I4 tracked as pipeline
    assert pct(stats["__team__"]) is not None
    assert outst["Ann"] == 1000 and oldest["Ann"] == 14


def test_message_has_stats_and_tone():
    items, stats, outst, oldest = run()
    subj, body, escalate = compose_rep("Ann Obi", [i for i in items if i["rep"] == "Ann"], stats, outst, oldest)
    assert escalate and "Chase it today" in body and "collection rate" in body and "not yet billed" in body.lower() and "Waiting on billing" in body


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


def verify(secret, token):
    """Mirror of apps_script/Code.gs verifyToken: returns payload or None."""
    import base64
    import hashlib
    import hmac
    import json
    b, _, sig = token.partition(".")
    if not hmac.compare_digest(sig, hmac.new(secret.encode(), b.encode(), hashlib.sha256).hexdigest()):
        return None
    return json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)))


def test_token_roundtrip_and_tamper():
    from remind import sign
    t = sign("s3cret", dict(r="Ann", o="Org", n="I2", e="2026-10-20"))
    assert verify("s3cret", t) == dict(r="Ann", o="Org", n="I2", e="2026-10-20")
    assert verify("wrong", t) is None
    b, sig = t.split(".")
    assert verify("s3cret", b[:-2] + "AA." + sig) is None and verify("s3cret", b + "." + "0" * 64) is None


def test_links_in_emails_only_when_configured():
    from remind import add_links
    items, stats, outst, oldest = run()
    ann = [i for i in items if i["rep"] == "Ann"]
    assert "Update:" not in compose_rep("Ann", ann, stats, outst, oldest)[1]           # no secret -> old behaviour
    assert "?t=" not in html_rep("Ann", ann, stats, outst, oldest, TODAY)
    add_links(items, "https://x.test/exec", "k", TODAY)
    assert all("link" not in i for i in items if i["rep"] == UN)                       # unassigned gets no link
    body = compose_rep("Ann", ann, stats, outst, oldest)[1]
    assert body.count("Update: https://x.test/exec?t=") == 1 and body.count("Client is waiting: https://x.test/exec?t=") == 1  # I2 open, I3 draft
    draft = next(i for i in ann if i["kind"] == "draft")
    assert verify("k", draft["link"].split("?t=")[1])["k"] == "d" and "k" not in verify("k", ann[0]["link"].split("?t=")[1])
    assert "exec?t=" in html_rep("Ann", ann, stats, outst, oldest, TODAY)
    p = verify("k", ann[0]["link"].split("?t=")[1])
    assert p["r"] == "Ann" and p["n"] == ann[0]["number"] and p["e"] == "2026-10-20"   # 14 days out


def test_run_log_failure_never_raises_and_ids_are_stable():
    import urllib.request

    import remind
    items, *_ = run()
    ev = remind.sent_events({"Ann": [i for i in items if i["rep"] == "Ann"]}, TODAY)
    assert [e["id"] for e in ev] == ["2026-10-06:sent:Ann", "2026-10-06:run"] and ev[0]["type"] == "sent"
    real = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(TimeoutError("down"))
    real_sleep, remind.time.sleep = remind.time.sleep, lambda s: None
    try:
        remind.post_events(ev, "https://x.test/exec", "k")                             # must swallow the error
    finally:
        urllib.request.urlopen, remind.time.sleep = real, real_sleep


def test_drafts_split_cta_and_stale_list():
    from datetime import date as D

    from remind import add_links, compose_manager, compose_rep, html_manager, split
    invs = {"Org": INVS["Org"] + [inv("D1", "A", "draft", 100, 100, "2026-08-01")]}      # 66d old draft
    fol = {**FOLLOW, ("org", "I3"): {"Status": "Client waiting"}}
    items, stats, outst, oldest = build(invs, REP_OF, fol, TODAY)
    ann = [i for i in items if i["rep"] == "Ann"]
    open_, recent, stale = split(ann)
    assert [i["number"] for i in recent] == ["I3"] and [i["number"] for i in stale] == ["D1"] and {i["number"] for i in open_} == {"I2"}
    assert next(i for i in ann if i["number"] == "I3")["urgent"]
    # rep email: open invoice + recent draft, never the stale one; flagged draft gets no second button
    add_links(items, "https://x.test/exec", "k", TODAY)
    subj, body, esc_ = compose_rep("Ann", [i for i in ann if i["number"] != "D1"], stats, outst, oldest)
    assert "D1" not in body and "CLIENT WAITING" in body and "Client is waiting:" not in body and subj.startswith("Collections: 1 invoice")
    # a rep with only a recent draft still gets a (draft-only) email and is not escalated
    only = [i for i in items if i["number"] == "I3"]
    subj, body, esc_ = compose_rep("Ann", only, stats, outst, oldest)
    assert subj.endswith("waiting on billing") and not esc_ and "Needs action" not in body
    assert "Finance has to issue" in html_rep("Ann", only, stats, outst, oldest, TODAY) or "Waiting on billing" in html_rep("Ann", only, stats, outst, oldest, TODAY)
    # manager: drafts for finance every day, stale list only on Mondays, no rep tokens leaked
    tue = compose_manager(items, stats, ["Ann", "Bob"], D(2026, 10, 6))[1]
    mon = compose_manager(items, stats, ["Ann", "Bob"], D(2026, 10, 12))[1]
    assert "I3" in tue and "D1" not in tue and "listed every Monday" in tue
    assert "D1" in mon and "Update:" not in mon and "Client is waiting:" not in mon and "?t=" not in mon
    assert "?t=" not in html_manager(items, stats, ["Ann", "Bob"], D(2026, 10, 12))


def test_invoice_url_is_stripped_and_optional():
    a, b = inv("U1", "A", "sent", 10, 10, "2026-10-01"), inv("U2", "A", "sent", 10, 10, "2026-10-01")
    a["invoice_url"] = "https://zohosecurepay.com/books/regtech/secure?CInvoiceID=abc "   # Zoho returns a trailing space
    items, stats, outst, oldest = build({"Org": [a, b]}, REP_OF, {}, TODAY)
    got = {i["number"]: i for i in items}
    assert got["U1"]["url"].endswith("abc") and got["U2"]["url"] == ""
    page = html_rep("Ann", items, stats, outst, oldest, TODAY)
    assert 'href="https://zohosecurepay.com/books/regtech/secure?CInvoiceID=abc"' in page.replace("&amp;", "&")
    body = compose_rep("Ann", items, stats, outst, oldest)[1]
    assert body.count("View: https://") == 1


def test_drafts_pipeline_and_snapshot():
    from datetime import date as D

    import remind
    invs = {"Org": INVS["Org"] + [inv("D1", "A", "draft", 700, 700, "2026-08-01")]}
    items, stats, *_ = build(invs, REP_OF, {}, TODAY)
    assert stats["__drafts__"][0] == 1000 + 700 and stats["__drafts__"][2] == 3        # I3, I4, D1 (I4 is inside the grace window but still pipeline)
    assert "NGN 1,700 in 3 drafts" in remind.compose_manager(items, stats, ["Ann"], D(2026, 10, 6))[1]
    assert "₦1,700 in 3 drafts" in remind.html_manager(items, stats, ["Ann"], D(2026, 10, 6))
    ev = remind.drafts_event(items, TODAY)
    assert ev["type"] == "drafts" and {r["n"] for r in ev["rows"]} == {"I3", "D1"}      # grace-window I4 not yet emailed, so not in items
    d1 = next(r for r in ev["rows"] if r["n"] == "D1")
    assert d1["amount"] == 700 and d1["created"] == "2026-08-01" and d1["days"] == 66
    assert remind.drafts_event([i for i in items if i["kind"] != "draft"], TODAY) is None


def test_paused_invoices_can_be_resumed_from_the_email():
    from remind import add_links
    paused = []
    items, stats, outst, oldest = build(INVS, REP_OF, FOLLOW, TODAY, paused=paused)
    assert [i["number"] for i in paused] == ["I7"] and paused[0]["rep"] == "Bob" and paused[0]["paused"]
    assert "I7" not in {i["number"] for i in items}                                    # still out of the reminders and totals
    bob = [i for i in items if i["rep"] == "Bob"]
    assert "Paused (" not in compose_rep("Bob", bob, stats, outst, oldest)[1]            # no links -> nothing to show
    add_links(items + paused, "https://x.test/", "k", TODAY)
    assert verify("k", paused[0]["link"].split("?t=")[1])["k"] == "p"
    body = compose_rep("Bob", bob, stats, outst, oldest, paused)[1]
    assert "Paused (1)" in body and body.count("Resume: https://x.test/?t=") == 1
    assert "Resume</a>" in html_rep("Bob", bob, stats, outst, oldest, TODAY, paused)
    assert "Paused (" not in compose_rep("Ann", [i for i in items if i["rep"] == "Ann"], stats, outst, oldest)[1]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("all tests passed")
