"""
agent.py

Reconciliation agent.

Pipeline:
  1. Fetch raw readings from three independent sources (different schemas,
     different update rhythms, different identifiers).
  2. Normalise them onto a common internal_sku via the crosswalk table.
  3. For each SKU, work through a fixed sequence of explanation checks
     rather than jumping straight to a verdict. This is a decision tree,
     not a search over hypotheses -- each check runs in a set order and
     either explains the discrepancy or hands off to the next one:
       - DATA_QUALITY       a source returned a physically impossible
                            value (e.g. negative stock); excluded from
                            trust scoring and flagged separately
       - INSUFFICIENT_EVIDENCE  fewer than 2 usable readings remain
       - TIMING_LAG          one source is behind its own normal
                            freshness window and the fresh sources agree
       - INVENTORY_SEMANTICS sources measure different things (physical
                            on-hand vs. sellable-available); once
                            normalised for known reservations, do they
                            actually agree? (and is that normalising
                            evidence itself fresh enough to trust?)
       - GENUINE_CONFLICT   still disagree after all of the above
  4. For GENUINE_CONFLICT, score each source's trustworthiness -- including
     whether another independent source corroborates it -- and pick one as
     authoritative for this SKU.
  5. Decide an action from the financial impact of that decision. An
     auto-reconcile action writes an actual correction record to
     logs/corrections.jsonl, not just a log line describing one.
  6. Write one JSON line per SKU to logs/reconciliation_log.jsonl and a
     human-readable Markdown summary to logs/reconciliation_summary.md.
     Every check performed -- including the ones that didn't explain
     anything.

Run:
    python generate_data.py   
    python src/agent.py
"""
import sqlite3
import json
import csv
import os
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
NOW = datetime(2026, 8, 14, 15, 0, 0)  # fixed clock, matches generate_data.py

# Config: documented, tunable thresholds. See README "Decision framework"
# for the reasoning behind each of these numbers.

# How stale a source can be before we consider it "still within its own
# normal freshness window" (minutes). Anything older than this is treated
# as a source that simply hasn't updated yet, not as live evidence.
FRESHNESS_WINDOW_MIN = {
    "WMS": 30,
    "ECOMMERCE": 120,
    "TPL": 25 * 60,  # daily batch + 1hr grace
}

# A source name that isn't in FRESHNESS_WINDOW_MIN (e.g. a new source wired
# into the fetchers without being registered here) must not crash the
# agent. Default conservatively: treat it as needing to be very fresh, so
# an unknown source's data is quickly treated as stale rather than
# silently assumed to have some generous, unexamined cadence.
DEFAULT_FRESHNESS_WINDOW_MIN = 15

# What each source is actually measuring. 100 (physical
# on-hand) and 94 (sellable-available) can BOTH be correct at the same
# instant -- they answer different questions. Only sources with the same
# `measures` value are directly comparable; anything reporting
# sellable_available is normalised against known reservations before it's
# compared to a physical_on_hand reading. See README "Inventory semantics".
SOURCE_MEASURES = {
    "WMS": "physical_on_hand",
    "TPL": "physical_on_hand",
    "ECOMMERCE": "sellable_available",
}

# Reservations context should reflect near-real-time order activity. If the
# reservations snapshot used to normalise a sellable-available reading is
# older than this, it's treated as evidence too stale to fully trust --
# the arithmetic can still "explain" a gap, but the agent won't close the
# case on a stale explanation without a human glancing at it first.
RESERVATIONS_FRESHNESS_WINDOW_MIN = 60

# Business-rule weight: policy is that the WMS is the system of record for
# *physical* stock because it is fed directly by barcode scans on the
# warehouse floor, whereas the e-commerce platform and 3PL are downstream
# consumers/syncs of that physical reality. Deliberately modest -- see
# WEIGHT_CORROBORATION below for why this must not be able to overrule two
# independent sources agreeing with each other.
BUSINESS_RULE_BONUS = {"WMS": 1.0, "ECOMMERCE": 0.0, "TPL": 0.0}

# Corroboration matters: a source's claim is stronger evidence if another
# independent source backs it up, and weaker if it stands alone against
# agreement elsewhere. Without this, a strong business-rule/history prior
# for one source could out-score two other sources that independently
# agree with each other -- which is backwards. WEIGHT_CORROBORATION is
# deliberately the largest single weight so that can't happen.
WEIGHT_RECENCY = 0.20
WEIGHT_HISTORY = 0.30
WEIGHT_BUSINESS = 0.15
WEIGHT_CORROBORATION = 0.35

AUTO_RECONCILE_MAX_GBP = 50.00     # below this: safe to auto-fix, low blast radius
ESCALATE_MIN_GBP = 500.00          # above this: needs a human now, not just a queue

# Blast-radius guard on auto_reconcile, independent of £ impact. A cheap SKU
# with a huge unit-count gap (e.g. 10 vs 5,000 units at £0.01 each) stays
# under the £50 threshold on value alone, but a discrepancy that large in
# raw quantity deserves a human look regardless of what it's worth -- a
# count error of that size usually signals something more wrong than a
# pricing calculation can capture.
#
# Blocking requires the deviation to be large in BOTH absolute count AND
# proportion -- not either alone. A small-quantity SKU naturally swings by
# a large percentage on a tiny absolute difference (3 units on a base of 10
# is "only" a few units but "30%"); blocking that would be overcautious. A
# huge-quantity SKU can have a large absolute gap that's a tiny proportion
# of its stock; that's not alarming either. It's genuinely dangerous only
# when a discrepancy is large on both axes at once, which is what SKU-F's
# 10-vs-5,000 case actually is (4,990 units AND 499x). Values are demo
# policy, not derived from anything -- see README.
AUTO_RECONCILE_MAX_ABS_QTY_DIFF = 100     # units
AUTO_RECONCILE_MAX_REL_QTY_DIFF = 0.20    # 20% of the trusted quantity

# Two readings are "in agreement" if they differ by less than this, so we
# don't fire on 1-2 unit noise from in-flight picks/packs.
def tolerance_for(values):
    avg = sum(values) / len(values)
    return max(2, round(0.05 * avg))


@dataclass
class Reading:
    source: str
    raw_id: str
    quantity: int
    updated_at: datetime

    @property
    def age_minutes(self):
        return (NOW - self.updated_at).total_seconds() / 60

    @property
    def within_freshness_window(self):
        return self.age_minutes <= FRESHNESS_WINDOW_MIN.get(self.source, DEFAULT_FRESHNESS_WINDOW_MIN)


# Fetchers -- one per source, each returns {internal_sku: Reading}

def load_crosswalk():
    crosswalk = {}
    with open(os.path.join(DATA_DIR, "sku_crosswalk.csv")) as f:
        for row in csv.DictReader(f):
            crosswalk[row["internal_sku"]] = row
    return crosswalk


def fetch_wms(crosswalk):
    path = os.path.join(DATA_DIR, "wms.db")
    conn = sqlite3.connect(path)
    out = {}
    for sku, qty, updated in conn.execute("SELECT internal_sku, qty_on_hand, last_updated FROM inventory"):
        out[sku] = Reading("WMS", sku, qty, datetime.fromisoformat(updated))
    conn.close()
    return out


def fetch_ecommerce(crosswalk):
    path = os.path.join(DATA_DIR, "ecommerce_feed.json")
    with open(path) as f:
        payload = json.load(f)
    by_product_id = {row["product_id"]: row for row in crosswalk.values()}
    out = {}
    for item in payload["items"]:
        cw = by_product_id.get(item["product_id"])
        if not cw:
            continue  # product not in our crosswalk, ignore (out of scope for this run)
        out[cw["internal_sku"]] = Reading(
            "ECOMMERCE", item["product_id"], item["stock_level"],
            datetime.fromisoformat(item["last_synced"]),
        )
    return out


def fetch_tpl(crosswalk):
    path = os.path.join(DATA_DIR, "tpl_feed.csv")
    by_upc = {row["upc"]: row for row in crosswalk.values()}
    out = {}
    with open(path) as f:
        for row in csv.DictReader(f):
            cw = by_upc.get(row["upc"])
            if not cw:
                continue
            out[cw["internal_sku"]] = Reading(
                "TPL", row["upc"], int(row["quantity_on_hand"]),
                datetime.fromisoformat(row["snapshot_time"]),
            )
    return out


def load_reservations():
    """
    internal_sku -> (reserved_units, as_of datetime). Used to translate a
    sellable-available reading (e-commerce) onto the same physical-on-hand
    basis as WMS/3PL: physical = sellable + reserved. A SKU with no row
    here is assumed to have 0 reservations (as_of=None).
    """
    path = os.path.join(DATA_DIR, "reservations.csv")
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path) as f:
        for row in csv.DictReader(f):
            out[row["internal_sku"]] = (int(row["reserved_units"]), datetime.fromisoformat(row["as_of"]))
    return out


def load_historical_accuracy():
    """
    Computes each source's historical accuracy from the log of past
    reconciliations that were later checked against a physical stocktake.
    Falls back to a conservative 0.75 prior for any source with no history.
    """
    path = os.path.join(DATA_DIR, "historical_reconciliations.csv")
    counts = {}
    with open(path) as f:
        for row in csv.DictReader(f):
            src = row["source"]
            correct = row["was_correct"].strip().lower() in ("true", "1", "yes")
            c = counts.setdefault(src, [0, 0])  # [correct, total]
            c[1] += 1
            if correct:
                c[0] += 1
    accuracy = {src: (correct / total if total else 0.75) for src, (correct, total) in counts.items()}
    for src in FRESHNESS_WINDOW_MIN:
        accuracy.setdefault(src, 0.75)
    return accuracy, counts


# Reconciliation logic

def classify_and_decide(sku, readings, unit_cost, description, historical_accuracy, historical_counts,
                         reservations=None):
    """
    readings: dict source -> Reading (only sources that reported this SKU)
    reservations: optional dict {sku: (reserved_units, as_of_datetime)};
        defaults to {} (i.e. assume 0 reserved, so sellable_available and
        physical_on_hand are treated as equal unless told otherwise). If
        as_of_datetime is None, the reservations figure is treated as
        fresh by default (no freshness data available to doubt it).
    Returns a fully-populated decision record ready to log, including a
    `checks` trail listing every explanation check performed for the
    discrepancy and why it was accepted or rejected.
    """
    reservations = reservations or {}
    checks = []  # [{check, status: accepted|rejected|partially_explained, reason}]

    record = {
        "timestamp": NOW.isoformat(),
        "case_id": f"REC-{sku}",
        "sku": sku,
        "description": description,
        "unit_cost_gbp": unit_cost,
        "evidence": [
            {
                "source": r.source,
                "raw_id": r.raw_id,
                "quantity": r.quantity,
                "measures": SOURCE_MEASURES.get(r.source, "physical_on_hand"),
                "updated_at": r.updated_at.isoformat(),
                "age_minutes": round(r.age_minutes, 1),
                "within_freshness_window": r.within_freshness_window,
                "freshness_window_minutes": FRESHNESS_WINDOW_MIN.get(r.source, DEFAULT_FRESHNESS_WINDOW_MIN),
            }
            for r in readings.values()
        ],
    }

    sources_present = set(readings.keys())
    all_sources = set(FRESHNESS_WINDOW_MIN.keys())
    missing = all_sources - sources_present
    if missing:
        record["missing_sources"] = sorted(missing)

    # --- Check: DATA_QUALITY -- strip out physically impossible readings 
    faulty = {s: r for s, r in readings.items() if r.quantity < 0}
    valid = {s: r for s, r in readings.items() if r.quantity >= 0}

    if faulty:
        faulty_names = ", ".join(f"{s} ({r.quantity})" for s, r in faulty.items())
        checks.append({
            "check": "data_quality",
            "status": "accepted",
            "reason": f"{faulty_names} reported a physically impossible negative quantity; "
                      f"excluded from trust scoring rather than treated as live evidence.",
        })
        if len(valid) < 2:
            record["checks"] = checks
            record["classification"] = "DATA_QUALITY_ERROR"
            record["reasoning"] = (
                f"{faulty_names} reported a physically impossible negative quantity. "
                f"Fewer than two other valid readings remain, so no reliable quantity "
                f"can be established for this SKU right now."
            )
            record["trusted_source"] = None
            record["financial_impact_gbp"] = None
            record["action"] = "flag_for_review"
            record["action_detail"] = "Data pipeline fault + insufficient valid evidence; needs manual stock check."
            return record
        record["data_quality_note"] = (
            f"{faulty_names} excluded from trust scoring: negative on-hand quantity "
            f"is invalid and indicates a feed bug, not a real stock conflict."
        )
        readings = valid
        sources_present = set(readings.keys())
    else:
        checks.append({
            "check": "data_quality",
            "status": "rejected",
            "reason": "All readings are non-negative and physically plausible.",
        })

    def finalize(rec):
        rec["checks"] = checks
        # A data-quality fault always earns at least a pipeline-fix flag,
        # even when the remaining valid sources happen to agree on quantity.
        if record.get("data_quality_note") and rec["action"] == "no_action":
            rec["action"] = "flag_for_review"
            rec["action_detail"] = (
                "Stock quantity itself is not in dispute among valid sources, but "
                + record["data_quality_note"]
                + " Flagging so the feed bug gets fixed before it masks a real conflict next time."
            )
        return rec

    if len(readings) < 2:
        checks.append({
            "check": "insufficient_evidence",
            "status": "accepted",
            "reason": f"Only {len(readings)} usable source(s) reported this SKU; no second "
                      f"opinion to cross-check against.",
        })
        record["checks"] = checks
        record["classification"] = "INSUFFICIENT_EVIDENCE"
        record["reasoning"] = (
            f"Only {len(readings)} usable source(s) reported this SKU "
            f"({', '.join(sources_present) or 'none'}); cannot cross-check without a second opinion."
        )
        record["trusted_source"] = next(iter(readings)) if readings else None
        record["financial_impact_gbp"] = None
        record["action"] = "flag_for_review"
        record["action_detail"] = "Not enough independent evidence to reconcile automatically."
        return record

    # --- Normalise onto a common basis: physical_on_hand ---
    # sellable_available = physical_on_hand - reserved, so a sellable
    # reading's physical-equivalent is (quantity + reserved_units). Sources
    # that already measure physical_on_hand pass through unchanged.
    reserved_units, reservations_as_of = reservations.get(sku, (0, None))
    if reservations_as_of is not None:
        reservations_age_minutes = (NOW - reservations_as_of).total_seconds() / 60
        reservations_fresh = reservations_age_minutes <= RESERVATIONS_FRESHNESS_WINDOW_MIN
    else:
        reservations_age_minutes = None
        reservations_fresh = True  # no freshness data supplied; nothing to doubt it with
    normalized = {}
    for s, r in readings.items():
        if SOURCE_MEASURES.get(s) == "sellable_available":
            normalized[s] = r.quantity + reserved_units
        else:
            normalized[s] = r.quantity

    raw_values = [r.quantity for r in readings.values()]
    raw_tol = tolerance_for(raw_values)
    raw_spread = max(raw_values) - min(raw_values)

    pairwise = {}
    src_list = list(readings.keys())
    for i in range(len(src_list)):
        for j in range(i + 1, len(src_list)):
            a, b = src_list[i], src_list[j]
            pairwise[f"{a}_vs_{b}"] = readings[a].quantity - readings[b].quantity
    record["pairwise_diffs"] = pairwise

    # --- Check: TIMING_LAG -- checked on the RAW readings first, since
    # a stale source's raw number is what's actually behind schedule ---
    fresh = {s: r for s, r in readings.items() if r.within_freshness_window}
    stale = {s: r for s, r in readings.items() if not r.within_freshness_window}

    values = normalized  # from here on, reason on the normalised basis
    norm_vals = list(values.values())
    tol = tolerance_for(norm_vals)
    spread = max(norm_vals) - min(norm_vals)

    if raw_spread <= raw_tol:
        checks.append({
            "check": "timing_lag",
            "status": "rejected",
            "reason": "Not applicable -- raw readings already agree within tolerance.",
        })
        checks.append({
            "check": "inventory_semantics",
            "status": "rejected",
            "reason": "Not applicable -- no unexplained gap remains.",
        })
        record["classification"] = "CONSISTENT"
        reasoning = (
            f"All {len(readings)} available sources agree within tolerance "
            f"(spread={raw_spread}, tolerance={raw_tol})."
        )
        if missing:
            reasoning += f" Note: {', '.join(sorted(missing))} did not report this SKU at all."
        record["reasoning"] = reasoning
        record["trusted_source"] = None
        record["financial_impact_gbp"] = 0
        record["action"] = "no_action" if not missing else "flag_for_review"
        record["action_detail"] = (
            "No discrepancy detected." if not missing
            else f"Consistent among reporting sources, but {', '.join(sorted(missing))} should be onboarded to this SKU."
        )
        return finalize(record)

    if stale and fresh:
        fresh_values = [r.quantity for r in fresh.values()]
        fresh_spread = max(fresh_values) - min(fresh_values) if len(fresh_values) > 1 else 0
        fresh_tol = tolerance_for(fresh_values) if len(fresh_values) > 1 else raw_tol
        if fresh_spread <= fresh_tol or len(fresh) == 1:
            stale_desc = ", ".join(
                f"{s} is {r.age_minutes:.0f}min old vs its own {FRESHNESS_WINDOW_MIN.get(s, DEFAULT_FRESHNESS_WINDOW_MIN)}min freshness window"
                for s, r in stale.items()
            )
            checks.append({
                "check": "timing_lag",
                "status": "accepted",
                "reason": f"{', '.join(fresh.keys())} agree with each other and are within their freshness window; "
                          f"{stale_desc}. Its divergent figure is explained by not having refreshed yet.",
            })
            record["classification"] = "TIMING_LAG"
            record["reasoning"] = (
                f"{', '.join(fresh.keys())} agree with each other and are within their normal "
                f"freshness window. {stale_desc}, so its lower/different figure is explained by "
                f"it simply not having refreshed yet, not by a real stock discrepancy."
            )
            record["trusted_source"] = list(fresh.keys())[0]
            record["financial_impact_gbp"] = 0
            record["action"] = "monitor"
            record["action_detail"] = (
                f"No action taken. Re-check after {', '.join(stale.keys())}'s next scheduled update; "
                f"escalate only if the gap persists past that point."
            )
            record["checks"] = checks
            return record

    checks.append({
        "check": "timing_lag",
        "status": "rejected",
        "reason": "All sources are within their own normal freshness window (or the ones that agree "
                  "don't leave a clean stale/fresh split), so lag cannot explain the spread.",
    })

    # --- Check: STALE_EVIDENCE -- is there ANY fresh source to anchor on? ---
    # If every reporting source is outside its own freshness window and they
    # still disagree, the earlier timing_lag check can't fire (it needs at
    # least one fresh source to corroborate against), and scoring a "trusted"
    # source purely on recency=0 signals for everyone would look identical to
    # a normal, well-evidenced conflict without actually being one. Caught by
    # an adversarial review (case: WMS=10/500min, ECOM=40/1000min, TPL=42/2000min
    # -- all stale, all disagreeing -- the earlier version confidently
    # escalated to WMS despite having no current evidence at all).
    if not fresh:
        oldest_desc = ", ".join(
            f"{s} is {r.age_minutes:.0f}min old (freshness window "
            f"{FRESHNESS_WINDOW_MIN.get(s, DEFAULT_FRESHNESS_WINDOW_MIN)}min)"
            for s, r in readings.items()
        )
        checks.append({
            "check": "stale_evidence",
            "status": "accepted",
            "reason": f"No reporting source is within its own freshness window ({oldest_desc}). "
                      f"A discrepancy between entirely out-of-date readings isn't reliable evidence "
                      f"of a CURRENT conflict, so a trusted source is not asserted with normal "
                      f"confidence here.",
        })
        record["classification"] = "STALE_EVIDENCE"
        record["reasoning"] = (
            f"Sources disagree (spread {raw_spread}, tolerance {raw_tol}), but every one of them is "
            f"outside its own freshness window: {oldest_desc}. Refusing to auto-reconcile or escalate "
            f"on stale data -- refresh at least one source before this can be judged as a genuine, "
            f"CURRENT conflict."
        )
        record["trusted_source"] = None
        record["financial_impact_gbp"] = None
        record["action"] = "flag_for_review"
        record["action_detail"] = (
            "All evidence is stale. Prioritise refreshing a source over reconciling a value that "
            "may already be out of date again by the time anyone acts on it."
        )
        record["checks"] = checks
        return record

    # --- Check: INVENTORY_SEMANTICS -- do different meanings explain it? 
    has_sellable_source = any(SOURCE_MEASURES.get(s) == "sellable_available" for s in readings)
    if has_sellable_source and reserved_units > 0:
        freshness_note = (
            f"Reservations snapshot is {reservations_age_minutes:.0f} min old "
            f"(max trusted lag {RESERVATIONS_FRESHNESS_WINDOW_MIN} min)."
            if reservations_age_minutes is not None else
            "No reservations timestamp available; treated as trustworthy by default."
        )
        if spread <= tol:
            base_reason = (
                f"Raw spread of {raw_spread} looked like a conflict, but e-commerce measures "
                f"sellable-available, not physical on-hand. Normalising with {reserved_units} "
                f"reserved units gives physical-equivalent values of "
                f"{ {s: v for s, v in normalized.items()} }, which agree within tolerance "
                f"({spread} <= {tol})."
            )
            if reservations_fresh:
                checks.append({
                    "check": "inventory_semantics",
                    "status": "accepted",
                    "reason": base_reason + f" {freshness_note} Not a genuine conflict -- "
                              f"different questions, same answer.",
                })
                record["classification"] = "CONSISTENT"
                record["normalized_quantities"] = normalized
                record["reasoning"] = checks[-1]["reason"]
                record["trusted_source"] = None
                record["financial_impact_gbp"] = 0
                record["action"] = "no_action"
                record["action_detail"] = (
                    "No discrepancy once inventory semantics are accounted for. No action needed."
                )
                return finalize(record)
            else:
                # The arithmetic closes the gap, but the evidence doing the
                # closing is itself stale -- don't silently trust it.
                checks.append({
                    "check": "inventory_semantics",
                    "status": "accepted_pending_verification",
                    "reason": base_reason + f" {freshness_note} This is stale enough that the "
                              f"reservations figure itself might no longer reflect reality -- the "
                              f"explanation is plausible but not confirmed, so this isn't closed "
                              f"automatically.",
                })
                record["classification"] = "CONSISTENT"
                record["normalized_quantities"] = normalized
                record["reservations_age_minutes"] = round(reservations_age_minutes, 1)
                record["reasoning"] = checks[-1]["reason"]
                record["trusted_source"] = None
                record["financial_impact_gbp"] = 0
                record["action"] = "flag_for_review"
                record["action_detail"] = (
                    f"Quantities appear consistent once normalised for reservations, but the "
                    f"reservations snapshot is {reservations_age_minutes:.0f} min old (normal "
                    f"freshness window <= {RESERVATIONS_FRESHNESS_WINDOW_MIN} min) -- refresh reservations data and "
                    f"confirm before treating this as fully resolved."
                )
                return finalize(record)
        else:
            explained = raw_spread - spread
            checks.append({
                "check": "inventory_semantics",
                "status": "partially_explained" if explained > 0 else "rejected",
                "reason": (
                    f"{reserved_units} reserved units account for {explained} of the {raw_spread}-unit "
                    f"raw spread, but {spread} units remain unexplained even on a normalised basis. "
                    f"Reservations alone don't close the gap. {freshness_note}"
                ),
            })
    else:
        checks.append({
            "check": "inventory_semantics",
            "status": "rejected",
            "reason": "No sellable-available source with a nonzero reservation figure is involved "
                      "in this discrepancy, so differing inventory meanings don't apply here.",
        })

    # --- Check: GENUINE_CONFLICT -- score sources and pick a winner ---
    # Scored and reconciled on the NORMALISED (physical-equivalent) basis so
    # that a legitimate reservations gap isn't double-counted as conflict.
    #
    # Corroboration: does another independent source back this one up?
    # Computed pairwise on normalised values using the same tolerance
    # already used to detect the conflict, so "agreement" means the same
    # thing here as it did when deciding there was a discrepancy at all.
    corroboration = {}
    for s in readings:
        others = [o for o in readings if o != s]
        if not others:
            corroboration[s] = 0.0
            continue
        agreeing = sum(1 for o in others if abs(normalized[s] - normalized[o]) <= tol)
        corroboration[s] = agreeing / len(others)

    scores = {}
    for s, r in readings.items():
        recency_score = max(0.0, 1 - r.age_minutes / FRESHNESS_WINDOW_MIN.get(s, DEFAULT_FRESHNESS_WINDOW_MIN))
        hist_score = historical_accuracy.get(s, 0.75)
        biz_score = BUSINESS_RULE_BONUS.get(s, 0.0)
        corrob_score = corroboration[s]
        composite = (WEIGHT_RECENCY * recency_score
                     + WEIGHT_HISTORY * hist_score
                     + WEIGHT_BUSINESS * biz_score
                     + WEIGHT_CORROBORATION * corrob_score)
        scores[s] = {
            "recency_score": round(recency_score, 3),
            "historical_accuracy": round(hist_score, 3),
            "historical_sample_size": historical_counts.get(s, [0, 0])[1],
            "business_rule_bonus": biz_score,
            "corroboration_score": round(corrob_score, 3),
            "composite_score": round(composite, 3),
        }

    trusted = max(scores.items(), key=lambda kv: kv[1]["composite_score"])[0]
    trusted_norm_qty = normalized[trusted]
    impact = max(abs(trusted_norm_qty - v) * unit_cost for s, v in normalized.items() if s != trusted)

    checks.append({
        "check": "genuine_conflict",
        "status": "accepted",
        "reason": f"No timing or semantic explanation closes the {spread}-unit normalised gap "
                  f"(tolerance {tol}); treating as a real stock discrepancy.",
    })

    record["checks"] = checks
    record["classification"] = "GENUINE_CONFLICT"
    record["normalized_quantities"] = normalized
    record["trust_scores"] = scores
    record["trusted_source"] = trusted
    record["financial_impact_gbp"] = round(impact, 2)
    ranked = sorted(scores.items(), key=lambda kv: -kv[1]["composite_score"])
    runner_up = ranked[1] if len(ranked) > 1 else None
    reasoning = (
        f"All reporting sources are within their own normal freshness window, and normalising for "
        f"known inventory-meaning differences (reserved units: {reserved_units}) still leaves a "
        f"spread of {spread} units (tolerance {tol}) -- not explainable by lag or semantics. Chose "
        f"{trusted} as authoritative: composite score {scores[trusted]['composite_score']} "
        f"(recency {scores[trusted]['recency_score']}, historical accuracy "
        f"{scores[trusted]['historical_accuracy']} over {scores[trusted]['historical_sample_size']} "
        f"past cases, business-rule bonus {scores[trusted]['business_rule_bonus']}, corroboration "
        f"{scores[trusted]['corroboration_score']} -- fraction of other reporting sources whose "
        f"normalised value agrees with this one within tolerance)."
    )
    if runner_up:
        reasoning += (
            f" Runner-up was {runner_up[0]} at {runner_up[1]['composite_score']} "
            f"-- margin of {round(scores[trusted]['composite_score'] - runner_up[1]['composite_score'], 3)}."
        )
    record["reasoning"] = reasoning

    if impact < AUTO_RECONCILE_MAX_GBP:
        max_abs_diff = max(abs(trusted_norm_qty - v) for s, v in normalized.items() if s != trusted)
        if trusted_norm_qty > 0:
            rel_diff = max_abs_diff / trusted_norm_qty
        else:
            rel_diff = float("inf") if max_abs_diff > 0 else 0.0
        quantity_safe = (max_abs_diff <= AUTO_RECONCILE_MAX_ABS_QTY_DIFF
                          or rel_diff <= AUTO_RECONCILE_MAX_REL_QTY_DIFF)

        if not quantity_safe:
            # Caught by an adversarial review: a cheap SKU with a huge unit
            # gap (e.g. 10 vs 5,000 units at £0.01 each) stays under the £50
            # threshold on value alone, so financial impact by itself would
            # wave through a discrepancy that's operationally alarming in
            # raw quantity. Blocking requires BOTH the absolute count AND
            # the proportion to be large -- see the constants' comment.
            record["action"] = "flag_for_review"
            record["action_detail"] = (
                f"Impact £{impact:.2f} is below the £{AUTO_RECONCILE_MAX_GBP:.2f} auto-reconcile "
                f"threshold, but the quantity deviation itself is too large to correct automatically: "
                f"{max_abs_diff} units ({rel_diff:.0%} relative to {trusted}'s {trusted_norm_qty}) "
                f"exceeds both the absolute ({AUTO_RECONCILE_MAX_ABS_QTY_DIFF} units) and relative "
                f"({AUTO_RECONCILE_MAX_REL_QTY_DIFF:.0%}) safety guards. A discrepancy this large in "
                f"raw unit count deserves a human look even when the £ exposure is small."
            )
        else:
            corrections = []
            for s, r in readings.items():
                if s == trusted:
                    continue
                # Translate the trusted physical-equivalent value back onto
                # this source's own measure before writing it as a correction.
                if SOURCE_MEASURES.get(s) == "sellable_available":
                    new_qty = trusted_norm_qty - reserved_units
                else:
                    new_qty = trusted_norm_qty
                corrections.append({
                    "source": s,
                    "old_quantity": r.quantity,
                    "new_quantity": new_qty,
                })
            negative = [c for c in corrections if c["new_quantity"] < 0]
            if negative:
                # Caught by an adversarial review: if reserved_units exceeds the
                # trusted physical value, translating it back onto a
                # sellable-available source's own measure produces a negative
                # "corrected" quantity -- the exact thing the data_quality check
                # elsewhere refuses to treat as valid. Silently writing that would
                # contradict the agent's own rule. Refuse auto-reconcile instead;
                # a reservations figure that implies negative sellable stock is
                # itself suspect and needs a human, not a silent write.
                record["action"] = "flag_for_review"
                record["action_detail"] = (
                    f"Impact £{impact:.2f} would normally auto-reconcile, but translating {trusted}'s "
                    f"physical-equivalent value of {trusted_norm_qty} back onto "
                    f"{', '.join(c['source'] for c in negative)} via reservations ({reserved_units} "
                    f"reserved) would produce a negative quantity, which is invalid. Reservations data "
                    f"itself is suspect here -- flagging for manual review instead of writing an "
                    f"impossible correction."
                )
            else:
                record["action"] = "auto_reconcile"
                record["corrections"] = corrections
                record["action_detail"] = (
                    f"Impact £{impact:.2f} is below the £{AUTO_RECONCILE_MAX_GBP:.2f} auto-reconcile "
                    f"threshold. Correcting {', '.join(c['source'] for c in corrections)} to {trusted}'s "
                    f"physical-equivalent value of {trusted_norm_qty} units; see logs/corrections.jsonl "
                    f"for the applied correction record(s), no human action required."
                )
    elif impact >= ESCALATE_MIN_GBP:
        record["action"] = "escalate"
        record["action_detail"] = (
            f"Impact £{impact:.2f} meets/exceeds the £{ESCALATE_MIN_GBP:.2f} escalation "
            f"threshold. Notifying warehouse manager immediately rather than queuing for "
            f"routine review; recommended value is {trusted}'s {trusted_norm_qty} units pending sign-off."
        )
    else:
        record["action"] = "flag_for_review"
        record["action_detail"] = (
            f"Impact £{impact:.2f} is between the auto-reconcile and escalation thresholds. "
            f"Queued for manual review with {trusted}'s {trusted_norm_qty} units as the recommended value."
        )
    return record


# Run

def run():
    os.makedirs(LOG_DIR, exist_ok=True)
    crosswalk = load_crosswalk()
    wms = fetch_wms(crosswalk)
    ecom = fetch_ecommerce(crosswalk)
    tpl = fetch_tpl(crosswalk)
    historical_accuracy, historical_counts = load_historical_accuracy()
    reservations = load_reservations()

    print("Historical accuracy priors (from past reconciliation outcomes):")
    for s, acc in historical_accuracy.items():
        n = historical_counts.get(s, [0, 0])[1]
        print(f"  {s}: {acc:.2%} correct over {n} past cases")
    print()

    decisions = []
    for sku, cw in crosswalk.items():
        readings = {}
        if sku in wms:
            readings["WMS"] = wms[sku]
        if sku in ecom:
            readings["ECOMMERCE"] = ecom[sku]
        if sku in tpl:
            readings["TPL"] = tpl[sku]
        if not readings:
            continue
        decision = classify_and_decide(
            sku, readings, float(cw["unit_cost_gbp"]), cw["description"],
            historical_accuracy, historical_counts, reservations,
        )
        decisions.append(decision)

    log_path = os.path.join(LOG_DIR, "reconciliation_log.jsonl")
    with open(log_path, "w") as f:
        for d in decisions:
            f.write(json.dumps(d) + "\n")

    
    corrections_path = os.path.join(LOG_DIR, "corrections.jsonl")
    corrections_written = 0
    with open(corrections_path, "w") as f:
        for d in decisions:
            if d["action"] != "auto_reconcile":
                continue
            for c in d.get("corrections", []):
                f.write(json.dumps({
                    "case_id": d["case_id"],
                    "sku": d["sku"],
                    "timestamp": d["timestamp"],
                    "target_source": c["source"],
                    "old_quantity": c["old_quantity"],
                    "new_quantity": c["new_quantity"],
                    "reason": f"{d['case_id']}: reconciled to {d['trusted_source']}'s value",
                }) + "\n")
                corrections_written += 1

    summary_path = os.path.join(LOG_DIR, "reconciliation_summary.md")
    with open(summary_path, "w") as f:
        f.write(f"# Reconciliation run — {NOW.isoformat()}\n\n")
        f.write("| SKU | Description | Classification | Action | Trusted source | Impact (GBP) |\n")
        f.write("|---|---|---|---|---|---|\n")
        for d in decisions:
            f.write(
                f"| {d['sku']} | {d['description']} | {d['classification']} | {d['action']} | "
                f"{d.get('trusted_source') or '-'} | "
                f"{d['financial_impact_gbp'] if d.get('financial_impact_gbp') is not None else '-'} |\n"
            )
        f.write("\n---\n\n")
        for d in decisions:
            f.write(f"## {d['sku']} — {d['description']}\n\n")
            f.write(f"**Classification:** {d['classification']}  \n")
            f.write(f"**Action:** {d['action']}  \n")
            if d.get("trusted_source"):
                f.write(f"**Trusted source:** {d['trusted_source']}  \n")
            if d.get("financial_impact_gbp") is not None:
                f.write(f"**Financial impact:** £{d['financial_impact_gbp']}  \n")
            f.write("\n**Evidence considered:**\n\n")
            for e in d["evidence"]:
                f.write(
                    f"- `{e['source']}` ({e['raw_id']}, measures {e['measures']}): qty={e['quantity']}, "
                    f"updated {e['age_minutes']} min ago, "
                    f"{'within' if e['within_freshness_window'] else 'OUTSIDE'} its normal "
                    f"{e['freshness_window_minutes']}min freshness window\n"
                )
            if d.get("data_quality_note"):
                f.write(f"\n> Data quality note: {d['data_quality_note']}\n")
            if d.get("checks"):
                f.write("\n**Checks performed:**\n\n")
                for h in d["checks"]:
                    mark = "✓" if h["status"] not in ("rejected",) else "✗"
                    f.write(f"- {mark} `{h['check']}` -- **{h['status']}**: {h['reason']}\n")
            if d.get("corrections"):
                f.write("\n**Corrections applied** (see `logs/corrections.jsonl`):\n\n")
                for c in d["corrections"]:
                    f.write(f"- `{c['source']}`: {c['old_quantity']} -> {c['new_quantity']}\n")
            f.write(f"\n**Reasoning:** {d['reasoning']}\n\n")
            f.write(f"**Action detail:** {d['action_detail']}\n\n---\n\n")

    print(f"Processed {len(decisions)} SKUs.")
    print(f"JSON log:      {log_path}")
    print(f"Corrections:   {corrections_path} ({corrections_written} correction record(s) written)")
    print(f"Human summary: {summary_path}\n")
    counts = {}
    for d in decisions:
        counts[d["classification"]] = counts.get(d["classification"], 0) + 1
    print("Classification breakdown:", counts)
    action_counts = {}
    for d in decisions:
        action_counts[d["action"]] = action_counts.get(d["action"], 0) + 1
    print("Action breakdown:", action_counts)


if __name__ == "__main__":
    run()
