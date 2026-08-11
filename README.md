# Canadian Recompete Radar

Federal contracts coming up for renewal — who holds them, what they're worth,
and how contested they were when last awarded.

Built from the Government of Canada **Proactive Publication of Contracts**
dataset. Open Government Licence – Canada.

---

## What this repo does by itself

Once set up, **nobody has to do anything again.** On the 5th of each month GitHub:

1. Runs the transform self-test
2. Downloads all ~1.31M federal contract records
3. Deduplicates amendments, filters to services/construction, derives expiry,
   competition density, vendor identity and category names
4. Builds ~2,100 static pages
5. **Audits the result against the source data**
6. Publishes — but only if the audit passed

If the audit fails, the deploy is skipped and you get an email. Bad data cannot
reach the public site.

---

## One-time setup

**1. Create the repository**

On GitHub: *New repository* → name it → **Public** (public repos get unlimited
Actions minutes) → Create.

**2. Upload these files**

On the empty repo page click *uploading an existing file*, drag in everything
here — including the `.github` folder — and commit.

> If drag-and-drop skips `.github`, create the file manually: *Add file →
> Create new file*, name it exactly
> `.github/workflows/refresh.yml`, paste the contents, commit.

**3. Turn on Pages**

*Settings → Pages → Source:* **GitHub Actions**.

**4. Tell it the site address**

*Settings → Secrets and variables → Actions → Variables → New variable*
Name `SITE_URL`, value your published URL, e.g.
`https://yourname.github.io/recompete-radar`

This matters: sitemaps require absolute URLs. Relative paths are rejected by
Search Console, which would silently kill search indexing.

**5. Run it**

*Actions → Refresh data and publish site → Run workflow.*
Takes ~20 minutes. Green tick = live.

---

## Getting it indexed

1. [Google Search Console](https://search.google.com/search-console) → add your
   URL as a property
2. Verify ownership (the HTML-file method works with GitHub Pages)
3. *Sitemaps* → submit `sitemap.xml`

Indexing takes weeks. That is normal and is the part you cannot rush.

---

## Running it on your own machine

Needs Python 3.9+. No packages to install — standard library only, deliberately,
so no dependency can ever break the build.

```bash
python3 ingest.py --self-test          # ~1s, no network
python3 ingest.py --full               # ~20 min, writes recompete_pipeline.json
python3 build_site.py --input recompete_pipeline.json --out site --base-url https://yoursite
python3 audit.py --input recompete_pipeline.json --site site
```

`audit.py` exits non-zero if anything is wrong. It does not import the
generator, so a generator bug cannot hide inside it.

---

## Files

| File | Purpose |
|---|---|
| `ingest.py` | Downloads and transforms the source data |
| `build_site.py` | Generates the static site |
| `audit.py` | Independently verifies the site against the data |
| `fixture_contracts.json` | 110 real API records used by `--self-test` |
| `.github/workflows/refresh.yml` | The automation |

---

## Known limitations

Stated plainly, because the product's value is accuracy.

**Incumbent totals are a floor, not an exact figure.** Vendor names are
normalized by stripping legal suffixes, which merges spelling variants. It does
**not** merge token subsets — "General Dynamics" and "General Dynamics Land
Systems" stay separate, because they are distinct procurement entities. 464 keys
(4.3%), covering $21.8B, are subsets of another key. Under-merging is the safer
error, but a corporate group's true exposure may be higher than one page shows.

**Bid data covers 82% of contracts but only 41% by value.** The largest
contracts disproportionately lack it.

**"Uncontested" is the norm, not the exception.** 75% of contracts with bid data
show one bid or zero. 2,969 records show literally zero bids, which is more
likely a reporting convention than no bidders — unconfirmed.

**Values are total contract value over the full term**, not annual spend. The
top 100 contracts are 81% of the total; the median contract is $50,911.

**Data is published quarterly**, so the most recent quarter will not appear.
Fine for a 12–24 month planning horizon; do not advertise real-time.

**Goods contracts are excluded.** For services and construction the published
"Contract Period End Date or Delivery Date" field is defined as the end of the
performance period. For goods it is a delivery date that only *may* be the end
date, so including them would produce unreliable expiry dates.
