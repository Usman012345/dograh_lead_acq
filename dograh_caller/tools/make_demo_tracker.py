#!/usr/bin/env python3
"""
Generate a fictional demo pipeline_tracker.xlsx (~60 leads) for the dashboard.

All business names contain "Fictional" and all US phone numbers use 555-01xx.
Refuses to overwrite the live output/pipeline_tracker.xlsx file.

Usage:
    python tools/make_demo_tracker.py --out output/demo_tracker.xlsx
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from decision_maker import Decision, Service, ServiceScore  # noqa: E402
from dograh_client import Agent, CallOutcome  # noqa: E402
from number_verifier import Verification  # noqa: E402
from pipeline_tracker import PipelineTracker  # noqa: E402

SERVICES = {
    "draftliner": Service(
        id="draftliner",
        name="Draftliner",
        agent_id=2,
        description="Remote AEC drafting and BIM teams.",
        good_fit="",
        not_a_fit="",
    ),
    "paradise": Service(
        id="paradise",
        name="Paradise Estimating",
        agent_id=3,
        description="Construction estimating and takeoffs.",
        good_fit="",
        not_a_fit="",
    ),
}

AGENTS = {
    "draftliner": Agent(id=2, name="Danial - Draftliner Outbound", version_status="published"),
    "paradise": Agent(id=3, name="Olivia - Paradise Outbound", version_status="published"),
}


def _make_decision(draft_pct: int, para_pct: int, draft_reason: str, para_reason: str, summary: str) -> Decision:
    d_score = ServiceScore("draftliner", "Draftliner", draft_pct / 100.0, draft_reason)
    p_score = ServiceScore("paradise", "Paradise Estimating", para_pct / 100.0, para_reason)
    scores = [d_score, p_score]
    best_score = max(scores, key=lambda s: s.probability)
    best_svc = SERVICES[best_score.service_id]
    assigned = best_svc if best_score.probability >= 0.80 else None
    pct_str = f"{best_score.probability:.0%}"
    if assigned is not None:
        verdict = f"Qualified for {assigned.name} at {pct_str} (the requirement is 80%)."
    else:
        verdict = f"Non-Qualified: the best match was {best_svc.name} at {pct_str}, below the 80% requirement."
    return Decision(
        probabilities={"draftliner": d_score.probability, "paradise": p_score.probability},
        service=assigned,
        probability=best_score.probability,
        scores=scores,
        best_service=best_svc,
        summary=summary,
        verdict=verdict,
        threshold=0.80,
        model="gemini-3.8-flash",
    )


def generate_demo_tracker(out_path: Path) -> int:
    resolved = out_path.resolve()
    if resolved == config.PIPELINE_TRACKER_FILE.resolve():
        raise ValueError(
            f"Refusing to overwrite live tracker at {config.PIPELINE_TRACKER_FILE}. "
            "Pass a separate path such as output/demo_tracker.xlsx."
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    for p in (out_path, out_path.with_name(f".{out_path.stem}.lock")):
        if p.exists():
            p.unlink()

    tracker = PipelineTracker(out_path)
    tracker.open()
    try:
        count = 0

        def add_lead(lead: Dict[str, Any], source: str = "streamer") -> str:
            nonlocal count
            count += 1
            return tracker.register(lead, source)

        # 1. Interrupted lead (from an earlier stopped run; mark interrupted BEFORE adding active/waiting leads!)
        lid_int = add_lead({"lead_id": "demo-int-1", "company": "Fictional Interrupted Steel", "category": "Structural Steel", "phone": "+15125550150"})
        tracker.record_verification(lid_int, Verification(True, "+15125550150", "+15125550150", "ok", "Texas", "America/Chicago"))
        tracker.record_decision(lid_int, _make_decision(
            86, 65, "Produces steel shop drawings.", "Secondary takeoff fit.",
            "Fictional Interrupted Steel fabricates steel and produces shop drawings. Fits Draftliner.",
        ))
        tracker.record_campaign(lid_int, AGENTS["draftliner"], 101, "Draftliner_2026-11-23")
        tracker.mark_unfinished_interrupted()

        # 2. Incoming (waiting for verifier) - 2 leads
        for i in range(1, 3):
            add_lead({
                "lead_id": f"demo-inc-{i}",
                "company": f"Fictional Incoming Logistics {i}",
                "category": "General Contractors",
                "city": "Austin",
                "state": "TX",
                "phone": f"(512) 555-01{10 + i:02d}",
                "source": "bbb",
            })

        # 3. Number rejected (all 8 drop reasons + verifier error) - 9 leads
        drop_cases = [
            ("Fictional NoPhone Roofing", "", None, "dropped_no_phone", "", ""),
            ("Fictional FaxOnly Masonry", "(512) 555-0115 (fax)", None, "dropped_fax_only", "", ""),
            ("Fictional BadNumber Plumbing", "555-123", None, "dropped_invalid_number", "", ""),
            ("Fictional Unreadable HVAC", "call main office", None, "dropped_unreadable_number", "", ""),
            ("Fictional Maple Leaf Builders", "+1 416 555 0107", None, "dropped_not_us (CA)", "", ""),
            ("Fictional Toll900 Electric", "1-900-555-0199", None, "dropped_special_number (PREMIUM_RATE)", "", ""),
            ("Fictional Repeat Concrete", "(512) 555-0116", "+15125550116", "dropped_already_called", "Texas", "America/Chicago"),
            ("Fictional Dup Framing", "(512) 555-0117", "+15125550117", "dropped_duplicate", "Texas", "America/Chicago"),
        ]
        for idx, (name, raw, e164, reason, loc, tz) in enumerate(drop_cases, start=1):
            lid = add_lead({
                "lead_id": f"demo-drop-{idx}",
                "company": name,
                "category": "Specialty Contractor",
                "phone": raw,
                "source": "googlemaps",
            })
            tracker.record_verification(lid, Verification(False, raw, e164, reason, loc, tz))

        lid_err = add_lead({
            "lead_id": "demo-verr-1",
            "company": "Fictional Verifier Error Co",
            "category": "Drywall",
            "phone": "(512) 555-0118",
        })
        tracker.record_finished(lid_err, "error", "verifier: unexpected parse state")

        # 3. Awaiting decision (passed verification) - 2 leads
        for i in range(1, 3):
            phone = f"+151255501{20 + i:02d}"
            lid = add_lead({
                "lead_id": f"demo-await-{i}",
                "company": f"Fictional Awaiting Decision {i}",
                "category": "Commercial Framing",
                "city": "Dallas",
                "state": "TX",
                "phone": phone,
            })
            tracker.record_verification(lid, Verification(True, phone, phone, "ok", "Texas", "America/Chicago"))

        # 4. Decision error (Gemini failed, saved for retry) - 2 leads
        for i in range(1, 3):
            phone = f"+130355501{20 + i:02d}"
            lid = add_lead({
                "lead_id": f"demo-derr-{i}",
                "company": f"Fictional Decision Retry {i}",
                "category": "Structural Steel",
                "city": "Denver",
                "state": "CO",
                "phone": phone,
            })
            tracker.record_verification(lid, Verification(True, phone, phone, "ok", "Colorado", "America/Denver"))
            tracker.record_decision_error(lid, "HTTP 503 Service Unavailable after retries")

        # 5. Non-Qualified leads (<80%): 18 leads including 79% boundary, <script> XSS test, javascript: link, >35k description
        long_desc = "Commercial sitework and excavation specifications. " * 750  # ~38,250 chars
        nq_specs = [
            (
                "Fictional Borderline Roofing 79",
                "Roofing Contractor",
                "Bids light residential and occasional commercial roofs.",
                "javascript:alert(1)",
                28, 79,
                "Category mentions roofing without CAD or BIM scope.",
                "Plausible roofing fit, but lacks explicit commercial bidding evidence.",
                "Fictional Borderline Roofing 79 installs roofs. Missing commercial bidding proof keeps its top score at 79%.",
            ),
            (
                "Fictional Script Safety Bistro",
                "Restaurant",
                'Neighborhood cafe <script>alert("xss")</script> serving lunch daily.',
                "https://fictional-bistro.example",
                2, 4,
                "Food service business outside AEC.",
                "Restaurant does not bid construction projects.",
                "Fictional Script Safety Bistro operates a restaurant. It is outside the construction and design market.",
            ),
            (
                "Fictional LongSpec Excavation",
                "Excavation & Grading",
                long_desc,
                "https://fictional-longspec.example",
                18, 72,
                "Heavy civil crew without dedicated drafting scope.",
                "Large spec text lacks explicit estimating or takeoff outsource indicators.",
                "Fictional LongSpec Excavation handles earthwork and grading. Evidence is mixed for outsourced estimating.",
            ),
        ]
        generic_nq = [
            ("Fictional Lawn Care Pros", "Landscaping Maintenance", "Weekly mowing and fertilization.", 1, 5),
            ("Fictional Handyman Express", "Handyman Services", "Small home repairs and odd jobs.", 4, 12),
            ("Fictional Sparkle Cleaners", "Janitorial", "Office cleaning crews.", 0, 2),
            ("Fictional Metro Pest Control", "Pest Control", "Residential pest treatments.", 0, 1),
            ("Fictional Oak Street Bakery", "Bakery", "Artisan breads and pastries.", 0, 0),
            ("Fictional Valley Real Estate", "Real Estate Broker", "Residential home sales.", 5, 8),
            ("Fictional Quick Appliance Fix", "Appliance Repair", "Washer and dryer repairs.", 2, 6),
            ("Fictional Riverbend Moving", "Moving Company", "Local residential movers.", 1, 3),
            ("Fictional Pinecrest Hardware", "Building Supply Retail", "Sells lumber and tools only.", 10, 22),
            ("Fictional Sunrise Dental", "Dentist", "Family dental clinic.", 0, 0),
            ("Fictional Cedar Gutter Wash", "Gutter Cleaning", "Residential gutter washouts.", 3, 19),
            ("Fictional Harbor Auto Glass", "Auto Repair", "Windshield replacements.", 0, 1),
            ("Fictional City Print Shop", "Commercial Printing", "Banners and flyers.", 8, 4),
            ("Fictional Midtown Yoga", "Fitness Studio", "Yoga classes.", 0, 0),
            ("Fictional Beacon Home Inspections", "Home Inspector", "Pre-purchase home walkthroughs.", 14, 38),
        ]
        for idx, spec in enumerate(nq_specs, start=1):
            name, cat, desc, web, d_pct, p_pct, d_r, p_r, summ = spec
            phone = f"+151255501{30 + idx:02d}"
            lid = add_lead({
                "lead_id": f"demo-nq-spec-{idx}",
                "company": name,
                "category": cat,
                "description": desc,
                "website": web,
                "city": "Austin",
                "state": "TX",
                "phone": phone,
                "source": "bbb",
            })
            tracker.record_verification(lid, Verification(True, phone, phone, "ok", "Texas", "America/Chicago"))
            tracker.record_decision(lid, _make_decision(d_pct, p_pct, d_r, p_r, summ))

        for idx, (name, cat, desc, d_pct, p_pct) in enumerate(generic_nq, start=1):
            phone = f"+121455501{10 + idx:02d}"
            lid = add_lead({
                "lead_id": f"demo-nq-{idx}",
                "company": name,
                "category": cat,
                "description": desc,
                "city": "Dallas",
                "state": "TX",
                "phone": phone,
                "source": "googlemaps",
            })
            tracker.record_verification(lid, Verification(True, phone, phone, "ok", "Texas", "America/Chicago"))
            tracker.record_decision(
                lid,
                _make_decision(
                    d_pct,
                    p_pct,
                    f"{cat} shows minimal drafting need.",
                    f"{cat} does not regularly bid jobs from drawings.",
                    f"{name} operates in {cat.lower()}. Neither AEC drafting nor estimating aligns strongly.",
                ),
            )

        # 6. Qualified leads (>=80%): 27 leads covering all calling_agent sub-stages and call_results outcomes
        qualified_waiting = [
            ("Fictional Summit MEP Engineering", "MEP Engineering", "draftliner", 94, 15,
             "Explicit MEP engineering and Revit coordination.", "Design firm rather than trade bidder.",
             "Fictional Summit MEP Engineering designs building systems in Revit. Strong match for Draftliner.", "+12155550141", "Pennsylvania", "America/New_York"),
            ("Fictional Horizon Concrete 80", "Concrete Subcontractor", "paradise", 30, 80,
             "No drafting staff indicators.", "Bids commercial concrete packages from drawings.",
             "Fictional Horizon Concrete 80 bids commercial concrete work. Meets estimating need for Paradise Estimating.", "+15125550142", "Texas", "America/Chicago"),
            ("Fictional Skyline Architecture", "Architecture Firm", "draftliner", 91, 12,
             "Produces architectural CAD and permit sets.", "Architecture studio, not a trade bidder.",
             "Fictional Skyline Architecture produces permit and CAD sets. Ideal fit for Draftliner remote staff.", "+13125550143", "Illinois", "America/Chicago"),
        ]
        for idx, (name, cat, _svc_id, dp, pp, dr, pr, summ, phone, loc, tz) in enumerate(qualified_waiting, start=1):
            lid = add_lead({
                "lead_id": f"demo-qw-{idx}",
                "company": name,
                "category": cat,
                "phone": phone,
                "city": "Metro",
                "state": loc[:2].upper(),
            })
            tracker.record_verification(lid, Verification(True, phone, phone, "ok", loc, tz))
            tracker.record_decision(lid, _make_decision(dp, pp, dr, pr, summ))

        calling_now = [
            ("Fictional Atlas Structural BIM", "Structural Engineering", "draftliner", 93, 20, "+14045550144", "Georgia", "America/New_York", 101, 4101),
            ("Fictional LoneStar Masonry Bids", "Masonry Contractor", "paradise", 22, 89, "+17135550145", "Texas", "America/Chicago", 102, 4102),
            ("Fictional Crestline Design-Build", "Design-Build GC", "draftliner", 87, 74, "+16025550146", "Arizona", "America/Phoenix", 101, 4103),
        ]
        for idx, (name, cat, svc_id, dp, pp, phone, loc, tz, cid, rid) in enumerate(calling_now, start=1):
            lid = add_lead({"lead_id": f"demo-call-{idx}", "company": name, "category": cat, "phone": phone})
            tracker.record_verification(lid, Verification(True, phone, phone, "ok", loc, tz))
            tracker.record_decision(lid, _make_decision(
                dp, pp,
                f"Design and CAD activity in {cat}.",
                f"Bidding workload in {cat}.",
                f"{name} works in {cat}. Clear fit for {SERVICES[svc_id].name}.",
            ))
            tracker.record_campaign(lid, AGENTS[svc_id], cid, f"{SERVICES[svc_id].name}_2026-11-23")
            tracker.record_call_started(lid, rid, 1)

        # not_sent (skipped_missing_data) - 2 leads
        for idx, (name, svc_id, phone) in enumerate([
            ("Fictional MissingCity Framing", "paradise", "+15125550147"),
            ("Fictional MissingScope Studio", "draftliner", "+15125550148"),
        ], start=1):
            lid = add_lead({"lead_id": f"demo-ns-{idx}", "company": name, "category": "Construction", "phone": phone})
            tracker.record_verification(lid, Verification(True, phone, phone, "ok", "Texas", "America/Chicago"))
            dp, pp = (88, 30) if svc_id == "draftliner" else (25, 88)
            tracker.record_decision(lid, _make_decision(
                dp, pp, "Drafting fit.", "Estimating fit.",
                f"{name} matches {SERVICES[svc_id].name}. Required prompt variable was absent.",
            ))
            tracker.record_finished(lid, "skipped_missing_data", "Missing value for city (used in agent prompt)")

        # scored_qualified (not_called in calling_agent) - 1 lead
        lid_sq = add_lead({"lead_id": "demo-sq-1", "company": "Fictional ScoreOnly Drywall", "category": "Drywall Subcontractor", "phone": "+15125550149"})
        tracker.record_verification(lid_sq, Verification(True, "+15125550149", "+15125550149", "ok", "Texas", "America/Chicago"))
        tracker.record_decision(lid_sq, _make_decision(
            20, 91, "No drafting scope.", "Drywall takeoff bidder.",
            "Fictional ScoreOnly Drywall bids commercial drywall. Strong Paradise Estimating fit.",
        ))
        tracker.record_finished(lid_sq, "scored_qualified")

        # call_finished: called (12 leads, 4 transferred), call_failed (2), call_timeout (1) -> 15 leads
        finished_specs = [
            # Transferred calls (4)
            ("Fictional Pinnacle Commercial Concrete", "Concrete Subcontractor", "paradise", 24, 96, "+15125550161", "called", "transfer_call", 142.0, 1, True, "https://example.com/t/5001", "https://example.com/r/5001", {"estimator_needed": "Commercial flatwork", "monthly_bids": "8-10"}),
            ("Fictional Vanguard BIM Partners", "Architecture & BIM", "draftliner", 95, 18, "+12125550162", "called", "call_transferred", 184.0, 1, True, "https://example.com/t/5002", "https://example.com/r/5002", {"software": "Revit 2025", "team_size": "2 drafters"}),
            ("Fictional Ironclad Electrical Co", "Electrical Contractor", "paradise", 19, 91, "+17135550163", "called", "transfer_call", 118.0, 2, True, "https://example.com/t/5003", "https://example.com/r/5003", {"bid_deadline": "Next Friday", "trade": "Commercial electrical"}),
            ("Fictional Northstar Design-Build", "Design-Build Firm", "draftliner", 89, 72, "+13035550164", "called", "user_hangup", 95.0, 1, True, "https://example.com/t/5004", "https://example.com/r/5004", {"next_step": "Specialist took over the call"}),
            # Regular completed calls (8)
            ("Fictional RedRiver Plumbing", "Plumbing Contractor", "paradise", 12, 88, "+15125550165", "called", "user_qualified", 74.0, 1, False, "https://example.com/t/5005", "https://example.com/r/5005", {"next_step": "Send sample takeoff"}),
            ("Fictional Meridian Civil Engineers", "Civil Engineering", "draftliner", 92, 22, "+14045550166", "called", "user_hangup", 41.0, 1, False, "https://example.com/t/5006", "https://example.com/r/5006", {}),
            ("Fictional Granite Roofing Systems", "Commercial Roofing", "paradise", 15, 85, "+16025550167", "called", "no-answer", 0.0, 2, False, "https://example.com/t/5007", "https://example.com/r/5007", {}),
            ("Fictional Beacon Custom Homes", "Custom Home Builder", "draftliner", 84, 70, "+18285550168", "called", "voicemail", 29.0, 1, False, "https://example.com/t/5008", "https://example.com/r/5008", {}),
            ("Fictional Keystone HVAC Mechanical", "HVAC Contractor", "paradise", 20, 86, "+12155550169", "called", "busy", 0.0, 2, False, "https://example.com/t/5009", "https://example.com/r/5009", {}),
            ("Fictional Timberline Millwork", "Architectural Millwork", "draftliner", 88, 35, "+15035550170", "called", "end_call_tool", 53.0, 1, False, "https://example.com/t/5010", "https://example.com/r/5010", {"notes": "Uses in-house shop team"}),
            ("Fictional Copperstate Fire Protection", "Fire Sprinkler Contractor", "paradise", 31, 83, "+14805550171", "called", "user_qualified", 82.0, 1, False, "javascript:alert(1)", "https://example.com/r/5011", {"follow_up": "Email brochure"}),
            ("Fictional BlueRidge CAD Studio", "Drafting Services", "draftliner", 97, 11, "+17045550172", "called", "user_hangup", 38.0, 1, False, "https://example.com/t/5012", "https://example.com/r/5012", {}),
            # Failed & timeout (3)
            ("Fictional Starlight Paving", "Asphalt Paving", "paradise", 10, 84, "+15125550173", "call_failed", "failed", None, 1, False, "", "", {}),
            ("Fictional Oakridge Interiors", "Interior Design", "draftliner", 86, 14, "+15125550174", "call_failed", "failed", None, 1, False, "", "", {}),
            ("Fictional Delta Sitework Co", "Sitework & Grading", "paradise", 16, 82, "+15125550175", "call_timeout", None, None, 1, False, "", "", {}),
        ]
        for idx, spec in enumerate(finished_specs, start=1):
            name, cat, svc_id, dp, pp, phone, cstatus, disp, dur, attempts, xfer, t_url, r_url, extra_ctx = spec
            cid = 201 if svc_id == "draftliner" else 202
            rid = 6000 + idx
            # Same file-key shape as real Dograh runs; demo files come from dashboard/demo_media.py
            scenario = "transfer" if xfer else {"user_qualified": "positive", "voicemail": "voicemail"}.get(disp, "hangup")
            has_audio = bool(r_url) and bool(dur)
            r_url = f"recordings/demo/{rid}.wav" if has_audio else ""
            if t_url.startswith("https://"):
                t_url = f"transcripts/demo/{scenario}-{svc_id}-{rid}.txt" if has_audio else ""
            lid = add_lead({
                "lead_id": f"demo-fin-{idx}",
                "company": name,
                "category": cat,
                "city": "Austin",
                "state": "TX",
                "phone": phone,
                "source": "bbb",
            })
            tracker.record_verification(lid, Verification(True, phone, phone, "ok", "Texas", "America/Chicago"))
            tracker.record_decision(lid, _make_decision(
                dp, pp,
                f"Assessment for Draftliner based on {cat}.",
                f"Assessment for Paradise Estimating based on {cat}.",
                f"{name} specializes in {cat.lower()}. Strong fit for {SERVICES[svc_id].name}.",
            ))
            tracker.record_campaign(lid, AGENTS[svc_id], cid, f"{SERVICES[svc_id].name}_2026-11-23")
            tracker.record_call_started(lid, rid, attempts)
            raw_run = {
                "id": rid,
                "is_completed": True,
                "mode": "outbound",
                "workflow_id": AGENTS[svc_id].id,
                "public_access_token": "SECRET-DOGRAH-TOKEN",
                "initial_context": {"lead_ref": lid, "phone_number": phone},
                "cost_info": {"call_duration_seconds": dur},
                "usage_info": {"tts_chars": 420},
                "transcript_public_url": None,
                "recording_public_url": None,
                "transcript_url": t_url,
                "recording_url": r_url,
                "bot_recording_url": f"recordings/demo/{rid}/bot.wav" if r_url else "",
                "user_recording_url": f"recordings/demo/{rid}/user.wav" if r_url else "",
                "gathered_context": {
                    "call_disposition": disp,
                    "call_tags": [disp] if disp else [],
                    "external_pbx_transferred": xfer and disp == "user_hangup",
                    "extracted_variables": extra_ctx,
                    "nodes_visited": ["greeting", "pitch", "closing"],
                },
            }
            if cstatus == "called":
                outcome = CallOutcome(
                    status="called",
                    run_id=rid,
                    disposition=disp,
                    duration_seconds=dur,
                    tags=[disp] if disp else [],
                    transcript_url=t_url or None,
                    recording_url=r_url or None,
                    attempts=attempts,
                    raw=raw_run,
                )
                tracker.record_finished(lid, "called", "", outcome)
            elif cstatus == "call_failed":
                outcome = CallOutcome(
                    status="call_failed",
                    run_id=rid,
                    disposition=disp,
                    attempts=attempts,
                    error="Telephony carrier rejected call",
                    raw=raw_run,
                )
                tracker.record_finished(lid, "call_failed", "Telephony carrier rejected call", outcome)
            else:
                outcome = CallOutcome(
                    status="call_timeout",
                    run_id=rid,
                    attempts=attempts,
                    error="No progress in campaign for 900s",
                    raw=raw_run,
                )
                tracker.record_finished(lid, "call_timeout", "No progress in campaign for 900s", outcome)

        tracker.save()
        return count
    finally:
        tracker.close()


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate fictional demo tracker workbook.")
    parser.add_argument("--out", type=Path, required=True, help="Output .xlsx path (must not be live tracker)")
    args = parser.parse_args(argv)
    total = generate_demo_tracker(args.out)
    print(f"Generated {total} fictional demo leads at {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
