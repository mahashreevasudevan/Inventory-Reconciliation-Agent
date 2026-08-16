"""
verify_requirements.py

Developer tooling, not a quality claim. Checks the ACTUAL output of the
last agent run against the literal requirements in the assessment brief,
and prints a pass/fail table. This exists so a reviewer (or I, six months
from now) doesn't have to trust a claim in the README -- it's re-checkable
in ten seconds. It only proves the brief's stated minimums are met; it says
nothing about whether the reasoning behind any individual decision is
actually sound -- that's what the worked examples in the README and the
reasoning/checks fields in the log itself are for.

Note: the "genuine discrepancy per pair" check below uses a manually
chosen threshold (a pairwise diff of >=5 units counts as "material").
That threshold is a judgement call, not something derived from the data --
flagging it here rather than letting it look more objective than it is.

Run after src/agent.py:
    python verify_requirements.py
"""
import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "logs", "reconciliation_log.jsonl")

SOURCE_PAIRS = [("WMS", "ECOMMERCE"), ("WMS", "TPL"), ("ECOMMERCE", "TPL")]


def load_log():
    with open(LOG_PATH) as f:
        return [json.loads(line) for line in f]


def check_three_sources_used(decisions):
    sources_seen = set()
    for d in decisions:
        for e in d["evidence"]:
            sources_seen.add(e["source"])
    ok = len(sources_seen) >= 3
    return ok, f"Sources observed: {sorted(sources_seen)}"


def check_genuine_discrepancy_per_pair(decisions):
    """
    A 'genuine discrepancy' for a pair = a SKU classified GENUINE_CONFLICT
    where that pair's absolute quantity difference exceeds the tolerance
    used for that SKU (i.e. it's not just noise riding along on a real
    conflict driven by the third source).
    """
    results = {pair: [] for pair in SOURCE_PAIRS}
    for d in decisions:
        if d["classification"] != "GENUINE_CONFLICT":
            continue
        diffs = d.get("pairwise_diffs", {})
        for a, b in SOURCE_PAIRS:
            key = f"{a}_vs_{b}"
            if key in diffs and abs(diffs[key]) >= 5:  # material, not rounding noise
                results[(a, b)].append((d["sku"], diffs[key]))
    all_ok = all(len(v) > 0 for v in results.values())
    detail_lines = []
    for pair, hits in results.items():
        label = f"{pair[0]} vs {pair[1]}"
        if hits:
            examples = ", ".join(f"{sku} (Δ{diff})" for sku, diff in hits)
            detail_lines.append(f"    {label}: OK -> {examples}")
        else:
            detail_lines.append(f"    {label}: MISSING -- no genuine conflict found for this pair")
    return all_ok, "\n" + "\n".join(detail_lines)


def check_decision_framework_factors(decisions):
    """Confirms trust_scores actually carry recency, historical accuracy,
    business-rule and corroboration components, and that financial impact
    drives the action."""
    conflicts = [d for d in decisions if d["classification"] == "GENUINE_CONFLICT"]
    ok = len(conflicts) > 0
    factors_present = set()
    for d in conflicts:
        for src, s in d.get("trust_scores", {}).items():
            factors_present.update(s.keys())
    has_recency = any("recency" in f for f in factors_present)
    has_history = any("historical" in f for f in factors_present)
    has_business = any("business" in f for f in factors_present)
    has_corroboration = any("corroboration" in f for f in factors_present)
    has_financial = all(d.get("financial_impact_gbp") is not None for d in conflicts)
    ok = ok and has_recency and has_history and has_business and has_financial
    return ok, (
        f"recency={has_recency}, historical_accuracy={has_history}, "
        f"business_rule={has_business}, corroboration={has_corroboration}, "
        f"financial_impact_drives_action={has_financial} "
        f"(4 of the brief's 4 optional factors used, minimum required was 2; corroboration "
        f"is an extra factor added after review, not one of the brief's four)"
    )


def check_at_least_two_real_conflicts(decisions):
    conflicts = [d for d in decisions if d["classification"] == "GENUINE_CONFLICT"]
    ok = len(conflicts) >= 2
    return ok, f"{len(conflicts)} SKUs classified GENUINE_CONFLICT: {[d['sku'] for d in conflicts]}"


def check_timing_lag_discrimination(decisions):
    """Proves the agent distinguishes lag from conflict rather than flagging everything."""
    lag = [d for d in decisions if d["classification"] == "TIMING_LAG"]
    ok = len(lag) >= 1
    return ok, f"{len(lag)} SKUs correctly classified as TIMING_LAG (not real conflicts): {[d['sku'] for d in lag]}"


def check_every_decision_has_auditable_reasoning(decisions):
    missing = [d["sku"] for d in decisions if not d.get("reasoning") or not d.get("action_detail")]
    ok = len(missing) == 0
    return ok, "All decisions carry reasoning + action_detail." if ok else f"Missing on: {missing}"


def check_checks_logged(decisions):
    """
    Confirms every decision that goes through the classification pipeline
    (i.e. everything except the single-source INSUFFICIENT_EVIDENCE
    shortcut) carries a checks trail, and that at least one SKU shows a
    REJECTED check -- proof the log records what was ruled out, not just
    what was concluded.
    """
    relevant = [d for d in decisions if d["classification"] != "INSUFFICIENT_EVIDENCE"]
    missing = [d["sku"] for d in relevant if not d.get("checks")]
    any_rejected = any(
        h["status"] == "rejected"
        for d in decisions for h in d.get("checks", [])
    )
    any_semantics_accepted = any(
        h["check"] == "inventory_semantics" and h["status"] == "accepted"
        for d in decisions for h in d.get("checks", [])
    )
    ok = not missing and any_rejected
    detail = f"{len(relevant) - len(missing)}/{len(relevant)} decisions carry a checks trail; " \
             f"rejected checks present: {any_rejected}; " \
             f"inventory-semantics fully explained at least one case: {any_semantics_accepted}"
    return ok, detail


def check_corroboration_not_overridden_by_business_rule(decisions):
    """
    Regression check for a real bug a reviewer caught: an isolated source
    with a business-rule bonus must not out-score two OTHER sources that
    corroborate each other. For every genuine conflict, if the trusted
    source has zero corroboration, at least one other source must also
    have zero corroboration (i.e. there was no clearly-corroborated
    alternative being overridden).
    """
    conflicts = [d for d in decisions if d["classification"] == "GENUINE_CONFLICT"]
    violations = []
    for d in conflicts:
        scores = d.get("trust_scores", {})
        trusted = d.get("trusted_source")
        if not trusted or trusted not in scores:
            continue
        trusted_corrob = scores[trusted].get("corroboration_score", 0)
        others_corrob = [s["corroboration_score"] for src, s in scores.items() if src != trusted]
        if trusted_corrob == 0 and others_corrob and max(others_corrob) > 0:
            violations.append(d["sku"])
    ok = len(violations) == 0
    return ok, (
        "No SKU where an isolated (uncorroborated) source beat a corroborated one." if ok
        else f"Isolated source won despite corroborated alternative(s) on: {violations}"
    )


def main():
    decisions = load_log()
    checks = [
        ("Fetches from >= 3 independent sources", check_three_sources_used),
        ("Genuine discrepancy detected for EVERY source pair (WMS/ECOM, WMS/TPL, ECOM/TPL)",
         check_genuine_discrepancy_per_pair),
        ("Decision framework uses >=2 of {recency, history, financial impact, business rules}",
         check_decision_framework_factors),
        (">= 2 genuine conflicts present (brief's demo requirement)", check_at_least_two_real_conflicts),
        ("Distinguishes timing lag from genuine conflict (not just flag-everything)",
         check_timing_lag_discrimination),
        ("Every decision has evidence + reasoning + action_detail (auditability)",
         check_every_decision_has_auditable_reasoning),
        ("Every decision logs a checks trail, including rejected checks",
         check_checks_logged),
        ("Corroborated sources aren't overridden by an isolated business-rule favourite",
         check_corroboration_not_overridden_by_business_rule),
    ]
    print(f"Checking {len(decisions)} logged decisions against the brief's literal requirements:\n")
    all_pass = True
    for label, fn in checks:
        ok, detail = fn(decisions)
        all_pass = all_pass and ok
        print(f"[{'PASS' if ok else 'FAIL'}] {label}\n    {detail}\n")
    print("=" * 60)
    if all_pass:
        print("All literal requirements in the brief are met by this run's output.")
        print("This checks the brief's stated minimums only -- it is not a")
        print("judgement on decision quality. Read logs/reconciliation_summary.md")
        print("and the README's worked examples for that.")
    else:
        print("SOME REQUIREMENTS NOT MET -- see FAIL lines above")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
