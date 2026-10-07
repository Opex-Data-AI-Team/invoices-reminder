"""Daily collections digest: Zoho Books (read-only) -> sales reps by email.

Zoho is the source of truth; reps/clients/follow-ups come from a Google Sheet (CSV).
Dry run by default: set SEND=1 to really send.
"""
import csv
import io
import json
import os
import smtplib
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage

WAT = timezone(timedelta(hours=1))  # Africa/Lagos
API = "https://www.zohoapis.com/books/v3"  # US data centre
TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
WINDOW = 30  # days used for collection rate
DRAFT_GRACE = 3  # days before a never-sent draft is flagged
MIN_RANK = 3  # invoices in window needed to be ranked
UN = "(unassigned)"
TIERS = [(21, "decision"), (14, "blunt"), (7, "firm"), (0, "coach")]
OPENERS = {
    "coach": "{n}, a quick nudge on your open invoices.",
    "firm": "{n}, some invoices are a week or more overdue. We need movement today.",
    "blunt": "{n}. Money is sitting uncollected. Chase it today and log the result, no excuses.",
    "decision": "{n}, an invoice is 21+ days overdue. Decide today: collect, void or write off, and log it.",
}
ORDER = ["coach", "firm", "blunt", "decision"]


# ---------- pure logic (tested in test_remind.py) ----------
def d(s):
    return date.fromisoformat(s[:10])


def is_workday(day):
    return day.weekday() < 5


def tier(days):
    for lo, name in TIERS:
        if days >= lo:
            return name
    return "coach"


def classify(inv):
    st = inv["status"]
    if st == "void":
        return "void"
    if st == "draft":
        return "draft"
    return "paid" if st == "paid" or inv["balance"] <= 0 else "open"


def build(invs_by_org, rep_of, follow, today):
    items = []
    stats = defaultdict(lambda: [0.0, 0.0, 0])  # key -> [invoiced, paid, count]
    outst, oldest = defaultdict(float), defaultdict(int)
    for org, invs in invs_by_org.items():
        for i in invs:
            kind = classify(i)
            if kind == "void":
                continue
            rep = rep_of.get((org.lower(), i["customer_name"].strip().lower()), UN)
            if kind in ("paid", "open") and (today - d(i["date"])).days <= WINDOW:
                for k in (rep, "__team__", "org:" + org):
                    s = stats[k]
                    s[0] += i["total"]
                    s[1] += i["total"] - i["balance"]
                    s[2] += 1
            if kind == "paid":
                continue
            if kind == "open":
                days = (today - d(i["due_date"])).days
                outst[rep] += i["balance"]
                oldest[rep] = max(oldest[rep], days)
                if days < 0:
                    continue
            else:
                days = (today - d(i["created_time"])).days
                if days < DRAFT_GRACE:
                    continue
            fu = follow.get((org.lower(), i["invoice_number"]), {})
            status = (fu.get("Status") or "").strip().lower()
            promised = (fu.get("Promised pay date") or "").strip()
            broken = False
            if status == "dead":
                continue
            if status == "promised" and promised:
                if d(promised) >= today:
                    continue
                broken = True
            t = tier(days)
            if broken and ORDER.index(t) < ORDER.index("blunt"):
                t = "blunt"
            items.append(dict(rep=rep, org=org, number=i["invoice_number"], client=i["customer_name"],
                              balance=i["balance"], kind=kind, days=days, tier=t, broken=broken,
                              note=fu.get("Rep note", "")))
    return items, stats, outst, oldest


def pct(s):
    return None if not s or s[0] <= 0 else s[1] / s[0] * 100


def fp(p):
    return "n/a" if p is None else f"{p:.0f}%"


def rank(stats, rep):
    elig = {k: v for k, v in stats.items()
            if k not in (UN, "__team__") and not k.startswith("org:") and v[2] >= MIN_RANK and v[0] > 0}
    if rep not in elig:
        return None
    order = sorted(elig, key=lambda k: -pct(elig[k]))
    return order.index(rep) + 1, len(order)


def line(it):
    what = "NEVER SENT (draft)" if it["kind"] == "draft" else f"{it['days']}d overdue"
    extra = "  ** BROKEN PROMISE **" if it["broken"] else ""
    return f"- [{it['tier'].upper()}] {it['org']} {it['number']} {it['client']}: NGN {it['balance']:,.0f}, {what}{extra}"


def stats_block(rep, items, stats, outst, oldest):
    r = rank(stats, rep)
    orgs = sorted({i["org"] for i in items})
    lines = [
        f"Your 30-day collection rate: {fp(pct(stats.get(rep)))} (NGN {stats[rep][1]:,.0f} of {stats[rep][0]:,.0f} invoiced)" if rep in stats
        else "Your 30-day collection rate: n/a (nothing invoiced in the window)",
        f"Team collection rate: {fp(pct(stats.get('__team__')))}   Your rank: {f'{r[0]} of {r[1]}' if r else 'n/a'}",
        "Org rates: " + (", ".join(f"{o} {fp(pct(stats.get('org:' + o)))}" for o in orgs) or "n/a"),
        f"Your outstanding: NGN {outst[rep]:,.0f}   Oldest unpaid: {oldest[rep]}d   Drafts never sent: {sum(1 for i in items if i['kind'] == 'draft')}",
    ]
    return "\n".join(lines)


def compose_rep(rep, items, stats, outst, oldest):
    worst = max((i["tier"] for i in items), key=ORDER.index)
    first = rep.split()[0]
    body = "\n".join([
        OPENERS[worst].format(n=first), "",
        stats_block(rep, items, stats, outst, oldest), "",
        "Needs action:", *[line(i) for i in sorted(items, key=lambda i: -i["days"])], "",
        "Log it in the Follow-ups tab: Status = Chasing / Promised (+ date) / Dead.",
    ])
    return f"Collections: {len(items)} invoice(s) need you today ({worst})", body, ORDER.index(worst) >= 2


def compose_manager(items, stats, reps):
    rows = [f"- {r}: rate {fp(pct(stats.get(r)))}" for r in reps]
    hot = [i for i in items if ORDER.index(i["tier"]) >= 2 or i["rep"] == UN]
    body = "\n".join([
        f"Team collection rate (30d): {fp(pct(stats.get('__team__')))}",
        "By rep:", *rows, "",
        "Orgs: " + ", ".join(f"{k[4:]} {fp(pct(v))}" for k, v in sorted(stats.items()) if k.startswith("org:")), "",
        "Escalations / unassigned:", *([f"- ({i['rep']}) " + line(i)[2:] for i in hot] or ["- none"]),
    ])
    return "Collections: manager summary", body


# ---------- IO ----------
def fetch(req, timeout=90, tries=3):
    """urlopen with retries: Apps Script cold starts can be slow, 5xx and timeouts are transient."""
    for n in range(tries):
        try:
            return urllib.request.urlopen(req, timeout=timeout).read()
        except urllib.error.HTTPError as e:
            if e.code < 500 or n == tries - 1:
                raise
        except (TimeoutError, urllib.error.URLError):
            if n == tries - 1:
                raise
        time.sleep(3 * 2 ** n)


def read_csv(src):
    if src.startswith("http"):
        text = fetch(src).decode("utf-8-sig")
    else:
        text = open(src, encoding="utf-8-sig").read()
    return list(csv.DictReader(io.StringIO(text)))


def zoho_token():
    q = urllib.parse.urlencode({"grant_type": "refresh_token", "client_id": os.environ["ZOHO_CLIENT_ID"],
                                "client_secret": os.environ["ZOHO_CLIENT_SECRET"],
                                "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"]})
    res = json.loads(fetch(urllib.request.Request(f"{TOKEN_URL}?{q}", method="POST")))
    if "access_token" not in res:
        raise SystemExit(f"Zoho token error: {res.get('error')}")
    return res["access_token"]


def fetch_invoices(token, org_id):
    out, page = [], 1
    while True:
        q = urllib.parse.urlencode({"organization_id": org_id, "page": page, "per_page": 200})
        req = urllib.request.Request(f"{API}/invoices?{q}", headers={"Authorization": f"Zoho-oauthtoken {token}"})
        res = json.loads(fetch(req))
        out += res["invoices"]
        if not res["page_context"]["has_more_page"]:
            return out
        page += 1
        time.sleep(0.7)  # stay well under 100 req/min


def deliver(msgs, send):
    # never print bodies in CI: public-repo logs would expose client data
    show = not os.environ.get("GITHUB_ACTIONS")
    if not send:
        for to, cc, subj, body in msgs:
            print(f"[DRY RUN] to={to} cc={cc} subject={subj}")
            if show:
                print(body, "\n" + "-" * 60)
        print(f"{len(msgs)} message(s) prepared, none sent.")
        return
    user = os.environ["GMAIL_USER"]
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(user, os.environ["GMAIL_APP_PASSWORD"])
        for to, cc, subj, body in msgs:
            m = EmailMessage()
            m["From"], m["To"], m["Subject"] = user, to, subj
            if cc:
                m["Cc"] = cc
            m.set_content(body)
            s.send_message(m)
    print(f"{len(msgs)} message(s) sent.")


def client_list(invs_by_org):
    """(Organization, Client name, Rep='') for clients with unpaid or draft invoices, biggest balance first."""
    rows = []
    for org, invs in invs_by_org.items():
        owed = defaultdict(float)
        for i in invs:
            if classify(i) in ("open", "draft"):
                owed[i["customer_name"].strip()] += i["balance"]
        rows += [(org, name, "") for name, _ in sorted(owed.items(), key=lambda kv: -kv[1])]
    return rows


def all_clients(invs_by_org):
    """Distinct client names across every invoice of any status, A-Z, for the Region/Rep sheet."""
    return sorted({i["customer_name"].strip() for invs in invs_by_org.values() for i in invs}, key=str.lower)


def export_clients(invs):
    emit([("Organization", "Client name", "Rep"), *client_list(invs)])


def export_regions(invs):
    emit([("Client", "Region", "Rep"), *[(n, "", "") for n in all_clients(invs)]])


def emit(rows):
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    print(buf.getvalue())
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:  # shows as a copyable block on the run page (private repo only)
        with open(summary, "a", encoding="utf-8") as f:
            f.write("```csv\n" + buf.getvalue() + "```\n")


def main():
    today = datetime.now(WAT).date()
    if not is_workday(today) and not os.environ.get("FORCE"):
        return print("Weekend, skipping.")
    if os.environ.get("INVOICES_FIXTURE"):  # offline testing
        invs = json.load(open(os.environ["INVOICES_FIXTURE"]))
    else:
        token = zoho_token()
        invs = {n.strip(): fetch_invoices(token, i.strip())
                for n, i in (p.split("=") for p in os.environ["ZOHO_ORGS"].split(","))}
    if os.environ.get("LIST_CLIENTS") == "1":
        return export_clients(invs)
    if os.environ.get("LIST_CLIENTS") == "all":
        return export_regions(invs)
    reps_rows = read_csv(os.environ["REPS_SRC"])
    clients = read_csv(os.environ["CLIENTS_SRC"])
    follow = {(r["Organization"].strip().lower(), r["Invoice"].strip()): r
              for r in read_csv(os.environ["FOLLOWUPS_SRC"])}
    reps = {r["Rep"].strip(): r for r in reps_rows if r["Role"].strip() == "rep"}
    managers = [r for r in reps_rows if r["Role"].strip() == "manager"]
    rep_of = {(c["Organization"].strip().lower(), c["Client name"].strip().lower()): c["Rep"].strip()
              for c in clients if c["Rep"].strip() in reps}
    items, stats, outst, oldest = build(invs, rep_of, follow, today)
    msgs = []
    for rep, row in reps.items():
        mine = [i for i in items if i["rep"] == rep]
        if not mine:
            continue
        subj, body, escalate = compose_rep(rep, mine, stats, outst, oldest)
        boss = next((m for m in managers if m["Rep"].strip() == row["Manager"].strip()), managers[0] if managers else None)
        msgs.append((row["Email"], boss["Email"] if escalate and boss else "", subj, body))
    if managers:
        subj, body = compose_manager(items, stats, list(reps))
        msgs += [(m["Email"], "", subj, body) for m in managers]
    deliver(msgs, os.environ.get("SEND") == "1")


if __name__ == "__main__":
    main()
