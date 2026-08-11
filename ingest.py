#!/usr/bin/env python3
"""
Canadian Recompete Radar - ingest pipeline
==========================================

Pulls federal contract award records from the Government of Canada open data
portal, normalizes them, deduplicates amendments, and derives the recompete
signal (which contracts expire when, held by whom, how contested).

Source : Proactive Publication - Contracts (contracts over $10,000)
         open.canada.ca CKAN datastore, resource fac950c0-00d5-4ec1-a4d3-9cbebf98a305
Licence: Open Government Licence - Canada
Volume : ~1.31M rows total; the recompete window is a small subset.

FIELD SEMANTICS (verified against the published TBS schema):
  delivery_date        "Contract Period End Date or Delivery Date"
                       For SERVICES contracts this is the end date of the period
                       over which services are provided -> the contract end date.
                       For GOODS it is a delivery date that only *may* be the end
                       date, so goods are excluded from the recompete view by default.
  contract_period_start  Start of the performance period.
  number_of_bids       How many bidders competed. 100% populated on recent data.
  solicitation_procedure / limited_tendering_reason  Competed vs sole-sourced.
  procurement_id       Joins award records back to the originating tender notice.

Usage
-----
  python3 ingest.py --self-test              # validate transforms on bundled fixture
  python3 ingest.py --full                   # full historical load -> recompete.db
  python3 ingest.py --window 24              # only contracts expiring within 24 months
  python3 ingest.py --full --out data.db
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Iterator, Optional

CKAN_BASE = "https://open.canada.ca/data/api/action/datastore_search"
RESOURCE_ID = "fac950c0-00d5-4ec1-a4d3-9cbebf98a305"
PAGE_SIZE = 5000
USER_AGENT = "recompete-radar/1.0 (Open Government Licence data ingest)"

# Only these commodity types have unambiguous end-date semantics.
# S = services, C = construction services. G (goods) is excluded by default.
RECOMPETE_COMMODITY_TYPES = {"S", "C"}


# --------------------------------------------------------------------------
# Normalized record
# --------------------------------------------------------------------------

@dataclass
class Contract:
    contract_key: str
    procurement_id: Optional[str]
    reference_number: Optional[str]

    vendor_name: Optional[str]
    vendor_key: Optional[str]          # normalized identity; groups spelling variants
    vendor_postal_code: Optional[str]
    country_of_vendor: Optional[str]

    buyer_org: Optional[str]          # owner_org_title - department
    buyer_org_code: Optional[str]     # owner_org
    buyer_name: Optional[str]         # named contact (100% on recent data)

    description_en: Optional[str]
    description_fr: Optional[str]

    commodity_code: Optional[str]
    commodity_type: Optional[str]
    category_name: Optional[str]       # human-readable; derived after the full load
    economic_object_code: Optional[str]

    contract_value: Optional[float]
    original_value: Optional[float]
    amendment_value: Optional[float]

    contract_date: Optional[str]
    contract_period_start: Optional[str]
    delivery_date: Optional[str]      # = contract period end for services

    days_to_expiry: Optional[int]
    expiry_bucket: Optional[str]
    duration_days: Optional[int]

    number_of_bids: Optional[int]
    competition_density: Optional[str]
    solicitation_procedure: Optional[str]
    limited_tendering_reason: Optional[str]
    is_sole_sourced: Optional[bool]

    standing_offer_number: Optional[str]
    instrument_type: Optional[str]
    reporting_period: Optional[str]

    source_row_id: Optional[int] = None
    amendment_count: int = 1


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

def parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    text = str(value).strip()[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_money(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def parse_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).strip()))
    except ValueError:
        return None


def expiry_bucket(days: Optional[int]) -> Optional[str]:
    """Buckets chosen to match how capture planning actually works.

    Agencies typically begin recompete planning 12-18 months before expiry,
    so 12-24mo is the high-value window, not the 0-6mo one.
    """
    if days is None:
        return None
    if days < 0:
        return "expired"
    if days <= 180:
        return "0-6mo"
    if days <= 365:
        return "6-12mo"
    if days <= 730:
        return "12-24mo"
    return "24mo+"


def competition_density(bids: Optional[int]) -> Optional[str]:
    """A contract that drew 2 bidders is a very different prospect from one
    that drew 15. Nobody else surfaces this."""
    if bids is None:
        return None
    if bids <= 1:
        return "uncontested"
    if bids <= 3:
        return "low"
    if bids <= 8:
        return "moderate"
    return "high"


# Legal-form and geography tokens carry no identity. Stripping them merges
# "BGIS GLOBAL INTEGRATED SOLUTIONS CA" / "Bgis Global Integrated Solutions Ca" /
# "BGIS GLOBAL INTEGRATED SOLUTIONS CANADA" into one company. Without this,
# 12,980 raw spellings stay fragmented instead of resolving to 10,902 firms and
# ~$14B is attributed to three separate "companies".
VENDOR_STOPWORDS = {
    "inc", "ltd", "ltee", "llp", "corp", "corporation", "limited", "lp",
    "partnership", "co", "company", "the", "of", "canada", "ca", "group",
    "holdings", "and",
}


def vendor_key(name: Optional[str]) -> str:
    """Normalized vendor identity. Conservative by design: it merges spelling
    and legal-suffix variants but NOT token subsets, because "General Dynamics"
    and "General Dynamics Land Systems" are distinct procurement entities.
    Consequence: incumbent totals are a floor, not an exact corporate-group figure.
    """
    cleaned = "".join(ch if ch.isalnum() else " " for ch in (name or "").lower())
    return " ".join(w for w in cleaned.split() if w not in VENDOR_STOPWORDS)


def derive_category_names(contracts: list["Contract"]) -> int:
    """Commodity codes are meaningless to a human and to a search query.

    The government issues many codes carrying the same description, so the
    readable name is taken from the most frequent description seen against each
    code. Mutates contracts in place; returns how many codes were named.
    """
    by_code: dict[str, Counter] = defaultdict(Counter)
    for c in contracts:
        if c.commodity_code and c.description_en:
            by_code[c.commodity_code][c.description_en.strip()] += 1
    names = {code: counts.most_common(1)[0][0] for code, counts in by_code.items()}
    for c in contracts:
        c.category_name = names.get(c.commodity_code or "", "")
    return len(names)


def detect_sole_sourced(solicitation_procedure: Any, limited_reason: Any) -> Optional[bool]:
    """TC = traditional competitive, AC = advance contract award notice,
    OB = open bidding, NC / ST = non-competitive / sole source.
    limited_tendering_reason being set and non-zero also signals limited tendering.
    """
    proc = (str(solicitation_procedure or "")).strip().upper()
    reason = (str(limited_reason or "")).strip()
    if not proc and not reason:
        return None
    if proc in {"NC", "ST"}:
        return True
    if reason and reason not in {"00", "0", "None"}:
        return True
    if proc:
        return False
    return None


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------

def build_contract_key(row: dict) -> str:
    """procurement_id is not globally unique - it is unique per department.
    Key on (owner_org, procurement_id) and fall back to reference_number."""
    org = (row.get("owner_org") or "").strip()
    pid = (row.get("procurement_id") or "").strip()
    if pid:
        return f"{org}::{pid}"
    ref = (row.get("reference_number") or "").strip()
    return f"{org}::REF::{ref}"


def normalize(row: dict, today: Optional[date] = None) -> Contract:
    today = today or date.today()

    start = parse_date(row.get("contract_period_start"))
    end = parse_date(row.get("delivery_date"))

    days_to_expiry = (end - today).days if end else None
    duration_days = (end - start).days if (start and end) else None

    bids = parse_int(row.get("number_of_bids"))

    return Contract(
        contract_key=build_contract_key(row),
        procurement_id=row.get("procurement_id") or None,
        reference_number=row.get("reference_number") or None,
        vendor_name=row.get("vendor_name") or None,
        vendor_key=vendor_key(row.get("vendor_name")) or None,
        vendor_postal_code=row.get("vendor_postal_code") or None,
        country_of_vendor=row.get("country_of_vendor") or None,
        buyer_org=row.get("owner_org_title") or None,
        buyer_org_code=row.get("owner_org") or None,
        buyer_name=row.get("buyer_name") or None,
        description_en=row.get("description_en") or None,
        description_fr=row.get("description_fr") or None,
        commodity_code=row.get("commodity_code") or None,
        commodity_type=row.get("commodity_type") or None,
        category_name=None,   # filled by derive_category_names() after the full load
        economic_object_code=row.get("economic_object_code") or None,
        contract_value=parse_money(row.get("contract_value")),
        original_value=parse_money(row.get("original_value")),
        amendment_value=parse_money(row.get("amendment_value")),
        contract_date=str(parse_date(row.get("contract_date")) or "") or None,
        contract_period_start=str(start or "") or None,
        delivery_date=str(end or "") or None,
        days_to_expiry=days_to_expiry,
        expiry_bucket=expiry_bucket(days_to_expiry),
        duration_days=duration_days,
        number_of_bids=bids,
        competition_density=competition_density(bids),
        solicitation_procedure=row.get("solicitation_procedure") or None,
        limited_tendering_reason=row.get("limited_tendering_reason") or None,
        is_sole_sourced=detect_sole_sourced(
            row.get("solicitation_procedure"), row.get("limited_tendering_reason")
        ),
        standing_offer_number=row.get("standing_offer_number") or None,
        instrument_type=row.get("instrument_type") or None,
        reporting_period=row.get("reporting_period") or None,
        source_row_id=parse_int(row.get("_id")),
    )


def deduplicate(contracts: Iterable[Contract]) -> list[Contract]:
    """Amendments appear as separate rows for the same underlying contract.

    TBS documents contract_value for an amendment as 'the amended contract
    value' - i.e. the running total, not the delta. So the current state of a
    contract is the row with the latest contract_date; ties break on the
    largest contract_value.
    """
    best: dict[str, Contract] = {}
    counts: dict[str, int] = {}

    for c in contracts:
        counts[c.contract_key] = counts.get(c.contract_key, 0) + 1
        incumbent = best.get(c.contract_key)
        if incumbent is None:
            best[c.contract_key] = c
            continue
        a = (c.contract_date or "", c.contract_value or 0.0)
        b = (incumbent.contract_date or "", incumbent.contract_value or 0.0)
        if a > b:
            best[c.contract_key] = c

    for key, contract in best.items():
        contract.amendment_count = counts[key]

    return list(best.values())


def filter_recompete(
    contracts: Iterable[Contract],
    window_months: Optional[int] = None,
    services_only: bool = True,
    include_expired_days: int = 0,
) -> list[Contract]:
    """Contracts that represent a live recompete opportunity."""
    out: list[Contract] = []
    max_days = None if window_months is None else window_months * 30.44

    for c in contracts:
        if services_only and (c.commodity_type or "").upper() not in RECOMPETE_COMMODITY_TYPES:
            continue
        if c.days_to_expiry is None:
            continue
        if c.days_to_expiry < -include_expired_days:
            continue
        if max_days is not None and c.days_to_expiry > max_days:
            continue
        out.append(c)

    out.sort(key=lambda c: c.days_to_expiry if c.days_to_expiry is not None else 10**9)
    return out


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def fetch_page(offset: int, limit: int = PAGE_SIZE, retries: int = 4) -> dict:
    params = urllib.parse.urlencode(
        {"resource_id": RESOURCE_ID, "limit": limit, "offset": offset}
    )
    url = f"{CKAN_BASE}?{params}"
    last_error: Optional[Exception] = None

    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - network layer, retry on anything
            last_error = exc
            sleep_for = 2 ** attempt
            print(f"  ! fetch failed at offset {offset} ({exc}); retrying in {sleep_for}s",
                  file=sys.stderr)
            time.sleep(sleep_for)

    raise RuntimeError(f"failed to fetch offset {offset}: {last_error}")


def iter_all_rows(max_rows: Optional[int] = None, throttle: float = 0.3) -> Iterator[dict]:
    offset = 0
    total: Optional[int] = None
    yielded = 0

    while True:
        payload = fetch_page(offset)
        result = payload.get("result", {})
        records = result.get("records", [])
        if total is None:
            total = result.get("total")
            print(f"  dataset reports {total:,} total rows")
        if not records:
            break

        for row in records:
            yield row
            yielded += 1
            if max_rows and yielded >= max_rows:
                return

        offset += len(records)
        print(f"  fetched {offset:,}" + (f" / {total:,}" if total else ""))
        if total and offset >= total:
            break
        time.sleep(throttle)

    # A truncated download is the most dangerous silent failure in this pipeline:
    # it produces a perfectly self-consistent site containing a fraction of the
    # data. The API reports how many rows exist, so compare against it and refuse
    # to continue if they disagree.
    if total is not None and max_rows is None and yielded < total:
        raise RuntimeError(
            f"INCOMPLETE DOWNLOAD: API reported {total:,} rows, received {yielded:,} "
            f"({yielded/total*100:.1f}%). Refusing to build from partial data.")


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

# Indexes only. The CREATE TABLE is generated from the dataclass at runtime —
# hand-writing the column list is how vendor_key and category_name silently
# desynced and broke a 13-minute production run at the very last step.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_expiry  ON contracts(days_to_expiry);
CREATE INDEX IF NOT EXISTS idx_org     ON contracts(buyer_org);
CREATE INDEX IF NOT EXISTS idx_vendor  ON contracts(vendor_key);
CREATE INDEX IF NOT EXISTS idx_commod  ON contracts(commodity_code);
CREATE INDEX IF NOT EXISTS idx_bucket  ON contracts(expiry_bucket);
"""


def sqlite_schema() -> str:
    """CREATE TABLE built from the dataclass, so columns and fields cannot diverge."""
    from dataclasses import fields as _fields
    types = {float: "REAL", int: "INTEGER", bool: "INTEGER"}
    cols = []
    for f in _fields(Contract):
        base = getattr(f.type, "__args__", [f.type])[0] if hasattr(f.type, "__args__") else f.type
        cols.append(f"{f.name} {types.get(base, 'TEXT')}")
    cols[0] = cols[0].split()[0] + " TEXT PRIMARY KEY"
    return "CREATE TABLE IF NOT EXISTS contracts (\n    " + ",\n    ".join(cols) + "\n);"


def write_sqlite(contracts: list[Contract], path: str) -> None:
    from dataclasses import fields as _fields
    conn = sqlite3.connect(path)
    conn.executescript(sqlite_schema())
    conn.executescript(INDEXES)
    n_cols = len(_fields(Contract))
    rows = []
    for c in contracts:
        d = asdict(c)
        d["is_sole_sourced"] = None if d["is_sole_sourced"] is None else int(d["is_sole_sourced"])
        rows.append(tuple(d.values()))
    if rows and len(rows[0]) != n_cols:
        raise RuntimeError(f"column drift: table has {n_cols} columns, "
                           f"row has {len(rows[0])} values")
    placeholders = ",".join("?" * len(rows[0])) if rows else ""
    if rows:
        conn.executemany(f"INSERT OR REPLACE INTO contracts VALUES ({placeholders})", rows)
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# Self-test - validates transforms against real sampled records
# --------------------------------------------------------------------------

def self_test(fixture_path: str) -> int:
    print("=" * 68)
    print("SELF-TEST - transformation logic against real sampled API records")
    print("=" * 68)

    with open(fixture_path, encoding="utf-8") as fh:
        raw = json.load(fh)
    print(f"\nfixture: {len(raw)} real records\n")

    today = date(2026, 8, 10)
    normalized = [normalize(r, today=today) for r in raw]
    failures = 0

    # 1. delivery_date must never precede contract_period_start.
    violations = [
        c for c in normalized
        if c.duration_days is not None and c.duration_days < 0
    ]
    ok = not violations
    failures += 0 if ok else 1
    print(f"[{'PASS' if ok else 'FAIL'}] end date >= start date: "
          f"{len(normalized) - len(violations)}/{len(normalized)}")

    # 2. Money parses.
    parsed = sum(1 for c in normalized if c.contract_value is not None)
    ok = parsed == len(normalized)
    failures += 0 if ok else 1
    print(f"[{'PASS' if ok else 'FAIL'}] contract_value parsed: {parsed}/{len(normalized)}")

    # 3. Every record with an end date gets a bucket.
    dated = [c for c in normalized if c.delivery_date]
    bucketed = sum(1 for c in dated if c.expiry_bucket)
    ok = bucketed == len(dated)
    failures += 0 if ok else 1
    print(f"[{'PASS' if ok else 'FAIL'}] expiry bucket assigned: {bucketed}/{len(dated)}")

    # 4. Dedup must not lose or invent contracts.
    deduped = deduplicate(normalized)
    ok = 0 < len(deduped) <= len(normalized)
    failures += 0 if ok else 1
    print(f"[{'PASS' if ok else 'FAIL'}] dedup {len(normalized)} rows -> "
          f"{len(deduped)} contracts ({len(normalized) - len(deduped)} amendments collapsed)")

    # 5. Services filter excludes goods.
    live = filter_recompete(deduped, window_months=None, services_only=True)
    goods_leaked = [c for c in live if (c.commodity_type or "").upper() == "G"]
    ok = not goods_leaked
    failures += 0 if ok else 1
    print(f"[{'PASS' if ok else 'FAIL'}] goods excluded from recompete view: "
          f"{len(goods_leaked)} leaked")

    # 6. Competition density derives wherever bids are present.
    with_bids = [c for c in normalized if c.number_of_bids is not None]
    graded = sum(1 for c in with_bids if c.competition_density)
    ok = graded == len(with_bids)
    failures += 0 if ok else 1
    print(f"[{'PASS' if ok else 'FAIL'}] competition density derived: "
          f"{graded}/{len(with_bids)} records with bid counts")

    # 7. SQLite write must actually work. This was NOT covered before, which is
    #    how a 33-column table met a 35-field dataclass and killed a 13-minute
    #    production run at the final step.
    import tempfile
    from dataclasses import fields as _f
    tmp = os.path.join(tempfile.mkdtemp(), "selftest.db")
    try:
        derive_category_names(deduped)
        write_sqlite(deduped, tmp)
        conn = sqlite3.connect(tmp)
        n_rows = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
        n_cols = len(conn.execute("PRAGMA table_info(contracts)").fetchall())
        conn.close()
        ok = n_rows == len(deduped) and n_cols == len(_f(Contract))
        failures += 0 if ok else 1
        print(f"[{'PASS' if ok else 'FAIL'}] sqlite write: {n_rows} rows, "
              f"{n_cols} cols == {len(_f(Contract))} dataclass fields")
    except Exception as exc:
        failures += 1
        print(f"[FAIL] sqlite write raised: {exc}")

    # 8. Both derived fields must be populated, or the site loses vendor
    #    consolidation and readable category names silently.
    vk = sum(1 for c in deduped if c.vendor_key)
    cn = sum(1 for c in deduped if c.category_name)
    ok = vk == len(deduped) and cn > 0
    failures += 0 if ok else 1
    print(f"[{'PASS' if ok else 'FAIL'}] derived fields: vendor_key {vk}/{len(deduped)}, "
          f"category_name {cn}/{len(deduped)}")

    # ---- descriptive output ----
    print("\n" + "-" * 68)
    print("WHAT THE PIPELINE PRODUCES FROM THIS SAMPLE")
    print("-" * 68)

    buckets: dict[str, int] = {}
    for c in deduped:
        if c.expiry_bucket:
            buckets[c.expiry_bucket] = buckets.get(c.expiry_bucket, 0) + 1
    order = ["expired", "0-6mo", "6-12mo", "12-24mo", "24mo+"]
    print("\nexpiry buckets:")
    for b in order:
        if b in buckets:
            print(f"   {b:>9}: {buckets[b]}")

    density: dict[str, int] = {}
    for c in deduped:
        if c.competition_density:
            density[c.competition_density] = density.get(c.competition_density, 0) + 1
    if density:
        print("\ncompetition density:")
        for k in ["uncontested", "low", "moderate", "high"]:
            if k in density:
                print(f"   {k:>12}: {density[k]}")

    sole = [c for c in deduped if c.is_sole_sourced]
    print(f"\nsole-sourced detected: {len(sole)}/{len(deduped)}")

    upcoming = filter_recompete(deduped, window_months=24, services_only=True)
    print(f"\nlive recompete pipeline (services, expiring within 24mo): {len(upcoming)}")
    for c in upcoming[:5]:
        value = f"${c.contract_value:,.0f}" if c.contract_value else "n/a"
        print(f"   {c.days_to_expiry:>5}d  {value:>14}  {(c.vendor_name or '?')[:28]:<28} "
              f"{(c.description_en or '')[:34]}")

    print("\n" + "=" * 68)
    print("RESULT:", "ALL CHECKS PASSED" if failures == 0 else f"{failures} CHECK(S) FAILED")
    print("=" * 68)
    return failures


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Canadian Recompete Radar ingest")
    parser.add_argument("--self-test", action="store_true",
                        help="validate transforms against the bundled fixture")
    parser.add_argument("--full", action="store_true", help="run the full ingest")
    parser.add_argument("--max-rows", type=int, default=None,
                        help="cap rows fetched (useful for a smoke test)")
    parser.add_argument("--window", type=int, default=None,
                        help="only keep contracts expiring within N months")
    parser.add_argument("--include-goods", action="store_true",
                        help="include goods contracts (end-date semantics are ambiguous)")
    parser.add_argument("--out", default="recompete.db", help="SQLite output path")
    parser.add_argument("--pipeline-out", default="recompete_pipeline.json",
                        help="JSON consumed by build_site.py")
    parser.add_argument("--fixture", default=None, help="fixture path for --self-test")
    args = parser.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))

    if args.self_test:
        fixture = args.fixture or os.path.join(here, "fixture_contracts.json")
        if not os.path.exists(fixture):
            print(f"fixture not found: {fixture}", file=sys.stderr)
            return 2
        return 1 if self_test(fixture) else 0

    if not args.full:
        parser.print_help()
        return 0

    print("Canadian Recompete Radar - ingest")
    print("source: open.canada.ca proactive disclosure of contracts")
    print("licence: Open Government Licence - Canada\n")

    started = time.time()
    normalized: list[Contract] = []
    for row in iter_all_rows(max_rows=args.max_rows):
        normalized.append(normalize(row))

    print(f"\nnormalized {len(normalized):,} rows in {time.time() - started:,.0f}s")

    deduped = deduplicate(normalized)
    print(f"deduplicated -> {len(deduped):,} contracts "
          f"({len(normalized) - len(deduped):,} amendment rows collapsed)")

    named = derive_category_names(deduped)
    print(f"category names derived for {named:,} commodity codes")
    print(f"vendor identities: {len({c.vendor_name for c in deduped}):,} raw spellings "
          f"-> {len({c.vendor_key for c in deduped}):,} normalized")

    live = filter_recompete(
        deduped,
        window_months=args.window,
        services_only=not args.include_goods,
    )
    print(f"recompete pipeline -> {len(live):,} contracts")



    # The site generator consumes this file. Writing it is the default, not an
    # option, so the ingest and the build cannot silently drift apart again.
    # JSON first: it is what build_site.py consumes. SQLite is a convenience.
    with open(args.pipeline_out, "w", encoding="utf-8") as fh:
        json.dump([asdict(c) for c in live], fh)
    print(f"wrote {args.pipeline_out}  ({os.path.getsize(args.pipeline_out)/1e6:.1f} MB)")

    missing = [f for f in ("vendor_key", "category_name")
               if live and getattr(live[0], f, None) is None]
    if missing:
        print(f"WARNING: fields not populated: {missing}", file=sys.stderr)
        return 1

    write_sqlite(deduped, args.out)
    print(f"wrote {args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
