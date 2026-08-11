#!/usr/bin/env python3
"""
Canadian Recompete Radar - static site generator
================================================

Input : recompete_pipeline.json produced by the ingest (list of contract dicts)
Output: a complete static site - landing page, index pages, and one page per
        department / incumbent / category, plus sitemap.xml

Design decisions that matter
----------------------------
1. THIN-CONTENT GUARD. 26k contracts implies thousands of unique vendors, most
   holding a single contract. Generating a page per vendor would produce
   thousands of near-empty pages, which search engines treat as thin content and
   which can suppress the whole domain. Pages are only generated where there is
   real substance (see MIN_* thresholds); everything else is listed on a browsable
   index page so it is still crawlable and internally linked.

2. VENDOR IDENTITY. Vendors are grouped on `vendor_key` (punctuation and legal
   suffixes stripped by the ingest), not on the raw string. Without this, BGIS
   alone splits across three spellings and ~$14B is attributed to three separate
   "companies". Display name is the most common raw spelling in the group.

3. CATEGORY NAMES. Commodity codes are meaningless to a human and to a search
   query. `category_name` is derived by the ingest from the most frequent
   contract description carrying that code, so pages get real titles.

4. Every page carries a unique <title> and meta description built from its own
   numbers. Boilerplate descriptions across thousands of pages is a known
   duplicate-content problem.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import date
from typing import Any, Iterable, Optional

SITE = "Canadian Recompete Radar"
TAG = ("Federal contracts coming up for renewal — who holds them, "
       "what they're worth, and how contested they were.")

# Thin-content thresholds. A page is generated only if the group clears one.
MIN_CONTRACTS = 3
MIN_VALUE = 5_000_000

CSS = """
:root{--bg:#0f1115;--pn:#171a21;--ln:#252a34;--tx:#e6e9ef;--dm:#98a1b3;
--ac:#4da3ff;--wn:#ffb454;--ht:#ff6b6b;--ok:#5ecb8b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,sans-serif}
a{color:var(--ac);text-decoration:none}a:hover{text-decoration:underline}
.w{max-width:1160px;margin:0 auto;padding:28px 20px 70px}
header{border-bottom:1px solid var(--ln);padding-bottom:15px;margin-bottom:20px}
h1{margin:0 0 5px;font-size:26px;letter-spacing:-.02em}
h2{font-size:18px;margin:30px 0 10px}
.sb{color:var(--dm);font-size:14px;margin:0}
.crumb{font-size:12.5px;color:var(--dm);margin-bottom:12px}
.cd{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:11px;margin:16px 0}
.c{background:var(--pn);border:1px solid var(--ln);border-radius:10px;padding:13px 15px}
.c.b{border-color:#2f6ea8}
.c .v{font-size:22px;font-weight:600;letter-spacing:-.02em}
.c .l{color:var(--dm);font-size:11px;text-transform:uppercase;letter-spacing:.06em;margin-top:3px}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;color:var(--dm);font-weight:500;font-size:11px;text-transform:uppercase;
letter-spacing:.06em;padding:8px;border-bottom:1px solid var(--ln)}
td{padding:9px 8px;border-bottom:1px solid var(--ln)}
tr:hover td{background:#141821}
.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.d{color:var(--dm);font-size:12.5px}
.p{display:inline-block;padding:2px 8px;border-radius:20px;font-size:10.5px;font-weight:600}
.hot{background:rgba(255,107,107,.17);color:var(--ht)}
.warn{background:rgba(255,180,84,.17);color:var(--wn)}
.good{background:rgba(94,203,139,.17);color:var(--ok)}
.dim{background:rgba(152,161,179,.13);color:var(--dm)}
.g{display:grid;grid-template-columns:1fr 1fr 1fr;gap:22px}
@media(max-width:900px){.g{grid-template-columns:1fr}}
ul{list-style:none;padding:0;margin:0}
li{display:flex;justify-content:space-between;gap:10px;padding:6px 0;
border-bottom:1px solid var(--ln);font-size:13.5px}
.cols3{column-count:3;column-gap:26px}
@media(max-width:900px){.cols3{column-count:1}}
.cols3 li{display:block;padding:4px 0;border:0;break-inside:avoid}
.tw{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:0 -4px;padding:0 4px}
.tw table{min-width:560px}
@media(max-width:560px){
  .tw table{min-width:0}
  .tw th:nth-child(4),.tw td:nth-child(4){display:none}
  .tw th:nth-child(5),.tw td:nth-child(5){display:none}
}
footer{margin-top:44px;padding-top:16px;border-top:1px solid var(--ln);
color:var(--dm);font-size:12px;line-height:1.7}
"""


# ------------------------------------------------------------------ helpers

def money(v: Optional[float]) -> str:
    v = v or 0
    if v >= 1e9:
        return f"${v/1e9:,.1f}B"
    if v >= 1e6:
        return f"${v/1e6:,.0f}M"
    if v >= 1e3:
        return f"${v/1e3:,.0f}K"
    return f"${v:,.0f}"


def slug(text: str, maxlen: int = 70) -> str:
    """URL slug with accents transliterated, not destroyed.

    This dataset is bilingual. A naive [^a-z0-9] filter turns "Défense
    nationale" into "d-fense-nationale" and "Pêches et Océans" into
    "p-ches-et-oc-ans" — URLs that look broken to a human and lose the keyword
    for search. NFKD decomposition splits "é" into "e" + combining accent, and
    dropping the combining marks leaves clean ASCII.
    """
    decomposed = unicodedata.normalize("NFKD", text or "")
    ascii_text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return (s[:maxlen].strip("-") or "x")


def esc(v: Any) -> str:
    return html.escape(str(v)) if v is not None else ""


def bucket_pill(b: Optional[str]) -> str:
    cls = {"0-6mo": "hot", "6-12mo": "warn", "12-24mo": "good"}.get(b or "", "dim")
    return f'<span class="p {cls}">{esc(b or "—")}</span>'


def density_pill(d: Optional[str]) -> str:
    cls = {"uncontested": "hot", "low": "warn", "moderate": "good", "high": "dim"}.get(d or "", "dim")
    return f'<span class="p {cls}">{esc(d or "—")}</span>'


def page(title: str, desc: str, body: str, depth: int = 0) -> str:
    root = "../" * depth
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}">
<style>{CSS}</style></head><body><div class="w">
<header><h1><a href="{root}index.html" style="color:inherit">{SITE}</a></h1>
<p class="sb">{esc(TAG)}</p></header>
{body}
<footer>Built from the Government of Canada <strong>Proactive Publication of Contracts</strong>
dataset (contracts over $10,000), Treasury Board of Canada Secretariat.
Open Government Licence – Canada.<br>
Figures are <strong>total contract value over the full contract term</strong>, not annual
spend. Only services and construction contracts are shown, where the published
"Contract Period End Date or Delivery Date" field is defined as the end of the
performance period. Published quarterly, so the most recent quarter may not appear.
Not affiliated with the Government of Canada.</footer>
</div></body></html>"""


def contract_table(rows: list[dict], show: tuple[str, ...] = ("dept", "cat"),
                   limit: int = 250) -> str:
    head = "<tr><th>Expires</th><th class='n'>Value</th><th>Incumbent</th>"
    if "dept" in show:
        head += "<th>Department</th>"
    if "cat" in show:
        head += "<th>Category</th>"
    head += "<th class='n'>Bidders</th><th>Last time</th></tr>"

    out = []
    for c in rows[:limit]:
        days = c.get("days_to_expiry")
        bids = c.get("number_of_bids")
        cells = [
            f'<td>{bucket_pill(c.get("expiry_bucket"))} <span class="d">{days}d</span></td>',
            f'<td class="n">{money(c.get("contract_value"))}</td>',
            f'<td>{esc((c.get("vendor_name") or "—")[:38])}</td>',
        ]
        if "dept" in show:
            cells.append(f'<td class="d">{esc((c.get("buyer_org") or "").split(" | ")[0][:38])}</td>')
        if "cat" in show:
            cells.append(f'<td class="d">{esc((c.get("category_name") or c.get("commodity_code") or "")[:38])}</td>')
        cells.append(f'<td class="n d">{bids if bids is not None else "—"}</td>')
        cells.append(f'<td>{density_pill(c.get("competition_density"))}</td>')
        out.append("<tr>" + "".join(cells) + "</tr>")
    # Wrapped: a 6-column table on a 375px viewport otherwise pushes the whole
    # page sideways, which Google reports as a mobile-usability failure
    # ("content wider than screen"). The wrapper confines scrolling to the table.
    return f'<div class="tw"><table>{head}{"".join(out)}</table></div>' 


def stat_cards(items: list[tuple[str, str]], highlight: int = 2) -> str:
    return '<div class="cd">' + "".join(
        f'<div class="c{" b" if i < highlight else ""}"><div class="v">{v}</div>'
        f'<div class="l">{l}</div></div>' for i, (v, l) in enumerate(items)) + "</div>"


# ------------------------------------------------------------------ grouping

def group(rows: list[dict], key_field: str, name_field: Optional[str] = None
          ) -> dict[str, dict]:
    """Group contracts, choosing the most common display name in each group."""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        k = (r.get(key_field) or "").strip()
        if k:
            buckets[k].append(r)

    out = {}
    for k, items in buckets.items():
        if name_field:
            names = Counter((i.get(name_field) or "").strip() for i in items if i.get(name_field))
            display = names.most_common(1)[0][0] if names else k
        else:
            display = k
        out[k] = {
            "display": display,
            "items": sorted(items, key=lambda c: c.get("days_to_expiry") or 10**9),
            "value": sum(i.get("contract_value") or 0 for i in items),
            "count": len(items),
        }
    return out


def substantial(g: dict) -> bool:
    return g["count"] >= MIN_CONTRACTS or g["value"] >= MIN_VALUE


def add_category_key(rows: list[dict]) -> None:
    """Collapse commodity codes onto their human name.

    The government issues many distinct commodity codes that carry the SAME
    description — 1,641 codes reduce to 313 real names. Grouping on the code
    fragments a single subject across dozens of near-identical pages: management
    consulting alone is $14.4B spread over 28 codes. Those pages compete with
    each other for one search term and each is individually weak.

    Grouping on the normalized name yields one strong page per subject.
    Normalization is case- and whitespace-insensitive, because the same
    description appears with inconsistent capitalisation
    ("Other Business services..." vs "Other business services...").
    """
    for r in rows:
        name = (r.get("category_name") or "").strip()
        r["category_key"] = re.sub(r"\s+", " ", name).lower() or (r.get("commodity_code") or "")


# ------------------------------------------------------------------ build

def build(rows: list[dict], outdir: str, base_url: str = "") -> dict:
    for sub in ("department", "incumbent", "category"):
        os.makedirs(os.path.join(outdir, sub), exist_ok=True)

    live = [r for r in rows if r.get("days_to_expiry") is not None
            and r["days_to_expiry"] >= 0]   # NB: `or -1` would drop day-zero (0 is falsy)
    live.sort(key=lambda c: c.get("days_to_expiry") or 10**9)
    total_value = sum(r.get("contract_value") or 0 for r in live)

    add_category_key(live)

    depts = group(live, "buyer_org")
    vendors = group(live, "vendor_key", "vendor_name")
    cats = group(live, "category_key", "category_name")

    counts = {"0-6mo": 0, "6-12mo": 0, "12-24mo": 0, "24mo+": 0}
    for r in live:
        b = r.get("expiry_bucket")
        if b in counts:
            counts[b] += 1

    written = {"department": 0, "incumbent": 0, "category": 0}
    urls = ["index.html"]

    # key -> actual filename written. Links MUST be built from this, never
    # recomputed from the display name, or collision-renamed pages get orphaned.
    filenames: dict[str, dict[str, str]] = {"department": {}, "incumbent": {}, "category": {}}

    def write_group_pages(groups: dict[str, dict], folder: str, label: str,
                          show: tuple[str, ...]) -> list[tuple[str, dict]]:
        listed = []
        used: set[str] = set()          # filenames already taken in this folder
        for key, g in groups.items():
            if not substantial(g):
                listed.append((key, g))
                continue

            # Slug collisions must be resolved against names already issued in
            # this run, not against os.path.exists — two groups can produce the
            # same slug AND the same fallback slug, silently overwriting each
            # other and leaving dead sitemap entries behind.
            base = slug(g["display"])
            fn = f"{base}.html"
            if fn in used:
                alt = f"{base}-{slug(key)[:14]}"
                fn = f"{alt}.html"
                n = 2
                while fn in used:
                    fn = f"{alt}-{n}.html"
                    n += 1
            used.add(fn)
            filenames[folder][key] = fn
            path = os.path.join(outdir, folder, fn)

            soon = sum(1 for i in g["items"] if (i.get("days_to_expiry") or 999) <= 365)
            unc = sum(1 for i in g["items"] if i.get("competition_density") == "uncontested")
            title = f"{g['display']} — contracts expiring | {SITE}"
            # The entity name must lead the description. Without it, groups that
            # happen to share a count/value/soon triple produce byte-identical
            # descriptions — 474 of them across 2,092 pages in testing.
            desc = (f"{g['display']}: {g['count']} federal contracts worth "
                    f"{money(g['value'])} coming up for renewal. "
                    f"{soon} expire within 12 months, {unc} were uncontested when "
                    f"last awarded. Incumbent, value, expiry and bidder count for each.")
            body = (
                f'<div class="crumb"><a href="../index.html">Home</a> › '
                f'<a href="../{folder}/index.html">{esc(label)}</a> › {esc(g["display"])}</div>'
                f"<h2>{esc(g['display'])}</h2>"
                + stat_cards([
                    (f"{g['count']:,}", "Contracts"),
                    (money(g["value"]), "Total value"),
                    (f"{soon:,}", "Within 12 months"),
                    (f"{unc:,}", "Uncontested last time"),
                ])
                + ('<p class="sb">Figures cover this supplier as named in the '
                   'published records. Related legal entities are listed separately, '
                   'so a corporate group\'s total exposure may be higher.</p>'
                   if folder == "incumbent" else "")
                + "<h2>Contracts by expiry</h2>"
                + contract_table(g["items"], show=show, limit=400))
            open(path, "w", encoding="utf-8").write(page(title, desc, body, 1))
            urls.append(f"{folder}/{fn}")
            written[folder] += 1
        return listed

    small_d = write_group_pages(depts, "department", "Department", ("cat",))
    small_v = write_group_pages(vendors, "incumbent", "Incumbent", ("dept", "cat"))
    small_c = write_group_pages(cats, "category", "Category", ("dept",))

    # ---- index pages (keeps small groups crawlable and internally linked)
    def write_index(groups: dict[str, dict], small: list, folder: str, label: str) -> None:
        """Index page(s). Every group appears somewhere — groups below the
        thin-content threshold don't get their own page, but they must still be
        listed and reachable, or the claim that they stay crawlable is false.
        The overflow list is paginated rather than truncated."""
        big = sorted([(k, g) for k, g in groups.items() if substantial(g)],
                     key=lambda kv: -kv[1]["value"])
        links = "".join(
            f'<li><a href="{filenames[folder][k]}">{esc(g["display"][:52])}</a> '
            f'<span class="d">{money(g["value"])} · {g["count"]}</span></li>'
            for k, g in big if k in filenames[folder])

        small_sorted = sorted(small, key=lambda kv: -kv[1]["value"])
        PER = 1000
        chunks = [small_sorted[i:i + PER] for i in range(0, len(small_sorted), PER)] or [[]]
        total_pages = len(chunks)

        for n, chunk in enumerate(chunks, start=1):
            fn = "index.html" if n == 1 else f"index-{n}.html"
            rest = "".join(
                f'<li class="d">{esc(g["display"][:52])} — {money(g["value"])} · {g["count"]}</li>'
                for _, g in chunk)
            nav = ""
            if total_pages > 1:
                parts = []
                for i in range(1, total_pages + 1):
                    target = "index.html" if i == 1 else f"index-{i}.html"
                    parts.append(f"<strong>{i}</strong>" if i == n
                                 else f'<a href="{target}">{i}</a>')
                nav = f'<p class="sb">Page {n} of {total_pages} — ' + " · ".join(parts) + "</p>"

            head = (f'<div class="crumb"><a href="../index.html">Home</a> › {esc(label)}'
                    + (f" › page {n}" if n > 1 else "") + "</div>")
            body = head + (
                f"<h2>All {label.lower()}s with contracts expiring</h2>"
                + stat_cards([(f"{len(groups):,}", f"Total {label.lower()}s"),
                              (f"{len(big):,}", "With detail pages")], highlight=1)
                + f"<ul>{links}</ul>" if n == 1 else
                f"<h2>Smaller {label.lower()}s — page {n}</h2>")
            if rest:
                body += (f"<h2>Smaller {label.lower()}s</h2>" if n == 1 else "")
                body += f"<ul class='cols3'>{rest}</ul>"
            body += nav

            suffix = "" if n == 1 else f" (page {n})"
            open(os.path.join(outdir, folder, fn), "w", encoding="utf-8").write(
                page(f"All {label.lower()}s{suffix} | {SITE}",
                     f"Every federal {label.lower()} with contracts coming up for "
                     f"renewal, ranked by total value{suffix}. "
                     f"{len(groups):,} in total, {len(big):,} with detail pages.", body, 1))
            urls.append(f"{folder}/{fn}")

    write_index(depts, small_d, "department", "Department")
    write_index(vendors, small_v, "incumbent", "Incumbent")
    write_index(cats, small_c, "category", "Category")

    # ---- landing page
    def toplist(groups: dict[str, dict], folder: str, n: int = 12) -> str:
        top = sorted([(k, g) for k, g in groups.items()
                      if substantial(g) and k in filenames[folder]],
                     key=lambda kv: -kv[1]["value"])[:n]
        return "".join(
            f'<li><a href="{folder}/{filenames[folder][k]}">{esc(g["display"][:44])}</a>'
            f'<span class="n d">{money(g["value"])} · {g["count"]:,}</span></li>'
            for k, g in top)

    # Headline stats alone are misleading: the top 100 contracts carry ~81% of the
    # value and the median is ~$51k. Showing the median next to the total stops a
    # reader inferring a far fatter middle than exists.
    values = sorted(r.get("contract_value") or 0 for r in live)
    median_value = values[len(values) // 2] if values else 0
    top100_share = (sum(values[-100:]) / sum(values) * 100) if sum(values) else 0
    with_bids = sum(1 for r in live if r.get("number_of_bids") is not None)
    uncontested = sum(1 for r in live if r.get("competition_density") == "uncontested")

    body = (
        stat_cards([(f"{len(live):,}", "Live contracts"), (money(total_value), "Pipeline value")]
                   + [(f"{counts[b]:,}", b) for b in ("0-6mo", "6-12mo", "12-24mo", "24mo+")]
                   + [(money(median_value), "Median contract")])
        # Every figure in this paragraph is computed from the dataset on this page.
        # It previously cited a 70-80% incumbent win rate taken from a US vendor's
        # marketing — a foreign statistic, unattributed, on a site whose whole value
        # is accuracy. Replaced with facts we can defend from our own data.
        + "<h2>Expiring soonest</h2>"
        + f'<p class="sb">Agencies typically begin recompete planning 12–18 months '
          f'before a contract ends. Of the {with_bids:,} contracts here that report a '
          f'bidder count, <strong>{uncontested:,} ({uncontested/max(with_bids,1)*100:.0f}%) '
          f'drew one bid or none</strong> when last awarded. The median contract is '
          f'{money(median_value)}; the largest {min(100, len(live))} account for '
          f'{top100_share:.0f}% of total value.</p>'
        + contract_table(live, limit=60)
        + '<div class="g">'
        + f'<div><h2>By department</h2><ul>{toplist(depts,"department")}</ul>'
          f'<p><a href="department/index.html">All departments →</a></p></div>'
        + f'<div><h2>By incumbent</h2><ul>{toplist(vendors,"incumbent")}</ul>'
          f'<p><a href="incumbent/index.html">All incumbents →</a></p></div>'
        + f'<div><h2>By category</h2><ul>{toplist(cats,"category")}</ul>'
          f'<p><a href="category/index.html">All categories →</a></p></div>'
        + "</div>")
    open(os.path.join(outdir, "index.html"), "w", encoding="utf-8").write(
        page(f"{SITE} — federal contracts up for renewal",
             f"{len(live):,} Canadian federal services contracts worth {money(total_value)} "
             f"are coming up for renewal. See the incumbent, value, expiry date and how "
             f"contested each was.", body, 0))

    # ---- sitemap + robots
    # The sitemap protocol REQUIRES fully-qualified URLs. Relative paths are
    # rejected outright by Search Console, which would silently kill the entire
    # indexing strategy this site depends on.
    base = (base_url or "").rstrip("/")
    today = date.today().isoformat()
    sm = ("<?xml version='1.0' encoding='UTF-8'?>\n"
          "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>\n"
          + "".join(f"  <url><loc>{base}/{u}</loc><lastmod>{today}</lastmod></url>\n"
                    for u in sorted(set(urls))) + "</urlset>\n")
    open(os.path.join(outdir, "sitemap.xml"), "w", encoding="utf-8").write(sm)

    robots = ("User-agent: *\nAllow: /\n\n"
              + (f"Sitemap: {base}/sitemap.xml\n" if base else "Sitemap: /sitemap.xml\n"))
    open(os.path.join(outdir, "robots.txt"), "w", encoding="utf-8").write(robots)

    return {"live": len(live), "value": total_value, "pages": len(urls),
            "departments": (written["department"], len(depts)),
            "incumbents": (written["incumbent"], len(vendors)),
            "categories": (written["category"], len(cats)),
            "buckets": counts}


def verify(outdir: str) -> list[str]:
    """Check every internal link resolves to a file that exists."""
    problems = []
    for root, _dirs, files in os.walk(outdir):
        for f in files:
            if not f.endswith(".html"):
                continue
            p = os.path.join(root, f)
            src = open(p, encoding="utf-8").read()
            for href in re.findall(r'href="([^"#]+\.html)"', src):
                target = os.path.normpath(os.path.join(root, href))
                if not os.path.exists(target):
                    problems.append(f"{os.path.relpath(p,outdir)} -> {href}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", default="site")
    ap.add_argument("--base-url", default="",
                    help="full site URL, e.g. https://example.com — required for a\n                          valid sitemap; relative paths are rejected by Search Console")
    args = ap.parse_args()

    rows = json.load(open(args.input, encoding="utf-8"))
    print(f"loaded {len(rows):,} contracts from {args.input}")
    s = build(rows, args.out, args.base_url)

    print(f"\nlive contracts : {s['live']:,}")
    print(f"pipeline value : ${s['value']:,.0f}")
    for b, n in s["buckets"].items():
        print(f"   {b:>8}: {n:,}")
    for k in ("departments", "incumbents", "categories"):
        made, total = s[k]
        print(f"{k:>12}: {made:,} pages generated of {total:,} groups "
              f"({total-made:,} listed on index, below thin-content threshold)")
    print(f"\ntotal pages    : {s['pages']:,}")

    bad = verify(args.out)
    print(f"link check     : {'ALL OK' if not bad else str(len(bad)) + ' BROKEN'}")
    for b in bad[:10]:
        print("   broken:", b)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
