#!/usr/bin/env python3
"""Generate the weekly recompete brief.

WHY THIS NEEDS NO DIFFING OR STORED STATE
-----------------------------------------
A contract "crosses into the 12-month planning window" purely as a function of
today's date and its own end date. Nothing about the source data has to change
for that to happen — time passing is the event. So this week's crossers are
exactly the contracts whose end date is 358-365 days away, and that can be read
off a single snapshot. No previous-week database, nothing to drift out of sync,
no risk of double-sending or missing a week if a run is skipped.

CRITICAL: the pipeline's own `days_to_expiry` is baked in at INGEST time. The
source data refreshes monthly but this brief runs weekly, so that field can be
up to ~30 days stale. Everything here is recomputed from `delivery_date`
against the send date instead. Using the stored field would silently select the
wrong contracts.

PRIVACY: `buyer_name` in the source contains named individuals (4,318 of them).
It is never emitted. There is a test for this.

Python standard library only, deliberately.
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import date, datetime, timedelta
from typing import Any, Optional

# The suppression rule is IMPORTED, never copied. The site withholds the names
# of vendors who are private people; this file reads the pipeline JSON straight
# from the ingest, where vendor_name is still raw. Without this import the email
# would send out exactly what the site hides. Importing keeps one rule in one
# place so the two cannot drift apart.
import build_site

# Where readers reply. Kept as a constant rather than a parameter so adding it
# does not change every render signature and every self-test call site.
CONTACT_EMAIL = "hello@recompeteradar.ca"

# One short question to readers, set per week from --question. The default is a
# standing question, so a week where nobody sets one still reads as intended
# rather than breaking or going out blank.
DEFAULT_QUESTION = ("What would make this worth opening every week? "
                    "Reply and tell me.")
BRIEF_QUESTION = DEFAULT_QUESTION

# An optional block for the occasional note from the operator, set from --note.
# Empty means nothing is rendered at all.
BRIEF_NOTE = ""

WINDOW_DAYS = 365          # the planning threshold subscribers care about
LOOKBACK_DAYS = 7          # one week's worth of crossings
MAX_ROWS = 25              # editorial cap; the rest are pointed at the site

# Fields that must never reach a subscriber's inbox.
FORBIDDEN_FIELDS = ("buyer_name",)


def esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def money(v: Optional[float]) -> str:
    v = v or 0
    if v >= 1e9:
        return f"${v/1e9:,.1f}B"
    if v >= 1e6:
        return f"${v/1e6:,.1f}M"
    if v >= 1e3:
        return f"${v/1e3:,.0f}K"
    return f"${v:,.0f}"


def parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(s)[:19], fmt).date()
        except ValueError:
            continue
    return None


def crossings(rows: list[dict], as_of: date,
              window: int = WINDOW_DAYS, lookback: int = LOOKBACK_DAYS) -> list[dict]:
    """Contracts that passed the `window`-day threshold in the last `lookback` days.

    Recomputed from delivery_date against as_of — never from the stored
    days_to_expiry, which is frozen at ingest time.
    """
    out = []
    for r in rows:
        d = parse_date(r.get("delivery_date"))
        if not d:
            continue
        days = (d - as_of).days
        if window - lookback < days <= window:
            r = dict(r)
            r["_days"] = days
            out.append(r)
    out.sort(key=lambda r: (-(r.get("contract_value") or 0), r.get("vendor_name") or ""))
    return out


def competition_note(r: dict) -> str:
    """Reads as a clause after the end date, e.g. 'ends 2027-08-11 · drew 1 bid'.

    Bid counts are absent for ~18% of contracts and 'zero bids' is more likely a
    reporting convention than a literal absence of bidders, so neither is
    presented as if it were a confirmed fact.
    """
    n = r.get("number_of_bids")
    if n is None:
        return "bid count not reported"
    if n == 0:
        return "no bids recorded"
    if n == 1:
        return "drew 1 bid — uncontested"
    return f"drew {n} bids"


def render_text(rows: list[dict], as_of: date, business: str, address: str,
                site: str) -> str:
    total = sum(r.get("contract_value") or 0 for r in rows)
    shown = rows[:MAX_ROWS]
    L = []
    L.append(f"RECOMPETE BRIEF — week ending {as_of:%d %B %Y}")
    L.append("")
    if not rows:
        L.append("No federal contracts crossed the 12-month planning threshold "
                 "this week. Next edition in seven days.")
    else:
        L.append(f"{len(rows)} contracts just crossed the 12-month mark, "
                 f"{money(total)} in total value.")
        L.append("Agencies typically begin recompete planning 12-18 months out, "
                 "so these are the ones to be asking about now.")
        L.append("")
        if BRIEF_NOTE:
            L.append(BRIEF_NOTE)
            L.append("")
        for r in shown:
            L.append(f"* {money(r.get('contract_value'))} — {r.get('vendor_name') or 'Unknown'}")
            L.append(f"  {r.get('buyer_org') or 'Unknown department'}")
            L.append(f"  {r.get('category_name') or 'Uncategorised'}")
            L.append(f"  ends {r.get('delivery_date')} · {competition_note(r)}")
            L.append("")
        if len(rows) > MAX_ROWS:
            L.append(f"...and {len(rows) - MAX_ROWS} more. Full list: {site}")
            L.append("")
    if BRIEF_QUESTION:
        L.append("-" * 60)
        L.append(BRIEF_QUESTION)
        L.append("")
    L.append("-" * 60)
    L.append(f"{business}, {address}")
    L.append(site)
    L.append(f"Questions, corrections or suggestions: {CONTACT_EMAIL}")
    L.append("")
    L.append("You are getting this because you signed up for the weekly brief "
             f"at {site}. The site gets changed based on what readers ask for, "
             "so tell me what is missing. Unsubscribe any time using the link "
             "below.")
    return "\n".join(L)


def render_html(rows: list[dict], as_of: date, business: str, address: str,
                site: str) -> str:
    total = sum(r.get("contract_value") or 0 for r in rows)
    shown = rows[:MAX_ROWS]

    if not rows:
        body = ('<p style="margin:0 0 16px">No federal contracts crossed the '
                '12&#8209;month planning threshold this week. Next edition in '
                'seven days.</p>')
    else:
        trs = []
        for r in shown:
            trs.append(
                '<tr>'
                f'<td style="padding:10px 8px;border-bottom:1px solid #e6e6e6;'
                f'font-weight:600;white-space:nowrap">{esc(money(r.get("contract_value")))}</td>'
                f'<td style="padding:10px 8px;border-bottom:1px solid #e6e6e6">'
                f'<strong>{esc(r.get("vendor_name") or "Unknown")}</strong><br>'
                f'<span style="color:#666;font-size:13px">{esc(r.get("buyer_org") or "")}</span><br>'
                f'<span style="color:#666;font-size:13px">{esc(r.get("category_name") or "")}</span></td>'
                f'<td style="padding:10px 8px;border-bottom:1px solid #e6e6e6;'
                f'white-space:nowrap;font-size:13px;color:#666">'
                f'ends {esc(r.get("delivery_date"))}<br>{esc(competition_note(r))}</td>'
                '</tr>')
        more = ""
        if len(rows) > MAX_ROWS:
            more = (f'<p style="margin:16px 0 0;font-size:14px">…and '
                    f'<strong>{len(rows) - MAX_ROWS:,} more</strong> this week. '
                    f'<a href="{esc(site)}" style="color:#1a5fb4">See the full list →</a></p>')
        body = (
            f'<p style="margin:0 0 6px;font-size:16px"><strong>{len(rows):,} contracts</strong> '
            f'just crossed the 12&#8209;month mark — <strong>{esc(money(total))}</strong> '
            f'in total value.</p>'
            f'<p style="margin:0 0 18px;color:#555;font-size:14px">Agencies typically begin '
            f'recompete planning 12&ndash;18 months out, so these are the ones worth asking '
            f'about now.</p>'
            f'<table style="width:100%;border-collapse:collapse;font-size:14px">'
            f'<tbody>{"".join(trs)}</tbody></table>{more}')

    return f"""<div style="max-width:640px;margin:0 auto;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1a1a1a;line-height:1.5">
<p style="margin:0 0 4px;font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#888">Recompete Brief</p>
<h1 style="margin:0 0 20px;font-size:20px">Week ending {as_of:%d %B %Y}</h1>
{body}
<hr style="border:0;border-top:1px solid #e6e6e6;margin:28px 0 14px">
<p style="margin:0 0 6px;font-size:12px;color:#888">
{esc(business)}, {esc(address)}<br>
<a href="{esc(site)}" style="color:#888">{esc(site)}</a>
</p>
<p style="margin:0;font-size:12px;color:#888">
You are getting this because you signed up for the weekly brief at recompeteradar.ca. Questions, corrections or suggestions go to {esc(CONTACT_EMAIL)}, and the site gets changed based on what readers ask for. You can
unsubscribe at any time using the link below.
</p>
</div>"""


def self_test() -> int:
    """Offline checks. Failures here mean the brief would be wrong."""
    fails = []
    today = date(2026, 8, 11)

    def mk(days_out, **kw):
        r = {"delivery_date": (today + timedelta(days=days_out)).isoformat(),
             "contract_value": 1000.0, "vendor_name": "V", "buyer_org": "B",
             "category_name": "C", "number_of_bids": 1,
             "days_to_expiry": 99999}          # deliberately wrong/stale
        r.update(kw)
        return r

    # 1. selects only the 7-day crossing band
    rows = [mk(400), mk(366), mk(365), mk(360), mk(359), mk(358), mk(357), mk(100), mk(-5)]
    got = sorted(r["_days"] for r in crossings(rows, today))
    if got != [359, 360, 365]:
        fails.append(f"1. crossing band wrong: {got} (want [359,360,365])")

    # 2. stale days_to_expiry must be ignored entirely
    stale = [mk(365, days_to_expiry=1)]
    if len(crossings(stale, today)) != 1:
        fails.append("2. recomputation ignored stale days_to_expiry")

    # 3. boundary: exactly 365 in, exactly 358 out
    if len(crossings([mk(365)], today)) != 1:
        fails.append("3a. day 365 should be included")
    if len(crossings([mk(358)], today)) != 0:
        fails.append("3b. day 358 should be excluded (already counted last week)")

    # 4. no crossings renders without raising
    for fn in (render_text, render_html):
        try:
            fn([], today, "B", "A", "https://x")
        except Exception as e:
            fails.append(f"4. {fn.__name__} empty-week crash: {e}")

    # 5. buyer_name must never appear in output
    leak = [mk(365, buyer_name="Jane Doe", vendor_name="Acme")]
    for fn in (render_text, render_html):
        out = fn(crossings(leak, today), today, "B", "A", "https://x")
        if "Jane Doe" in out:
            fails.append(f"5. {fn.__name__} leaked buyer_name")

    # 5b. A private person's name must never reach a subscriber. Test 5 above
    # passes trivially, because ingest drops buyer_name long before this file
    # sees it. THIS is the check that bites: the site withholds sole proprietors
    # but the brief reads the pipeline JSON directly, where vendor_name is raw.
    person = [mk(365, vendor_name="TREMBLAY, Marie")]
    build_site.VENDOR_ALLOWLIST = set()
    build_site.suppress_individuals(person)
    for fn in (render_text, render_html):
        out = fn(crossings(person, today), today, "B", "A", "https://x")
        if "TREMBLAY" in out:
            fails.append(f"5b. {fn.__name__} leaked an individual's name")
        elif build_site.PERSON_LABEL not in out:
            fails.append(f"5b. {fn.__name__} dropped the withheld label")

    # 6. HTML escaping of hostile vendor names
    xss = [mk(365, vendor_name='<script>alert(1)</script>')]
    out = render_html(crossings(xss, today), today, "B", "A", "https://x")
    if "<script>" in out:
        fails.append("6. unescaped HTML in vendor name")

    # 7. missing/garbage delivery_date is skipped, not fatal
    if crossings([{"delivery_date": None}, {"delivery_date": "not-a-date"}, {}], today):
        fails.append("7. bad dates should be skipped")

    # 8. determinism
    rows2 = [mk(365, contract_value=5.0, vendor_name="B"),
             mk(360, contract_value=5.0, vendor_name="A")]
    a = render_text(crossings(rows2, today), today, "B", "A", "https://x")
    b = render_text(crossings(rows2, today), today, "B", "A", "https://x")
    if a != b:
        fails.append("8. output not deterministic")

    # 9. sorted by value descending
    rows3 = [mk(365, contract_value=10.0), mk(365, contract_value=999.0),
             mk(365, contract_value=50.0)]
    vals = [r["contract_value"] for r in crossings(rows3, today)]
    if vals != sorted(vals, reverse=True):
        fails.append(f"9. not sorted by value: {vals}")

    for f in fails:
        print("FAIL " + f, file=sys.stderr)
    print(f"self-test: {9 - len(set(f[0] for f in fails))}/9 groups passed"
          if fails else "self-test: 9/9 passed")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Weekly recompete brief")
    ap.add_argument("--input")
    ap.add_argument("--html-out", default="brief.html")
    ap.add_argument("--text-out", default="brief.txt")
    ap.add_argument("--as-of", default="", help="YYYY-MM-DD; defaults to today")
    ap.add_argument("--business-name", default="Canadian Recompete Radar")
    ap.add_argument("--mailing-address",
                    default="PO Box 1184, Pembroke, Ontario K8A 6Y6")
    ap.add_argument("--site", default="https://marbocreatives.github.io/recompete-radar")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--question", default="",
                    help="One short question to readers, for this edition only.\n"
                         "Empty uses the standing question, so a skipped week\n"
                         "still reads as intended.")
    ap.add_argument("--note", default="",
                    help="An optional block near the top, for the monthly note\n"
                         "from the operator or a one-off announcement. Empty\n"
                         "renders nothing at all.")
    a = ap.parse_args()

    global BRIEF_QUESTION, BRIEF_NOTE
    BRIEF_QUESTION = a.question.strip() or DEFAULT_QUESTION
    BRIEF_NOTE = a.note.strip()

    if a.self_test:
        return self_test()
    if not a.input:
        print("ERROR: --input is required (or use --self-test)", file=sys.stderr)
        return 2

    as_of = parse_date(a.as_of) or date.today()
    rows = json.load(open(a.input, encoding="utf-8"))

    # Apply the site's own suppression before anything is rendered. Without
    # this the email sends out the individual names the site withholds.
    build_site.VENDOR_ALLOWLIST = build_site.load_vendor_allowlist(
        "vendor_allowlist.txt")
    withheld = build_site.suppress_individuals(rows)
    print(f"withheld {withheld:,} individual vendor names")
    sel = crossings(rows, as_of)

    h = render_html(sel, as_of, a.business_name, a.mailing_address, a.site)
    t = render_text(sel, as_of, a.business_name, a.mailing_address, a.site)

    # Belt and braces: never ship a brief containing a named individual.
    for field in FORBIDDEN_FIELDS:
        for r in rows:
            v = r.get(field)
            if v and (str(v) in h or str(v) in t):
                print(f"ERROR: {field} value leaked into the brief", file=sys.stderr)
                return 3

    open(a.html_out, "w", encoding="utf-8").write(h)
    open(a.text_out, "w", encoding="utf-8").write(t)

    total = sum(r.get("contract_value") or 0 for r in sel)
    print(f"week ending    : {as_of:%Y-%m-%d}")
    print(f"crossings      : {len(sel):,}")
    print(f"total value    : {money(total)}")
    print(f"shown in email : {min(len(sel), MAX_ROWS)}")
    print(f"wrote          : {a.html_out}, {a.text_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
