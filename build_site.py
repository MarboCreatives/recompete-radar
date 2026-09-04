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
import sys
import unicodedata
import urllib.parse
from collections import Counter, defaultdict
from datetime import date
from typing import Any, Iterable, Optional

SITE = "Canadian Recompete Radar"
TAG = ("Federal contracts coming up for renewal — who holds them, "
       "what they're worth, and how contested they were.")

# Visitors read the site as a tender board and then wonder why nothing has a
# "bid" button. Nothing here is open to bid: these are contracts already
# awarded, listed by when they expire, so a supplier can approach the
# department before the recompete rather than after the notice. The site never
# said so anywhere, which made every other number on the page confusing.
NOT_A_TENDER_BOARD = (
    "These contracts are already awarded. Nothing here is open to bid today — "
    "the point is to see a renewal coming while there is still time to talk to "
    "the department. Open tenders are posted on CanadaBuys.")

# Raised by a retired federal procurement officer, Aug 2026, and it is the
# single most important limit on how these dates should be read. A contract is
# often a short base period plus option years. The disclosure data publishes
# only the period actually committed to, so an option exercised later simply
# moves the end date and nothing announces it in advance. Read the date as a
# floor, never as a promise.
# Solicitation procedure codes from the source data. TN and AC are awards that
# were never opened to competition at all, so a bid count of one is the definition
# of the procedure rather than a finding about it. Quoting them alongside genuine
# competitions inflates the single-bid share; raised by a procurement veteran on
# Reddit, Aug 2026, and he was right. Anything not in either set is counted in
# neither, so an unknown future code is excluded rather than silently miscounted.
# Directory badge, HOME PAGE ONLY. Maidensail hands out a dofollow link in
# exchange for the embed, which is a reciprocal link arrangement. One link from
# the home page is a small bet; the same link on all 2,099 pages would be a
# sitewide reciprocal footprint, which is exactly the pattern search engines
# discount. width and height are set so the external image cannot shift the
# layout while it loads, and it is lazy so it never blocks first paint.
MAIDENSAIL_BADGE = (
    '<br><a href="https://maidensail.com/startup/canadian-recompete-radar" rel="dofollow">'
    '<img src="https://maidensail.com/badge/canadian-recompete-radar.svg?theme=dark" '
    'alt="Featured on Maidensail" width="190" height="44" loading="lazy"></a>')

COMPETITIVE_PROCEDURES = {"TC", "OB", "ST"}      # traditional competitive, open bidding, selective tendering
NONCOMPETITIVE_PROCEDURES = {"TN", "AC"}         # traditional non-competitive, ACAN

EYEBROW = "Government of Canada contract data"

# Site analytics, added Sep 2026. GoatCounter collects no cookies and no
# personal data, and it reads utm_source/utm_campaign off the URL on its own,
# which is the only way to tell an organic X post, a boosted X post and a
# LinkedIn click apart. Before this the site had no analytics of any kind, so
# there was no way to see whether posts with thousands of views were even
# reaching the site, let alone where a visitor dropped off. async and placed
# last in <head> so it can never block first paint.
GOATCOUNTER_SCRIPT = ('<script data-goatcounter="https://canadianrecompeteradar.goatcounter.com/count" '
                      'async src="//gc.zgo.at/count.js"></script>')

OPTION_YEARS = (
    "Expiry dates show the period a department has committed to. Many contracts "
    "carry option years that are not published until they are exercised, so treat "
    "a date here as the earliest a contract could come back, not a guarantee "
    "that it will.")

# Thin-content thresholds. A page is generated only if the group clears one.
MIN_CONTRACTS = 3
MIN_VALUE = 5_000_000

# --- Email capture -----------------------------------------------------------
# GitHub Pages is static and cannot process a form POST, so the form must submit
# to a third-party endpoint (Buttondown, MailerLite, Kit, Formspree...). Set it
# with --signup-action. If it is NOT set, no form is rendered at all: a form that
# posts nowhere silently swallows signups, which is worse than having none.
SIGNUP_ACTION = ""
SIGNUP_CATEGORIES: list[str] = []   # populated from the data in build()

# Optional profile fields, posted as fields[<key>] alongside fields[category].
#
# These exist to make a subscriber describable. An email address on its own can
# be counted but not characterised, and a list you cannot characterise is worth
# close to nothing to a referral partner. The answers cannot be collected
# retroactively without emailing strangers to ask what they do for a living, so
# every signup that arrives before these ship is permanently unsegmented.
#
# Each list's first entry MUST have an empty value and MUST be the one the
# browser preselects. A dropdown that opens on "Alberta" collects "Alberta" from
# everyone who ignores it, and those people then cannot be told apart from actual
# Albertans. A blank default records "no answer", which is true. A silent default
# records a lie that looks like data.
#
# None of the three is `required`. Three mandatory questions between a stranger
# and a free newsletter costs more subscribers than the answers are worth.
#
# The keys (role, bids_federal, province) must match the custom-field keys in the
# mail provider EXACTLY. Kit accepts a POST containing unrecognised fields[...]
# keys, returns success, and stores nothing — so a typo here produces a form that
# passes every test except the one that matters. Create the fields in Kit first,
# copy the keys it assigns, then verify with one real signup that the values
# actually land on the subscriber record.
SIGNUP_ROLE_OPTIONS: list[tuple[str, str]] = [
    ("", "Your role (optional)"),
    ("independent", "Independent contractor"),
    ("owner", "Small business owner"),
    ("consultant", "Consultant or professional services"),
    ("bid_staff", "Bid or proposal staff"),
    ("subcontractor", "Subcontractor to a prime"),
    ("other", "Other"),
]

# "watching" is not padding. It catches journalists, researchers and competitors,
# who would otherwise hide inside the other four answers and inflate any
# engagement figure quoted to a sponsor. "not_yet" is the commercially valuable
# one: someone who wants to bid and does not yet is exactly who a bid consultant
# would pay to reach.
SIGNUP_BIDS_OPTIONS: list[tuple[str, str]] = [
    ("", "Do you bid federally? (optional)"),
    ("regularly", "Regularly"),
    ("occasionally", "Occasionally"),
    ("not_yet", "Not yet, but want to"),
    ("watching", "Just keeping an eye on it"),
]

# The subscriber's OWN province. Not the place of performance, and not the
# supplier address the province pages are built from — three different things.
SIGNUP_PROVINCE_OPTIONS: list[tuple[str, str]] = [
    ("", "Your province (optional)"),
    ("AB", "Alberta"),
    ("BC", "British Columbia"),
    ("MB", "Manitoba"),
    ("NB", "New Brunswick"),
    ("NL", "Newfoundland and Labrador"),
    ("NS", "Nova Scotia"),
    ("NT", "Northwest Territories"),
    ("NU", "Nunavut"),
    ("ON", "Ontario"),
    ("PE", "Prince Edward Island"),
    ("QC", "Quebec"),
    ("SK", "Saskatchewan"),
    ("YT", "Yukon"),
]

# CASL identification. The Electronic Commerce Protection Regulations require a
# REQUEST for consent — not just the emails that follow — to set out the name of
# the business seeking consent, a mailing address, a contact method, and a
# statement that consent can be withdrawn. A signup box without these is itself
# the violation, so the build refuses to render one. Kept as parameters so the
# address can be swapped (e.g. home -> PO box) with a flag, not a code edit.
SIGNUP_BUSINESS = ""
SIGNUP_ADDRESS = ""
SIGNUP_CONTACT = ""

# The address the double opt-in confirmation is sent from. 17 of the first 32
# signups never confirmed because the confirmation went out from a free gmail.com
# address through the mail provider's servers: gmail.com does not authorise those
# servers, so the mail failed authentication and was filed as spam or dropped with
# no bounce. Telling people what to look for is the cheap half of the fix. This
# MUST match the default from address set in the mail provider.
CONFIRM_SENDER = "hello@recompeteradar.ca"
SIGNUP_PER_WEEK = 0        # derived in build(); see note there
SIGNUP_YEAR_VALUE = 0.0
SIGNUP_CROSSING_N = 0

# Google Search Console site-verification token. Emitted into every page's
# <head> when set. See page() for why this is generated rather than a file.
GOOGLE_VERIFICATION = ""

# Absolute site root, e.g. "https://recompeteradar.ca". Set in build() from
# --base-url and emitted as <link rel="canonical"> on every page. A module
# global for the same reason GOOGLE_VERIFICATION is one: page() is called from
# three places and threading the value through every caller buys nothing.
BASE_URL = ""


def declared_path(url: str) -> str:
    """The ONE address a generated file is published at.

    GitHub Pages serves foo/index.html at foo/ as well, so declaring the
    index.html form splits a single page across two addresses that a search
    engine sees as competing duplicates. Every internal link, the http-to-https
    redirect and every human-typed link land on the directory form, so that is
    the form this site declares.

    The sitemap and the canonical tag BOTH go through this function, so they
    cannot drift apart; there is only one rule and one place to change it.
    """
    if url == "index.html":
        return ""
    if url.endswith("/index.html"):
        return url[:-len("index.html")]
    return url

# folder -> group key -> the filename actually written for that group.
# Populated in build() BEFORE any page is rendered, because a department page
# links to incumbent pages and vice versa; if this were filled in as pages were
# written, whichever folder was written first would link to nothing.
# Links are built from this map, never by re-deriving the slug from a display
# name — collision-renamed pages would be silently orphaned.
FILENAMES: dict[str, dict[str, str]] = {"department": {}, "incumbent": {}, "category": {},
                                        "province": {}}

# key -> "index-N.html#anchor" for groups BELOW the thin-content threshold.
# Those groups have no page, but they are all listed on the folder index, so the
# index row is the honest destination for a click on their name.
ANCHORS: dict[str, dict[str, str]] = {"department": {}, "incumbent": {}, "category": {},
                                      "province": {}}

# Groups per index page. Used by both the anchor assignment and the index
# writer; they MUST agree or a link lands on the wrong page.
SMALL_PER_PAGE = 1000


def entity_link(folder: str, key: Optional[str], text: str, depth: int) -> str:
    """Link to an entity page, or to the entity's row on the folder index.

    Groups below the thin-content threshold get no page of their own, so there
    is nothing to link to directly. Returning bare text for those was correct
    but read as broken: a reader sees a supplier name sitting in a table next to
    a dozen linked ones and cannot tell whether the site is missing a page or
    their click failed. Fragment links to the index row keep the thin-content
    guard exactly as it was (no extra pages are generated) while giving every
    name somewhere to go.
    """
    safe = esc(text)
    k = key or ""
    fn = FILENAMES.get(folder, {}).get(k)
    prefix = "../" * depth
    if fn:
        return f'<a href="{prefix}{folder}/{fn}">{safe}</a>'
    target = ANCHORS.get(folder, {}).get(k)
    if target:
        return f'<a class="ix" href="{prefix}{folder}/{target}">{safe}</a>'
    return safe


# The government publishes its own record page for every contract in this
# dataset, on the Open Government contract search. Nothing in the source data
# carries a URL, but the path is built from two fields the pipeline already
# holds: the department's org code and the contract's reference number.
#
# Verified against the live service before this was written: a 15,000-row
# sample carried both fields on 100% of rows, reference numbers use only
# [A-Z0-9-] and org codes only [a-z-], and 6 of 6 spot-checked links across
# six departments and seven years resolved to the correct contract.
SOURCE_RECORD_BASE = "https://search.open.canada.ca/contracts/record/"


def source_record_url(org_code: Optional[str], reference_number: Optional[str]) -> Optional[str]:
    """The government's own published record for one contract, or None.

    None when either field is missing. A half-built URL does not error, it
    lands on somebody else's record or a search page, and a link that
    confidently points at the wrong contract is worse than no link on a site
    whose whole claim is that the numbers can be checked.

    The FULL reference number goes in the href. The table clips the displayed
    text to 34 characters, and a URL built from clipped text points at a
    contract that does not exist.

    The comma between the two values is a delimiter in the path segment rather
    than part of either value, so it is written percent-encoded and the values
    themselves are quoted with nothing left safe.
    """
    org = urllib.parse.quote(str(org_code or "").strip(), safe="")
    ref = urllib.parse.quote(str(reference_number or "").strip(), safe="")
    if not org or not ref:
        return None
    return f"{SOURCE_RECORD_BASE}{org}%2C{ref}"


def signup_pitch() -> str:
    """The volume claim, phrased so it stays true at any data size.

    A weekly rate is the most compelling framing but becomes nonsense when the
    flow is under one a week ("Around 0 contracts...cross every week"), so fall
    back to the annual figure, and drop the claim entirely if nothing is
    crossing. Real data currently gives ~63/week; these branches exist so a
    future refresh cannot turn the pitch into a false or absurd statement.
    """
    if SIGNUP_CROSSING_N <= 0:
        return "Federal contracts approaching renewal, summarised weekly by email, free."
    # The count is 1 often enough on a small slice to matter: this pitch sits on
    # every page, so "1 federal contracts cross" was the most-printed of the
    # agreement faults even though it is the least-linked.
    if SIGNUP_PER_WEEK >= 1:
        lead = (f"Around <strong>{count_noun(SIGNUP_PER_WEEK, 'federal contract')}</strong> "
                f"{'crosses' if SIGNUP_PER_WEEK == 1 else 'cross'} into the "
                f"12&#8209;month planning window every week")
    else:
        lead = (f"<strong>{count_noun(SIGNUP_CROSSING_N, 'federal contract')}</strong> "
                f"{'crosses' if SIGNUP_CROSSING_N == 1 else 'cross'} into the "
                f"12&#8209;month planning window over the coming year")
    return (f"{lead} — {money(SIGNUP_YEAR_VALUE)} of contract value a year. "
            f"Get the week's list by email, free.")


def _select(name: str, label: str, options: list[tuple[str, str]]) -> str:
    """One optional profile dropdown.

    The first option carries an empty value and is emitted first, so it is what
    the browser preselects: an unanswered question must record nothing rather
    than silently recording whichever answer happened to sort first.
    """
    opts = "".join(f'<option value="{esc(v)}">{esc(t)}</option>' for v, t in options)
    return (f'<select name="fields[{esc(name)}]" aria-label="{esc(label)}">'
            f'{opts}</select>')


VALUE_LINE = ("See a renewal before the notice goes out — the kind of "
              "intel GovCon sales teams pay for, free here.")


def signup_cta() -> str:
    """One line in the header pointing at the form further down the page.

    The form used to sit directly under the header on every page, which put five
    controls in front of the data. Moving it below the content risked the
    signups, so the pitch stays up top as a single line and jumps to the form.

    Sep 2026: swapped the volume-stat clause for VALUE_LINE, the "why use this"
    line Taj and Martin both asked for independently. The stat it replaces is
    not lost, it still runs in full in signup_block() further down the page;
    this is a swap, not an addition, so the header word count does not creep
    back up the way it did before the redesign.
    """
    if not SIGNUP_ACTION:
        return ""
    return (f'<p class="sb">{VALUE_LINE} '
            f'<a href="#brief"><strong>Get the free weekly brief &rarr;</strong></a></p>')


def signup_block() -> str:
    """One email box, on every page. Feeds both revenue models:
    the address builds the newsletter list; the optional category field is what
    turns a subscriber into a routable referral lead. Consent is explicit and the
    referral intent is disclosed up front — PIPEDA applies, and burying it would
    poison the only asset this product has, which is being trustworthy.

    The three profile dropdowns sit in a second, lighter row below the button.
    Putting them in the primary row would have made a five-control wall in front
    of an email box, and the email box is the only field that must be filled.
    """
    if not SIGNUP_ACTION:
        return ""
    opts = "".join(f'<option value="{esc(c)}">{esc(clip(c, 60))}</option>'
                   for c in SIGNUP_CATEGORIES)
    profile = (
        _select("role", "Your role", SIGNUP_ROLE_OPTIONS)
        + _select("bids_federal", "Whether you bid on federal work",
                  SIGNUP_BIDS_OPTIONS)
        + _select("province", "Your province", SIGNUP_PROVINCE_OPTIONS)
    )
    return f"""
<section class="sub" id="brief">
  <h2>Weekly recompete brief</h2>
  <p class="sb">{signup_pitch()}</p>
  <form class="subf" action="{esc(SIGNUP_ACTION)}" method="post" target="_blank">
    <input type="email" name="email_address" required placeholder="you@company.ca"
           aria-label="Email address">
    <select name="fields[category]" aria-label="Category you bid in">
      <option value="">What do you bid on? (optional)</option>
      {opts}
    </select>
    <button type="submit">Get the brief</button>
    <div class="subx">
      <span class="fine">Optional, and it helps me make the brief useful to you.</span>
      {profile}
    </div>
  </form>
  <p class="fine">One email will arrive from <strong>{esc(CONFIRM_SENDER)}</strong>
  asking you to confirm. If it is not in your inbox, look in spam or promotions and
  mark it &quot;not spam&quot;, or the weekly brief will not reach you either.</p>
  <p class="fine">Free, weekly. <strong>You can withdraw your consent and
  unsubscribe at any time</strong>, using the link in every email. We may
  occasionally introduce you to bid consultants or proposal specialists relevant
  to your category — only if you ask, never by passing your details on without
  you saying yes first.</p>
  <p class="fine" id="casl">Sent by {esc(SIGNUP_BUSINESS)}, {esc(SIGNUP_ADDRESS)} &middot; <a href="{esc(SIGNUP_CONTACT)}">{esc(SIGNUP_CONTACT)}</a></p>
</section>"""

CSS = """
:root{--bg:#0f1115;--pn:#171a21;--ln:#252a34;--tx:#e6e9ef;--dm:#98a1b3;
--ac:#4da3ff;--wn:#ffb454;--ht:#ff6b6b;--ok:#5ecb8b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);
font:15.5px/1.62 'Inter',ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,sans-serif}
a{color:var(--ac);text-decoration:none}a:hover{text-decoration:underline}
.w{max-width:1160px;margin:0 auto;padding:28px 20px 70px}
header{border-bottom:1px solid var(--ln);padding-bottom:20px;margin-bottom:26px}
h1{margin:0 0 10px;font-size:36px;font-weight:800;letter-spacing:-.028em;line-height:1.05}
h2{font-size:21px;font-weight:700;letter-spacing:-.018em;margin:38px 0 12px}
.sb{color:var(--dm);font-size:14px;margin:0}
.crumb{font-size:12.5px;color:var(--dm);margin-bottom:12px}
.cd{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:11px;margin:16px 0}
.c{background:var(--pn);border:1px solid var(--ln);border-radius:10px;padding:13px 15px}
.c.b{border-color:#2f6ea8}
.c .v{font-size:27px;font-weight:700;letter-spacing:-.025em;font-variant-numeric:tabular-nums}
.c .l{color:var(--dm);font-size:10.5px;font-weight:600;text-transform:uppercase;letter-spacing:.10em;margin-top:3px}
/* Eyebrow, notes panel and the header call to action. Added Aug 2026 after two
   people on X independently said the page felt sterile and that too much sat in
   front of the data. */
.eyebrow{font-family:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
 font-size:12px;font-weight:500;letter-spacing:.18em;text-transform:uppercase;color:var(--ac);margin:0 0 12px}
header p.sb{max-width:66ch;font-size:16px;line-height:1.55}
.notes{border:1px solid var(--ln);border-radius:12px;background:var(--pn);padding:16px 18px;margin:24px 0 8px}
.notes p{margin:0 0 9px;max-width:78ch}
.notes p:last-child{margin-bottom:0}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;color:var(--dm);font-weight:500;font-size:11px;text-transform:uppercase;
letter-spacing:.06em;padding:8px;border-bottom:1px solid var(--ln)}
td{padding:9px 8px;border-bottom:1px solid var(--ln)}
tr:hover td{background:#141821}
.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.d{color:var(--dm);font-size:12.5px}
/* Contract reference number: present for anyone who needs to quote it to a
   department, visually subordinate so it doesn't compete with the vendor name. */
.ref{color:var(--dm);font-size:11px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;opacity:.75}
/* The buyer's own note on what the work is. Set below the incumbent name
   because it describes the contract, not the supplier, and the incumbent
   column is the only one wide enough to carry a sentence. */
.scope{display:block;color:var(--tx);font-size:12px;opacity:.82;margin-top:3px;max-width:34em}
/* The "not a tender board" line. Sits directly under the tagline on every
   page, bordered so it reads as a statement of scope rather than blurb. */
.nb{border-left:2px solid var(--ac);padding-left:9px;margin-top:7px;max-width:56em;font-size:13px}
.flag{display:inline-block;font-size:10px;font-weight:600;letter-spacing:.04em;
text-transform:uppercase;color:var(--dm);border:1px solid var(--ln);
border-radius:4px;padding:1px 5px;margin-top:4px;margin-right:4px}
.flag.no{color:var(--ht);border-color:rgba(255,107,107,.4)}
td a{color:inherit;text-decoration:none;border-bottom:1px solid var(--ln)}
td a:hover{color:var(--ac);border-bottom-color:var(--ac)}
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
/* The signup box now sits directly under the header rather than at the foot of
   the page, so the margins are inverted: no gap above, breathing room below
   before the numbers. Leaving the old 38px-top/0-bottom would have hung it off
   the header and jammed it against the stat cards. */
.sub{margin:0 0 24px;padding:20px;background:var(--pn);border:1px solid #2f6ea8;border-radius:10px}
.sub h2{margin:0 0 6px;font-size:17px}
.subf{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 10px}
.subf input,.subf select{flex:1 1 200px;min-width:0;padding:9px 11px;border-radius:7px;
border:1px solid var(--ln);background:var(--bg);color:var(--tx);font-size:14px}
.subf button{padding:9px 18px;border-radius:7px;border:0;background:var(--ac);
color:#06121f;font-weight:600;font-size:14px;cursor:pointer;white-space:nowrap}
.subf button:hover{filter:brightness(1.08)}
.fine{color:var(--dm);font-size:11.5px;margin:0;line-height:1.55}
/* Names with no page of their own link to their row on the folder index.
   Dotted underline so it is honestly distinguishable from a link to a real
   page, and :target flashes the row so the reader can see where they landed
   in a list of a thousand. */
a.ix{border-bottom:1px dotted #3d6f9e}
li.d:target{background:#1d2836;outline:2px solid var(--ac);border-radius:5px;
padding:2px 6px;color:var(--tx)}
/* Second row, full width inside the flex form. Deliberately quieter than the
   primary row: smaller, dimmer text, so the eye still lands on the email box. */
.subx{flex:1 1 100%;display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:2px}
.subx .fine{flex:1 1 100%;margin-bottom:2px}
.subf .subx select{flex:1 1 158px;padding:7px 9px;font-size:12.5px;color:var(--dm)}
@media(max-width:560px){.subf input,.subf select,.subf button{flex:1 1 100%}
.subf .subx select{flex:1 1 100%}}
.tw{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:0 -4px;padding:0 4px}
.tw table{min-width:560px}
@media(max-width:560px){
  .tw table{min-width:0}
  .tw th:nth-child(4),.tw td:nth-child(4){display:none}
  .tw th:nth-child(5),.tw td:nth-child(5){display:none}
}
footer{margin-top:44px;padding-top:16px;border-top:1px solid var(--ln);
color:var(--dm);font-size:12px;line-height:1.7}

/* Primary navigation. Two separate users asked for browse-by-category when it
   already existed, because the only route to it was a link at the foot of the
   landing page. It now sits under the masthead on every page. */
.nav{display:flex;flex-wrap:wrap;gap:8px;margin:13px 0 0}
.nav a{display:inline-block;padding:5px 12px;border:1px solid var(--ln);
border-radius:20px;background:var(--pn);color:var(--tx);font-size:12.5px}
.nav a:hover{border-color:var(--ac);color:var(--ac);text-decoration:none}
@media(max-width:560px){.nav a{font-size:12px;padding:5px 10px}}

/* Sortable columns. The arrow is drawn in CSS from the aria-sort attribute the
   script sets, so the accessible state and the visible state cannot disagree. */
th[data-s]{cursor:pointer;user-select:none;-webkit-user-select:none;white-space:nowrap}
th[data-s]:hover,th[data-s]:focus{color:var(--ac)}
th[data-s]:focus{outline:1px solid var(--ac);outline-offset:2px}
th[data-s]::after{content:"\\2195";margin-left:5px;font-size:10px;opacity:.35}
th[data-s][aria-sort="ascending"]::after{content:"\\2191";opacity:1;color:var(--ac)}
th[data-s][aria-sort="descending"]::after{content:"\\2193";opacity:1;color:var(--ac)}
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


# --------------------------------------------------------------------------
# English surface text
#
# Every string a reader sees here is generated, never typed, so a proofreader
# cannot find a fault in it. These three helpers exist because all three faults
# below reached the live site and stayed there:
#
#   * `label.lower() + "s"` published "categorys" in an <h2>, a <title> and a
#     meta description on /category/index.html.
#   * `display[:52]` cut 250 distinct names mid-word with no ellipsis, and the
#     [:60] variant put "...including semina" in the signup dropdown on all
#     2,101 pages.
#   * A fixed plural verb printed "1 federal contracts", "1 expire within 12
#     months" and "1 were uncontested" across 660 meta descriptions.
#
# Meta descriptions are the text Google prints under the result, so these are
# read by more people than any page body on the site.
# --------------------------------------------------------------------------

def plural(word: str) -> str:
    """Plural of an index label. The four live labels are department,
    incumbent, category and supplier province; the -y rule is the one that
    matters and the others are here so a new label cannot reintroduce this."""
    w = word or ""
    if w.endswith("y") and (len(w) < 2 or w[-2].lower() not in "aeiou"):
        return w[:-1] + "ies"
    if w.endswith(("s", "x", "z", "ch", "sh")):
        return w + "es"
    return w + "s"


def clip(text: str, limit: int) -> str:
    """Shorten to `limit` characters on a word boundary, marking the cut.

    A bare slice is what produced "...telecommunications consul". Falling back
    to a hard cut when a single token is longer than the limit is deliberate:
    some vendor names are one very long word, and returning them in full would
    break the column the caller sized the limit for.
    """
    t = " ".join((text or "").split())
    if len(t) <= limit:
        return t
    cut = t[:limit - 1].rstrip()
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:-–—/") + "…"


def count_noun(n: int, singular: str, plural_form: Optional[str] = None) -> str:
    """'1 contract' / '2 contracts', with the number group-separated."""
    return f"{n:,} {singular if n == 1 else (plural_form or plural(singular))}"


def bucket_pill(b: Optional[str]) -> str:
    cls = {"0-6mo": "hot", "6-12mo": "warn", "12-24mo": "good"}.get(b or "", "dim")
    return f'<span class="p {cls}">{esc(b or "—")}</span>'


def density_pill(d: Optional[str]) -> str:
    cls = {"uncontested": "hot", "low": "warn", "moderate": "good", "high": "dim"}.get(d or "", "dim")
    return f'<span class="p {cls}">{esc(d or "—")}</span>'


# The only JavaScript on the site, and it is strictly additive: every table is
# fully readable and correctly ordered with scripting disabled. Sorting reads a
# numeric `data-s` attribute rather than the visible text, because "$5.7B" sorts
# before "$97K" as a string and "—" is not a number at all. Rows with no value
# stay at the bottom in both directions rather than flipping to the top, which is
# what people actually mean by "sort by bidders".
SORT_JS = """<script>
(function(){
 document.querySelectorAll('table').forEach(function(t){
  var hs=t.querySelectorAll('th[data-s]');
  if(!hs.length)return;
  hs.forEach(function(h){
   h.tabIndex=0;h.setAttribute('role','button');
   var go=function(){
    var i=h.cellIndex,
        dir=h.getAttribute('aria-sort')==='ascending'?-1:1;
    hs.forEach(function(o){o.removeAttribute('aria-sort');});
    h.setAttribute('aria-sort',dir===1?'ascending':'descending');
    var rows=Array.prototype.slice.call(t.rows,1);
    rows.sort(function(a,b){
     var x=parseFloat(a.cells[i].getAttribute('data-s')),
         y=parseFloat(b.cells[i].getAttribute('data-s')),
         xn=isNaN(x),yn=isNaN(y);
     if(xn&&yn)return 0;
     if(xn)return 1;
     if(yn)return -1;
     return (x-y)*dir;
    });
    var f=document.createDocumentFragment();
    rows.forEach(function(r){f.appendChild(r);});
    t.tBodies[0].appendChild(f);
   };
   h.addEventListener('click',go);
   h.addEventListener('keydown',function(e){
    if(e.key==='Enter'||e.key===' '){e.preventDefault();go();}
   });
  });
 });
})();
</script>"""

# Ranking for the "Last time" column. The visible cell is a word, but the useful
# order is how contested the contract was, so sort on that instead of the label.
DENSITY_RANK = {"uncontested": 0, "low": 1, "moderate": 2, "high": 3}


def sort_key(value) -> str:
    """Render a data-s attribute, or nothing when there is no value to sort on."""
    return "" if value is None else f' data-s="{value}"'


def page(title: str, desc: str, body: str, depth: int = 0, url: str = "",
         extra_notes: str = "") -> str:
    root = "../" * depth
    # Search Console verification is emitted by the generator, not dropped in as
    # a static file. The site directory is rebuilt from scratch every refresh,
    # so an uploaded google*.html would be deleted and Google would silently
    # un-verify the property. A generated tag cannot be lost that way.
    gv = (f'\n<meta name="google-site-verification" content="{esc(GOOGLE_VERIFICATION)}">'
          if GOOGLE_VERIFICATION else "")
    # One self-referencing canonical per page. Without it every page is reachable
    # at more than one address (directory form vs index.html, and anything a
    # linker appends), and the crawler splits its budget across the duplicates
    # instead of indexing the ~2,100 pages that matter.
    can = (f'\n<link rel="canonical" href="{BASE_URL}/{declared_path(url)}">'
           if BASE_URL else "")
    badge = MAIDENSAIL_BADGE if url == "index.html" else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}">{gv}{can}
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=IBM+Plex+Mono:wght@500&display=swap" rel="stylesheet">
<style>{CSS}</style>{GOATCOUNTER_SCRIPT}</head><body><div class="w">
<header><p class="eyebrow">{esc(EYEBROW)}</p>
<h1><a href="{root}index.html" style="color:inherit">{SITE}</a></h1>
<p class="sb">{esc(TAG)}</p>
{signup_cta()}
<nav class="nav" aria-label="Browse the dataset">
<a href="{root}index.html">Expiring soonest</a>
<a href="{root}category/index.html">Browse by category</a>
<a href="{root}department/index.html">Browse by department</a>
<a href="{root}incumbent/index.html">Browse by incumbent</a>
<a href="{root}province/index.html">Browse by supplier province</a>
</nav></header>
{body}
<div class="notes"><p class="nb">{esc(NOT_A_TENDER_BOARD)}</p>
<p>{esc(OPTION_YEARS)}</p>{extra_notes}</div>
{signup_block()}
<footer>Built from the Government of Canada <strong>Proactive Publication of Contracts</strong>
dataset (contracts over $10,000), Treasury Board of Canada Secretariat.
Open Government Licence – Canada.<br>
Figures are <strong>total contract value over the full contract term</strong>, not annual
spend. Only services and construction contracts are shown, where the published
"Contract Period End Date or Delivery Date" field is defined as the end of the
performance period. Published quarterly, so the most recent quarter may not appear.
Not affiliated with the Government of Canada.{badge}</footer>
</div>{SORT_JS}</body></html>"""


def contract_table(rows: list[dict], show: tuple[str, ...] = ("dept", "cat"),
                   limit: int = 250, depth: int = 0) -> str:
    """The main listing. Entity names link through to their own pages where one
    exists — this is both the obvious navigation people expect and the bulk of
    the site's internal linking, which is how search engines discover the ~2,100
    detail pages in the first place.

    `depth` is how many folders deep the containing page is, so relative links
    resolve from both the root index and from pages inside folder/.
    """
    # data-s marks a column as sortable. Only the four numeric/ordinal columns
    # get it — sorting "Incumbent" alphabetically is a different feature and a
    # sort control that does nothing useful is worse than no control.
    head = ("<tr><th data-s>Expires</th><th class='n' data-s>Value</th>"
            "<th>Incumbent</th>")
    if "dept" in show:
        head += "<th>Department</th>"
    if "cat" in show:
        head += "<th>Category</th>"
    head += "<th class='n' data-s>Bidders</th><th data-s>Last time</th></tr>"

    out = []
    for c in rows[:limit]:
        days = c.get("days_to_expiry")
        bids = c.get("number_of_bids")
        # The published reference number is what lets someone quote the exact
        # contract when they contact the department. Kept as a subordinate line
        # inside the incumbent cell rather than its own column: an 8th column
        # reintroduces the mobile overflow the .tw wrapper exists to prevent.
        # It also carries the link out to the government's own published record
        # for this contract, so any row here can be checked against the source.
        # Plain text is the fallback when either field the URL needs is missing.
        ref = c.get("reference_number")
        src = source_record_url(c.get("buyer_org_code"), ref)
        if ref and src:
            ref_html = (f'<br><span class="ref"><a href="{esc(src)}"'
                        f' title="This contract on the Government of Canada'
                        f' contract search">{esc(str(ref)[:34])}</a></span>')
        elif ref:
            ref_html = f'<br><span class="ref">{esc(str(ref)[:34])}</span>'
        else:
            ref_html = ""

        # What the contract is actually for, when the buyer wrote it down and
        # the text passes the name scan. This is the field readers asked for:
        # the Category column alone is often "Other professional services not
        # elsewhere specified", which tells them nothing.
        scope = scope_text(c.get("comments_en"))
        scope_html = f'<span class="scope">{esc(scope)}</span>' if scope else ""

        # Both of these are already computed and have never been displayed.
        # "You cannot bid on this one" is as useful to a small supplier as the
        # opposite, and it is the difference between a lead and a wasted call.
        flags = ""
        if c.get("is_sole_sourced"):
            flags += '<span class="flag no" title="Awarded without competition">Sole-sourced</span>'
        if c.get("standing_offer_number"):
            flags += ('<span class="flag" title="Called up against an existing '
                      'standing offer, not tendered separately">Standing offer</span>')
        flags_html = f"<br>{flags}" if flags else ""
        cells = [
            f'<td{sort_key(days)}>{bucket_pill(c.get("expiry_bucket"))} '
            f'<span class="d">{days}d</span></td>',
            f'<td class="n"{sort_key(c.get("contract_value"))}>{money(c.get("contract_value"))}</td>',
            f'<td>{entity_link("incumbent", c.get("vendor_key"), clip(c.get("vendor_name") or "—", 38), depth)}'
            f'{ref_html}{flags_html}{scope_html}</td>',
        ]
        if "dept" in show:
            dept_txt = clip((c.get("buyer_org") or "").split(" | ")[0], 38)
            cells.append(f'<td class="d">{entity_link("department", c.get("buyer_org"), dept_txt, depth)}</td>')
        if "cat" in show:
            # A placeholder name has no page to link to, and printing it as
            # plain text would put "#" in the Category column of every
            # department and incumbent page that carries these rows.
            raw_cat = (c.get("category_name") or "").strip()
            cat_txt = ("—" if is_placeholder_category(raw_cat)
                       else clip(raw_cat or c.get("commodity_code") or "", 38))
            cells.append(f'<td class="d">{entity_link("category", c.get("category_key"), cat_txt, depth)}</td>')
        cells.append(f'<td class="n d"{sort_key(bids)}>'
                     f'{bids if bids is not None else "—"}</td>')
        cells.append(f'<td{sort_key(DENSITY_RANK.get(c.get("competition_density")))}>'
                     f'{density_pill(c.get("competition_density"))}</td>')
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


# Category names that name no subject. Found on the live site 2026-09-03: a
# published category page called "#" holding 19 Fisheries and Oceans contracts,
# and pages called "NA" and "N/A". These are placeholders and export artifacts
# in the source data, not subjects a reader can browse.
#
# Stored letters-only and lowercase, so one entry covers "NA", "N/A", "n.a."
# and "N / A" without listing each spelling.
PLACEHOLDER_CATEGORY_NAMES = {
    "na", "nil", "none", "null", "nulle", "unknown", "unspecified",
    "notapplicable", "sansobjet", "tbd", "tobedetermined",
}


def is_placeholder_category(name: str) -> bool:
    """True when a category name carries no subject a reader could browse.

    Two rules, both written from faults that reached the live site:

    1. No letter anywhere. "#" is the case that prompted this. A name like that
       also slugs to an empty string, so it can only reach a URL through the
       fallback in slug() — which is how "#" became /category/x.html.
    2. A known placeholder word. "NA" and "N/A" are ordinary strings with
       letters in them, so rule 1 on its own leaves both of them published.

    Only the WHOLE name is tested, never a word inside it, so real categories
    such as "Other buildings" and "Rental - Other" are untouched. An empty name
    is NOT treated as a placeholder here: add_category_key already falls back to
    the commodity code for those rows, and that behaviour is left alone.
    """
    n = re.sub(r"\s+", " ", (name or "")).strip()
    if not n:
        return False
    if not any(ch.isalpha() for ch in n):
        return True
    return re.sub(r"[^a-z]", "", n.lower()) in PLACEHOLDER_CATEGORY_NAMES


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

    A name that is only a placeholder gets no key at all. group() skips a row
    with an empty key, so those contracts get no category page and no category
    link, while staying fully visible under their department and incumbent.
    """
    for r in rows:
        name = (r.get("category_name") or "").strip()
        if is_placeholder_category(name):
            r["category_key"] = ""
            continue
        r["category_key"] = re.sub(r"\s+", " ", name).lower() or (r.get("commodity_code") or "")


# Postal-code prefix -> province. The published field carries only the vendor's
# forward sortation area (three characters, e.g. "H4B"), and it is the address of
# the SUPPLIER. The dataset has no place-of-performance field at all, so this must
# never be labelled as where the work is. "X" spans both NT and NU and cannot be
# split, so it is reported as the pair rather than guessed.
PROVINCE_CODE = {"A": "NL", "B": "NS", "C": "PE", "E": "NB",
                 "G": "QC", "H": "QC", "J": "QC",
                 "K": "ON", "L": "ON", "M": "ON", "N": "ON", "P": "ON",
                 "R": "MB", "S": "SK", "T": "AB", "V": "BC",
                 "X": "NT-NU", "Y": "YT"}

PROVINCE_NAME = {"NL": "Newfoundland and Labrador", "NS": "Nova Scotia",
                 "PE": "Prince Edward Island", "NB": "New Brunswick",
                 "QC": "Quebec", "ON": "Ontario", "MB": "Manitoba",
                 "SK": "Saskatchewan", "AB": "Alberta", "BC": "British Columbia",
                 "NT-NU": "Northwest Territories and Nunavut", "YT": "Yukon"}


def add_province_key(rows: list[dict]) -> None:
    """Tag each contract with the supplier province, where one can be read.

    Contracts with no postal code, or a foreign one, get no key and are simply
    absent from the province pages. An "Unknown" province page would be a large
    page that tells the reader nothing, which is the thin content this build
    already works to avoid.
    """
    for r in rows:
        pc = (r.get("vendor_postal_code") or "").strip().upper()
        code = PROVINCE_CODE.get(pc[:1]) if pc else None
        r["province_key"] = code or ""
        r["province_name"] = PROVINCE_NAME.get(code or "", "")


# --- Withholding the names of individual people ------------------------------
# The source names sole proprietors: interpreters, translators and consultants
# contracting as themselves. Publishing a searchable page per person, with their
# contract values, amplifies personal information well beyond what the government
# already does. The contract itself stays in every total; only the name goes.

PERSON_LABEL = "Individual supplier (name withheld)"

# Any of these tokens in a name means it is an organisation, not a person.
# Generous on purpose: a missed suppression exposes someone, a false positive
# only withholds a company name that the tender reference still leads back to.
CORP_WORDS = frozenset("""
INC INCORPORATED LTD LTEE LIMITED LIMITEE LLP LLC LP ULC CORP CORPORATION CO COMPANY
COMPAGNIE SENC SENCRL SRL LDA ENR ENRG GMBH PLC AG SA SAS SARL NV BV PTY GROUP GROUPE
SERVICES SERVICE SOLUTIONS CONSULTING CONSULTANTS CONSULTANT TECHNOLOGIES TECHNOLOGY
SYSTEMS SYSTEMES ASSOCIATES ASSOCIES PARTNERS PARTNERSHIP HOLDINGS HOLDING ENTERPRISES
ENTREPRISES INDUSTRIES INTERNATIONAL UNIVERSITY UNIVERSITE COLLEGE INSTITUTE INSTITUT
SOCIETY SOCIETE ASSOCIATION FOUNDATION FONDATION TRUST BANK BANQUE SCHOOL ECOLE
HOSPITAL CENTRE CENTER AGENCY AGENCE CANADA CANADIAN NATIONAL NATIONALE CONSTRUCTION
ENGINEERING MARINE AVIATION LOGISTICS LOGISTIQUE MANAGEMENT MEDIA DESIGN STUDIO LABS
LABORATORY LABORATOIRES CLINIC CLINIQUE FARMS RANCH AUTO MOTORS EQUIPMENT SUPPLY
SUPPLIES PRODUCTS FOODS TRAVEL HOTEL RESORT PROPERTIES REALTY INSURANCE CAPITAL
VENTURES GLOBAL WORLDWIDE NETWORK NETWORKS DIGITAL SOFTWARE DATA SECURITY STAFFING
RECRUITMENT TRAINING ACADEMY PRESS PUBLISHING PRINTING TRANSPORT TRANSPORTATION
SHIPPING ENERGY POWER ELECTRIC ELECTRICAL MECHANICAL PLUMBING ROOFING LANDSCAPING
CLEANING MAINTENANCE REPAIR RENTAL LEASING IMPRIMERIE TRADUCTION TRANSLATION
INTERPRETATION DBA OPERATING GENERAL AND ET THE OF DES DU LA LE LES GOVERNMENT
GOUVERNEMENT MINISTRY MINISTERE BOARD COUNCIL CONSEIL COMMISSION OFFICE BUREAU
DEPARTMENT CITY VILLE TOWN MUNICIPALITY COUNTY REGION PROVINCE FIRST NATION NATIONS
BAND TRIBAL HEALTH SANTE LAW AVOCATS NOTAIRES ARCHITECTS ARCHITECTES SURVEYORS
ACCOUNTING ACCOUNTANTS CPA SONS BROS BROTHERS ENTERPRISE COOPERATIVE COOP
MUSEUM MUSEE GALLERY GALERIE THEATRE ORCHESTRA CHOIR LIBRARY BIBLIOTHEQUE
DEMENAGEMENT MOVING STORAGE ENTREPOSAGE WORKS WORKSHOP ATELIER COMPONENTS PARTS
INDUSTRIAL INDUSTRIEL READY MIX CONCRETE ASPHALT PAVING AGGREGATE QUARRY
TOOLS HARDWARE LUMBER TIMBER STEEL METALS WELDING FABRICATION MACHINE MACHINERY
FARM ORCHARD GREENHOUSE NURSERY GARDEN LANDSCAPE FORESTRY LOGGING
CATERING RESTAURANT CAFE BAKERY BREWERY DISTILLERY WINERY BISTRO CUISINE
PHARMACY PHARMACIE DENTAL OPTICAL VETERINARY THERAPY REHAB WELLNESS FITNESS
SALON SPA BARBER LAUNDRY JANITORIAL SANITATION DISPOSAL RECYCLING
TOWING GARAGE COLLISION TIRE TIRES FUEL PETROLEUM PETRO GAS PROPANE
SIGNS PRINTS GRAPHICS IMAGING PHOTO VIDEO FILMS PRODUCTIONS ENTERTAINMENT
SPORTS ATHLETIC RECREATION ARENA STADIUM CLUB LODGE CAMP OUTFITTERS ADVENTURE
AIRLINES AIRWAYS AIRPORT HELICOPTERS CHARTERS FREIGHT COURIER DELIVERY MOVERS
VENTURES EQUITY FUND FUNDS INVESTMENTS ADVISORY ADVISORS BROKERAGE
PLUS PRO EXPRESS DIRECT PRIME ELITE PREMIER SUPERIOR UNITED ALLIED ALLIANCE
NORTHERN SOUTHERN EASTERN WESTERN ATLANTIC PACIFIC COMMISSIONNAIRES
REGROUPEMENT CONCIERGERIE TOURISM AUTHORITY HTO AEC INDIGENOUS METIS INUIT
""".split())

# Common given names, used only for the weaker second rule below. Deliberately
# short: it exists to catch "GIVEN SURNAME" and "SURNAME GIVEN" forms that carry
# no comma, not to be a census.
GIVEN_NAMES = frozenset("""
james john robert michael william david richard joseph thomas charles christopher
daniel matthew anthony mark donald steven paul andrew joshua kenneth kevin brian
george timothy ronald jason edward jeffrey ryan jacob gary nicholas eric stephen
jonathan larry justin scott brandon benjamin samuel frank gregory raymond alexander
patrick jack dennis jerry tyler aaron jose adam nathan henry douglas peter zachary
kyle walter ethan jeremy harold keith christian roger noah gerald carl terry sean
austin arthur lawrence jesse dylan bryan joe jordan billy bruce albert willie gabriel
logan alan juan wayne roy ralph randy eugene vincent russell elmer louis philip
johnny mary patricia jennifer linda elizabeth barbara susan jessica sarah karen nancy
lisa margaret betty sandra ashley dorothy kimberly emily donna michelle carol amanda
melissa deborah stephanie rebecca laura sharon cynthia kathleen amy shirley angela
helen anna brenda pamela nicole ruth katherine samantha christine emma catherine
debra virginia rachel carolyn janet maria heather diane julie joyce victoria kelly
christina joan evelyn lauren judith megan cheryl andrea hannah martha jacqueline
frances gloria ann teresa kathryn sara janice jean alice madison doris abigail julia
judy grace denise amber marilyn danielle beverly charlotte natalie theresa diana
brittany kayla alexis lori marie jeanne pascale olivier pierre jacques michel andre
francois luc marc claude gilles yves serge alain sylvain martin nathalie sylvie
isabelle chantal manon lucie helene johanne josee guylaine genevieve veronique
stephane mathieu simon etienne benoit denis jean-pierre marie-claude
greg gregg tom tommy bob bobby dave davey mike mikey jim jimmy bill billy steve
rob robbie dan danny tony ed eddie ted teddy sam sammy ben benny nick nicky
chris matt andy joe joey pat ken kenny ron ronnie jamie rick ricky tim timmy
jack jake jeff josh kate katie kathy liz beth maggie meg molly sally sue tina
val vicky wendy cindy sandy jenny jess abby josie rosie cathy connie bonnie
kirsten kirsty anders lars nils bjorn erik erika ingrid soren jorgen
darcy declan cormac niamh siobhan aoife eamon padraig fergus rory brendan
armand bertrand francine ghislain gaetan raynald normand fernand adrien
lucien marcel gaston edouard hubert laurent thierry remi cedric fabrice andree
sable roxanne shauna sheena kendra kelsey brooke paige sierra jenna leah
miles milo malcolm murray magnus duncan angus lachlan ewan callum blair
priya priyanka raj rajesh rajiv ravi ramesh suresh sunil anil vijay vikram amit
sanjay deepak manish rahul arun ashok mohan krishna gopal hari shiv arjun karan
rohit nikhil ajay akash aditya siddharth neha pooja anjali kavita meera geeta
rekha shweta divya swati nisha ritu asha usha lata sunita anita seema veena
subarno subrata sourav sanjeev pankaj gaurav abhishek prashant vivek naveen
mohammed muhammad mohamed ahmed ahmad ali hassan hussein hussain omar khaled
khalid tariq yasser samir karim rashid mahmoud mustafa ibrahim ismail youssef
yusuf hamed hamid nasser fadi ziad bilal imad wael nabil adel hisham riad
ghaida fatima fatema aisha ayesha layla leila noor nour huda mona rania dina
yasmin yasmine amira hala samira nadia soraya farah rima maha lina zeinab asad
soheil fariha wei ming jian jun feng lei tao hui ying mei jing ling
seo hyun joon sung dong hae eun yong chul kyung soo woo chih
hiroshi takashi kenji yuki akira satoshi taro naoko yumiko keiko kazuo noriko
kwame kofi ama adwoa amara chidi ngozi olu ade tunde sipho thabo nkechi obi
emeka chinwe uche ifeoma bongani lerato mandla zanele chukwuemeka mawusi dumenu
vladimir dmitri dmitry sergei sergey ivan boris oleg nikolai alexei aleksei
mikhail yuri anton pavel andrei andrey natasha olga svetlana tatiana irina
ludmila katya anya galina nina larisa vera zoya pylypuk
garen aram vartan hagop sarkis armen ani lusine anahit tigran levon
anibal alberto jose carlos luis miguel rafael fernando ricardo eduardo
alejandro javier sergio diego pablo gonzalo mateo santiago rodrigo ignacio
isabella sofia camila valentina lucia elena rosa carmen pilar mercedes veronica
dimitri dimitrios nikos yannis kostas stavros eleni vasilis christos
minh thanh hoang tuan hung linh trang mai lan phuong quang duc binh
ewa agnieszka malgorzata krzysztof wojciech grzegorz jacek marek piotr tomasz
zoltan attila laszlo istvan gabor tibor csaba bela ferenc katalin erzsebet
johanna madeleine bernadette suzanne tanya marlene violette taffot
""".split())

# Names listed here are never suppressed. This is the correction channel for a
# real company that the rules below match by accident.
VENDOR_ALLOWLIST: set[str] = set()


def _name_tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^A-Za-z\u00C0-\u024F'\u2019-]+", name or "") if t]


def _norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def load_vendor_allowlist(path: str) -> set[str]:
    """One name per line, # starts a comment. Missing file is not an error."""
    out: set[str] = set()
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    out.add(_norm_name(line))
    return out


def is_individual(name: str) -> bool:
    """True when a published vendor name is a private person, not an organisation.

    Two rules, both measured against the full live vendor list before being used:

    1. "SURNAME, Given" with EXACTLY ONE comma and one or two tokens on each side.
       935 matches. The one-comma limit is what keeps multi-partner law firms out;
       without it, three surnames in a row read as a person.
    2. A two or three word name with no comma, containing a common given name.
       411 matches. Weaker, hence the allowlist.
    3. A LONGER name that opens with a given name and carries no corporate word
       anywhere. This is the sole trader who registered under their own name and
       then described the work: "SVETLANA DAMNJANOVIC PREDUZETNIK KONSULTANTSKE
       USLUGE", "DAVID LITTLE O/A PACIFICWIND POWERWASHING", "PIERRE JEAN OUELLET
       R PSYCH". Rule 2's three-token ceiling misses every one of them, and the
       trailing description is in whatever language the vendor registered in, so
       CORP_WORDS will never cover it.

       Measured against all 12,899 live vendor names: 32 additional matches, of
       which 31 are natural people. The single false positive is the accounting
       firm RAYMOND CHABOT GRANT THORNTON, which is in vendor_allowlist.txt.
       That is the trade suppress_individuals already describes — withholding a
       company name costs a reader one label, missing one exposes a person.

    A rule matching any 2-3 word name WITHOUT the given-name test was tried and
    rejected: it swept up 1,740 names including plain companies. Do not add it.
    Rule 3 is NOT that rule: it keeps the given-name test and only relaxes the
    length ceiling.
    """
    n = (name or "").strip()
    if not n or any(ch.isdigit() for ch in n):
        return False
    toks = _name_tokens(n)
    if not toks or any(t.upper() in CORP_WORDS for t in toks):
        return False
    if n.count(",") == 1:
        left, right = (p.strip() for p in n.split(","))
        lt, rt = _name_tokens(left), _name_tokens(right)
        if 1 <= len(lt) <= 2 and 1 <= len(rt) <= 2:
            return True
    if "," not in n and 2 <= len(toks) <= 3:
        if any(t.lower() in GIVEN_NAMES for t in toks):
            return True
    if len(toks) >= 4 and toks[0].lower() in GIVEN_NAMES:
        return True
    return False


def is_person_shaped(name: str) -> bool:
    """Looser than is_individual: any name that COULD belong to a person.

    Used for ONE thing — deciding whether a group may have a URL fragment.
    is_individual decides what is published and is deliberately conservative,
    because withholding a real company's name costs the reader something. An
    anchor costs the reader nothing: without one the row still appears, with
    its value and contract count, exactly as it did before anchors existed.

    So the trade here is the opposite way round. A fragment is a permanent,
    linkable, shareable pointer at one named party, which is what hard rule 8
    exists to prevent. Being wrong in this direction costs a company an anchor.
    Being wrong in the other direction puts a private person's name in a URL.

    No given-name test on purpose. That test is what lets non-Anglo names
    through, because no word list covers every given name on earth.
    """
    n = (name or "").strip()
    if not n or any(ch.isdigit() for ch in n):
        return False
    toks = _name_tokens(n)
    if not toks or any(t.upper() in CORP_WORDS for t in toks):
        return False
    if n.count(",") == 1:
        return True
    return 2 <= len(toks) <= 3


# --------------------------------------------------------------------------
# Scope text
#
# comments_en is the buyer's own note on what the contract is for. It is the
# single most useful field the source publishes and the site has never shown
# it: description_en, which is shown instead, is only the commodity category
# and is frequently "Other professional services not elsewhere specified".
#
# It is also the only free-text field on the site. A person typed it into a
# government form, so it can contain a name, and hard rule 8 does not care
# that the name arrived by an unusual route. Everything below exists to decide
# whether one string is safe to print.
# --------------------------------------------------------------------------

_SCOPE_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[A-Za-z]{2,}")
_SCOPE_INITIAL = re.compile(r"\b[A-Z]\.\s*[A-Z][a-z]")          # "M. Tremblay"
_SCOPE_CAPTOK = re.compile(r"\b[A-Z][A-Za-z']+\b")
_SCOPE_COMMA = re.compile(r"\b([A-Z][a-z']{1,})\s*,\s*([A-Z][a-z']{1,})\b")

# Place names keep "Ottawa, Ontario" and "Toronto, Ontario" from reading as
# "Surname, Given". Without this the comma rule alone withholds site-visit and
# office-fit-up scope text, which is a large slice of the construction records.
PLACE_WORDS = frozenset("""
ONTARIO QUEBEC ALBERTA MANITOBA SASKATCHEWAN NUNAVUT YUKON NOVA SCOTIA BRUNSWICK
NEWFOUNDLAND LABRADOR COLUMBIA EDWARD ISLAND TERRITORIES NORTHWEST CANADA
OTTAWA TORONTO MONTREAL VANCOUVER CALGARY EDMONTON WINNIPEG HALIFAX REGINA
SASKATOON VICTORIA GATINEAU HAMILTON LONDON KINGSTON WINDSOR MISSISSAUGA
BRAMPTON FREDERICTON CHARLOTTETOWN WHITEHORSE YELLOWKNIFE IQALUIT MONCTON
STREET AVENUE ROAD BOULEVARD NORTH SOUTH EAST WEST
""".split())


def scope_text(comment: Optional[str], limit: int = 180) -> Optional[str]:
    """The publishable scope note for a contract, or None to withhold it.

    Withholding is all-or-nothing by choice. Redacting the matched token was
    the alternative and it is worse twice over: a partial redaction that misses
    a second name still exposes someone, and it puts a mangled sentence on a
    public page. Dropping the whole string costs a reader one line of detail
    and cannot half-fail.

    The scan is deliberately not is_individual(). That function tests whether a
    WHOLE string is a person's name; this one hunts a name inside a sentence,
    which is a different problem — "Consulting services provided by John Smith"
    is not a person-shaped string but it does name a person.

    Measured against the 54 populated comments in the bundled fixture: zero
    withheld. Against hand-written positives covering Anglo, French, South
    Asian, Arabic and East Asian names in both orders, plus initials and an
    email address: all withheld.
    """
    t = " ".join((comment or "").split())
    if len(t) < 3:
        return None
    if _SCOPE_EMAIL.search(t) or _SCOPE_INITIAL.search(t):
        return None

    # "Nguyen, Thi" — surname-first, the form that carries no given name the
    # word list would recognise. This is the rule that protects the names no
    # list covers, which is most of them.
    for m in _SCOPE_COMMA.finditer(t):
        a, b = m.groups()
        if a.upper() in CORP_WORDS or b.upper() in CORP_WORDS:
            continue
        if a.upper() in PLACE_WORDS or b.upper() in PLACE_WORDS:
            continue
        return None

    # A known given name sitting next to another plain capitalised word.
    # Adjacency is what stops "Bill C-69", "May 2026" and "Grant and
    # Contribution Audit" from being withheld: the neighbour there is a
    # number, a code or a CORP_WORD, never a surname.
    toks = _SCOPE_CAPTOK.findall(t)
    for i, tok in enumerate(toks):
        if tok.lower() not in GIVEN_NAMES:
            continue
        for other in (toks[i + 1] if i + 1 < len(toks) else None,
                      toks[i - 1] if i else None):
            if not other or not other.isalpha() or len(other) < 2:
                continue
            if other.upper() in CORP_WORDS or other.upper() in PLACE_WORDS:
                continue
            return None

    return clip(t, limit)


def scope_stats(rows: list[dict]) -> tuple[int, int]:
    """(published, withheld) for the build log. Counts only — never a string.

    Same reason as suppress_individuals: the withheld text is the thing being
    protected, and this log is public on a public repo.
    """
    published = withheld = 0
    for r in rows:
        raw = r.get("comments_en")
        if not raw:
            continue
        if scope_text(raw):
            published += 1
        else:
            withheld += 1
    return published, withheld


def suppress_individuals(rows: list[dict]) -> int:
    """Withhold the names of vendors who are private people.

    The contract stays in EVERY total - department, category, province, value and
    bidder counts are all untouched. Only the displayed name changes. Blanking
    vendor_key is enough to stop a page or an index entry being made, because
    group() skips empty keys and entity_link() falls back to plain text. The
    published tender reference stays visible, so the public record is traceable.

    The list of suppressed names is deliberately NOT written to a file, an
    artifact or the build log. That list IS the personal information. Workflow
    artifacts and Actions logs on a public repo are readable by anyone, which is
    exactly how buyer_name leaked before. Only the count is reported.
    """
    n = 0
    for r in rows:
        name = r.get("vendor_name") or ""
        if _norm_name(name) in VENDOR_ALLOWLIST:
            continue
        if is_individual(name):
            r["vendor_name"] = PERSON_LABEL
            r["vendor_key"] = ""
            n += 1
    return n


# ------------------------------------------------------------------ build

def build(rows: list[dict], outdir: str, base_url: str = "") -> dict:
    # Set BEFORE any page is rendered. page() reads this global, so assigning it
    # later would emit canonical-less pages and no error.
    global BASE_URL
    BASE_URL = (base_url or "").rstrip("/")

    for sub in ("department", "incumbent", "category", "province"):
        os.makedirs(os.path.join(outdir, sub), exist_ok=True)

    live = [r for r in rows if r.get("days_to_expiry") is not None
            and r["days_to_expiry"] >= 0]   # NB: `or -1` would drop day-zero (0 is falsy)
    live.sort(key=lambda c: c.get("days_to_expiry") or 10**9)
    total_value = sum(r.get("contract_value") or 0 for r in live)

    add_category_key(live)
    add_province_key(live)
    suppress_individuals(live)

    # Dropdown options come from the data, so they match what people actually
    # bid on rather than a guessed list.
    global SIGNUP_CATEGORIES
    _cc = Counter(r.get("category_name") for r in live if r.get("category_name"))
    SIGNUP_CATEGORIES = [c for c, _ in _cc.most_common(14)]

    depts = group(live, "buyer_org")
    vendors = group(live, "vendor_key", "vendor_name")
    cats = group(live, "category_key", "category_name")
    provs = group(live, "province_key", "province_name")

    counts = {"0-6mo": 0, "6-12mo": 0, "12-24mo": 0, "24mo+": 0}
    for r in live:
        b = r.get("expiry_bucket")
        if b in counts:
            counts[b] += 1

    # Figures quoted in the signup copy are DERIVED, never hardcoded. Contracts
    # currently sitting in the 12-24mo bucket are exactly those that will cross
    # the 12-month planning threshold over the coming year, so that bucket is
    # both the annual flow and (÷52) the weekly rate. Hardcoding these would
    # quietly become a false claim on 2,000+ pages at the next data refresh.
    global SIGNUP_PER_WEEK, SIGNUP_YEAR_VALUE, SIGNUP_CROSSING_N
    _crossing = [r for r in live if r.get("expiry_bucket") == "12-24mo"]
    SIGNUP_CROSSING_N = len(_crossing)
    SIGNUP_PER_WEEK = round(SIGNUP_CROSSING_N / 52)
    SIGNUP_YEAR_VALUE = sum(r.get("contract_value") or 0 for r in _crossing)

    written = {"department": 0, "incumbent": 0, "category": 0, "province": 0}
    urls = ["index.html"]

    # key -> actual filename written. Links MUST be built from this, never
    # recomputed from the display name, or collision-renamed pages get orphaned.
    global FILENAMES, ANCHORS
    FILENAMES = {"department": {}, "incumbent": {}, "category": {}, "province": {}}
    ANCHORS = {"department": {}, "incumbent": {}, "category": {}, "province": {}}
    filenames = FILENAMES

    def small_pages(small: list) -> list[list]:
        """Sort and paginate the below-threshold groups.

        Called twice — once to assign anchor targets before any page is
        rendered, once when the index is actually written. The ordering lives
        here and nowhere else, because if the two callers ever disagreed every
        fragment link would silently point at the wrong page.
        """
        s = sorted(small, key=lambda kv: -kv[1]["value"])
        return [s[i:i + SMALL_PER_PAGE]
                for i in range(0, len(s), SMALL_PER_PAGE)] or [[]]

    def assign_anchors(small: list, folder: str) -> None:
        """Give every thin group a stable id and record where it can be found.

        Ids are slugged from the group key and de-duplicated within the folder;
        two keys can slug identically, and a repeated id would send half the
        links to the wrong row.
        """
        used: set[str] = set()
        for n, chunk in enumerate(small_pages(small), start=1):
            fn = "index.html" if n == 1 else f"index-{n}.html"
            for key, _g in chunk:
                # A name that could belong to a person gets no fragment. The row
                # is still listed; entity_link() falls back to plain text, which
                # is exactly how it behaved before anchors were introduced. See
                # is_person_shaped for why the test is looser than suppression.
                if is_person_shaped(_g.get("display", "")):
                    continue
                base = slug(key)
                anchor, i = base, 2
                while anchor in used:
                    anchor = f"{base}-{i}"
                    i += 1
                used.add(anchor)
                ANCHORS[folder][key] = f"{fn}#{anchor}"

    def assign_filenames(groups: dict[str, dict], folder: str) -> list[tuple[str, dict]]:
        """Decide every page's filename BEFORE anything is rendered.

        Pages cross-link (a department page lists incumbents and vice versa), so
        the complete map has to exist before the first page is written —
        otherwise whichever folder rendered first would emit plain text where a
        link belonged.
        """
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
        return listed

    def write_group_pages(groups: dict[str, dict], folder: str, label: str,
                          show: tuple[str, ...]) -> None:
        for key, g in groups.items():
            fn = filenames[folder].get(key)
            if not fn:                   # below the thin-content threshold
                continue
            path = os.path.join(outdir, folder, fn)

            soon = sum(1 for i in g["items"] if (i.get("days_to_expiry") or 999) <= 365)
            unc = sum(1 for i in g["items"] if i.get("competition_density") == "uncontested")
            title = f"{g['display']} — contracts expiring | {SITE}"
            # The entity name must lead the description. Without it, groups that
            # happen to share a count/value/soon triple produce byte-identical
            # descriptions — 474 of them across 2,092 pages in testing.
            # Singular counts are common here — 660 pages printed "1 federal
            # contracts", "1 expire within 12 months" and "1 were uncontested".
            # This string is the snippet Google prints under the result.
            desc = (f"{g['display']}: {count_noun(g['count'], 'federal contract')} "
                    f"worth {money(g['value'])} coming up for renewal. "
                    f"{soon:,} {'expires' if soon == 1 else 'expire'} within 12 months, "
                    f"{unc:,} {'was' if unc == 1 else 'were'} uncontested when "
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
                + ('<p class="sb">Province is read from the postal code of the '
                   'supplier in the published record, so this is where the incumbent '
                   'is based, not where the work is performed. The published data '
                   'carries no place-of-performance field at all. Contracts with no '
                   'postal code, or a foreign one, appear on no province page.</p>'
                   if folder == "province" else "")
                + "<h2>Contracts by expiry</h2>"
                + contract_table(g["items"], show=show, limit=400, depth=1))
            open(path, "w", encoding="utf-8").write(page(title, desc, body, 1, f"{folder}/{fn}"))
            urls.append(f"{folder}/{fn}")
            written[folder] += 1

    # PASS 1 — every filename decided before a single page is rendered.
    small_d = assign_filenames(depts, "department")
    small_v = assign_filenames(vendors, "incumbent")
    small_c = assign_filenames(cats, "category")
    small_p = assign_filenames(provs, "province")

    # Anchors are assigned in pass 1 for the same reason filenames are: pages
    # cross-link, so every destination has to be known before the first page is
    # rendered.
    assign_anchors(small_d, "department")
    assign_anchors(small_v, "incumbent")
    assign_anchors(small_c, "category")
    assign_anchors(small_p, "province")

    # PASS 2 — render, now that cross-folder links can all be resolved.
    write_group_pages(depts, "department", "Department", ("cat",))
    write_group_pages(vendors, "incumbent", "Incumbent", ("dept", "cat"))
    write_group_pages(cats, "category", "Category", ("dept",))
    write_group_pages(provs, "province", "Supplier province", ("dept", "cat"))

    # ---- index pages (keeps small groups crawlable and internally linked)
    def write_index(groups: dict[str, dict], small: list, folder: str, label: str) -> None:
        """Index page(s). Every group appears somewhere — groups below the
        thin-content threshold don't get their own page, but they must still be
        listed and reachable, or the claim that they stay crawlable is false.
        The overflow list is paginated rather than truncated."""
        big = sorted([(k, g) for k, g in groups.items() if substantial(g)],
                     key=lambda kv: -kv[1]["value"])
        links = "".join(
            f'<li><a href="{filenames[folder][k]}">{esc(clip(g["display"], 52))}</a> '
            f'<span class="d">{money(g["value"])} · {g["count"]}</span></li>'
            for k, g in big if k in filenames[folder])

        chunks = small_pages(small)
        total_pages = len(chunks)

        for n, chunk in enumerate(chunks, start=1):
            fn = "index.html" if n == 1 else f"index-{n}.html"
            # The id has to come from ANCHORS, not be recomputed here: the
            # de-duplication happened once, during assignment, and recomputing
            # it would risk drifting from the links already written.
            rest = "".join(
                (f'<li class="d" id="{esc(ANCHORS[folder][k].split("#")[-1])}">'
                 if k in ANCHORS[folder] else '<li class="d">')
                + f'{esc(clip(g["display"], 52))} — {money(g["value"])} · {g["count"]}</li>'
                for k, g in chunk)
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
            # plural(), not label + "s". "Category" is the label that breaks the
            # naive form, and it broke in all five places at once.
            many = plural(label.lower())
            body = head + (
                f"<h2>All {many} with contracts expiring</h2>"
                + stat_cards([(f"{len(groups):,}", f"Total {many}"),
                              (f"{len(big):,}", "With detail pages")], highlight=1)
                + f"<ul>{links}</ul>" if n == 1 else
                f"<h2>Smaller {many} — page {n}</h2>")
            if rest:
                body += (f"<h2>Smaller {many}</h2>" if n == 1 else "")
                body += f"<ul class='cols3'>{rest}</ul>"
            body += nav

            suffix = "" if n == 1 else f" (page {n})"
            open(os.path.join(outdir, folder, fn), "w", encoding="utf-8").write(
                page(f"All {many}{suffix} | {SITE}",
                     f"Every federal {label.lower()} with contracts coming up for "
                     f"renewal, ranked by total value{suffix}. "
                     f"{len(groups):,} in total, "
                     f"{len(big):,} with detail pages.", body, 1, f"{folder}/{fn}"))
            urls.append(f"{folder}/{fn}")

    write_index(depts, small_d, "department", "Department")
    write_index(vendors, small_v, "incumbent", "Incumbent")
    write_index(cats, small_c, "category", "Category")
    write_index(provs, small_p, "province", "Supplier province")

    # ---- landing page
    def toplist(groups: dict[str, dict], folder: str, n: int = 12) -> str:
        top = sorted([(k, g) for k, g in groups.items()
                      if substantial(g) and k in filenames[folder]],
                     key=lambda kv: -kv[1]["value"])[:n]
        return "".join(
            f'<li><a href="{folder}/{filenames[folder][k]}">{esc(clip(g["display"], 44))}</a>'
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

    # Split the bid count by whether the award was actually competed. Derived, never
    # hardcoded, for the same reason the signup figures are.
    _bidded = [r for r in live if r.get("number_of_bids") is not None]
    comp_rows = [r for r in _bidded if r.get("solicitation_procedure") in COMPETITIVE_PROCEDURES]
    comp_total = len(comp_rows)
    comp_uncontested = sum(1 for r in comp_rows if (r.get("number_of_bids") or 0) <= 1)
    noncomp_total = sum(1 for r in _bidded
                        if r.get("solicitation_procedure") in NONCOMPETITIVE_PROCEDURES)

    # Browse controls used to sit at the foot of this page. Two users asked for
    # filtering that already existed because they never scrolled that far, so the
    # grid is now built here and placed above the 60-row table.
    browse_grid = (
        '<div class="g">'
        + f'<div><h2>By department</h2><ul>{toplist(depts,"department")}</ul>'
          f'<p><a href="department/index.html">All departments →</a></p></div>'
        + f'<div><h2>By incumbent</h2><ul>{toplist(vendors,"incumbent")}</ul>'
          f'<p><a href="incumbent/index.html">All incumbents →</a></p></div>'
        + f'<div><h2>By category</h2><ul>{toplist(cats,"category")}</ul>'
          f'<p><a href="category/index.html">All categories →</a></p></div>'
        + "</div>")

    # Every figure in this note is computed from the dataset on this page. It
    # previously cited a 70-80% incumbent win rate taken from a US vendor's
    # marketing, a foreign statistic, unattributed, on a site whose whole value is
    # accuracy. Replaced with facts we can defend from our own data.
    #
    # It sits BELOW the table rather than in front of it. Two people on X said
    # independently that the page buried its own data under explanation, and they
    # were right: 840 words stood between the top of the page and the first row.
    stats_note = (
        f'<p>Of the {comp_total:,} contracts here that were openly competed and '
        f'report a bidder count, <strong>{comp_uncontested:,} '
        f'({comp_uncontested/max(comp_total,1)*100:.0f}%) drew one bid or none</strong> '
        f'when last awarded. A further {noncomp_total:,} were never competed at all, '
        f'being non-competitive awards or advance contract award notices, so those '
        f'are counted separately. The median contract is '
        f'{money(median_value)}; the largest {min(100, len(live))} account for '
        f'{top100_share:.0f}% of total value.</p>')

    body = (
        stat_cards([(f"{len(live):,}", "Live contracts"), (money(total_value), "Pipeline value")]
                   + [(f"{counts[b]:,}", b) for b in ("0-6mo", "6-12mo", "12-24mo", "24mo+")]
                   + [(money(median_value), "Median contract")])
        # The data first. One line of orientation, then the rows.
        + "<h2>Expiring soonest</h2>"
        + '<p class="sb">Agencies typically begin recompete planning 12&#8211;18 months '
          'before a contract ends.</p>'
        + contract_table(live, limit=60)
        + "<h2>Browse by department, incumbent or category</h2>"
        + browse_grid
        + "<h2>By supplier province</h2>"
        + '<p class="sb">Where the incumbent is based, read from the postal code in '
          'the published record. Not where the work is performed.</p>'
        + f'<ul class="cols3">{toplist(provs, "province", 12)}</ul>')
    open(os.path.join(outdir, "index.html"), "w", encoding="utf-8").write(
        page(f"{SITE} — federal contracts up for renewal",
             f"{len(live):,} Canadian federal services contracts worth {money(total_value)} "
             f"are coming up for renewal. See the incumbent, value, expiry date and how "
             f"contested each was.", body, 0, "index.html", stats_note))

    # ---- sitemap + robots
    # The sitemap protocol REQUIRES fully-qualified URLs. Relative paths are
    # rejected outright by Search Console, which would silently kill the entire
    # indexing strategy this site depends on.
    base = (base_url or "").rstrip("/")
    today = date.today().isoformat()
    sm = ("<?xml version='1.0' encoding='UTF-8'?>\n"
          "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>\n"
          + "".join(f"  <url><loc>{base}/{declared_path(u)}</loc><lastmod>{today}</lastmod></url>\n"
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
    """Check every INTERNAL link resolves to a file that exists.

    Absolute URLs are skipped. The canonical tag emits an absolute href ending
    in .html, and treating that as a relative path reports every page as
    linking to a broken file. The check has always been about internal links;
    before the canonical tag existed there simply were no absolute ones to
    exclude.
    """
    problems = []
    for root, _dirs, files in os.walk(outdir):
        for f in files:
            if not f.endswith(".html"):
                continue
            p = os.path.join(root, f)
            src = open(p, encoding="utf-8").read()
            for href in re.findall(r'href="(?!https?://)([^"#]+\.html)"', src):
                target = os.path.normpath(os.path.join(root, href))
                if not os.path.exists(target):
                    problems.append(f"{os.path.relpath(p,outdir)} -> {href}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", default="site")
    ap.add_argument("--signup-action", default="",
                    help="Kit form POST endpoint for email capture, e.g.\n"
                         "https://app.kit.com/forms/1234567/subscriptions\n"
                         "Omitted = no form rendered, because a form posting nowhere\n"
                         "silently loses signups.")
    ap.add_argument("--business-name", default="",
                    help="CASL: the name you carry on business under. Required\n"
                         "whenever --signup-action is set.")
    ap.add_argument("--mailing-address", default="",
                    help="CASL: a current mailing address, valid 60+ days.\n"
                         "Required whenever --signup-action is set.")
    ap.add_argument("--contact-url", default="",
                    help="CASL: a contact web address. Defaults to --base-url.")
    ap.add_argument("--vendor-allowlist", default="vendor_allowlist.txt",
                    help="File of vendor names that must never be treated as\n"
                         "individual people. One per line, # starts a comment.\n"
                         "Missing file is fine.")
    ap.add_argument("--google-verification", default="",
                    help="Google Search Console token (the content=\"...\" value\n"
                         "only, not the whole meta tag).")
    ap.add_argument("--base-url", default="",
                    help="full site URL, e.g. https://example.com — required for a\n                          valid sitemap; relative paths are rejected by Search Console")
    args = ap.parse_args()

    global SIGNUP_ACTION
    # The form's field names (email_address, fields[category]) are Kit's schema.
    # Kit accepts a POST with unrecognised keys and returns success while storing
    # nothing — so pointing this at a non-Kit endpoint would produce a form that
    # looks like it works and quietly discards every signup. Refuse instead.
    if args.signup_action:
        u = urllib.parse.urlparse(args.signup_action)
        ok_host = u.netloc in ("app.kit.com", "app.convertkit.com")
        ok_path = re.fullmatch(r"/forms/\d+/subscriptions/?", u.path or "")
        if u.scheme != "https" or not ok_host or not ok_path:
            print(f"ERROR: --signup-action is not a Kit form endpoint:\n"
                  f"  got      {args.signup_action}\n"
                  f"  expected https://app.kit.com/forms/<numeric id>/subscriptions\n"
                  f"The form posts Kit-specific field names. Another provider would\n"
                  f"accept the request and silently drop the data.", file=sys.stderr)
            return 2

    # CASL: a consent request missing the identification block is itself the
    # violation. Refuse to render one rather than publish it on 2,000+ pages.
    global SIGNUP_BUSINESS, SIGNUP_ADDRESS, SIGNUP_CONTACT
    if args.signup_action:
        missing = [n for n, v in (("--business-name", args.business_name),
                                  ("--mailing-address", args.mailing_address))
                   if not v.strip()]
        if missing:
            print(f"ERROR: --signup-action is set but {' and '.join(missing)} "
                  f"{'is' if len(missing)==1 else 'are'} missing.\n"
                  f"CASL requires a request for consent to identify the sender "
                  f"and give a mailing address.\nRefusing to publish a signup "
                  f"form without them.", file=sys.stderr)
            return 3
        SIGNUP_BUSINESS = args.business_name.strip()
        SIGNUP_ADDRESS = args.mailing_address.strip()
        SIGNUP_CONTACT = (args.contact_url or args.base_url).strip()
        if not SIGNUP_CONTACT:
            print("ERROR: --contact-url or --base-url required for the CASL "
                  "contact method.", file=sys.stderr)
            return 3
    SIGNUP_ACTION = args.signup_action

    # Google's UI hands you a whole <meta ...> tag, so pasting the tag rather
    # than the bare token is the obvious mistake. Nesting a tag inside an
    # attribute would produce broken HTML and silent verification failure, so
    # recover the token if we can and refuse if we cannot.
    global GOOGLE_VERIFICATION
    gv = (args.google_verification or "").strip()
    if gv:
        if "<" in gv or "meta" in gv.lower():
            m = re.search(r'content=["\']([^"\']+)["\']', gv)
            if not m:
                print("ERROR: --google-verification looks like a meta tag but no "
                      "content=\"...\" value could be read from it.\n"
                      "Pass only the token, e.g. --google-verification abc123",
                      file=sys.stderr)
                return 4
            gv = m.group(1).strip()
            print(f"note: extracted verification token from the pasted meta tag")
        if not re.fullmatch(r"[A-Za-z0-9_\-]{20,100}", gv):
            print(f"ERROR: --google-verification does not look like a Google token: {gv!r}",
                  file=sys.stderr)
            return 4
        GOOGLE_VERIFICATION = gv

    global VENDOR_ALLOWLIST
    VENDOR_ALLOWLIST = load_vendor_allowlist(args.vendor_allowlist)

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
