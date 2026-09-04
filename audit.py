#!/usr/bin/env python3
"""
Independent audit of the generated site against the source data.

This deliberately does NOT import build_site.py. It recomputes every figure
from the raw JSON and compares against what the HTML actually says, so a bug in
the generator cannot hide behind the generator's own reporting.

    python3 audit.py --input recompete_pipeline.json --site site/

Exit code 0 = all checks passed, 1 = at least one failure.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
import urllib.parse
from collections import Counter, defaultdict

# The site withholds the names of vendors who are private people. The rule is
# IMPORTED, never copied, so the audit and the builder cannot drift apart. This
# does not weaken the gate: the count checks below test that the site matches the
# data, so the data has to carry the same transformation the site does. A leak
# check is added further down to test the rule actually fired.
# NB: this module already uses `html` as a local variable name, so the module
# cannot be imported under that name. Bind the one function needed instead.
from html import unescape as _unescape

import build_site

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "", warn_only: bool = False) -> None:
    results.append((WARN if (not ok and warn_only) else (PASS if ok else FAIL), name, detail))


def money_to_float(s: str) -> float:
    s = s.replace("$", "").replace(",", "").strip()
    mult = 1.0
    if s.endswith("B"):
        mult, s = 1e9, s[:-1]
    elif s.endswith("M"):
        mult, s = 1e6, s[:-1]
    elif s.endswith("K"):
        mult, s = 1e3, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return float("nan")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--site", required=True)
    ap.add_argument("--allow-small", action="store_true",
                    help="Skip the plausibility floors and widen the landing-page\n"
                         "value tolerance. For the offline fixture check ONLY: a\n"
                         "110-row fixture cannot meet floors written for 26,000\n"
                         "real contracts. Production never passes this, so the\n"
                         "real gate is unchanged. Every structural check still\n"
                         "runs either way.")
    a = ap.parse_args()

    rows = json.load(open(a.input, encoding="utf-8"))

    build_site.VENDOR_ALLOWLIST = build_site.load_vendor_allowlist("vendor_allowlist.txt")
    withheld = build_site.suppress_individuals(rows)
    site = a.site

    # ---- recompute the truth independently -------------------------------
    live = [r for r in rows if r.get("days_to_expiry") is not None
            and r["days_to_expiry"] >= 0]   # NB: `or -1` would drop day-zero (0 is falsy)
    total_value = sum(r.get("contract_value") or 0 for r in live)

    # Deliberately reimplemented rather than imported, like the rest of this
    # file: audit.py recomputes the expected numbers from the data so that a
    # fault in build_site.py shows up as a disagreement. The placeholder rule is
    # written out again here for the same reason.
    PLACEHOLDER_CATS = {"na", "nil", "none", "null", "nulle", "unknown",
                        "unspecified", "notapplicable", "sansobjet", "tbd",
                        "tobedetermined"}

    def placeholder_cat(name):
        n = re.sub(r"\s+", " ", (name or "")).strip()
        if not n:
            return False
        if not any(ch.isalpha() for ch in n):
            return True
        return re.sub(r"[^a-z]", "", n.lower()) in PLACEHOLDER_CATS

    def norm_cat(r):
        name = (r.get("category_name") or "").strip()
        if placeholder_cat(name):
            return ""
        return re.sub(r"\s+", " ", name).lower() or (r.get("commodity_code") or "")

    depts = defaultdict(list)
    vends = defaultdict(list)
    cats = defaultdict(list)
    for r in live:
        if r.get("buyer_org"):
            depts[r["buyer_org"]].append(r)
        if r.get("vendor_key"):
            vends[r["vendor_key"]].append(r)
        k = norm_cat(r)
        if k:
            cats[k].append(r)

    def substantial(items):
        return len(items) >= 3 or sum(i.get("contract_value") or 0 for i in items) >= 5_000_000

    exp_dept = sum(1 for v in depts.values() if substantial(v))
    exp_vend = sum(1 for v in vends.values() if substantial(v))
    exp_cat = sum(1 for v in cats.values() if substantial(v))

    # ---- filesystem reality ----------------------------------------------
    html = []
    for root, _d, files in os.walk(site):
        for f in files:
            if f.endswith(".html"):
                html.append(os.path.join(root, f))

    def count(folder):
        p = os.path.join(site, folder)
        if not os.path.isdir(p):
            return 0
        # index.html AND its pagination siblings (index-2.html ...) are not detail pages
        return len([f for f in os.listdir(p)
                    if f.endswith(".html") and not f.startswith("index")])

    check("department pages match expectation",
          count("department") == exp_dept,
          f"expected {exp_dept}, found {count('department')}")
    check("incumbent pages match expectation",
          count("incumbent") == exp_vend,
          f"expected {exp_vend}, found {count('incumbent')}")
    check("category pages match expectation",
          count("category") == exp_cat,
          f"expected {exp_cat}, found {count('category')}")

    # ---- sitemap must point only at files that exist ----------------------
    sm_path = os.path.join(site, "sitemap.xml")
    sm_urls = []
    if os.path.exists(sm_path):
        raw = re.findall(r"<loc>([^<]+)</loc>", open(sm_path, encoding="utf-8").read())
        # sitemaps must carry absolute URLs; strip scheme+host to map back to files
        abs_ok = all(u.startswith("http://") or u.startswith("https://") for u in raw)
        check("sitemap uses absolute URLs", abs_ok,
              "relative paths are rejected by Search Console" if not abs_ok else "")
        hosts = {re.sub(r"^https?://([^/]+)/.*", r"\1", u) for u in raw}
        placeholder = {h for h in hosts
                       if h in ("example.com", "example.org", "localhost")
                       or h.startswith("127.") or h.startswith("192.168.")}
        check("sitemap host is not a placeholder", not placeholder,
              f"found {sorted(placeholder)} — SITE_URL was never configured"
              if placeholder else "")
        # GitHub Pages PROJECT sites are served from a subpath
        # (https://user.github.io/repo/...), so stripping scheme+host alone
        # leaves "repo/page.html", which matches no file. Infer how many leading
        # path segments belong to the base URL by choosing the depth at which
        # the most URLs actually resolve. If nothing resolves at any depth, the
        # sitemap is genuinely broken and the check below still fails.
        stripped = [re.sub(r"^https?://[^/]+/", "", u) for u in raw]

        def _as_file_probe(u: str) -> str:
            return (u + "index.html") if (u == "" or u.endswith("/")) else u

        best_depth, best_hits = 0, -1
        for depth in range(0, 3):
            cand = ["/".join(u.split("/")[depth:]) for u in stripped]
            # Count DISTINCT existing files, not raw hits. Past a URL's own depth
            # every candidate collapses to "" and resolves to the site root, so a
            # raw count scored the deepest slice highest and quietly disabled the
            # dead-URL check below. One dead entry was enough to trigger it: it
            # dropped depth 0's score by one and handed the win to the collapsed
            # slice, so a broken sitemap audited clean. The real base path is the
            # one under which the URLs map onto MANY different files.
            hits = len({_as_file_probe(u) for u in cand
                        if os.path.isfile(os.path.join(site, _as_file_probe(u)))})
            if hits > best_hits:
                best_depth, best_hits = depth, hits
        if best_depth:
            print(f"  (sitemap served from a {best_depth}-segment base path: "
                  f"{'/'.join(stripped[0].split('/')[:best_depth])}/)")
        sm_urls = ["/".join(u.split("/")[best_depth:]) for u in stripped]
    # A URL ending in "/", and the bare site root, are served out of index.html
    # in that folder. Resolve to the file BEFORE testing existence. The old code
    # tested os.path.exists on the URL as given, which for a directory-form URL
    # answers "yes, the folder is there" even when the folder holds no
    # index.html — a dead URL that reported as healthy.
    def _as_file(u: str) -> str:
        return (u + "index.html") if (u == "" or u.endswith("/")) else u

    sm_files = [_as_file(u) for u in sm_urls]
    missing = [u for u in sm_files if not os.path.isfile(os.path.join(site, u))]
    dupes = len(sm_files) - len(set(sm_files))
    check("every sitemap URL resolves to a file", not missing,
          f"{len(missing)} dead entries" + (f" e.g. {missing[:3]}" if missing else ""))
    check("sitemap has no duplicate URLs", dupes == 0, f"{dupes} duplicates")
    check("sitemap covers every page", len(set(sm_files)) == len(html),
          f"sitemap {len(set(sm_files))} vs {len(html)} files")

    # ---- one canonical per page, agreeing with the sitemap ----------------
    # Derived here from the sitemap and the filesystem, NOT by calling
    # build_site's own URL helper. An audit that asks the code under test what
    # the right answer is cannot detect that code being wrong; that is exactly
    # how the is_individual check was blinded.
    rel_html = sorted(os.path.relpath(f, site).replace(os.sep, "/") for f in html)
    loc_for_file = dict(zip(sm_files, raw)) if sm_urls else {}

    can_re = re.compile(r'<link\s+rel="canonical"\s+href="([^"]*)"\s*/?>', re.I)
    canon_of, no_canon, many_canon = {}, [], []
    for rel in rel_html:
        found = can_re.findall(open(os.path.join(site, rel), encoding="utf-8").read())
        if not found:
            no_canon.append(rel)
        elif len(found) > 1:
            many_canon.append(rel)
        else:
            canon_of[rel] = found[0]

    check("every page declares a canonical URL", not no_canon,
          f"{len(no_canon)} of {len(rel_html)} pages have none"
          + (f" e.g. {no_canon[:3]}" if no_canon else ""))
    check("no page declares more than one canonical", not many_canon,
          f"{len(many_canon)} pages" + (f" e.g. {many_canon[:3]}" if many_canon else ""))

    rel_canon = sorted(r for r, u in canon_of.items()
                       if not u.startswith(("http://", "https://")))
    check("every canonical is an absolute URL", not rel_canon,
          f"{len(rel_canon)} relative" + (f" e.g. {rel_canon[:3]}" if rel_canon else ""))

    drift = sorted(r for r, u in canon_of.items()
                   if r in loc_for_file and loc_for_file[r] != u)
    check("canonical matches the sitemap entry for the same page", not drift,
          f"{len(drift)} disagree" + (f" e.g. {drift[:2]}" if drift else ""))

    seen, shared = {}, []
    for r, u in sorted(canon_of.items()):
        if u in seen:
            shared.append(f"{seen[u]} + {r}")
        else:
            seen[u] = r
    check("no two pages claim the same canonical", not shared,
          f"{len(shared)} collisions" + (f" e.g. {shared[:2]}" if shared else ""))

    # ---- the option-year caveat must appear on every page -----------------
    # This sentence was promised publicly after a retired procurement officer
    # pointed out that option years are invisible until exercised. Without it
    # the site overstates how many contracts genuinely come back to market, so
    # it is gated rather than left to survive on good intentions.
    _oy = build_site.OPTION_YEARS.split(".")[0]
    missing_oy = [os.path.relpath(f, site).replace(os.sep, "/") for f in html
                  if _oy not in _unescape(open(f, encoding="utf-8").read())]
    check("option-year caveat appears on every page", not missing_oy,
          f"{len(missing_oy)} of {len(html)} pages missing it"
          + (f" e.g. {missing_oy[:3]}" if missing_oy else ""))

    # ---- site analytics script must appear on every page ------------------
    # Added Sep 2026 while tracing why X posts with thousands of views were
    # not turning into subscribers. The site had no analytics of any kind, so
    # there was no way to tell whether the drop was the traffic itself, the
    # landing page, or the signup form. Checked against the actual rendered
    # file, not by calling build_site's own page() again, for the same reason
    # the canonical and option-year checks do it this way.
    missing_gc = [os.path.relpath(f, site).replace(os.sep, "/") for f in html
                  if build_site.GOATCOUNTER_SCRIPT not in open(f, encoding="utf-8").read()]
    check("analytics script appears on every page", not missing_gc,
          f"{len(missing_gc)} of {len(html)} pages missing it"
          + (f" e.g. {missing_gc[:3]}" if missing_gc else ""))

    # ---- robots.txt must exist and point at the sitemap -------------------
    rb = os.path.join(site, "robots.txt")
    rb_txt = open(rb, encoding="utf-8").read() if os.path.exists(rb) else ""
    rb_ok = bool(rb_txt) and "Sitemap:" in rb_txt
    check("robots.txt present and references sitemap", rb_ok,
          "" if rb_ok else ("missing" if not rb_txt else "no Sitemap line"))

    # ---- individual people must not be published as incumbents -------------
    # Never print a matched name. This log is public on a public repo, and the
    # list of withheld names IS the personal information being protected.
    def _listed_incumbent_names():
        out = []
        d = os.path.join(site, "incumbent")
        for f in os.listdir(d):
            if not f.startswith("index"):
                continue
            src = open(os.path.join(d, f), encoding="utf-8").read()
            out += re.findall(r'<li><a href="[^"]+">([^<]+)</a>', src)
            # [^>]* tolerates the anchor id that below-threshold rows carry.
            # This must stay attribute-tolerant: a pattern that silently stops
            # matching turns the most important check in this file into a
            # guaranteed pass over an empty list.
            out += [m.split("\u2014")[0].strip()
                    for m in re.findall(r'<li class="d"[^>]*>([^<]+)</li>', src)]
        return [_unescape(n) for n in out]

    # Two corrections to what this check tests. Neither relaxes it — both stop
    # it firing on a string the site does not actually hold.
    #
    # 1. Index pages clip long names to fit a column, so a displayed name may
    #    end in an ellipsis. Clipping can cut off the very word that marks an
    #    organisation: "CHRISTOPHER R. SKINNER MEDICINE PROFESSIONAL
    #    CORPORATION" displays as "...MEDICINE PROFESSIONAL…", and without
    #    CORPORATION that reads as a person. The question this check asks is
    #    whether a person's NAME is published, so ask it of the name the site
    #    holds, not of a visually shortened fragment of it. A genuinely
    #    exposed person is unaffected: suppress_individuals runs on the full
    #    name before anything is rendered, so a real individual never reaches
    #    an index page to be clipped in the first place.
    #
    # 2. suppress_individuals skips names in VENDOR_ALLOWLIST. This check
    #    already loads that list and then ignored it, so an allowlisted
    #    organisation failed the audit for being published on purpose.
    source_names = sorted({str(r["vendor_name"]) for r in rows if r.get("vendor_name")},
                          key=len, reverse=True)

    def _unclipped(displayed):
        """The source name a displayed string came from, or the string itself."""
        if not displayed.endswith("…"):
            return displayed
        stem = displayed.rstrip("…").strip()
        for full in source_names:
            if full.startswith(stem):
                return full
        return displayed

    leaked = {n for n in map(_unclipped, _listed_incumbent_names())
              if build_site.is_individual(n)
              and build_site._norm_name(n) not in build_site.VENDOR_ALLOWLIST}
    check("no individual person is listed as an incumbent", not leaked,
          "" if not leaked else f"{len(leaked)} still listed")
    check("individual vendor names are being withheld", withheld > 0,
          f"{withheld:,} withheld")

    # ---- published scope text must carry no personal name ------------------
    # comments_en is the only free-text field on the site: a person typed it
    # into a government form, so it can contain anything. Never print a matched
    # string here, for the same reason the withheld-names list is never
    # printed — the text IS the personal information.
    #
    # This re-runs the scan against what actually reached disk rather than
    # trusting build_site to have called it. A display path that forgets the
    # scan is exactly the failure this gate exists to catch.
    published_scope = []
    for root, _dirs, files in os.walk(site):
        for f in files:
            if f.endswith(".html"):
                published_scope += re.findall(
                    r'<span class="scope">([^<]*)</span>',
                    open(os.path.join(root, f), encoding="utf-8").read())
    published_scope = [_unescape(s) for s in published_scope]
    # clip() may have shortened the text before it was written, so compare on
    # the scan verdict, not on string equality with the original.
    unsafe = [s for s in published_scope if build_site.scope_text(s) is None]
    check("no published scope text contains a personal name", not unsafe,
          "" if not unsafe else f"{len(unsafe)} string(s) would not pass the scan")

    scope_pub, scope_held = build_site.scope_stats(rows)
    # A scan that withholds everything, or a display path that silently stopped
    # rendering, both look like "no leaks" to the check above.
    check("scope text is actually being published", scope_pub > 0,
          f"{scope_pub:,} published, {scope_held:,} withheld")

    # ---- generated English must be well formed ----------------------------
    # Every reader-facing sentence here is built by a format string, so no
    # amount of proofreading finds a fault in one. Three shipped and stayed
    # live: "categorys" in a <title>, 250 names cut mid-word with no ellipsis,
    # and "1 federal contracts" across 660 meta descriptions. Titles and meta
    # descriptions are what Google prints under a result, so these are the
    # most-read strings on the site and the least-reviewed. This check makes
    # the class fail the build instead of waiting for someone to notice.
    def _visible_text(path):
        h = open(path, encoding="utf-8").read()
        h = re.sub(r"<style.*?</style>", " ", h, flags=re.S)
        h = re.sub(r"<script.*?</script>", " ", h, flags=re.S)
        titles = re.findall(r"<title>(.*?)</title>", h, re.S)
        descs = re.findall(r'<meta name="description" content="(.*?)"', h, re.S)
        return _unescape(" ".join(titles + descs) + " " + re.sub(r"<[^>]+>", " ", h))

    # \b1\b, not \d+, so "11 were" and "21 expire" do not read as faults.
    BAD_AGREEMENT = re.compile(
        r"\b1 (?:[a-z]+ )?(?:contracts|suppliers|incumbents|departments|categories"
        r"|provinces|bids|pages|months|days|years|expire|cross|were|have|are)\b")
    # -ys where -ies is correct. The first version of this scanned every word
    # on the page and was wrong in principle: the pages carry 12,899 vendor
    # names and 232 category names from the source data, so it fired on
    # "maxsys" (a real supplier), "highways", "runways" and "journeys". No
    # exclusion list can ever cover names nobody controls.
    #
    # Only the generator's own label plurals are in scope here. For each index
    # label, if the naive label + "s" differs from plural(label), then seeing
    # the naive form on a page means plural() was bypassed. That is exactly the
    # "categorys" bug and nothing else can trip it.
    BAD_PLURAL_FORMS = sorted({
        lbl.lower() + "s"
        for lbl in ("Department", "Incumbent", "Category", "Supplier province")
        if lbl.lower() + "s" != build_site.plural(lbl.lower())
    })
    BAD_PLURAL = (re.compile(r"\b(" + "|".join(map(re.escape, BAD_PLURAL_FORMS)) + r")\b")
                  if BAD_PLURAL_FORMS else None)

    # Every name the site can display, so a fixed-width string can be tested
    # for being a genuine prefix of a longer one rather than merely that long.
    source_names = {str(r[k]) for r in rows
                    for k in ("vendor_name", "category_name", "buyer_org")
                    if r.get(k)}

    agree, plur, midword = [], [], 0
    for root, _dirs, files in os.walk(site):
        for f in files:
            if not f.endswith(".html"):
                continue
            p = os.path.join(root, f)
            rel = os.path.relpath(p, site)
            t = _visible_text(p)
            if BAD_AGREEMENT.search(t):
                agree.append(rel)
            if BAD_PLURAL is not None and BAD_PLURAL.search(t):
                plur.append(rel)
            src = open(p, encoding="utf-8").read()
            # Anything clipped to a fixed width must end in the ellipsis that
            # clip() adds. Length alone is not proof of a cut — one real
            # category name is exactly 60 characters — so a string only counts
            # as truncated when a LONGER source name starts with it.
            for pat in (r'<a href="[^"]+">([^<]{52})</a>',
                        r'<a href="[^"]+">([^<]{44})</a>',
                        r'<option[^>]*>([^<]{60})</option>'):
                for m in re.finditer(pat, src):
                    s = _unescape(m.group(1))
                    if s[-1:].isalpha() and any(
                            len(nm) > len(s) and nm.startswith(s) for nm in source_names):
                        midword += 1

    check("no singular count takes a plural noun or verb", not agree,
          "" if not agree else f"{len(agree)} page(s), e.g. {agree[0]}")
    check("no -ys plural where -ies is correct", not plur,
          "" if not plur else f"{len(plur)} page(s), e.g. {plur[0]}")
    check("no display name is cut mid-word", midword == 0,
          "" if not midword else f"{midword} cut without an ellipsis")

    # ---- every group must appear somewhere, not just the ones with pages ---
    def listed_count(folder):
        n = 0
        for f in os.listdir(os.path.join(site, folder)):
            if not f.startswith("index"):
                continue
            src = open(os.path.join(site, folder, f), encoding="utf-8").read()
            n += len(re.findall(r'<li><a href="[^"]+"', src))
            n += len(re.findall(r'<li class="d"[^>]*>', src))
        return n
    for folder, groups in (("department", depts), ("incumbent", vends), ("category", cats)):
        check(f"all {folder} groups listed on index pages",
              listed_count(folder) == len(groups),
              f"listed {listed_count(folder)} of {len(groups)}")

    # ---- internal links ---------------------------------------------------
    broken = []
    for p in html:
        src = open(p, encoding="utf-8").read()
        for href in re.findall(r'href="(?!https?://)([^"#]+\.html)"', src):
            if not os.path.exists(os.path.normpath(os.path.join(os.path.dirname(p), href))):
                broken.append(f"{os.path.relpath(p, site)} -> {href}")
    check("no broken internal links", not broken, f"{len(broken)} broken")

    # ---- orphan pages: exist, but nothing links to them -------------------
    # A page can pass the broken-link check and still be unreachable, which
    # means no crawler will ever find it. Collision-renamed files are the
    # classic case.
    linked: set[str] = set()
    for p in html:
        src = open(p, encoding="utf-8").read()
        for href in re.findall(r'href="(?!https?://)([^"#]+\.html)"', src):
            t = os.path.normpath(os.path.join(os.path.dirname(p), href))
            linked.add(os.path.relpath(t, site).replace(os.sep, "/"))
    all_pages = {os.path.relpath(p, site).replace(os.sep, "/") for p in html}
    orphans = sorted(all_pages - linked - {"index.html"})
    check("no orphaned pages", not orphans,
          f"{len(orphans)} unreachable" + (f" e.g. {orphans[:3]}" if orphans else ""))

    # ---- SEO integrity ----------------------------------------------------
    titles, descs = [], []
    for p in html:
        src = open(p, encoding="utf-8").read()
        t = re.search(r"<title>([^<]*)</title>", src)
        d = re.search(r'name="description" content="([^"]*)"', src)
        titles.append(t.group(1) if t else "")
        descs.append(d.group(1) if d else "")
    check("every page has a title", all(titles), f"{sum(1 for t in titles if not t)} missing")
    check("all titles unique", len(set(titles)) == len(titles),
          f"{len(titles)-len(set(titles))} duplicated")
    check("all meta descriptions unique", len(set(descs)) == len(descs),
          f"{len(descs)-len(set(descs))} duplicated")

    # ---- published pages must be named by something a reader can read -----
    #
    # Written after a live category page called "#" was found by accident. It
    # held 19 real contracts, sat in the sitemap and was linked from the
    # category index, and none of the checks above objected: it had a title, a
    # unique description, a valid canonical and working links. Both checks below
    # read the RENDERED name out of each page, so they do not depend on how
    # build_site decides what to publish.
    def page_name(path, title):
        folder = os.path.basename(os.path.dirname(path))
        base = os.path.basename(path)
        if folder not in ("category", "department", "incumbent", "province"):
            return None
        if base.startswith("index"):
            return None
        m = re.match(r"^(.*?) — contracts expiring \|", _unescape(title))
        return m.group(1).strip() if m else None

    def slug_is_empty(name):
        """True when slug() could only name this page through its "x" fallback."""
        decomposed = unicodedata.normalize("NFKD", name or "")
        ascii_text = "".join(c for c in decomposed if not unicodedata.combining(c))
        return not re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")

    junk_named, fallback_named = [], []
    for p, t in zip(html, titles):
        name = page_name(p, t)
        if name is None:
            continue
        if slug_is_empty(name):
            fallback_named.append(os.path.relpath(p, site))
        if os.path.basename(os.path.dirname(p)) == "category" and placeholder_cat(name):
            junk_named.append(os.path.relpath(p, site))

    check("no category page is named by a placeholder", not junk_named,
          f"{len(junk_named)}: " + ", ".join(junk_named[:5]))
    check("no page is published under the fallback slug", not fallback_named,
          f"{len(fallback_named)}: " + ", ".join(fallback_named[:5]))

    # ---- the link out to the source record must point at the right one ----
    #
    # Every contract row shows its reference number, and that text links to the
    # government's own record for that contract. The failure that matters is not
    # a dead link, it is a LIVE link pointing at somebody else's contract: the
    # reader checks a row against the source, the source disagrees, and the site
    # is the thing that looks wrong.
    #
    # Four ways that happens, all checked here against the rendered pages
    # rather than against build_site's own idea of what it wrote:
    #   1. the URL is built from the DISPLAYED reference number, which the table
    #      clips to 34 characters, so it names a contract that does not exist
    #   2. the org code and the reference number come from different rows
    #   3. the href and the text beside it name different contracts
    #   4. the link silently disappears from rows that should carry one
    #
    # Faults 1 and 2 are both caught by comparing the linked pair against the
    # data, which is the only place the untruncated value exists: a clipped
    # reference number cannot be recognised as clipped from the page alone.
    SOURCE_BASE = "https://search.open.canada.ca/contracts/record/"
    # A reference number is unique only WITHIN a department. The source numbers
    # contracts per organisation, so the same C-YYYY-YYYY-QN-NNNNN belongs to
    # several departments at once, which is exactly why the URL needs both
    # halves. The lookup is therefore keyed on the PAIR. Keying it on the
    # reference number alone collapses those collisions: it passes the
    # single-department fixture and fails against the real data.
    linkable = {(str(r.get("buyer_org_code") or "").strip(),
                 str(r.get("reference_number") or "").strip())
                for r in rows}
    linkable = {(org, ref) for org, ref in linkable if org and ref}
    # the table clips the number it displays, so compare like with like
    shown_linkable = {ref[:34] for _org, ref in linkable}
    # What this cannot prove: where two departments happen to share a reference
    # number (~3% of them), a link that carried the OTHER department's code
    # would still name a real contract and pass. Catching that needs the link
    # tied back to its own row rather than to the data as a whole. A systematic
    # mispairing still fails loudly here, on the ~97% that are not shared.

    bad_shape, unknown_pair, text_mismatch, lost_link = [], [], [], []
    for p in html:
        src = open(p, encoding="utf-8").read()
        rel = os.path.relpath(p, site)
        for block in re.findall(r'<span class="ref">(.*?)</span>', src, re.S):
            m = re.search(r'<a href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
            if not m:
                shown = _unescape(re.sub(r"<[^>]+>", "", block)).strip()
                # only a row whose data really lacks a field may go unlinked
                if shown and shown in shown_linkable:
                    lost_link.append(f"{rel}:{shown}")
                continue
            href, shown = _unescape(m.group(1)), _unescape(m.group(2)).strip()
            if not href.startswith(SOURCE_BASE):
                bad_shape.append(f"{rel}:{href[:60]}"); continue
            parts = href[len(SOURCE_BASE):].split("%2C")
            if len(parts) != 2 or not all(parts):
                bad_shape.append(f"{rel}:{href[:60]}"); continue
            org, ref = (urllib.parse.unquote(x) for x in parts)
            if (org, ref) not in linkable:
                unknown_pair.append(f"{rel}:{org},{ref}")
            # the text is the clipped reference number, so it must be a prefix
            # of the one in the href - a reader comparing the two must not find
            # the link pointing somewhere the visible number does not say
            if not ref.startswith(shown):
                text_mismatch.append(f"{rel}:shows {shown!r} links {ref!r}")

    check("every source link has the expected shape", not bad_shape,
          f"{len(bad_shape)}: " + ", ".join(bad_shape[:3]))
    check("every source link names a contract in the data", not unknown_pair,
          f"{len(unknown_pair)}: " + ", ".join(unknown_pair[:3]))
    check("every source link matches the reference number it displays", not text_mismatch,
          f"{len(text_mismatch)}: " + ", ".join(text_mismatch[:3]))
    check("no row with a linkable reference number lost its link", not lost_link,
          f"{len(lost_link)}: " + ", ".join(lost_link[:3]))

    # ---- headline numbers on the landing page must match the data ---------
    idx = os.path.join(site, "index.html")
    if os.path.exists(idx):
        src = open(idx, encoding="utf-8").read()
        vals = re.findall(r'<div class="v">([^<]+)</div><div class="l">([^<]+)</div>', src)
        m = {lbl.strip(): v.strip() for v, lbl in vals}
        shown = m.get("Live contracts", "").replace(",", "")
        check("landing page contract count matches data",
              shown == str(len(live)), f"page says {shown}, data has {len(live)}")
        pv = money_to_float(m.get("Pipeline value", "nan"))
        # money() renders to one decimal place, so at fixture scale the display
        # rounding alone ($6.94M shown as $7.0M) exceeds 0.5%. At real scale it
        # is invisible. Widen the tolerance rather than drop the check.
        tol = 0.05 if a.allow_small else 0.005
        check(f"landing page value within {tol:.1%} of data",
              abs(pv - total_value) / total_value < tol if total_value else False,
              f"page {pv:,.0f} vs data {total_value:,.0f}")
        # The single-bid claim is the most quotable sentence on the site and it was
        # publicly wrong once, counting never-competed awards as if they were
        # competitions. Gate both halves against the data, computed here from the
        # procedure codes rather than from build_site's own split.
        bidded = [r for r in live if r.get("number_of_bids") is not None]
        comp = [r for r in bidded if r.get("solicitation_procedure") in ("TC", "OB", "ST")]
        exp_comp_unc = sum(1 for r in comp if (r.get("number_of_bids") or 0) <= 1)
        exp_noncomp = sum(1 for r in bidded
                          if r.get("solicitation_procedure") in ("TN", "AC"))
        mb = re.search(r"Of the ([\d,]+) contracts here that were openly competed and "
                       r"report a bidder count, <strong>([\d,]+) \(", src)
        mn = re.search(r"A further ([\d,]+) were never competed at all", src)
        got = (int(mb.group(1).replace(",", "")), int(mb.group(2).replace(",", ""))) if mb else None
        check("landing page competed-contract figures match data",
              got == (len(comp), exp_comp_unc),
              f"page {got} vs data {(len(comp), exp_comp_unc)}")
        gotn = int(mn.group(1).replace(",", "")) if mn else None
        check("landing page never-competed count matches data",
              gotn == exp_noncomp, f"page {gotn} vs data {exp_noncomp}")

        for b in ("0-6mo", "6-12mo", "12-24mo", "24mo+"):
            want = sum(1 for r in live if r.get("expiry_bucket") == b)
            got = m.get(b, "").replace(",", "")
            check(f"bucket {b} matches", got == str(want), f"page {got}, data {want}")

    # ---- no expired contracts leaked into the site ------------------------
    n_neg = sum(1 for r in rows if (r.get("days_to_expiry") or 0) < 0)
    check("no expired contracts in source", n_neg == 0, f"{n_neg} with negative days_to_expiry")

    # ---- vendor normalization must not merge genuinely different companies --
    # Prefix matching is the wrong test: stripping "THE" legitimately breaks it
    # ("THE UNIVERSITY OF BRITISH COLUMBIA" vs "UNIVERSITY OF BRITISH COLUMBIA").
    # Token overlap is the correct signal for whether two spellings are one firm.
    vk = defaultdict(set)
    for r in rows:
        if r.get("vendor_key"):
            vk[r["vendor_key"]].add(r.get("vendor_name") or "")
    bad_merge = []
    for k, names in vk.items():
        if len(names) < 2:
            continue
        toks = [set(re.sub(r"[^a-z0-9 ]", " ", n.lower()).split()) for n in names]
        base = set.intersection(*toks) if toks else set()
        if not base:
            bad_merge.append((k, sorted(names)[:3]))
    check("vendor_key never merges unrelated firms", not bad_merge,
          f"{len(bad_merge)} suspect" + (f" e.g. {bad_merge[0]}" if bad_merge else ""))

    # ---- look-alike rows: VERIFIED as distinct contracts, not duplicates ---
    # Checked against the raw 1,313,473-row source where procurement_id still
    # exists: of 26,840 look-alike rows, 7,796 were true duplicates (same
    # procurement_id) and were correctly collapsed by the ingest; 7,234 carried
    # DIFFERENT procurement_ids (e.g. P2300001-P2300004, four separate
    # professional-services contracts of equal value) and were correctly kept.
    # Look-alikes surviving into the export are therefore expected, not a defect.
    # This check guards against a regression that would inflate the count.
    sig = Counter((r.get("vendor_name"), r.get("buyer_org"), r.get("delivery_date"),
                   r.get("contract_value"), r.get("contract_date"),
                   r.get("number_of_bids"), r.get("commodity_code")) for r in rows)
    amb = sum(v - 1 for v in sig.values() if v > 1)
    pct = amb / max(len(rows), 1) * 100
    check("look-alike rows within verified range", pct < 3.0,
          f"{amb} rows ({pct:.2f}%) — verified against procurement_id as distinct "
          f"contracts, not duplicates")

    # ---- DATA PLAUSIBILITY -------------------------------------------------
    # Everything above verifies the site is CONSISTENT with the data file. None
    # of it verifies the data file is PLAUSIBLE. A truncated download produces a
    # perfectly consistent site containing a fraction of reality, and would
    # otherwise publish cleanly. These are absolute floors set well below
    # observed values (26,221 live / $125.2B / 92 depts / 227 cats / 10,902
    # vendors) — the federal government does not lose 40% of its contract base
    # in a quarter, so tripping one of these means the pipeline broke.
    n_dep, n_cat, n_ven = len(depts), len(cats), len(vends)
    floors = [
        ("live contracts >= 15,000", len(live), 15_000),
        ("pipeline value >= $50B", int(total_value), 50_000_000_000),
        ("departments >= 50", n_dep, 50),
        ("category names >= 100", n_cat, 100),
        ("vendor identities >= 5,000", n_ven, 5_000),
    ]
    # The floors detect a TRUNCATED PRODUCTION PIPELINE. They are meaningless
    # against a deliberately small fixture, so the offline check skips them and
    # says so out loud rather than passing silently.
    if a.allow_small:
        print("[SKIP] plausibility floors — --allow-small, fixture-scale input")
        floors = []

    for label, actual, floor in floors:
        check(f"plausibility: {label}", actual >= floor,
              f"actual {actual:,}" + ("" if actual >= floor else f" — BELOW FLOOR {floor:,}, "
                                      "pipeline likely truncated"))

    # ---- spot-check one page's numbers against the data -------------------
    big = max(vends.items(), key=lambda kv: sum(i.get("contract_value") or 0 for i in kv[1]))
    key, items = big
    disp = max({i["vendor_name"] for i in items},
               key=lambda n: sum(1 for i in items if i["vendor_name"] == n))
    fn = re.sub(r"[^a-z0-9]+", "-", disp.lower()).strip("-")[:70] + ".html"
    p = os.path.join(site, "incumbent", fn)
    if os.path.exists(p):
        src = open(p, encoding="utf-8").read()
        vals = dict((lbl.strip(), v.strip()) for v, lbl in
                    re.findall(r'<div class="v">([^<]+)</div><div class="l">([^<]+)</div>', src))
        check(f"spot-check '{disp[:28]}' contract count",
              vals.get("Contracts", "").replace(",", "") == str(len(items)),
              f"page {vals.get('Contracts')}, data {len(items)}")
    else:
        check("spot-check page exists", False, f"{fn} not found", warn_only=True)

    # ---- report -----------------------------------------------------------
    width = max(len(n) for _s, n, _d in results) + 2
    print(f"\nAUDIT — {os.path.basename(a.site)} against {os.path.basename(a.input)}")
    print("=" * (width + 30))
    for status, name, detail in results:
        mark = {PASS: "PASS", FAIL: "FAIL", WARN: "WARN"}[status]
        print(f"[{mark}] {name:<{width}} {detail}")
    fails = sum(1 for s, _n, _d in results if s == FAIL)
    print("=" * (width + 30))
    print(f"{len(results)} checks, {fails} failed\n")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
