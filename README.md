# Inventory Reconciliation Agent


An agent that watches inventory across three fragmented warehouse systems,
works out on its own when two sources disagreeing is a real problem versus
just one of them running behind, decides which source to believe and why,
and takes a proportionate action with every decision logged well enough
that a warehouse manager could audit it without re-running anything.


## 1. Intro

- Inventory across three or more independent systems, decide *without being told which pairs to check* whether a disagreement is a genuine conflict or just a timing artefact, choose which source to trust and be able to defend that choice with evidence, and decide what to actually do about it, flag it, fix it automatically, or escalate it, in a way a human could audit later without re-running the code.
- This repo is that system, built against three stub sources with genuinely different schemas, identifiers, and update rhythms (SQLite/WMS, JSON/ e-commerce, CSV/3PL), covering an 88-SKU catalog rather than a handful of bespoke test cases (see "Data" below).
- It went through three rounds of adversarial pressure after the first working version and a source-authority scoring bug, a set of uncomfortable edge cases, and a blast-radius safety gap where each one found, fixed, and locked in with a regression test rather than patched around.

### Data

- The repository uses **synthetic operational data**, not real or scraped retail data. Three independent source fixtures (`data/wms.db`, `data/ecommerce_feed.json`, `data/tpl_feed.csv`) simulate a WMS database, an e-commerce inventory feed, and a 3PL export, covering 88 SKUs. They intentionally differ in schema, identifiers, freshness, and occasional data quality.
- `scripts/generate_fixtures.py` builds them **once**, seeded (`random.seed(42)`) for reproducibility. `src/agent.py` never imports or calls it.
- When you run `python src/agent.py`, the agent is reading three existing, independent snapshots it had no hand in creating and discovering what's wrong in them, the same way it would against real systems.
- Of the 88 SKUs, 13 are curated scenarios, one each for the specific behaviours discussed throughout this README (timing lag, inventory semantics, the corroboration fix, stale evidence, a data-quality fault, a missing source).
- The other 75 are a bulk catalog: mostly ordinary, consistent inventory (mimicking a real catalog), with a smaller, randomly-assigned share of timing lag, genuine conflicts, and missing-source cases mixed in and is generated the same way, not hand-authored one at a time, so the interesting cases appear organically among normal inventory rather than being the entire dataset.

### Quick start

```bash
python scripts/generate_fixtures.py  # builds the 88-SKU catalog across 3 sources (run once; committed)
python src/agent.py                  # runs the agent against the existing data, writes logs/
python verify_requirements.py        # checks a run's output against the brief's literal requirements
python tests/test_agent.py           # 14 unit tests on the decision logic (no external deps)
python adversarial_review.py         # 10 uncomfortable stress-test cases, run standalone
```

No dependencies beyond the Python 3 standard library. Tested on 3.10+.
Output: `logs/reconciliation_log.jsonl` (machine-readable, one record per
SKU), `logs/reconciliation_summary.md` (the same decisions, human-readable),
`logs/corrections.jsonl` (real write-back records for auto-reconciled SKUs).


## 2. Objectives


1. Fetch inventory state from 3+ sources with different schemas, cadences,
   or latency profiles, and detect a genuine discrepancy for **every**
   source pair, without being told which pairs to check.
2. Build a decision framework using at least 2 of {recency, historical
   accuracy, financial impact, business rules}, and document it with a
   worked example showing *why* one source was chosen over another.
3. Log every reconciliation decision, evidence considered, source chosen,
   action taken, so a human reviewer can understand it without re-running
   the agent.
4. **corroboration** between independent sources (see
Methodology), and **inventory semantics**, recognising that two sources
can both be correct while disagreeing, because they're measuring different
things (physical stock vs. sellable stock).

---

## 3. Methodology

### 3.1 A decision tree of checks, not a search over hypotheses

For each SKU, the agent works through a **fixed, ordered sequence** of
checks. Each one either explains the discrepancy and stops, or is rejected
and hands off to the next:

```
data_quality  ->  insufficient_evidence  ->  timing_lag  ->
stale_evidence  ->  inventory_semantics  ->  genuine_conflict
```

This is a decision tree, not an inference engine searching over
possibilities. Every check performed is still written to the log (`checks` field), including
the ones that were rejected.

### 3.2 Timing lag vs. genuine conflict — per-source freshness windows

- Each source has its own expected freshness window (`FRESHNESS_WINDOW_MIN`
in `src/agent.py`): WMS 30 min, e-commerce 120 min, 3PL 25 hours (daily
batch + grace).
- If one source is older than *its own* window while the
rest agree, that's a **timing lag** — logged as "monitor, re-check after
next scheduled update," not a false alarm.
- If every source is within its
own window and they still disagree, lag is rejected and the agent moves on.

### 3.3 Inventory semantics — do the sources even disagree?

- WMS and the 3PL both report **physical on-hand** stock. The e-commerce
platform reports **sellable-available** stock — physical minus units
already reserved against pending orders. 50 (physical) and 42 (sellable)
can both be correct at the same instant if 8 units are reserved.
- The agent normalises a sellable reading onto the physical basis
(`physical_equivalent = sellable_reading + reserved_units`, from
`data/reservations.csv`) before comparing anything. Three outcomes, all
distinctly logged: **fully explained** (not a conflict at all — see
Results, SKU-1011), **partially explained** (a smaller, genuine conflict
remains — SKU-1004), or **not applicable**.
- The reservations feed is also
checked for its own freshness, if it's stale, the agent won't silently
close a case just because the arithmetic happens to work (SKU-1012, see
Results). 

### 3.4 Which source to trust

For each source in a genuine conflict:

```
composite_score = 0.20 x recency_score
                 + 0.30 x historical_accuracy
                 + 0.15 x business_rule_bonus
                 + 0.35 x corroboration_score
```

- **recency_score** — normalised to the source's *own* freshness window.
- **historical_accuracy** — computed at startup from
  `data/historical_reconciliations.csv` (a log of past discrepancies
  checked against a physical stocktake), not hardcoded. Currently WMS 93%,
  TPL 81%, e-commerce 70%.
- **business_rule_bonus** — 1.0 for WMS (fed directly by barcode scans),
  0 otherwise. Deliberately modest — see below.
- **corroboration_score** — fraction of the *other* reporting sources
  whose normalised value agrees with this one. A source standing alone
  scores 0; a source another independent source backs up scores higher.

- **Corroboration exists because an earlier version of this framework got a
case wrong.** With weights recency 0.30 / history 0.40 / business 0.30 and
no corroboration term, the framework saw WMS: 5, e-commerce: 40, 3PL: 42,
two independent sources agreeing within 2 units, one outlier and chose
- **WMS's isolated 5** as authoritative, purely on business-rule and
history priors. A prior should never be strong enough to
overrule two sources actively corroborating each other.
- Adding corroboration as the largest-weighted factor fixed it. The trusted source
is now TPL, by a properly narrow margin (0.615 vs. 0.603) rather than a
confident-looking but wrong call. See Results and Challenges Addressed for
the full story; it's locked in with `test_corroboration_beats_isolated_business_rule_favourite`.

### 3.5 What to do about it

```
impact_gbp = abs(trusted_qty - other_qty) x unit_cost   (worst case, on NORMALISED values)
```

- `< £50` **and** quantity deviation bounded -> **auto-reconcile**, with a
  real write-back to `logs/corrections.jsonl`
- `£50–£500`, or `< £50` but the quantity guard fails -> **flag for
  manual review**
- `>= £500` -> **escalate** immediately

- The quantity guard is a second, independent safety condition on top of £
impact, added after adversarial testing found £ impact alone wasn't
sufficient (see Challenges Addressed): `auto_reconcile` also requires
`max_abs_diff <= 100 units` **or** `relative_diff <= 20%` of the trusted
quantity.
- It's an OR, not an AND blocking on either alone would flag
ordinary small-quantity variance (3 units on a base of 10 is "only" 3
units but "30%"); the guard should fire only when a discrepancy is large
on *both* axes at once, which is what actually distinguishes a dangerous
case from routine noise.

- The £50/£500 thresholds and the tolerance function
(`max(2, 5% of the average reading)`, used for both discrepancy detection
and corroboration) are flat, demo-level business rules, not derived from
anything — see Future Work.


## 4. System design / model pipeline

```
 ONE-TIME, OFFLINE, COMMITTED (agent never runs or imports this):

   scripts/generate_fixtures.py  --seed 42-->

 data/wms.db      data/ecommerce_feed.json   data/tpl_feed.csv
 (SQLite,             (JSON,                    (CSV,
  internal_sku,        product_id,                upc,
  88 records)          88 records)                85 records)

 ─────────────────────────────────────────────────────────────
 RUNTIME (src/agent.py -- only ever reads the files above):

      |                    |                         |
      v                    v                         v
  fetch_wms()        fetch_ecommerce()          fetch_tpl()
      |                    |                         |
      +--------------------+-------------------------+
                           |
              joined via data/sku_crosswalk.csv
              into normalised Reading objects,
                 grouped by internal_sku
                           |
     data/reservations.csv (+ freshness) --->  |
     data/historical_reconciliations.csv --->  |
                           v
           classify_and_decide(sku, readings, ...)
                           |
        +------------------------------------------+
        |  data_quality -> insufficient_evidence -> |
        |  timing_lag -> stale_evidence ->          |
        |  inventory_semantics -> genuine_conflict   |
        +------------------------------------------+
                           |
              trust scoring (recency, history,
              business rule, corroboration)
                           |
              action policy (£ impact AND
              quantity-deviation guard)
                           |
       +-------------------+--------------------+
       v                   v                    v
 auto_reconcile      flag_for_review         escalate
       |
       v
 logs/corrections.jsonl (real correction records)
                           |
                           v
   logs/reconciliation_log.jsonl (JSONL, machine-readable)
   logs/reconciliation_summary.md (Markdown, human-readable)
```



## 5. Challenges addressed


- **Schema/identifier fragmentation** — three different keys (`internal_sku`,
  `product_id`, `upc`), three different formats, joined through
  `data/sku_crosswalk.csv`. Not cosmetic renaming of one field.
- **Distinguishing timing lag from genuine conflict** — per-source
  freshness windows (3.2), not a single global cutoff.
- **Discrepancy detection without being told which pairs to check** — the
  agent groups by SKU and compares whatever sources reported it; no
  hardcoded "compare WMS to ERP." `verify_requirements.py` checks the
  actual log to confirm all three pairs produce a genuine discrepancy
  somewhere, rather than trusting the dataset design.

- **Different inventory meanings aren't a conflict** — SKU-1011: raw
  numbers look like a 3-way disagreement; accounting for reservations
  shows they were never in conflict at all (3.3).
- **An explanation can itself be unreliable** — SKU-1012: arithmetically
  identical to SKU-1011, but the reservations snapshot explaining the gap
  is 5 hours stale, so the agent flags for confirmation instead of
  auto-closing.
- **Missing source, not silent agreement** — SKU-1006: 3PL never reported
  this SKU; two-out-of-three isn't treated as full consistency.
- **Invalid data isn't a stock conflict** — SKU-1007: a negative 3PL
  reading is excluded from trust scoring and flagged as a pipeline bug,
  separate from any genuine disagreement.

**Found via a dedicated adversarial pass (`adversarial_review.py`, 10
deliberately uncomfortable cases run after the framework "worked"), fixed,
and locked in as regression tests:**

- **All sources stale AND disagreeing.** The timing-lag check needs at
  least one fresh source to anchor against, so when *none* are fresh, the
  agent fell through to normal scoring and confidently escalated to a
  "trusted" source built from recency scores of 0, indistinguishable in
  the log from a well-evidenced decision.
  - Fixed with a new `STALE_EVIDENCE` classification that refuses to assert a trusted value
  or act on financial impact until a source refreshes. Found on a
  synthetic case in the adversarial script; SKU-1013 was then added to the
  actual demo dataset so the fix is visible in a real run too (Results,
  Worked example D), not only in a standalone test.
- **Reservation translation could write a negative correction.**
  `trusted_physical - reserved_units` can go negative if reservations
  exceed trusted stock, exactly what the `data_quality` check elsewhere
  refuses to trust. Fixed: any correction that would go negative refuses
  `auto_reconcile` and flags for review instead.
- **A cheap SKU with a huge quantity gap could auto-reconcile purely
  because £ impact stayed low.** `WMS=10, e-commerce=5,000, unit cost
  £0.01` is £49.90, under threshold, despite a 4,990-unit, 499x gap.
  Fixed with the quantity-deviation guard described in 3.5.
- **An unregistered source name crashed the agent** (`KeyError`) instead
  of degrading gracefully. Fixed with a conservative default freshness
  window.

## 6. Results

A full run against the demo dataset (**88 SKUs** — 13 curated scenarios +
75-item bulk catalog, `python src/agent.py`):

| Classification | Count | Action | Count |
|---|---|---|---|
| CONSISTENT | 72 | no_action | 67 |
| TIMING_LAG | 6 | monitor | 6 |
| GENUINE_CONFLICT | 9 | flag_for_review | 11 |
| STALE_EVIDENCE | 1 | auto_reconcile | 1 |
| | | escalate | 3 |

`verify_requirements.py` confirms the brief's per-source-pair
discrepancy requirement is met not just by the curated scenarios but with
contributions from the bulk catalog too (e.g. `SKU-2011`, `SKU-2015`,
`SKU-2046`).

2 correction records written to `logs/corrections.jsonl`. Full detail for
every SKU — evidence, checks trail, reasoning, action is in
`logs/reconciliation_summary.md`.

**Test results:** 14/14 unit tests pass (`tests/test_agent.py`) including
one dedicated regression test per bug found during adversarial review.
8/8 requirement checks pass (`verify_requirements.py`) against the brief's
literal requirements. 10/10 adversarial cases run clean, zero crashes
(`adversarial_review.py`).

### Example A — a genuine conflict, escalated (SKU-1005)

- Noise Cancelling Headphones, unit cost £89.99. WMS: 12 (5 min old), 3PL: 13
(60 min old), e-commerce: 45 (110 min old) — all within their own freshness
windows, so not a timing lag.
- WMS wins trust scoring (composite 0.922 vs.
3PL 0.614 vs. e-commerce 0.307), corroborated by 3PL's 13. Impact =
`|12-45| x £89.99 = £2,969.67` → above £500 → **escalate**, recommended
value 12 units.

### Example B — not a conflict at all (SKU-1011)

- Portable SSD, unit cost £64.50. WMS: 50, 3PL: 49 (physical), e-commerce: 42
(sellable). Raw spread of 8 looks exactly like example A.
- Once normalised for 8 reserved units, e-commerce's physical-equivalent is `42+8=50` —
spread of 1, well within tolerance. **Classified CONSISTENT, no_action.**
This is the case that best shows the agent reasoning about *why* numbers
differ, not just detecting *that* they differ.

### Example C — the corroboration fix (SKU-1010)

- Ergonomic Wrist Rest, unit cost £7.85. WMS: 5, e-commerce: 40, 3PL: 42 —
e-commerce and 3PL independently agree within 2 units; WMS stands alone.
- Before the fix in 3.4, the framework trusted WMS's isolated 5 anyway. After
adding corroboration, TPL wins (composite 0.615, corroboration 0.5) over
WMS (0.603, corroboration 0.0), a properly narrow, defensible margin
instead of a confidently wrong call. Impact = `£290.45` → **flag_for_review**.

### Example D — refusing to guess on stale data (SKU-1013)

- Anti-Static Wrist Strap, unit cost £6.40. WMS: 8 (~3.5 days old), e-commerce:
25 (~2 days old), 3PL: 9 (~2 days old), every source is outside its own
freshness window, and they still disagree.
- The pre-adversarial-review version of this framework would have scored a "trusted" source anyway,
built entirely from recency values of 0, and could have escalated on data
that's days stale. **Classified STALE_EVIDENCE, flag_for_review, no
trusted source asserted** 

## 7. Impact

- For a warehouse manager, the concrete value is threefold: fewer false
alarms (timing lag and explainable semantic differences don't generate
review work), fewer silent wrong corrections (the quantity-deviation and
negative-write-back guards exist specifically to stop the agent from
"fixing" something into a worse state), and an audit trail detailed enough
to answer "why did the system believe X" **after the fact**, without
needing to reproduce the run.

- The more important impact is what the adversarial-review process demonstrates about how I'd actually operate on a real reconciliation system: I don't think a framework is trustworthy because it passes its own designed-in checks. `verify_requirements.py` says nothing about whether any individual decision is *right*.
- The corroboration bug, the stale-evidence bug, and the quantity-deviation gap were all found by deliberately trying to break the system after it "worked," not by
building more of it and I think that habit matters more for an autonomous system touching inventory numbers than any single feature would.

## 8. Future work

- **Tolerance isn't SKU-aware.** `max(2, 5% of the average reading)` is a
  flat rule used for both discrepancy detection and corroboration. In
  production this should vary by SKU, category, or sales velocity, a
  fast-moving SKU has more natural in-flight variance than a slow one.
  Kept flat here deliberately.
- **Real abstention / confidence intervals.** A genuinely ambiguous 3-way
  split with zero corroboration anywhere currently still produces a single
  "winner," just with a low composite score. Surfacing the runner-up
  margin as an actual confidence measure would let truly uncertain cases
  get a different action, or abstain outright, rather than always naming a
  trusted source.
- **Causal event reconstruction.** Inventory semantics (3.3) handles
  *static* differences in what a source measures. It doesn't reconstruct
  the actual sequence of picks/receipts/returns between two snapshots to
  explain a delta causally. Meaningfully larger scope (needs an event log,
  not a point-in-time reservations figure).
- **Idempotent write-back.** `auto_reconcile` writes a correction record
  but doesn't check whether that exact correction was already applied;
  reprocessing the same case would repeat it. Needs a case-ID-keyed
  idempotency check.
- **Temporal re-evaluation.** Every run is a single snapshot at a fixed
  clock. A timing lag that hasn't resolved by the stale source's next
  expected update should itself escalate, the log already says as much,
  but there's no state carried between runs to act on it.
- **Real source connectors** in place of the file/DB stubs, a fetcher-level change, not a rewrite, since the reconciliation logic only sees `Reading` objects.
- **Feedback loop into historical accuracy.** The accuracy log is static;
  every resolved decision should append to it so trust weighting actually
  improves over time.
- **Rescale £ thresholds and the quantity guard** with real sell-through
  and category data instead of the placeholder £50/£500/100-units/20%
  used here.



## 9. Technology and tools

- **Python 3.10+, standard library only** — `sqlite3`, `csv`, `json`,
  `dataclasses`, `datetime`, `random` (seeded, for reproducible fixture
  generation). No external dependencies, no `requirements.txt` entries,
  nothing to `pip install`. 
- **A hand-rolled test runner** (`if __name__ == "__main__"` loop in
  `tests/test_agent.py`) instead of `pytest`
- **SQLite, JSON, CSV** as the three source formats, chosen specifically
  because they're schema-divergent by construction, not because of any
  framework preference.



## 10. Code

| File | Contains |
|---|---|
| `scripts/generate_fixtures.py` | Builds the 88-SKU catalog across the three stub sources (`data/wms.db`, `data/ecommerce_feed.json`, `data/tpl_feed.csv`), the reservations feed, and the historical-accuracy log. Run **once**, output committed — `src/agent.py` never imports or calls this file (see Section 4 and the "Data" note in Section 1). 13 curated scenarios (SKU-1001–1013) plus a seeded-random 75-item bulk catalog (SKU-2001+). |
| `src/agent.py` | The agent itself: fetchers (`fetch_wms`, `fetch_ecommerce`, `fetch_tpl`), the `Reading` dataclass, `classify_and_decide()` (the full checks pipeline, trust scoring, and action policy), and `run()` (orchestration + log writing). All thresholds and weights are declared as named constants near the top with comments explaining each one. |
| `tests/test_agent.py` | 14 unit tests against `classify_and_decide()` directly (no data files needed) — one per major behaviour, including a dedicated regression test for each bug found during adversarial review. |
| `verify_requirements.py` | Reads the actual `logs/reconciliation_log.jsonl` from the last run and checks it against the brief's literal requirements (3+ sources, genuine discrepancy per pair, 2+ decision factors, etc.), printing PASS/FAIL per requirement rather than asserting compliance in prose. |
| `adversarial_review.py` | 10 standalone stress-test cases (all-stale evidence, negative reservations, unregistered sources, tolerance boundaries, and more) run directly against `classify_and_decide()`, printing the agent's actual decision for each so it can be judged rather than assumed correct. Not part of the automated test suite — a one-off adversarial pass, documented in Section 5. |
| `data/sku_crosswalk.csv` | Source-of-truth SKU ↔ `product_id` ↔ `upc` mapping for all 88 SKUs, plus unit cost for financial-impact scoring. Fully regenerated (curated + bulk rows) each time the fixture script runs. |
| `data/reservations.csv` | Units reserved per SKU + timestamp, used to reconcile physical-vs-sellable inventory semantics (3.3). |
| `data/historical_reconciliations.csv` | Simulated log of past discrepancies checked against a physical stocktake; `historical_accuracy` in trust scoring is computed from this file at startup, not hardcoded. |
| `data/wms.db`, `data/ecommerce_feed.json`, `data/tpl_feed.csv` | The three generated stub sources — 88 records each (85 for 3PL; 3 SKUs are deliberately missing from it). Committed as static fixtures, not regenerated at agent runtime (see "Data", Section 1). |
| `logs/reconciliation_log.jsonl` | One JSON record per SKU (88 total): evidence, checks trail, trust scores, reasoning, action, corrections. Machine-readable, committed so a reviewer can read a real run without executing anything. |
| `logs/reconciliation_summary.md` | The same decisions rendered as human-readable Markdown. |
| `logs/corrections.jsonl` | Real write-back records for every `auto_reconcile` decision (case ID, target source, old/new quantity). |
