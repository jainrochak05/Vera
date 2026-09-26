"""Compose Vera WhatsApp messages from the 4-context pack.

Uses Gemini 1.5 Flash, then Groq Llama, then a deterministic high-specificity
template. Templates are the latency guarantee; LLMs are quality polish.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

CANNED_PATTERNS = [
    r"out of office",
    r"auto-?reply",
    r"automated (message|assistant)",
    r"thanks for reaching out",
    r"we will get back to you",
    r"thank you for contacting",
    r"our team will (respond|get back|reach out)",
    r"aapka.?jaankari ke liye",
    r"aapki jaankari ke liye",
    r"main ek automated",
    r"this is an automated",
    r"currently unavailable",
]

ACCEPT_PATTERNS = [
    r"\byes\b",
    r"yeah",
    r"yep",
    r"ok(ay)?",
    r"let'?s do it",
    r"lets do it",
    r"go ahead",
    r"send (it|me|the)",
    r"agree",
    r"confirm",
    r"proceed",
    r"do it",
    r"what's next",
    r"whats next",
    r"sure",
    r"haan",
    r"kar do",
    r"bhej do",
    r"theek hai",
    r"join",
    r"judrna",
    r"judna",
]

STOP_PATTERNS = [
    r"\bstop\b",
    r"unsubscribe",
    r"not interested",
    r"useless",
    r"spam",
    r"don't (message|text|contact)",
    r"dont (message|text|contact)",
    r"leave me",
    r"bothering",
    r"band karo",
    r"mat bhejo",
]

URL_RE = re.compile(r"https?://\S+", re.I)


def _re_any(patterns: list[str], text: str) -> bool:
    lower = text.lower()
    return any(re.search(p, lower) for p in patterns)


def is_canned_autoreply(message: str) -> bool:
    return _re_any(CANNED_PATTERNS, message or "")


def is_acceptance(message: str) -> bool:
    return _re_any(ACCEPT_PATTERNS, message or "")


def is_stop(message: str) -> bool:
    return _re_any(STOP_PATTERNS, message or "")


def is_off_topic(message: str) -> bool:
    lower = (message or "").lower()
    return any(k in lower for k in ("gst", "income tax", "itr", "loan", "crypto"))


def strip_urls(text: str) -> str:
    return URL_RE.sub("", text or "").strip()


def format_iso_time(iso: str) -> str:
    """Convert '2026-04-26T19:30:00+05:30' → '7:30 PM' for human-readable output."""
    if not iso:
        return ""
    try:
        import re as _re
        m = _re.match(r"\d{4}-\d{2}-\d{2}T(\d{2}):(\d{2})", iso.strip())
        if m:
            h, mn = int(m.group(1)), int(m.group(2))
            suffix = "AM" if h < 12 else "PM"
            h12 = h % 12 or 12
            return f"{h12}:{mn:02d} {suffix}"
    except Exception:
        pass
    return iso




def _first(name: str) -> str:
    return (name or "").split()[0] if name else ""


def merchant_name(merchant: dict) -> str:
    ident = merchant.get("identity") or {}
    return ident.get("name") or merchant.get("name") or "there"


def owner_first(merchant: dict) -> str:
    ident = merchant.get("identity") or {}
    return ident.get("owner_first_name") or _first(merchant_name(merchant))


def category_slug(merchant: dict, category: dict) -> str:
    return merchant.get("category_slug") or category.get("slug") or ""


def wants_hinglish(merchant: dict, customer: Optional[dict] = None) -> bool:
    if customer:
        pref = ((customer.get("identity") or {}).get("language_pref") or "").lower()
        if "hi" in pref or "mix" in pref or "te" in pref:
            return True
    langs = (merchant.get("identity") or {}).get("languages") or []
    return "hi" in langs


def active_offers(merchant: dict) -> list[str]:
    titles = []
    for o in merchant.get("offers") or []:
        if isinstance(o, dict) and o.get("status") == "active" and o.get("title"):
            titles.append(o["title"])
        elif isinstance(o, str):
            titles.append(o)
    return titles


def catalog_titles(category: dict) -> list[str]:
    out = []
    for o in category.get("offer_catalog") or []:
        if isinstance(o, dict) and o.get("title"):
            out.append(o["title"])
        elif isinstance(o, str):
            out.append(o)
    return out


def pick_service_price(merchant: dict, category: dict) -> str:
    offers = active_offers(merchant)
    if offers:
        return offers[0]
    catalog = catalog_titles(category)
    return catalog[0] if catalog else "a service-at-price offer from your catalog"


def find_digest_item(category: dict, item_id: Optional[str]) -> dict:
    digest = category.get("digest") or []
    if item_id:
        for item in digest:
            if isinstance(item, dict) and item.get("id") == item_id:
                return item
    return digest[0] if digest and isinstance(digest[0], dict) else {}


def salutation(merchant: dict, category: dict) -> str:
    slug = category_slug(merchant, category)
    first = owner_first(merchant)
    if slug == "dentists":
        return f"Dr. {first}" if first else merchant_name(merchant)
    return first or merchant_name(merchant)


def peer_ctr(category: dict) -> str:
    stats = category.get("peer_stats") or {}
    ctr = stats.get("avg_ctr")
    if ctr is None:
        return ""
    return f"{float(ctr) * 100:.1f}%"


def merchant_ctr(merchant: dict) -> str:
    perf = merchant.get("performance") or {}
    ctr = perf.get("ctr")
    if ctr is None:
        return ""
    return f"{float(ctr) * 100:.1f}%"


def format_pct(value: Any) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(n) <= 1.5:
        return f"{n * 100:.0f}%"
    return f"{n:.0f}%"


def template_name_for(kind: str, send_as: str) -> str:
    if send_as == "merchant_on_behalf":
        return "merchant_customer_outreach_v1"
    mapping = {
        "research_digest": "vera_research_digest_v1",
        "regulation_change": "vera_compliance_v1",
        "recall_due": "merchant_recall_reminder_v1",
        "perf_dip": "vera_perf_alert_v1",
        "perf_spike": "vera_perf_spike_v1",
        "renewal_due": "vera_renewal_v1",
        "festival_upcoming": "vera_festival_v1",
        "curious_ask_due": "vera_curious_ask_v1",
        "ipl_match_today": "vera_event_v1",
        "cde_opportunity": "vera_cde_v1",
        "competitor_opened": "vera_competitor_v1",
        "supply_alert": "vera_supply_alert_v1",
        "gbp_unverified": "vera_gbp_v1",
        "active_planning_intent": "vera_planning_v1",
    }
    return mapping.get(kind, "vera_generic_v1")


def cta_for(kind: str, send_as: str) -> str:
    informational = {
        "research_digest",
        "milestone_reached",
        "perf_spike",
        "curious_ask_due",
        "category_seasonal",
        "seasonal_perf_dip",
    }
    if send_as == "merchant_on_behalf":
        return "multi_choice_slot"
    if kind in informational:
        return "open_ended"
    return "binary_yes_no"


def compose_template(
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: Optional[dict] = None,
) -> dict:
    ident = merchant.get("identity") or {}
    name = merchant_name(merchant)
    who = salutation(merchant, category)
    slug = category_slug(merchant, category)
    locality = ident.get("locality") or ""
    city = ident.get("city") or ""
    perf = merchant.get("performance") or {}
    views = perf.get("views", "?")
    calls = perf.get("calls", "?")
    ctr = merchant_ctr(merchant)
    peer = peer_ctr(category)
    offers = pick_service_price(merchant, category)
    agg = merchant.get("customer_aggregate") or {}
    signals = merchant.get("signals") or []
    kind = trigger.get("kind") or "nudge"
    payload = trigger.get("payload") or {}
    send_as = "merchant_on_behalf" if trigger.get("scope") == "customer" or customer else "vera"
    mix = wants_hinglish(merchant, customer)

    digest_id = payload.get("top_item_id") or payload.get("digest_item_id") or payload.get("alert_id")
    item = find_digest_item(category, digest_id)

    body = ""
    cta = cta_for(kind, send_as)

    if kind == "research_digest":
        title = item.get("title") or payload.get("title") or "this week's category digest"
        source = item.get("source") or "category digest"
        trial_n = item.get("trial_n")
        summary = item.get("summary") or ""
        trial_bit = f"{trial_n}-patient trial: " if trial_n else ""
        cohort = ""
        if "high_risk" in str(signals) or item.get("patient_segment"):
            cohort = " Relevant to your high-risk adult cohort." if slug == "dentists" else ""
        body = (
            f"{who}, digest just dropped ({source}). {title}.{cohort} {trial_bit}{summary} "
            f"Worth a 2-min look. Want me to pull the abstract + draft a patient WhatsApp you can share?"
        )
        cta = "open_ended"

    elif kind == "regulation_change":
        deadline = payload.get("deadline_iso") or "the stated deadline"
        title = item.get("title") or "a regulation update in your category pack"
        summary = item.get("summary") or ""
        body = (
            f"{who}, compliance note: {title}. {summary} Deadline {deadline}. "
            f"Want me to turn this into a 5-line SOP checklist for {name}?"
        )
        cta = "binary_yes_no"

    elif kind in ("recall_due",) and customer:
        cident = customer.get("identity") or {}
        cname = cident.get("name") or "a patient"
        rel = customer.get("relationship") or {}
        last = payload.get("last_service_date") or rel.get("last_visit") or "their last visit"
        slots = payload.get("available_slots") or []
        slot_txt = " / ".join(s.get("label") for s in slots if isinstance(s, dict) and s.get("label"))
        if not slot_txt:
            slot_txt = "a weekday evening slot"
        body = (
            f"{who}, {cname} hasn't been in since {last} — 6-month cleaning recall is due. "
            f"I can send them a reminder with these slots: {slot_txt} ({offers}). "
            f"Reply YES and I'll draft the WhatsApp to send on your behalf."
        )
        send_as = "merchant_on_behalf"
        cta = "binary_yes_no"


    elif kind == "perf_dip":
        metric = payload.get("metric") or "calls"
        delta = format_pct(payload.get("delta_pct", perf.get("delta_7d", {}).get("calls_pct", -0.4)))
        baseline = payload.get("vs_baseline", "")
        extra = f" vs baseline {baseline}" if baseline != "" else ""
        ctr_line = f" Your CTR is {ctr}" + (f" vs peer {peer}" if peer else "") + "."
        body = (
            f"{who}, {locality} {city}: {metric} dropped {delta} this week{extra}. "
            f"30d snapshot — views {views}, calls {calls}.{ctr_line} "
            f"I've drafted a {offers} push for the next 48h. Reply YES to launch it."
        )
        cta = "binary_yes_no"

    elif kind == "renewal_due":
        days = payload.get("days_remaining") or (merchant.get("subscription") or {}).get("days_remaining")
        plan = payload.get("plan") or (merchant.get("subscription") or {}).get("plan")
        amount = payload.get("renewal_amount")
        amt = f" ₹{amount}" if amount else ""
        body = (
            f"{who}, {plan} renews in {days} days{amt}. "
            f"You're at {views} views / {calls} calls (30d)"
            + (f", CTR {ctr} vs peer {peer}" if ctr and peer else "")
            + f". Reply YES and I'll lock renewal + keep {offers} live."
        )
        cta = "binary_yes_no"

    elif kind == "festival_upcoming":
        fest = payload.get("festival") or "the festival"
        date = payload.get("date") or ""
        days = payload.get("days_until")
        when = f" on {date}" if date else ""
        days_bit = f" ({days} days away)" if days is not None else ""
        # Category-specific festive spike language
        if slug in ("salons", "spas"):
            spike = f"{locality} appointment slots fill up 3 days before {fest}"
        elif slug in ("restaurants",):
            spike = f"covers and home orders spike 2× around {fest} in {locality}"
        elif slug in ("gyms",):
            spike = f"new-member walk-ins jump during the {fest} break in {locality}"
        elif slug in ("pharmacies",):
            spike = f"OTC wellness demand rises sharply around {fest} in {locality}"
        else:
            spike = f"{locality} bookings typically spike around {fest}"
        body = (
            f"{who}, {fest}{when}{days_bit} — {spike}. "
            f"I've queued {offers} as the lead offer (specific service + price, not a vague % off). "
            f"Reply YES to post it on GBP + WhatsApp now."
        )
        cta = "binary_yes_no"


    elif kind == "curious_ask_due":
        body = (
            f"{who}, quick pulse from {locality}: which service actually walked in most this week — "
            f"and should I push {offers} against that, or something else?"
        )
        cta = "open_ended"

    elif kind in ("wedding_package_followup",) and customer:
        cname = (customer.get("identity") or {}).get("name") or "a client"
        wdate = payload.get("wedding_date") or (customer.get("preferences") or {}).get("wedding_date")
        trial = payload.get("trial_completed") or ""
        body = (
            f"{who}, {cname}'s bridal trial on {trial} is complete (wedding: {wdate}). "
            f"Their 30-day skin-prep window is open now. Want me to send them the follow-up plan? Reply YES to reach out."
        )
        send_as = "merchant_on_behalf"
        cta = "binary_yes_no"

    elif kind == "winback_eligible":
        days = payload.get("days_since_expiry")
        dip = format_pct(payload.get("perf_dip_pct", -0.3))
        lapsed = payload.get("lapsed_customers_added_since_expiry")
        # Category-aware framing
        if slug in ("salons", "spas"):
            action = f"re-activate your client list with {offers} — a targeted booking campaign"
        elif slug in ("gyms",):
            action = f"run a 'Come Back' membership offer with {offers} this week"
        elif slug in ("restaurants",):
            action = f"push a comeback combo featuring {offers} to past regulars"
        elif slug in ("pharmacies",):
            action = f"reach out to lapsed patients with a home-delivery + {offers} bundle"
        else:
            action = f"restart with {offers} this week"
        lapsed_bit = f"; {lapsed} more customers went quiet" if lapsed else ""
        body = (
            f"{who}, it's been {days} days since your plan lapsed — footfall and calls are down {dip}{lapsed_bit}. "
            f"I can {action}. Reply YES to draft the re-engagement sequence now."
        )
        cta = "binary_yes_no"

    elif kind == "ipl_match_today":
        match = payload.get("match") or "tonight's match"
        venue = payload.get("venue") or city
        raw_time = payload.get("match_time_iso") or ""
        match_time = format_iso_time(raw_time) or "tonight"
        # Category-specific spike
        if slug in ("restaurants",):
            spike = f"delivery orders and dine-in covers will spike from {match_time} — lead with"
        elif slug in ("gyms",):
            spike = f"post-match walk-ins surge after {match_time} — attract them with"
        elif slug in ("salons", "spas"):
            spike = f"pre-match grooming demand picks up before {match_time} — push"
        elif slug in ("pharmacies",):
            spike = f"convenience store runs spike around match time ({match_time}) — feature"
        else:
            spike = f"local footfall spikes around {match_time} — lead with"
        body = (
            f"{who}, {match} is at {venue} at {match_time}. {spike} {offers} "
            f"(specific service + price, not a generic %). "
            f"Reply YES and I'll post it on GBP + WhatsApp before kickoff."
        )
        cta = "binary_yes_no"

    elif kind == "review_theme_emerged":
        raw_theme = payload.get("theme") or "service quality"
        theme = str(raw_theme).replace("_", " ")
        n = payload.get("occurrences_30d") or "several"
        quote = payload.get("common_quote") or ""
        body = (
            f"{who}, {n} recent reviews flagged {theme}"
            + (f' — one customer wrote: \"{quote}\"' if quote else "")
            + ". I can draft a polite owner response and a 3-step ops fix. Reply YES to get both."
        )
        cta = "binary_yes_no"

    elif kind == "milestone_reached":
        metric = str(payload.get("metric") or "reviews").replace("_", " ")
        now_val = payload.get("value_now")
        target = payload.get("milestone_value")
        # Category-appropriate milestone language
        if slug in ("dentists", "pharmacies"):
            ask = f"a short patient review-ask message using {offers} for this week's appointments"
        elif slug in ("salons", "spas"):
            ask = f"a client appreciation message with a referral incentive using {offers}"
        elif slug in ("gyms",):
            ask = f"a member shoutout + referral push using {offers} for this week's walk-ins"
        else:
            ask = f"a review-ask WhatsApp with {offers} for this week's customers"
        body = (
            f"{who}, you're at {now_val} {metric} — just {int(target or 0) - int(now_val or 0)} away from {target}. "
            f"Should I draft {ask}? A few more reviews this week could close the gap."
        )
        cta = "open_ended"

    elif kind == "active_planning_intent":
        topic = str(payload.get("intent_topic") or "your plan").replace("_", " ")
        last = payload.get("merchant_last_message") or ""
        # Category-specific next step
        if slug in ("gyms",):
            next_step = (
                f"I'll build a class schedule + membership tier for '{topic}' using {offers} and {locality} pricing. "
                f"Ready to send you the draft — reply CONFIRM to review it."
            )
        elif slug in ("salons", "spas"):
            next_step = (
                f"I'll draft a service menu + booking flow for '{topic}' anchored on {offers} at {locality} rates. "
                f"Reply CONFIRM and I'll share the draft."
            )
        elif slug in ("restaurants",):
            next_step = (
                f"I'll put together a launch menu + promo plan for '{topic}' featuring {offers}. "
                f"Reply CONFIRM and I'll send the draft now."
            )
        elif slug in ("pharmacies",):
            next_step = (
                f"I'll prepare a patient communication plan for '{topic}' using {offers} and your {locality} delivery zone. "
                f"Reply CONFIRM to get the draft."
            )
        else:
            next_step = (
                f"I'll lock a 1-page plan for '{topic}' using {offers} and {locality} pricing. "
                f"Reply CONFIRM and I'll send the draft."
            )
        body = f"{who}, you asked: \"{last}\". {next_step}"
        cta = "binary_confirm_cancel"

    elif kind in ("seasonal_perf_dip", "category_seasonal"):
        raw_note = payload.get("season_note") or payload.get("season") or "this seasonal shift"
        note = str(raw_note).replace("_", " ")
        delta = payload.get("delta_pct")
        trends = payload.get("trends") or []
        cleaned_trends = [str(t).replace("_", " ") for t in trends[:3]]
        trend_txt = ", ".join(cleaned_trends)
        dip = f" Footfall is down {format_pct(delta)} this week." if delta is not None else ""
        body = (
            f"{who}, heads-up on {note}.{dip} "
            + (f"Top local trends: {trend_txt}. " if trend_txt else "")
            + "No pressure — just flagging so you can plan ahead. Want a 3-bullet action checklist?"
        )
        cta = "open_ended"

    elif kind in ("customer_lapsed_hard", "trial_followup") and customer:
        cname = (customer.get("identity") or {}).get("name") or "a client"
        if kind == "trial_followup":
            trial = payload.get("trial_date") or ""
            opts = payload.get("next_session_options") or []
            slot = opts[0].get("label") if opts else "the next available slot"
            # Category-specific trial language
            if slug in ("gyms",):
                label = "training session"
            elif slug in ("salons", "spas"):
                label = "appointment"
            elif slug in ("dentists",):
                label = "check-up"
            else:
                label = "session"
            body = (
                f"{who}, {cname} finished their trial {label} on {trial}. "
                f"Their next {label} is open at {slot}. Should I send them a quick follow-up to lock it in? Reply YES."
            )
        else:
            days = payload.get("days_since_last_visit") or "?"
            raw_focus = payload.get("previous_focus") or ""
            focus = str(raw_focus).replace("_", " ") if raw_focus else ""
            # Category-specific lapsed language
            if slug in ("gyms",):
                context = f"They were working on {focus}" if focus else "They were a regular member"
                action = f"a '3-session comeback pass' using {offers}"
            elif slug in ("salons", "spas"):
                context = f"Their last service was {focus}" if focus else "They were a regular client"
                action = f"a personalised rebooking offer using {offers}"
            elif slug in ("dentists",):
                context = f"Their last treatment was for {focus}" if focus else "They're a registered patient"
                action = f"a recall reminder with {offers}"
            elif slug in ("pharmacies",):
                context = f"They usually pick up {focus}" if focus else "They're a regular customer"
                action = f"a home delivery prompt with {offers}"
            elif slug in ("restaurants",):
                context = f"They loved {focus}" if focus else "They were a regular"
                action = f"a comeback offer featuring {offers}"
            else:
                context = f"Previously: {focus}" if focus else ""
                action = f"a win-back message with {offers}"
            body = (
                f"{who}, {cname} hasn't visited in {days} days. {context}. "
                f"I can send them {action} to bring them back. Reply YES to draft it."
            )
        send_as = "merchant_on_behalf"
        cta = "binary_yes_no"

    elif kind == "supply_alert":
        mol = payload.get("molecule") or (item.get("title") or "a key SKU")
        batches = payload.get("affected_batches") or []
        mfr = payload.get("manufacturer") or ""
        body = (
            f"{who}, urgent supply alert on {mol}"
            + (f" ({mfr})" if mfr else "")
            + (f" — batches {', '.join(batches)} affected" if batches else "")
            + ". Pull these SKUs off the shelf immediately. I'll draft the patient notice. Reply YES to send the script now."
        )
        cta = "binary_yes_no"

    elif kind == "chronic_refill_due" and customer:
        cname = (customer.get("identity") or {}).get("name") or "a patient"
        mols = payload.get("molecule_list") or []
        last = payload.get("last_refill") or ""
        out_date = str(payload.get("stock_runs_out_iso") or "").split("T")[0]  # strip time from ISO
        mol_txt = ", ".join(mols) if mols else "their regular medication"
        body = (
            f"{who}, {cname}'s supply of {mol_txt} (last refilled {last}) is estimated to run out "
            + (f"by {out_date}" if out_date else "soon")
            + ". Home delivery is on file — I can send them a refill confirmation right now. Reply YES."
        )
        send_as = "merchant_on_behalf"
        cta = "binary_yes_no"

    elif kind == "gbp_unverified":
        path = payload.get("verification_path") or "postcard or phone"
        uplift = payload.get("estimated_uplift_pct")
        up = f"Verified listings get ~{format_pct(uplift)} more inbound calls on average. " if uplift is not None else ""
        # Category-specific first GBP post angle
        if slug in ("dentists",):
            post_angle = f"a 'New Patient Welcome' post featuring {offers}"
        elif slug in ("pharmacies",):
            post_angle = f"a 'Fast Home Delivery' post featuring {offers}"
        elif slug in ("gyms",):
            post_angle = f"a 'Free Trial Session' post featuring {offers}"
        elif slug in ("salons", "spas"):
            post_angle = f"a 'Book Now' post featuring {offers}"
        elif slug in ("restaurants",):
            post_angle = f"a 'Today's Specials' post featuring {offers}"
        else:
            post_angle = f"a GBP post featuring {offers}"
        body = (
            f"{who}, your Google listing for {name} in {locality} isn't verified yet — you're losing free discovery. "
            f"{up}Verification via {path} takes under 5 minutes. Reply YES and I'll walk you through it + publish {post_angle} the same day."
        )
        cta = "binary_yes_no"

    elif kind == "cde_opportunity":
        credits = payload.get("credits") or item.get("credits") or ""
        fee = payload.get("fee") or item.get("actionable") or ""
        date = item.get("date") or ""
        title = item.get("title") or "a CDE session in your digest"
        # Category-specific ROI framing
        if slug in ("dentists",):
            roi = "New techniques you can bill as premium services within the month."
        elif slug in ("pharmacies",):
            roi = "Skills you can use to upsell clinical consultations immediately."
        else:
            roi = "Actionable skills you can apply with your team this week."
        credit_bit = f" {credits} CME credits." if credits else ""
        fee_bit = f" {fee}." if fee else ""
        date_bit = f" on {date}" if date else ""
        body = (
            f"{who}, there's a '{title}' session{date_bit}.{credit_bit}{fee_bit} {roi} "
            f"Reply YES and I'll add it to your calendar + prepare a 3-point staff briefing template."
        )
        cta = "binary_yes_no"

    elif kind == "competitor_opened":
        comp = payload.get("competitor_name") or "a new competitor nearby"
        km = payload.get("distance_km")
        their = payload.get("their_offer") or ""
        opened = payload.get("opened_date") or ""
        body = (
            f"{who}, {comp} just opened{(' ' + opened) if opened else ''}"
            + (f" ({km} km away)" if km is not None else "")
            + (f", leading with {their}" if their else "")
            + f". Your existing {offers} is already stronger — I can refresh your GBP this week with a peer-tone update (no hype, just facts). Reply YES to draft it."
        )
        cta = "binary_yes_no"

    elif kind == "perf_spike":
        metric = str(payload.get("metric") or "calls").replace("_", " ")
        delta = format_pct(payload.get("delta_pct", 0.15))
        driver = str(payload.get("likely_driver") or "recent activity").replace("_", " ")
        baseline = payload.get("vs_baseline") or calls
        # Category-specific spike action
        if slug in ("gyms",):
            action = f"a time-limited membership push using {offers} to convert this week's walk-ins"
        elif slug in ("restaurants",):
            action = f"a featured combo push using {offers} to double-down on the momentum"
        elif slug in ("salons", "spas"):
            action = f"a 'Book This Week' campaign using {offers} to fill the calendar while demand is high"
        elif slug in ("pharmacies",):
            action = f"a WhatsApp status update featuring {offers} to capture the spike"
        else:
            action = f"a follow-up push using {offers} while the window is open"
        body = (
            f"{who}, {metric} jumped {delta} vs last week (baseline: {baseline}). "
            f"Likely driver: {driver}. I can run {action} — want me to do that now?"
        )
        cta = "open_ended"

    elif kind == "dormant_with_vera":
        days = payload.get("days_since_last_merchant_message") or 14
        raw_topic = str(payload.get("last_topic") or "").replace("_", " ").strip()
        last = f"our chat about {raw_topic}" if raw_topic else "our last conversation"
        # Category-specific re-engagement
        if slug in ("gyms",):
            nudge = f"pull up your member retention numbers and run a quick re-activation with {offers}"
        elif slug in ("salons", "spas"):
            nudge = f"check your appointment calendar and run a 'fill empty slots' push with {offers}"
        elif slug in ("restaurants",):
            nudge = f"refresh your GBP with today's specials using {offers}"
        elif slug in ("pharmacies",):
            nudge = f"flag your fastest-moving SKUs and run a home-delivery push with {offers}"
        else:
            nudge = f"refresh your GBP with {offers}"
        body = (
            f"{who}, it's been {days} days since {last}. "
            f"Quick snapshot: {views} views, {calls} calls, CTR {ctr}"
            + (f" vs category average {peer}" if peer else "")
            + f". One useful thing I can do today: {nudge}. Reply YES if that helps."
        )
        cta = "binary_yes_no"

    else:
        why = json.dumps(payload)[:180] if payload else kind
        body = (
            f"{who}, flag from {locality} {city}: {kind.replace('_', ' ')}. "
            f"30d: {views} views, {calls} calls, CTR {ctr}. Live offer: {offers}. {why} "
            f"Reply YES if you want me to act on this now."
        )
        cta = "binary_yes_no"

    body = strip_urls(re.sub(r"\s+", " ", body).strip())
    rationale = (
        f"Trigger {kind} for {name} ({slug}, {locality}). Anchored on live context facts "
        f"(perf/offers/digest) without fabricating. send_as={send_as}, cta={cta}."
    )
    params = [who, kind, offers]
    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "template_name": template_name_for(kind, send_as),
        "template_params": params,
        "rationale": rationale,
    }

SYSTEM_PROMPT = """YOU ARE VERA: A senior growth consultant embedded inside WhatsApp, advising Indian SMB owners directly.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE 1 — STRICT B2B PERSONA (NEVER TALK TO END-CONSUMERS)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
You always address the MERCHANT/BUSINESS OWNER. Never write to their customer.
If the trigger is customer-scoped, tell the merchant about the customer — don't address the customer directly.

  ✗ WRONG: "Hi Priya, Dr. Meera's clinic here — your cleaning is due."
  ✓ RIGHT:  "Hi Dr. Meera, Priya hasn't been in for 6 months. Should I send her a cleaning recall reminder?"

Never start a message with a customer's name. Always open with the owner's name or salutation.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE 2 — NO RAW DATABASE STRINGS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Never output snake_case keys, JSON field names, enum values, or system tags verbatim.
Always translate data into natural, conversational English before including it.

  ✗ WRONG: "seasonal note (post_resolution_window_apr_jun) — delta_pct: -0.3"
  ✓ RIGHT:  "April–June is typically your slower window — views are down ~30% this week."

  ✗ WRONG: "Trigger kind: perf_dip. Metric: calls. Source: internal."
  ✓ RIGHT:  "Your inbound calls dropped 50% this week compared to your 12-call baseline."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE 3 — CONVERSATIONAL BUSINESS PEER & HIGH SPECIFICITY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Sound like a sharp, busy colleague — not a system reading a spreadsheet.
Every message must contain at least **two verifiable data points** from the context payload (e.g., exact view counts, exact percentages, item names, prices, or dates).
Keep messages to 2–4 sentences max. No filler intros. No passive commentary.

  ✗ WRONG: "Your performance is down. Let's run a discount."
  ✓ RIGHT:  "Dr. Bharat, your calls dropped 50% this week in Andheri West (views at 145, CTR 2.1%). I've drafted a Smile Brighter check-up package at ₹499. Reply YES to launch it."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE 4 — ZERO META-TALK
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Never reference yourself as an AI, bot, or system. Never acknowledge auto-replies or system logs.
Act as a human growth advisor at all times.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE 5 — FACT DISCIPLINE & CATEGORY VOICE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Use ONLY facts present in the context JSON. Never invent competitors, prices, dates, or data.
- Prefer specific service+price ("Cleaning @ ₹499") over vague "% off".
- Dentists/pharmacies: clinical, precise, trust-first. No "guaranteed cure".
- Salons/gyms: visual outcomes, easy booking. Restaurants: operator-to-operator, volume-focused.
- If merchant already said YES: act immediately — "Sending the draft now." Never re-qualify.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE 6 — EVENT-TO-BUSINESS TIE-INS (MANDATORY)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Whenever a trigger references a local event, sports match, festival, or holiday, you MUST:
1. Explicitly connect it to a concrete business outcome for that merchant category using exact localized metrics.
2. Propose a specific, named action with an active offer/price from their catalog — never a generic discount.
3. Include time-bound urgency tied directly to the event countdown.

Examples by category:
- Restaurant + IPL match: "DC vs MI is playing at Arun Jaitley Stadium tonight — delivery orders will spike 40% by 7 PM. I've set up your Paneer Tikka Combo @ ₹249 as the featured push. Reply YES and I'll push it live before kickoff."
- Salon + Festival: "Diwali is 188 days away, but Kapra booking windows open early. I've lined up your Bridal Glow package at ₹1,499. Reply YES to post it to GBP and WhatsApp."
- Gym + Seasonal: "PowerHouse Fitness has been quiet for 57 days. Winter drop-offs are peaking. Let's run the 30-day Shred Package at ₹999. Reply YES to draft the win-back sequence."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE 7 — TONE CONSISTENCY BY CATEGORY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Match tone to the merchant type. Never be over-enthusiastic for clinical settings or too dry for lifestyle brands.

- Dentists / Pharmacies: Clinical precision, patient-safety focus, trust-first language. Use "patients", "recalls", "prescriptions". No guarantees.
- Salons / Spas: Warm, aspirational, outcome-focused ("bridal glow", "fresh look"). Easy booking. Use "clients", "appointments".
- Restaurants: Peer-to-peer operator tone. Volume, speed, combo value. Use "covers", "orders", "footfall".
- Gyms / Fitness: Motivational but grounded. Membership value, seasonal traffic spikes. Use "members", "sessions", "walk-ins".
- All categories: Moderate energy. Never shout. Never over-promise. Never use exclamation marks more than once per message.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE 8 — UNIVERSAL DYNAMIC FORMULA FOR UNKNOWN TRIGGERS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
If you encounter a trigger, event, or data point you have never seen before, construct your response using this strict 3-part structure:
1. **The Fact:** State the primary data point or event name from the trigger context precisely.
2. **The Impact:** Connect it logically to the merchant's category performance (e.g., footfall, order volume, or client retention).
3. **The Action:** Propose leveraging their active catalog offer ([INSERT ACTIVE OFFER/PRICE]) with a clear binary CTA ("Reply YES to launch.").

Never output blank, generic, or passive text just because a trigger format is unfamiliar. Always anchor it to their live performance metrics and active offers.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CTA RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Actionable trigger → exactly ONE binary CTA at the end ("Reply YES to publish.", "Confirm and I'll send.").
- Informational digest/update → curiosity question only, no sales push.

OUTPUT REQUIREMENTS — respond ONLY with valid JSON, no markdown fences:
{"body": "The exact WhatsApp message to the merchant", "cta": "binary_yes_no|open_ended|binary_confirm_cancel|multi_choice_slot", "rationale": "1-sentence technical justification"}"""



def _llm_gemini(prompt: str, system: str = SYSTEM_PROMPT) -> str:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key or "your_gemini" in key:
        raise RuntimeError("gemini key missing")

    model_name = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

    # Try new google.genai SDK first (recommended)
    try:
        import warnings
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=key)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            resp = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=0.2,
                    max_output_tokens=500,
                    response_mime_type="application/json",
                ),
            )
        return resp.text or ""
    except ImportError:
        pass

    # Fallback to legacy google.generativeai if new SDK not installed
    import warnings
    import google.generativeai as genai_legacy  # type: ignore
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        genai_legacy.configure(api_key=key)
        model = genai_legacy.GenerativeModel(
            model_name,
            system_instruction=system,
            generation_config={
                "temperature": 0.2,
                "max_output_tokens": 500,
                "response_mime_type": "application/json",
            },
        )
        resp = model.generate_content(prompt)
        return resp.text or ""


def _llm_groq(prompt: str) -> str:
    key = os.getenv("GROQ_API_KEY", "")
    if not key or "your_groq" in key:
        raise RuntimeError("groq key missing")
    from groq import Groq

    model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    client = Groq(api_key=key)
    resp = client.chat.completions.create(
        model=model,
        temperature=0.2,
        max_tokens=500,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    return resp.choices[0].message.content or ""


def _parse_llm_json(text: str) -> Optional[dict]:
    if not text:
        return None
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    body = strip_urls(str(data.get("body") or "").strip())
    if not body:
        return None
    return {
        "body": body,
        "cta": data.get("cta") or "open_ended",
        "rationale": data.get("rationale") or "LLM composition from current context.",
    }


async def compose_llm(
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: Optional[dict],
    history: list[dict],
    extra_instruction: str = "",
    timeout_s: float = 12.0,
) -> Optional[dict]:
    slim = {
        "category": {
            "slug": category.get("slug"),
            "voice": category.get("voice"),
            "peer_stats": category.get("peer_stats"),
            "digest": (category.get("digest") or [])[:4],
            "offer_catalog": (category.get("offer_catalog") or [])[:6],
        },
        "merchant": {
            "merchant_id": merchant.get("merchant_id"),
            "identity": merchant.get("identity"),
            "subscription": merchant.get("subscription"),
            "performance": merchant.get("performance"),
            "offers": merchant.get("offers"),
            "signals": merchant.get("signals"),
            "customer_aggregate": merchant.get("customer_aggregate"),
        },
        "trigger": trigger,
        "customer": customer,
        "recent_turns": history[-6:],
        "instruction": extra_instruction,
    }

    # Build a rich category-vertical preamble so the LLM always stays on-category
    cat_slug = category.get("slug") or merchant.get("category_slug") or ""
    trigger_kind = str(trigger.get("kind") or "").replace("_", " ")
    merchant_name_val = ((merchant.get("identity") or {}).get("name") or merchant.get("name") or "this merchant")
    locality_val = (merchant.get("identity") or {}).get("locality") or ""
    owner_name = (merchant.get("identity") or {}).get("owner_first_name") or ""
    salutation_prefix = f"Dr. {owner_name}" if (cat_slug == "dentists" and owner_name) else owner_name

    VERTICAL_HINTS = {
        "dentists":   "dental clinic. Speak clinically. Use: patients, recall, check-up, treatment. No cure guarantees.",
        "pharmacies": "pharmacy. Speak clinically + conveniently. Use: patients, prescriptions, refills, home delivery.",
        "gyms":       "fitness centre / gym. Speak motivationally. Use: members, sessions, walk-ins, classes, memberships.",
        "salons":     "beauty salon / spa. Speak aspirationally. Use: clients, appointments, treatments, bookings.",
        "spas":       "spa. Speak aspirationally. Use: clients, appointments, treatments, bookings.",
        "restaurants":"restaurant. Speak as a peer operator. Use: covers, orders, footfall, combos, delivery.",
    }
    vertical_hint = VERTICAL_HINTS.get(cat_slug, f"{cat_slug or 'local business'}. Match language to the merchant category.")

    preamble = (
        f"MERCHANT VERTICAL: {vertical_hint}\n"
        f"MERCHANT: {merchant_name_val}"
        + (f" in {locality_val}" if locality_val else "")
        + (f" | Owner: {salutation_prefix}" if salutation_prefix else "")
        + f"\nTRIGGER TYPE: {trigger_kind}\n"
        f"TASK: Write a WhatsApp message to the OWNER (not the customer). "
        f"Every sentence must reflect {cat_slug or 'this category'}'s specific language and business context. "
        f"No snake_case, no raw ISO timestamps, no raw JSON keys in the output.\n\n"
    )
    user_prompt = preamble + "CONTEXT:\n" + json.dumps(slim, default=str)[:7500]


    async def _call() -> Optional[dict]:
        try:
            raw = await asyncio.to_thread(_llm_gemini, user_prompt)
            parsed = _parse_llm_json(raw)
            if parsed:
                return parsed
        except Exception:
            pass
        try:
            raw = await asyncio.to_thread(_llm_groq, user_prompt)
            return _parse_llm_json(raw)
        except Exception:
            return None

    try:
        return await asyncio.wait_for(_call(), timeout=timeout_s)
    except Exception:
        return None


async def compose_message(
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: Optional[dict] = None,
    history: Optional[list] = None,
    extra_instruction: str = "",
    use_llm: bool = True,
) -> dict:
    fallback = compose_template(category, merchant, trigger, customer)
    if use_llm:
        llm = await compose_llm(
            category, merchant, trigger, customer, history or [], extra_instruction
        )
        if llm:
            fallback["body"] = llm["body"]
            fallback["cta"] = llm.get("cta") or fallback["cta"]
            fallback["rationale"] = llm.get("rationale") or fallback["rationale"]
    fallback["body"] = strip_urls(fallback["body"])
    return fallback


def action_reply_for_accept(merchant: dict, category: dict, conv: dict) -> dict:
    who = salutation(merchant, category)
    offer = pick_service_price(merchant, category)
    name = merchant_name(merchant)
    ident = merchant.get("identity") or {}
    locality = ident.get("locality") or ""
    perf = merchant.get("performance") or {}
    last_bot = ""
    for turn in reversed(conv.get("turns") or []):
        if turn.get("from") in ("vera", "bot"):
            last_bot = turn.get("body") or ""
            break
    if last_bot:
        # Already mid-conversation: just confirm and execute
        body = (
            f"Got it, {who}! I'm on it — preparing the campaign now. "
            f"Reply CONFIRM and I'll push it live."
        )
    else:
        # Fresh accept: paint a clear next-step picture with concrete offer
        loc_bit = f"for your {locality} " if locality else "for your "
        body = (
            f"Got it, {who}! I've prepped the draft {loc_bit}— {offer} as the lead. "
            f"Reply CONFIRM and I'll send it out right now."
        )
    return {
        "action": "send",
        "body": strip_urls(body),
        "cta": "binary_confirm_cancel",
        "rationale": "Merchant explicit accept. Switched to action mode: drafting/sending immediately, no re-qualification.",
    }
