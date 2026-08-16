"""
Lightweight sanity tests for the reconciliation logic. Run with:
    python -m pytest tests/ -v
or, with no pytest available:
    python tests/test_agent.py
"""
import sys
import os
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import agent  # noqa: E402
from agent import Reading, classify_and_decide, NOW, AUTO_RECONCILE_MAX_GBP  # noqa: E402


def mk(source, qty, minutes_ago):
    return Reading(source, f"raw-{source}", qty, NOW - timedelta(minutes=minutes_ago))


def default_history():
    return {"WMS": 0.9, "ECOMMERCE": 0.7, "TPL": 0.8}, {"WMS": [27, 30], "ECOMMERCE": [21, 30], "TPL": [24, 30]}


def test_consistent():
    hist, counts = default_history()
    readings = {"WMS": mk("WMS", 10, 5), "ECOMMERCE": mk("ECOMMERCE", 10, 10), "TPL": mk("TPL", 11, 20)}
    d = classify_and_decide("SKU-X", readings, 10.0, "Test Item", hist, counts)
    assert d["classification"] == "CONSISTENT", d
    assert d["action"] == "no_action", d


def test_timing_lag_detected_not_flagged_as_conflict():
    hist, counts = default_history()
    readings = {
        "WMS": mk("WMS", 50, 2),
        "ECOMMERCE": mk("ECOMMERCE", 50, 5),
        "TPL": mk("TPL", 30, 26 * 60),  # a full day stale, outside its own cadence
    }
    d = classify_and_decide("SKU-X", readings, 10.0, "Test Item", hist, counts)
    assert d["classification"] == "TIMING_LAG", d
    assert d["action"] == "monitor", d


def test_genuine_conflict_escalates_on_high_impact():
    hist, counts = default_history()
    readings = {
        "WMS": mk("WMS", 10, 3),
        "ECOMMERCE": mk("ECOMMERCE", 60, 10),  # big, fresh, disagreement
        "TPL": mk("TPL", 11, 15),
    }
    d = classify_and_decide("SKU-X", readings, 100.0, "Expensive Item", hist, counts)
    assert d["classification"] == "GENUINE_CONFLICT", d
    assert d["trusted_source"] == "WMS", d  # agrees with TPL + business-rule bonus
    assert d["action"] == "escalate", d


def test_genuine_conflict_auto_reconciles_on_low_impact():
    hist, counts = default_history()
    readings = {
        "WMS": mk("WMS", 10, 3),
        "ECOMMERCE": mk("ECOMMERCE", 7, 10),
        "TPL": mk("TPL", 10, 15),
    }
    d = classify_and_decide("SKU-X", readings, 1.0, "Cheap Item", hist, counts)
    assert d["classification"] == "GENUINE_CONFLICT", d
    assert d["action"] == "auto_reconcile", d


def test_negative_quantity_excluded_and_flagged():
    hist, counts = default_history()
    readings = {
        "WMS": mk("WMS", 10, 3),
        "ECOMMERCE": mk("ECOMMERCE", 10, 10),
        "TPL": mk("TPL", -4, 15),
    }
    d = classify_and_decide("SKU-X", readings, 10.0, "Buggy Feed Item", hist, counts)
    assert "data_quality_note" in d, d
    assert d["action"] == "flag_for_review", d


def test_insufficient_evidence_with_single_source():
    hist, counts = default_history()
    readings = {"WMS": mk("WMS", 10, 3)}
    d = classify_and_decide("SKU-X", readings, 10.0, "Lonely Item", hist, counts)
    assert d["classification"] == "INSUFFICIENT_EVIDENCE", d


def test_inventory_semantics_fully_explains_apparent_conflict():
    """Raw numbers look like a 3-way conflict; reservations fully close the gap."""
    hist, counts = default_history()
    readings = {
        "WMS": mk("WMS", 50, 5),
        "ECOMMERCE": mk("ECOMMERCE", 42, 15),  # sellable = physical - reserved
        "TPL": mk("TPL", 49, 40),
    }
    d = classify_and_decide("SKU-X", readings, 10.0, "SSD", hist, counts, reservations={"SKU-X": (8, NOW)})
    assert d["classification"] == "CONSISTENT", d
    assert d["action"] == "no_action", d
    names = [h["check"] for h in d["checks"]]
    assert "inventory_semantics" in names, d
    sem = next(h for h in d["checks"] if h["check"] == "inventory_semantics")
    assert sem["status"] == "accepted", sem


def test_inventory_semantics_only_partially_explains_conflict():
    """Reservations explain some of the gap but a genuine conflict remains."""
    hist, counts = default_history()
    readings = {
        "WMS": mk("WMS", 30, 8),
        "ECOMMERCE": mk("ECOMMERCE", 18, 35),
        "TPL": mk("TPL", 32, 50),
    }
    d = classify_and_decide("SKU-X", readings, 10.0, "Monitor Stand", hist, counts, reservations={"SKU-X": (2, NOW)})
    assert d["classification"] == "GENUINE_CONFLICT", d
    sem = next(h for h in d["checks"] if h["check"] == "inventory_semantics")
    assert sem["status"] == "partially_explained", sem
    # financial impact should reflect the NORMALISED (reservation-adjusted) gap, not the raw one
    raw_gap_impact = abs(30 - 18) * 10.0
    assert d["financial_impact_gbp"] < raw_gap_impact, d


def test_stale_reservations_flagged_not_silently_closed():
    """Arithmetic fully explains the gap, but the reservations snapshot is
    hours old -- the agent should flag for confirmation, not auto-close."""
    hist, counts = default_history()
    readings = {
        "WMS": mk("WMS", 30, 6),
        "ECOMMERCE": mk("ECOMMERCE", 24, 20),
        "TPL": mk("TPL", 29, 45),
    }
    stale_as_of = NOW - timedelta(hours=5)
    d = classify_and_decide("SKU-X", readings, 10.0, "Pencil Set", hist, counts,
                             reservations={"SKU-X": (6, stale_as_of)})
    assert d["classification"] == "CONSISTENT", d
    assert d["action"] == "flag_for_review", d
    sem = next(h for h in d["checks"] if h["check"] == "inventory_semantics")
    assert sem["status"] == "accepted_pending_verification", sem


def test_corroboration_beats_isolated_business_rule_favourite():
    """
    The exact failure mode a reviewer flagged: WMS gets a business-rule
    bonus, but that must not be enough to beat two OTHER independent
    sources that agree closely with each other while WMS stands alone.
    """
    hist, counts = default_history()
    readings = {
        "WMS": mk("WMS", 5, 4),          # isolated, no other source agrees
        "ECOMMERCE": mk("ECOMMERCE", 40, 20),
        "TPL": mk("TPL", 42, 35),        # ECOMMERCE and TPL agree within 2 units
    }
    d = classify_and_decide("SKU-X", readings, 10.0, "Wrist Rest", hist, counts)
    assert d["classification"] == "GENUINE_CONFLICT", d
    assert d["trusted_source"] in ("ECOMMERCE", "TPL"), (
        f"WMS should not win against two corroborating sources: {d['trust_scores']}"
    )
    assert d["trust_scores"]["WMS"]["corroboration_score"] == 0.0, d["trust_scores"]
    assert d["trust_scores"][d["trusted_source"]]["corroboration_score"] > 0, d["trust_scores"]


def test_all_stale_disagreeing_flags_instead_of_false_confidence():
    """
    Adversarial finding: if every source is outside its own freshness
    window and they still disagree, the agent must not confidently pick a
    "trusted" source and escalate as if this were normal, current evidence.
    """
    hist, counts = default_history()
    readings = {
        "WMS": mk("WMS", 10, 500),
        "ECOMMERCE": mk("ECOMMERCE", 40, 1000),
        "TPL": mk("TPL", 42, 2000),
    }
    d = classify_and_decide("SKU-X", readings, 50.0, "Everything Stale", hist, counts)
    assert d["classification"] == "STALE_EVIDENCE", d
    assert d["action"] == "flag_for_review", d
    assert d["trusted_source"] is None, d


def test_reservation_translation_never_writes_negative_correction():
    """
    Adversarial finding: translating a trusted physical value back onto a
    sellable-available source via (trusted - reserved_units) can go
    negative if reservations exceed the trusted stock. That must not be
    silently written as a correction -- it should refuse auto_reconcile.
    """
    hist, counts = default_history()
    readings = {
        "WMS": mk("WMS", 5, 3),
        "ECOMMERCE": mk("ECOMMERCE", 30, 5),
        "TPL": mk("TPL", 5, 4),
    }
    d = classify_and_decide("SKU-X", readings, 0.5, "Over-reserved SKU", hist, counts,
                             reservations={"SKU-X": (40, NOW)})
    assert d["classification"] == "GENUINE_CONFLICT", d
    assert d["action"] != "auto_reconcile", d
    for c in d.get("corrections", []):
        assert c["new_quantity"] >= 0, d


def test_unregistered_source_does_not_crash():
    """Adversarial finding: a source name outside the static config dicts
    must degrade gracefully (conservative default freshness window),
    never raise."""
    hist, counts = default_history()
    readings = {
        "WMS": mk("WMS", 10, 20),
        "NEWSOURCE": mk("NEWSOURCE", 30, 5),
    }
    d = classify_and_decide("SKU-X", readings, 10.0, "Unregistered Source", hist, counts)
    assert d["classification"] in (
        "CONSISTENT", "TIMING_LAG", "GENUINE_CONFLICT", "STALE_EVIDENCE",
    ), d


def test_quantity_deviation_guard_blocks_cheap_sku_huge_gap():
    """
    Adversarial finding: a cheap SKU with a huge unit-count gap stays under
    the £ auto-reconcile threshold on value alone. Auto-reconcile must also
    require the quantity deviation itself to be bounded, not just cheap.
    """
    hist, counts = default_history()
    readings = {
        "WMS": mk("WMS", 10, 5),
        "ECOMMERCE": mk("ECOMMERCE", 5000, 10),
        "TPL": mk("TPL", 12, 8),
    }
    d = classify_and_decide("SKU-X", readings, 0.01, "Cheap SKU Huge Gap", hist, counts)
    assert d["classification"] == "GENUINE_CONFLICT", d
    assert d["financial_impact_gbp"] < AUTO_RECONCILE_MAX_GBP, d  # confirms it WOULD pass on £ alone
    assert d["action"] != "auto_reconcile", d
    assert d["action"] == "flag_for_review", d


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    if failed:
        print(f"\n{failed} test(s) failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed")
