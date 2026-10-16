"""Daily collections digest: Zoho Books (read-only) -> sales reps by email.

Zoho is the source of truth; reps/clients/follow-ups come from a Google Sheet (CSV).
Dry run by default: set SEND=1 to really send.
"""
import base64
import csv
import hashlib
import hmac
import io
import json
import os
import smtplib
import statistics
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta, timezone
from email.message import EmailMessage
from html import escape as esc

WAT = timezone(timedelta(hours=1))  # Africa/Lagos
API = "https://www.zohoapis.com/books/v3"  # US data centre
TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
FY_START_MONTH = 1  # fiscal year start (1 = calendar year); collection rate runs from here to today
DRAFT_GRACE = 3  # days before a never-sent draft is flagged
STALE_DRAFT_DAYS = 30  # older drafts leave the daily emails; finance gets them in a weekly (Monday) list
STALE_LIST_MAX = 40  # cap on the Monday dormant-opportunity list
WA_MAX_INVOICES = 3  # invoices per WhatsApp message: signed links are long and wa.me URLs inside emails have a length limit
COUNTRY_CODE = "234"  # Nigeria: a local number like 0803... becomes 234803...
LINK_DAYS = 14  # how long a "Log update" link stays valid
IGNORE_DAYS = 3  # working days with no tap after the first email -> tier goes up one step (manager Cc'd a tier early)
HISTORY_DAYS = 60  # how much of the Log tab feeds the rules
SCORE_DAYS = 30  # window for the manager scorecard
DEFAULT_SEND_CAP = 400  # abort a run that would send more than this (Gmail allows ~500/day on free accounts)
UN = "(unassigned)"
TIERS = [(21, "decision"), (14, "blunt"), (7, "firm"), (0, "coach")]
OPENERS = {
    "early": "{n}, you have open invoices that are not yet due. Confirm each client has received theirs and knows the due date.",
    "coach": "{n}, a quick nudge on your open invoices.",
    "firm": "{n}, some invoices are a week or more overdue. We need movement today.",
    "blunt": "{n}. Money is sitting uncollected. Chase it today and log the result, no excuses.",
    "decision": "{n}, an invoice is 21+ days overdue. Decide today: collect it or escalate it to management, and log it.",
    "drafts": "{n}, some invoices for your clients have not been billed yet. Finance has to issue them; you cannot fix them in Zoho.",
}
ORDER = ["early", "coach", "firm", "blunt", "decision"]


# ---------- pure logic (tested in test_remind.py) ----------
def d(s):
    return date.fromisoformat(s[:10])


def fy_start(today):
    y = today.year if today.month >= FY_START_MONTH else today.year - 1
    return date(y, FY_START_MONTH, 1)


def is_workday(day):
    return day.weekday() < 5


def tier(days):
    for lo, name in TIERS:
        if days >= lo:
            return name
    return "early"  # not yet due


def classify(inv):
    st = inv["status"]
    if st == "void":
        return "void"
    if st == "draft":
        return "draft"
    return "paid" if st == "paid" or inv["balance"] <= 0 else "open"


# ---------- response history (Log tab) -> escalation rule and scorecard ----------
def log_ts(s):
    """Log timestamp -> datetime; tolerates Sheets turning the text into a locale date. None if unreadable."""
    s = (s or "").strip()
    for fmt, text in (("%Y-%m-%dT%H:%M:%S", s[:19]), ("%Y-%m-%d", s[:10]), ("%m/%d/%Y %H:%M:%S", s), ("%m/%d/%Y", s)):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def log_key(org, number):
    return (org.strip().lower(), number.strip())


def sent_keys(r):
    """A 'sent' Log row lists its invoices as 'Org|Number, Org|Number'."""
    return [log_key(*k.split("|", 1)) for k in (r.get("invoice") or "").split(", ") if "|" in k]


def parse_history(rows, today):
    """-> {'last_tap': {key: date}, 'first_sent': {key: date}} over the last HISTORY_DAYS."""
    cut = datetime.combine(today - timedelta(days=HISTORY_DAYS), dtime.min)
    last_tap, first_sent = {}, {}
    for r in sorted(rows, key=lambda r: log_ts(r.get("ts")) or datetime.min):
        ts = log_ts(r.get("ts"))
        if not ts or ts < cut:
            continue
        if r.get("type") == "tap":
            last_tap[log_key(r.get("org") or "", r.get("invoice") or "")] = ts.date()
        elif r.get("type") == "sent":
            for k in sent_keys(r):
                first_sent.setdefault(k, ts.date())
    return dict(last_tap=last_tap, first_sent=first_sent)


def workdays_since(start, today):
    return sum(1 for n in range(1, (today - start).days + 1) if is_workday(start + timedelta(days=n)))


def silent_days(history, org, number, today):
    """Working days since the rep last tapped this invoice (or since it was first emailed). 0 when unknown."""
    if not history:
        return 0
    k = log_key(org, number)
    starts = [x for x in (history["last_tap"].get(k), history["first_sent"].get(k)) if x]
    return workdays_since(max(starts), today) if starts else 0


def scorecard(rows, open_keys, today):
    """Per rep and '__team__' over the last SCORE_DAYS: how many emailed invoices got a tap, how fast, and promises kept.
    A promise counts once its date has passed; it is kept when the invoice is no longer open in Zoho."""
    cut = datetime.combine(today - timedelta(days=SCORE_DAYS), dtime.min)
    sent, taps, promise = {}, defaultdict(list), {}
    for r in sorted(rows, key=lambda r: log_ts(r.get("ts")) or datetime.min):
        ts, rep = log_ts(r.get("ts")), (r.get("rep") or "").strip()
        if not ts or ts < cut or not rep:
            continue
        if r.get("type") == "sent":
            for k in sent_keys(r):
                sent.setdefault((rep, k), ts)
        elif r.get("type") == "tap":
            k = log_key(r.get("org") or "", r.get("invoice") or "")
            taps[(rep, k)].append(ts)
            if r.get("action") == "promised":
                promise[(rep, k)] = log_ts(r.get("detail"))  # latest promise per invoice wins
    out = defaultdict(lambda: dict(sent=0, acks=[], promises=0, kept=0))
    for (rep, k), t0 in sent.items():
        first = next((t for t in taps.get((rep, k), []) if t >= t0), None)
        for who in (rep, "__team__"):
            out[who]["sent"] += 1
            if first:
                out[who]["acks"].append((first - t0).total_seconds() / 86400)
    for (rep, k), due in promise.items():
        if due and due.date() < today:
            for who in (rep, "__team__"):
                out[who]["promises"] += 1
                out[who]["kept"] += k not in open_keys
    return {w: dict(sent=v["sent"], rate=len(v["acks"]) / v["sent"] * 100 if v["sent"] else None,
                    ack=statistics.median(v["acks"]) if v["acks"] else None,
                    kept=v["kept"] / v["promises"] * 100 if v["promises"] else None, promises=v["promises"])
            for w, v in out.items()}


def score_cells(s):
    """-> (responded, median time to first tap, promises kept) as display strings."""
    if not s:
        return "n/a", "n/a", "n/a"
    return (fp(s["rate"]), "n/a" if s["ack"] is None else f"{s['ack']:.1f}d",
            "n/a" if s["kept"] is None else f"{fp(s['kept'])} of {s['promises']}")


def build(invs_by_org, rep_of, follow, today, reps=None, paused=None, promised=None, history=None):
    """paused: optional list that receives invoices a rep set to Dead, so they can be resumed from the email without touching the sheet.
    promised: optional list that receives invoices with a live (future-dated) promise; they leave the nagging list until the date passes.
    history: optional parse_history() result; invoices with no tap for IGNORE_DAYS working days go up one tier."""
    known = {r.lower(): r for r in reps} if reps is not None else None  # None: trust Zoho salesperson as-is
    items = []
    stats = defaultdict(lambda: [0.0, 0.0, 0])  # key -> [invoiced, paid, count]
    outst, oldest = defaultdict(float), defaultdict(int)
    for org, invs in invs_by_org.items():
        for i in invs:
            kind = classify(i)
            if kind == "void":
                continue
            sp = (i.get("salesperson_name") or "").strip()
            if known is not None:  # unknown salesperson falls back to Clients tab, else unassigned (never silently dropped)
                sp = known.get(sp.lower(), "")
            rep = sp or rep_of.get((org.lower(), i["customer_name"].strip().lower()), UN)
            if kind == "draft":  # unbilled: not in the collection rate, tracked as pipeline instead
                stats["__drafts__"][0] += i["total"]
                stats["__drafts__"][2] += 1
            elif d(i["date"]) >= fy_start(today):
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
                if days < 0 and (today - d(i["created_time"])).days < 1:
                    continue  # raised today: reminders start tomorrow
            else:
                days = (today - d(i["created_time"])).days
                if days < DRAFT_GRACE:
                    continue
            fu = follow.get((org.lower(), i["invoice_number"]), {})
            status = (fu.get("Status") or "").strip().lower()
            urgent = status == "client waiting"
            promised_on = (fu.get("Promised pay date") or "").strip()
            broken = False
            if status == "dead":
                if paused is not None and kind == "open":
                    paused.append(dict(rep=rep, org=org, number=i["invoice_number"], client=i["customer_name"], balance=i["balance"],
                                       kind=kind, days=days, paused=True))
                continue
            if status == "promised" and promised_on:
                if d(promised_on) < today:
                    broken = True  # a promise no longer pauses reminders; only a missed one escalates
                elif promised is not None and kind == "open":
                    promised.append(dict(rep=rep, org=org, number=i["invoice_number"], client=i["customer_name"], balance=i["balance"],
                                         kind=kind, days=days, promise=promised_on))
                    continue
            t = tier(days)
            if broken and ORDER.index(t) < ORDER.index("blunt"):
                t = "blunt"
            silent = silent_days(history, org, i["invoice_number"], today) if kind == "open" else 0
            ignored = silent >= IGNORE_DAYS
            if ignored:
                t = ORDER[min(ORDER.index(t) + 1, len(ORDER) - 1)]
            items.append(dict(rep=rep, org=org, number=i["invoice_number"], client=i["customer_name"],
                              balance=i["balance"], kind=kind, days=days, tier=t, broken=broken, ignored=ignored, silent=silent,
                              note=fu.get("Rep note", ""), urgent=urgent, created_by=(i.get("created_by") or "").strip(),
                              url=(i.get("invoice_url") or "").strip()))
    return items, stats, outst, oldest


def pct(s):
    return None if not s or s[0] <= 0 else s[1] / s[0] * 100


def fp(p):
    return "n/a" if p is None else f"{p:.0f}%"


def split(items):
    """-> (open invoices, recent drafts, stale drafts). Drafts are finance's job, so reps only see recent ones."""
    drafts = [i for i in items if i["kind"] == "draft"]
    return ([i for i in items if i["kind"] != "draft"],
            [i for i in drafts if i["days"] < STALE_DRAFT_DAYS], [i for i in drafts if i["days"] >= STALE_DRAFT_DAYS])


def pipeline(stats, money_fmt):
    """Drafts are unbilled opportunities, never voided: shown beside the collection rate, not inside it."""
    s = stats.get("__drafts__")
    return f"Unbilled pipeline: {money_fmt(s[0])} in {s[2]} drafts (kept, never voided)" if s else "Unbilled pipeline: none"


def when(days):
    return f"due in {-days}d" if days < 0 else "due today" if days == 0 else f"{days}d overdue"


def line(it, links=True):
    draft = it["kind"] == "draft"
    what = f"NOT YET BILLED (draft, {it['days']}d old)" if draft else when(it["days"])
    extra = "  ** BROKEN PROMISE **" if it["broken"] else ""
    extra += "  ** CLIENT WAITING **" if it["urgent"] else ""
    extra += f"  ** NO RESPONSE LOGGED, {it['silent']} working days **" if it.get("ignored") else ""
    out = f"- {it['org']} {it['number']} {it['client']}: NGN {it['balance']:,.0f}, {what}{extra}"
    if links and it.get("url"):
        out += f"\n    View: {it['url']}"
    if links and it.get("link") and not it["urgent"]:
        out += f"\n    {'Client is waiting' if draft else 'Update'}: {it['link']}"
    return out


def wa_number(raw):
    n = "".join(c for c in (raw or "") if c.isdigit())
    return COUNTRY_CODE + n[1:] if n.startswith("0") else n


def wa_links(reps, items):
    """Manager's WhatsApp buttons: [(rep, wa.me url, open invoices, no-response count)], neediest rep first.
    Each message carries the rep's most overdue invoices with their Log update links."""
    out = []
    for rep, row in reps.items():
        num = wa_number(row.get("WhatsApp"))
        mine = sorted((i for i in items if i["rep"] == rep and i["kind"] != "draft"), key=lambda i: -i["days"])
        if not (num and mine):
            continue
        lines = [f"Hi {rep.split()[0]}, {len(mine)} invoice(s) still need a status update. Tap each link and log where it stands:"]
        for i in mine[:WA_MAX_INVOICES]:
            lines += ["", f"{i['client']}, NGN {i['balance']:,.0f}, {when(i['days'])}"] + ([i["link"]] if i.get("link") else [])
        if len(mine) > WA_MAX_INVOICES:
            lines += ["", f"+{len(mine) - WA_MAX_INVOICES} more in your email."]
        out.append((rep, f"https://wa.me/{num}?text={urllib.parse.quote(chr(10).join(lines))}", len(mine), sum(1 for i in mine if i.get("ignored"))))
    return sorted(out, key=lambda w: (-w[3], -w[2]))


def wa_group(reps, items):
    """One message for the reps' WhatsApp group: a line per rep, no invoice links or client names (links are signed to one rep, the group sees everyone).
    wa.me/?text= makes WhatsApp ask which chat to send to. '' when nobody has open invoices."""
    lines = []
    for rep in reps:
        mine = [i for i in items if i["rep"] == rep and i["kind"] != "draft"]
        if mine:
            quiet = sum(1 for i in mine if i.get("ignored"))
            lines.append(f"*{rep}*: {len(mine)} invoice(s) need a status update" + (f", {quiet} with no response logged" if quiet else ""))
    if not lines:
        return ""
    text = "\n".join(["Collections: please log where your invoices stand today. Open the link in your email and tap Log update.", "", *lines])
    return f"https://wa.me/?text={urllib.parse.quote(text)}"


def stats_block(rep, items, stats, outst, oldest):
    orgs = sorted({i["org"] for i in items})
    lines = [
        f"Your fiscal-year collection rate: {fp(pct(stats.get(rep)))} (NGN {stats[rep][1]:,.0f} of {stats[rep][0]:,.0f} invoiced)" if rep in stats
        else "Your fiscal-year collection rate: n/a (nothing invoiced this fiscal year)",
        f"Team collection rate: {fp(pct(stats.get('__team__')))}",
        "Org rates: " + (", ".join(f"{o} {fp(pct(stats.get('org:' + o)))}" for o in orgs) or "n/a"),
        f"Your outstanding: NGN {outst[rep]:,.0f}   Oldest unpaid: {oldest[rep]}d   Drafts awaiting billing: {sum(1 for i in items if i['kind'] == 'draft')}",
    ]
    return "\n".join(lines)


def compose_rep(rep, items, stats, outst, oldest, paused=(), promised=()):
    open_, drafts, _ = split(items)
    worst = max((i["tier"] for i in open_), key=ORDER.index) if open_ else None
    first = rep.split()[0]
    parts = [(OPENERS[worst] if worst else OPENERS["drafts"]).format(n=first), "", stats_block(rep, items, stats, outst, oldest), ""]
    if open_:
        parts += ["Needs action:", *[line(i) for i in sorted(open_, key=lambda i: -i["days"])], "",
                  "Tap Update under an invoice to log it: Chasing / Promised (+ date) / Paid / Disputed / Paused."
                  if any(i.get("link") for i in open_) else
                  "Log it in the Follow-ups tab: Status = Chasing / Promised (+ date) / Dead."]
    if drafts:
        parts += ["", "Waiting on billing (finance issues these; nothing for you to fix):",
                  *[line(i) for i in sorted(drafts, key=lambda i: -i["days"])]]
        if any(i.get("link") for i in drafts):
            parts += ["If a client is expecting one, tap Client is waiting so finance prioritises it."]
    promised = [i for i in promised if i.get("link")]
    if promised:
        parts += ["", f"Promised ({len(promised)}): the client promised to pay. No chasing until the date; tap Update if it changes.",
                  *[f"- {i['org']} {i['number']} {i['client']}: NGN {i['balance']:,.0f}, promised {i['promise']}\n    Update: {i['link']}" for i in promised]]
    paused = [i for i in paused if i.get("link")]
    if paused:
        parts += ["", f"Paused ({len(paused)}): reminders are off for these. Tap Resume to turn them back on.",
                  *[f"- {i['org']} {i['number']} {i['client']}: NGN {i['balance']:,.0f}\n    Resume: {i['link']}" for i in paused]]
    subj = f"Collections: {len(open_)} invoice(s) need you today" if open_ else f"Collections: {len(drafts)} invoice(s) waiting on billing"
    return subj, "\n".join(parts), bool(worst) and ORDER.index(worst) >= ORDER.index("blunt")


def draft_sections(items, today):
    """Finance/manager view of drafts: recent ones (urgent first), plus the stale list on Mondays only."""
    _, recent, stale = split(items)
    recent = sorted(recent, key=lambda i: (not i["urgent"], -i["days"]))
    stale = sorted(stale, key=lambda i: -i["balance"])  # biggest opportunity first; stale drafts are never voided
    show_stale = today is not None and today.weekday() == 0
    return recent, stale, (stale[:STALE_LIST_MAX] if show_stale else [])


def compose_manager(items, stats, reps, today=None, score=None):
    rows = [f"- {r}: rate {fp(pct(stats.get(r)))}" for r in reps]
    cells = lambda w: "responded {}, median first tap {}, promises kept {}".format(*score_cells((score or {}).get(w)))  # noqa: E731
    score_rows = [f"- {w if w != '__team__' else 'Team'}: {cells(w)}" for w in [*reps, "__team__"]] if score is not None else []
    open_ = [i for i in items if i["kind"] != "draft"]
    hot = [i for i in open_ if ORDER.index(i["tier"]) >= ORDER.index("blunt") or i["rep"] == UN]
    recent, stale, stale_shown = draft_sections(items, today)
    dline = lambda i: f"- ({i['rep']}, by {i['created_by'] or '?'}) " + line(i, links=False)[2:]  # noqa: E731
    body = "\n".join([
        f"Team collection rate (fiscal year): {fp(pct(stats.get('__team__')))}",
        pipeline(stats, lambda x: f"NGN {x:,.0f}"),
        "By rep:", *rows, "",
        *(["Rep response, last %d days:" % SCORE_DAYS, *score_rows, ""] if score_rows else []),
        "Orgs: " + ", ".join(f"{k[4:]} {fp(pct(v))}" for k, v in sorted(stats.items()) if k.startswith("org:")), "",
        "Escalations / unassigned:", *([f"- ({i['rep']}, {i['tier']}) " + line(i, links=False)[2:] for i in hot] or ["- none"]), "",
        f"Drafts for finance to issue ({len(recent)}; client-waiting first):", *([dline(i) for i in recent] or ["- none"]), "",
        (f"Dormant opportunities, drafts {STALE_DRAFT_DAYS}+ days old (top {len(stale_shown)} of {len(stale)} by value; keep, never void, win these clients back):" if stale_shown
         else f"Dormant opportunities ({STALE_DRAFT_DAYS}+ days old drafts): {len(stale)}, listed every Monday."),
        *[dline(i) for i in stale_shown],
    ])
    return "Collections: manager summary", body


# ---------- HTML email (inline styles only: email clients ignore <style>/external CSS) ----------
TIER_COLOR = {"early": "#0d9488", "coach": "#2563eb", "firm": "#d97706", "blunt": "#ea580c", "decision": "#dc2626"}
INK, MUTED, LINE = "#0f172a", "#64748b", "#e2e8f0"
LINK = "#2563eb"  # same blue as the "coach" tier, so links read as links without adding a new colour


def rate_color(p):  # green >= 75%, amber >= 50%, red below
    return "#94a3b8" if p is None else "#16a34a" if p >= 75 else "#d97706" if p >= 50 else "#dc2626"


def money(x):
    return f"₦{x:,.0f}"


def bar(p, color):
    w = 0 if p is None else min(100, max(0, round(p)))
    fill = f'<td width="{w}%" style="background:{color};height:8px;line-height:8px;font-size:0;border-radius:4px">&nbsp;</td>' if w else ""
    return (f'<table width="100%" cellpadding="0" cellspacing="0" style="background:{LINE};border-radius:4px"><tr>{fill}'
            '<td style="height:8px;line-height:8px;font-size:0">&nbsp;</td></tr></table>')


def pill(text, color):
    return (f'<span style="display:inline-block;padding:3px 10px;border:1px solid {color};border-radius:999px;'
            f'color:{color};font-size:12px;font-weight:600;white-space:nowrap">{text}</span>')


def kpi(label, value, color=INK, sub="", p=None, first=False):
    b = f'<div style="margin-top:10px">{bar(p, color)}</div>' if p is not None else ""
    border = "" if first else f"border-left:1px solid {LINE};"
    return (f'<td valign="top" style="padding:18px 20px;{border}">'
            f'<div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:{MUTED}">{label}</div>'
            f'<div style="font-size:30px;font-weight:700;color:{color};margin-top:4px">{value}</div>{b}'
            f'<div style="font-size:12px;color:{MUTED};margin-top:6px">{sub}</div></td>')


def row(inner, pad="22px 28px", border=True, bg="#fff"):
    top = f"border-top:1px solid {LINE};" if border else ""
    return f'<tr><td style="background:{bg};padding:{pad};{top}">{inner}</td></tr>'


def heading(text):
    return f'<div style="font-size:13px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:{INK};margin-bottom:12px">{text}</div>'


def table(head, rows):
    th = "".join(f'<th align="{a}" style="padding:0 8px 8px;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:{MUTED};font-weight:600;border-bottom:2px solid {LINE}">{h}</th>'
                 for h, a in head)
    return f'<table width="100%" cellpadding="0" cellspacing="0" style="font-size:14px"><tr>{th}</tr>{"".join(rows)}</table>'


def shell(title, sub, rows):
    return (f'<!doctype html><html><body style="margin:0;background:#f1f5f9;font-family:Segoe UI,Helvetica,Arial,sans-serif;color:{INK}">'
            '<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px 12px">'
            '<table width="100%" cellpadding="0" cellspacing="0" style="max-width:640px">'
            f'<tr><td style="background:{INK};border-radius:12px 12px 0 0;padding:26px 28px">'
            f'<div style="font-size:24px;font-weight:700;color:#fff">{title}</div>'
            f'<div style="font-size:13px;color:#94a3b8;margin-top:4px">{sub}</div></td></tr>'
            f'{"".join(rows)}'
            f'<tr><td style="background:#fff;border-top:1px solid {LINE};border-radius:0 0 12px 12px;padding:14px 28px;font-size:11px;color:{MUTED}">'
            'Automated daily brief. Source: Zoho Books. Amounts in NGN.</td></tr>'
            '</table></td></tr></table></body></html>')


def status_pills(it, links=True):
    draft = it["kind"] == "draft"
    p = pill(f"Not yet billed ({it['days']}d)" if draft else when(it["days"]).capitalize(), "#64748b" if draft else TIER_COLOR[it["tier"]])
    p += " " + pill("Broken promise", "#dc2626") if it["broken"] else ""
    p += " " + pill("Client waiting", "#d97706") if it["urgent"] else ""
    p += " " + pill(f"No response ({it['silent']} working days)", "#7c3aed") if it.get("ignored") else ""
    if links and it.get("link") and not it["urgent"]:
        p += (f'<br><a href="{esc(it["link"])}" style="display:inline-block;margin-top:8px;padding:6px 12px;background:{INK};'
              f'color:#fff;border-radius:6px;font-size:12px;font-weight:600;text-decoration:none">{"Client is waiting" if draft else "Log update"}</a>')
    return p


def action_table(items, with_rep=False, links=True, keep_order=False):
    rows = []
    for i in (items if keep_order else sorted(items, key=lambda i: -i["days"])):
        who = f" &middot; {esc(i['rep'])}" if with_rep else ""
        who += f" &middot; by {esc(i['created_by'])}" if with_rep and i["kind"] == "draft" and i["created_by"] else ""
        num = f'<b>{esc(i["number"])}</b>'
        if i.get("url"):  # client-facing page: opens without a Zoho login
            num = f'<a href="{esc(i["url"])}" style="color:{LINK};text-decoration:underline">{num}</a>'
        color = "#94a3b8" if i["kind"] == "draft" else TIER_COLOR[i["tier"]]
        rows.append(
            f'<tr><td style="padding:12px 8px;border-left:4px solid {color};border-bottom:1px solid {LINE}">'
            f'{num}<br><span style="font-size:12px;color:{MUTED}">{esc(i["org"])}{who}</span></td>'
            f'<td style="padding:12px 8px;border-bottom:1px solid {LINE}">{esc(i["client"])}</td>'
            f'<td align="right" style="padding:12px 8px;border-bottom:1px solid {LINE}"><b>{money(i["balance"])}</b></td>'
            f'<td align="right" style="padding:12px 8px;border-bottom:1px solid {LINE}">{status_pills(i, links)}</td></tr>')
    return table([("Invoice", "left"), ("Client", "left"), ("Balance", "right"), ("Status", "right")], rows)


def steps(texts):
    out = []
    for n, t in enumerate(texts, 1):
        out.append(f'<tr><td width="36" valign="top" style="padding:6px 0"><div style="width:26px;height:26px;line-height:26px;border-radius:50%;'
                   f'background:{INK};color:#fff;text-align:center;font-size:13px;font-weight:700">{n}</div></td>'
                   f'<td style="padding:6px 0;font-size:14px;line-height:20px">{t}</td></tr>')
    return f'<table cellpadding="0" cellspacing="0" width="100%">{"".join(out)}</table>'


def paused_table(paused):  # also used for promised invoices: i["promise"] shows under the number, i["btn"] names the button
    rows = [f'<tr><td style="padding:8px"><b>{esc(i["number"])}</b><br><span style="font-size:12px;color:{MUTED}">{esc(i["org"])}{" &middot; promised " + esc(i["promise"]) if i.get("promise") else ""}</span></td>'
            f'<td style="padding:8px">{esc(i["client"])}</td><td align="right" style="padding:8px"><b>{money(i["balance"])}</b></td>'
            f'<td align="right" style="padding:8px"><a href="{esc(i["link"])}" style="display:inline-block;padding:6px 12px;background:#fff;color:{INK};'
            f'border:1px solid {INK};border-radius:6px;font-size:12px;font-weight:600;text-decoration:none">{"Update" if i.get("promise") else "Resume"}</a></td></tr>' for i in paused]
    return table([("Invoice", "left"), ("Client", "left"), ("Balance", "right"), ("", "right")], rows)


def html_rep(rep, items, stats, outst, oldest, today, paused=(), promised=()):
    paused = [i for i in paused if i.get("link")]
    promised = [i for i in promised if i.get("link")]
    open_, drafts, _ = split(items)
    worst = max((i["tier"] for i in open_), key=ORDER.index) if open_ else None
    s, team = stats.get(rep), pct(stats.get("__team__"))
    p = pct(s)
    orgs = " &nbsp;&middot;&nbsp; ".join(f"{esc(o)} <b>{fp(pct(stats.get('org:' + o)))}</b>" for o in sorted({i["org"] for i in items}))
    sub = f"{money(s[1])} of {money(s[0])} invoiced" if s else "Nothing invoiced yet"
    kpis = ('<table width="100%" cellpadding="0" cellspacing="0"><tr>'
            + kpi("Your collection rate", fp(p), rate_color(p), sub, p, first=True)
            + kpi("Team rate", fp(team), rate_color(team), "All reps and unassigned", team)
            + kpi("Outstanding", money(outst[rep]), INK, f"Oldest unpaid: {oldest[rep]}d") + '</tr></table>')
    return shell("Collections brief", f"{today:%A, %d %B %Y} &nbsp;&middot;&nbsp; {esc(rep)}", [
        row(f'<div style="border-left:4px solid {TIER_COLOR[worst or "coach"]};padding:4px 14px;font-size:16px;line-height:24px">'
            f'{esc((OPENERS[worst] if worst else OPENERS["drafts"]).format(n=rep.split()[0]))}</div>', border=False),
        row(kpis, pad="0"),
        row(f'<span style="font-size:12px;color:{MUTED}">Fiscal year to date (from {fy_start(today):%d %b %Y}) &nbsp;|&nbsp; Org rates: {orgs} &nbsp;|&nbsp; Drafts awaiting billing: <b>{len(drafts)}</b></span>',
            pad="12px 28px", bg="#f8fafc"),
        *([row(heading(f"Needs action ({len(open_)})") + action_table(open_)),
           row(heading("What to do today") + steps([
               "Call the clients above, <b>most overdue first</b>. Tap an invoice number to see the invoice and forward it to the client.",
               "Log the outcome with <b>Log update</b>: Chasing, Promised (add the date), Paid, Disputed or Paused.",
           ]))] if open_ else []),
        *([row(heading(f"Waiting on billing ({len(drafts)})")
               + f'<div style="font-size:13px;color:{MUTED};margin-bottom:10px">Finance issues these. Nothing for you to fix; tap <b>Client is waiting</b> if the client is expecting one.</div>'
               + action_table(drafts))] if drafts else []),
        *([row(heading(f"Promised ({len(promised)})")
               + f'<div style="font-size:13px;color:{MUTED};margin-bottom:10px">The client promised to pay. No chasing until the date; tap <b>Update</b> if it changes.</div>'
               + paused_table(promised))] if promised else []),
        *([row(heading(f"Paused ({len(paused)})")
               + f'<div style="font-size:13px;color:{MUTED};margin-bottom:10px">Reminders are off for these. Tap <b>Resume</b> to turn them back on.</div>'
               + paused_table(paused))] if paused else []),
    ])


def html_manager(items, stats, reps, today, score=None, wa=(), group=""):
    team = pct(stats.get("__team__"))
    open_ = [i for i in items if i["kind"] != "draft"]
    urgent = [i for i in open_ if ORDER.index(i["tier"]) >= ORDER.index("blunt")]
    unas = [i for i in open_ if i["rep"] == UN]
    hot = [i for i in open_ if i in urgent or i in unas]
    recent, stale, stale_shown = draft_sections(items, today)
    orgs = " &nbsp;&middot;&nbsp; ".join(f"{esc(k[4:])} <b>{fp(pct(v))}</b>" for k, v in sorted(stats.items()) if k.startswith("org:"))
    kpis = ('<table width="100%" cellpadding="0" cellspacing="0"><tr>'
            + kpi("Team collection rate", fp(team), rate_color(team), "Fiscal year to date", team, first=True)
            + kpi("Escalations", str(len(urgent)), "#dc2626" if urgent else "#16a34a", "14+ days overdue or broken promise")
            + kpi("Unassigned", str(len(unas)), "#dc2626" if unas else "#16a34a", "No rep on the Clients tab") + '</tr></table>')
    rep_rows = []
    for r in reps:
        p = pct(stats.get(r))
        rep_rows.append(f'<tr><td style="padding:9px 8px;border-bottom:1px solid {LINE}">{esc(r)}</td>'
                        f'<td width="45%" style="padding:9px 8px;border-bottom:1px solid {LINE}">{bar(p, rate_color(p))}</td>'
                        f'<td align="right" style="padding:9px 8px;border-bottom:1px solid {LINE}"><b style="color:{rate_color(p)}">{fp(p)}</b></td></tr>')
    score_rows = [f'<tr><td style="padding:9px 8px;border-bottom:1px solid {LINE}">{"<b>Team</b>" if w == "__team__" else esc(w)}</td>'
                  + "".join(f'<td align="right" style="padding:9px 8px;border-bottom:1px solid {LINE}">{c}</td>' for c in score_cells(score.get(w)))
                  + '</tr>' for w in [*reps, "__team__"]] if score is not None else []
    esc_html = (action_table(hot, with_rep=True, links=False) if hot
                else '<div style="color:#16a34a;font-weight:600">All clear. Nothing escalated.</div>')
    note = lambda t: f'<div style="font-size:13px;color:{MUTED};margin-bottom:10px">{t}</div>'  # noqa: E731
    stale_note = (f"Top {len(stale_shown)} of {len(stale)} by value. Keep every draft: each is a client to win back with the original or a better-fitting offer." if stale_shown
                  else f"{len(stale)} drafts are {STALE_DRAFT_DAYS}+ days old. The top ones by value are listed every Monday.")
    return shell("Collections summary", f"{today:%A, %d %B %Y} &nbsp;&middot;&nbsp; Management", [
        row(kpis, pad="0", border=False),
        row(f'<span style="font-size:12px;color:{MUTED}">Org rates: {orgs} &nbsp;|&nbsp; {pipeline(stats, money)}</span>', pad="12px 28px", bg="#f8fafc"),
        row(heading("Collection rate by rep") + table([("Rep", "left"), ("", "left"), ("Rate", "right")], rep_rows)),
        *([row(heading(f"Rep response, last {SCORE_DAYS} days")
               + table([("Rep", "left"), ("Responded", "right"), ("Median first tap", "right"), ("Promises kept", "right")], score_rows)
               + note("Responded: emailed invoices that got a Log update tap. Promises kept: promised dates that passed with the invoice no longer open."))]
          if score_rows else []),
        *([row(heading("Nudge on WhatsApp")
               + (note("<b>Whole group:</b> opens WhatsApp, pick the reps' group, then Send. To buzz a rep, type @ over their bold name.")
                  + f'<a href="{esc(group)}" style="display:inline-block;margin:0 0 14px;padding:10px 18px;background:#15803d;color:#fff;border-radius:6px;'
                    'font-size:14px;font-weight:700;text-decoration:none">Post to the reps group</a>' if group else "")
               + (note("<b>One rep, with their update links</b> (private chat, then Send). No-response reps first.") if wa else "")
               + "".join(f'<a href="{esc(u)}" style="display:inline-block;margin:0 8px 8px 0;padding:8px 14px;background:#16a34a;color:#fff;border-radius:6px;'
                         f'font-size:13px;font-weight:600;text-decoration:none">{esc(r)} &middot; {n}{f" ({k} no response)" if k else ""}</a>' for r, u, n, k in wa))]
          if wa or group else []),
        row(heading("Escalations and unassigned") + esc_html),
        row(heading(f"Drafts for finance to issue ({len(recent)})")
            + (note("Client-waiting first, then oldest.") + action_table(recent, with_rep=True, links=False, keep_order=True)
               if recent else '<div style="color:#16a34a;font-weight:600">No recent drafts.</div>')),
        row(heading(f"Dormant opportunities, drafts {STALE_DRAFT_DAYS}+ days ({len(stale)})") + note(stale_note)
            + (action_table(stale_shown, with_rep=True, links=False, keep_order=True) if stale_shown else "")),
    ])


# ---------- "Log update" links and run log (Apps Script web app, see apps_script/) ----------
def sign(secret, payload):
    """token = base64url(json payload) + '.' + hex HMAC-SHA256 over that base64 string; apps_script/Code.gs verifies it."""
    b = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=").decode()
    return b + "." + hmac.new(secret.encode(), b.encode(), hashlib.sha256).hexdigest()


def add_links(items, url, secret, today):
    exp = (today + timedelta(days=LINK_DAYS)).isoformat()
    for i in items:
        if i["rep"] != UN:  # unassigned has no rep to attribute a tap to
            payload = dict(r=i["rep"], o=i["org"], n=i["number"], e=exp, c=i["client"], a=round(i["balance"]))  # c/a: shown on the tap page; signed, so not forgeable
            if i["kind"] == "draft":
                payload["k"] = "d"  # draft token: the page offers only "Client is waiting"
            elif i.get("paused"):
                payload["k"] = "p"  # paused token: the page offers only "Resume"
            i["link"] = f"{url}?t={sign(secret, payload)}"


def post_events(events, url, secret):
    """Best effort: a logging failure must never stop or undo the reminders themselves."""
    if not (url and secret and events):
        return
    body = json.dumps(events).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    try:
        fetch(urllib.request.Request(f"{url}?sig={sig}", data=body, method="POST",
                                     headers={"Content-Type": "text/plain"}), timeout=30, tries=2)
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: could not write run log ({type(e).__name__}); reminders were unaffected.")


def drafts_event(items, today):
    """Every draft, upserted weekly-or-daily into the Opportunities tab so the upsell agent has the data even if a draft is later deleted in Zoho."""
    rows = [dict(o=i["org"], n=i["number"], client=i["client"], amount=i["balance"], days=i["days"],
                 created=(today - timedelta(days=i["days"])).isoformat(), by=i["created_by"], rep=i["rep"])
            for i in items if i["kind"] == "draft"]
    return dict(id=f"{today}:drafts", type="drafts", rows=rows) if rows else None


def sent_events(msgs_by_rep, today):
    """Event ids are stable per rep/day so a retried POST can never double-log."""
    def worst(its):
        open_ = [i["tier"] for i in its if i["kind"] != "draft"]
        return max(open_, key=ORDER.index) if open_ else "drafts only"
    ev = [dict(id=f"{today}:sent:{rep}", type="sent", rep=rep, detail=f"{len(its)} invoice(s), worst tier {worst(its)}",
               invoices=[f"{i['org']}|{i['number']}" for i in its]) for rep, its in msgs_by_rep.items()]
    return ev + [dict(id=f"{today}:run", type="run", detail=f"{len(msgs_by_rep)} rep email(s)")]


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
        for to, cc, subj, body, html in msgs:
            print(f"[DRY RUN] to={to} cc={cc} subject={subj}")
            if show:
                print(body, "\n" + "-" * 60)
                os.makedirs("preview", exist_ok=True)  # open in a browser to see the HTML email
                path = f"preview/{to}.html"
                open(path, "w", encoding="utf-8").write(html)
                print("HTML preview:", path)
        print(f"{len(msgs)} message(s) prepared, none sent.")
        return
    user = os.environ["GMAIL_USER"]
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(user, os.environ["GMAIL_APP_PASSWORD"])
        for to, cc, subj, body, html in msgs:
            m = EmailMessage()
            m["From"], m["To"], m["Subject"] = user, to, subj
            if cc:
                m["Cc"] = cc
            m.set_content(body)  # plain-text fallback
            m.add_alternative(html, subtype="html")
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


def diagnose(invs, reps_rows, clients, follow, reps, rep_of, items):
    """DIAGNOSE=1: explain who gets what. Counts and names only, no amounts, no emails sent."""
    from collections import Counter
    pr = print
    sec, u = os.environ.get("LINK_SECRET", ""), os.environ.get("APPS_SCRIPT_URL", "")
    pr("LINK_SECRET set:", bool(sec), "| length:", len(sec), "| fingerprint:", hashlib.sha256(sec.encode()).hexdigest()[:8] if sec else "-",
       "| has surrounding whitespace:", sec != sec.strip())
    pr("APPS_SCRIPT_URL host/path shape:", urllib.parse.urlparse(u.strip()).netloc, "/".join(p[:6] for p in urllib.parse.urlparse(u.strip()).path.split("/")), "| contains /u/:", "/u/" in u)
    pr("Reps tab columns:", list(reps_rows[0]) if reps_rows else "EMPTY")
    pr("Reps tab roles:", dict(Counter(r.get("Role", "?").strip() for r in reps_rows)), "| reps used:", len(reps))
    pr("Clients tab columns:", list(clients[0]) if clients else "EMPTY", "| rows:", len(clients), "| mapped to a known rep:", len(rep_of))
    pr("Clients tab Organization values:", dict(Counter(c.get("Organization", "").strip() for c in clients)))
    pr("Followups rows:", len(follow))
    seen = set()
    for org, lst in invs.items():
        pr(f"Org '{org}': invoices by (kind, has invoice_url):", dict(Counter((classify(i), bool((i.get("invoice_url") or "").strip())) for i in lst)),
           "| salesperson_name set on:", sum(1 for i in lst if (i.get("salesperson_name") or "").strip()))
        seen |= {(org.lower(), i["customer_name"].strip().lower()) for i in lst if classify(i) in ("open", "draft")}
    pr("Emailable items by rep:", dict(Counter(i["rep"] for i in items)))
    pr("Items by rep and kind (open / recent draft / stale draft):",
       {r: (sum(1 for i in items if i["rep"] == r and i["kind"] != "draft"),
            sum(1 for i in items if i["rep"] == r and i["kind"] == "draft" and i["days"] < STALE_DRAFT_DAYS),
            sum(1 for i in items if i["rep"] == r and i["kind"] == "draft" and i["days"] >= STALE_DRAFT_DAYS)) for r in sorted({i["rep"] for i in items})})
    pr("Open invoices (client | rep | days overdue | tier | has link):",
       [(i["client"], i["rep"], i["days"], i["tier"], bool(i["url"])) for i in items if i["kind"] != "draft"])
    page = html_manager(items, {}, list(reps), datetime.now(WAT).date())
    pr("Manager HTML: invoice hyperlinks present:", page.count("zohosecurepay.com"))
    pr("Unassigned clients (open/draft items):", sorted({i["client"] for i in items if i["rep"] == UN})[:25])
    pr("Clients-tab rows that match no open/draft invoice:", sorted({c.get("Client name", "").strip() for c in clients
                                                                    if (c.get("Organization", "").strip().lower(), c.get("Client name", "").strip().lower()) not in seen})[:25])


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
    paused = []
    phase2 = os.environ.get("PHASE2") == "1" and os.environ.get("LOG_SRC")  # response-aware rules + scorecard; off until the shadow run is reviewed
    log_rows = read_csv(os.environ["LOG_SRC"]) if phase2 else []
    promised = [] if phase2 else None
    items, stats, outst, oldest = build(invs, rep_of, follow, today, reps, paused, promised, parse_history(log_rows, today) if phase2 else None)
    open_keys = {log_key(o, i["invoice_number"]) for o, lst in invs.items() for i in lst if classify(i) == "open"}
    score = scorecard(log_rows, open_keys, today) if phase2 else None
    if os.environ.get("DIAGNOSE") == "1":
        return diagnose(invs, reps_rows, clients, follow, reps, rep_of, items)
    url, secret = os.environ.get("APPS_SCRIPT_URL"), os.environ.get("LINK_SECRET")
    if url and secret:
        # LINK_PAGE_URL: static page that calls Apps Script via fetch, so Google's /u/N/ account redirect never hits the rep's phone
        add_links(items + paused + (promised or []), (os.environ.get("LINK_PAGE_URL") or url).strip(), secret, today)
    msgs, by_rep = [], {}
    for rep, row in reps.items():
        if os.environ.get("MANAGERS_ONLY") == "1":  # shadow run: managers see the new rules, reps are untouched
            break
        mine = [i for i in items if i["rep"] == rep and not (i["kind"] == "draft" and i["days"] >= STALE_DRAFT_DAYS)]
        if not mine:  # dormant drafts go to management as opportunities, not to reps
            continue
        by_rep[rep] = mine
        mine_paused = [i for i in paused if i["rep"] == rep]
        mine_promised = [i for i in promised or [] if i["rep"] == rep]
        subj, body, escalate = compose_rep(rep, mine, stats, outst, oldest, mine_paused, mine_promised)
        boss = next((m for m in managers if m["Rep"].strip() == row["Manager"].strip()), managers[0] if managers else None)
        msgs.append((row["Email"], boss["Email"] if escalate and boss else "", subj, body,
                     html_rep(rep, mine, stats, outst, oldest, today, mine_paused, mine_promised)))
    if managers:
        subj, body = compose_manager(items, stats, list(reps), today, score)
        page = html_manager(items, stats, list(reps), today, score, wa_links(reps, items), wa_group(reps, items))
        msgs += [(m["Email"], "", subj, body, page) for m in managers]
    send = os.environ.get("SEND") == "1"
    cap = int(os.environ.get("DAILY_SEND_CAP") or DEFAULT_SEND_CAP)
    if send and len(msgs) > cap:
        raise SystemExit(f"Refusing to send {len(msgs)} emails (cap {cap}). Raise DAILY_SEND_CAP if this is expected.")
    deliver(msgs, send)
    if send:
        post_events(sent_events(by_rep, today) + [e for e in [drafts_event(items, today)] if e], url, secret)


if __name__ == "__main__":
    main()
