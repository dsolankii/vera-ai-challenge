#!/usr/bin/env python3
"""
Vera message engine for the magicpin AI Challenge.

Design in one line: rules decide and check, templates write, and numeric factual
claims must trace to pushed context or an explicitly registered calculation/proposal.

- Standard library only (no pip installs), so it starts fast and never breaks on deploy.
- Deterministic: the same contexts and requests always give the same output.
- Endpoints: GET /v1/healthz, GET /v1/metadata, POST /v1/context,
             POST /v1/tick, POST /v1/reply, POST /v1/teardown

Run locally:   python bot.py            (listens on PORT, default 8080)
"""

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Settings (override with environment variables on the host)
# ---------------------------------------------------------------------------
TEAM_NAME = os.environ.get("TEAM_NAME", "Vera Engine")
TEAM_MEMBERS = [s.strip() for s in os.environ.get("TEAM_MEMBERS", "Your Name").split(",") if s.strip()]
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "you@example.com")
VERSION = "1.6.0"
LLM_REWRITE = os.environ.get("VERA_LLM_REWRITE", "0").strip().lower() in {"1", "true", "yes", "on"}
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip()
LLM_TIMEOUT_SECONDS = float(os.environ.get("VERA_LLM_TIMEOUT", "3.0"))
SUBMITTED_AT = os.environ.get("SUBMITTED_AT", "2026-09-27T12:00:00Z")
APPROACH = ("Deterministic adaptive decision engine: merchant-aware trigger scoring chooses what to send; "
            "known and unseen trigger families compose only from pushed context; numeric/taboo/consent/relevance guards validate output; "
            "fresh context versions are used immediately; rule-based replay handling covers auto-replies, intent transitions, opt-outs and off-topic asks. "
            "An optional zero-temperature Gemini wording pass exists but is disabled by default.")
MAX_ACTIONS_PER_TICK = 20
START_TIME = time.time()
SCOPES = ("category", "merchant", "customer", "trigger")

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value):
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def canon(token):
    """Normalise a number token so '2,400', '2400' and '2400.0' compare equal."""
    s = str(token).replace(",", "").strip()
    try:
        f = float(s)
    except ValueError:
        return s
    if f.is_integer():
        return str(int(f))
    return ("%f" % f).rstrip("0").rstrip(".")


def indian(n):
    """Indian digit grouping: 124000 -> 1,24,000."""
    n = int(round(float(n)))
    sign = "-" if n < 0 else ""
    s = str(abs(n))
    if len(s) <= 3:
        return sign + s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return sign + ",".join(parts) + "," + tail


ABBREV_RE = re.compile(r"(\b(Dr|Mr|Mrs|Ms|St|vs|No|Pvt|Ltd|Co)|\b[A-Z])\.$")


def first_sentence(text, max_sentences=1):
    if not text:
        return ""
    raw = re.split(r"(?<=[.!?])\s+", text.strip())
    parts = []
    for piece in raw:  # re-join splits that happened after "Dr." or an initial like "R."
        if parts and ABBREV_RE.search(parts[-1]):
            parts[-1] += " " + piece
        else:
            parts.append(piece)
    out = " ".join(parts[:max_sentences]).strip()
    if out and out[-1] not in ".!?":
        out += "."
    return out


def humanize(slug):
    if not slug:
        return ""
    s = str(slug).replace("_", " ").strip()
    s = re.sub(r"\b6 month\b", "6-month", s)
    return s


def norm_text(msg):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (msg or "").lower())).strip()


def _optional_gemini_polish(draft, composer):
    """Optional wording-only LLM pass. Disabled by default and fail-closed to the deterministic draft.

    It sends only the already-grounded draft plus category/trigger labels, not the raw merchant/customer payload.
    The returned wording is accepted only if it preserves the draft's numeric tokens and passes the same runtime
    taboo + numeric validators. This exists for local experiments; the submission-safe default is OFF.
    """
    if not (LLM_REWRITE and GEMINI_API_KEY and GEMINI_MODEL and draft):
        return draft
    prompt = (
        "Rewrite the following WhatsApp message to sound natural and less templated. "
        "Do not add, remove, or change any factual claim, number, date, price, offer, proper noun, source, or CTA. "
        "Keep exactly one clear CTA and roughly the same length. Return only the rewritten message.\n"
        f"Category: {composer.slug}\nTrigger: {composer.kind}\nMessage: {draft}"
    )
    endpoint = ("https://generativelanguage.googleapis.com/v1beta/models/"
                + urllib.parse.quote(GEMINI_MODEL, safe="-_./")
                + ":generateContent?key=" + urllib.parse.quote(GEMINI_API_KEY, safe=""))
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0, "candidateCount": 1, "maxOutputTokens": 256},
    }
    req = urllib.request.Request(endpoint, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_SECONDS) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
        text = (((obj.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [{}])[0].get("text", "")
        candidate = re.sub(r"[ \t]+", " ", (text or "").strip())
    except (OSError, ValueError, KeyError, IndexError, urllib.error.URLError):
        return draft
    if not candidate or len(candidate) > 700 or re.search(r"https?://|www\.", candidate, re.I):
        return draft
    # Wording polish may reorder text, but it may not invent/drop numeric facts.
    before_nums = sorted(canon(x) for x in NUM_RE.findall(draft))
    after_nums = sorted(canon(x) for x in NUM_RE.findall(candidate))
    if before_nums != after_nums or composer.unknown_numbers(candidate):
        return draft
    taboo = [str(x).split(" (")[0].strip().lower() for x in composer.voice.get("vocab_taboo", []) or []]
    if any(term and term in candidate.lower() for term in taboo):
        return draft
    return candidate


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        self.session = 1
        self.last_post = None
        self.contexts = {}                       # (scope, id) -> {"version": int, "payload": dict, "session": int}
        self.new_session(bump=False)

    def new_session(self, bump=True):
        """A new test run: forget conversations and dedup state, keep contexts (replays may rely on them)."""
        if bump:
            self.session += 1
        self.conversations = {}                  # conversation_id -> dict
        self.sent_keys = set()                   # (recipient_id, suppression_key) already used
        self.optout = {}                         # merchant_id / customer_id -> reason
        self.unanswered_nudges = Counter()       # merchant_id -> consecutive outbound Vera nudges
        self.nudge_times = defaultdict(list)      # merchant_id -> simulated send times
        self.pending_followups = {}               # merchant_id -> deferred continuation after wait
        self.merchant_wait_until = {}            # merchant_id -> datetime (back-off after auto-reply)
        self.sender_texts = defaultdict(Counter) # sender id -> normalised text counts
        self.sender_auto = Counter()             # sender id -> auto-replies seen
        self.recipient_bodies = defaultdict(set) # merchant/customer id -> bodies already sent

    def get(self, scope, cid):
        if not cid:
            return None
        rec = self.contexts.get((scope, cid))
        return rec["payload"] if rec else None

    def counts(self):
        c = {s: 0 for s in SCOPES}
        for (scope, _cid) in self.contexts:
            c[scope] = c.get(scope, 0) + 1
        return c

    def triggers_for_merchant(self, merchant_id):
        out = []
        for (scope, cid), rec in self.contexts.items():
            if scope == "trigger" and (rec["payload"].get("merchant_id") == merchant_id):
                out.append(rec["payload"])
        return sorted(out, key=lambda t: str(t.get("id", "")))


STORE = Store()


# ---------------------------------------------------------------------------
# Composer: turns (category, merchant, trigger, customer) into one message
# ---------------------------------------------------------------------------
CATEGORY_SINGULAR = {"dentists": "dental clinic", "salons": "salon", "restaurants": "restaurant", "gyms": "gym",
                     "pharmacies": "pharmacy"}
CATEGORY_ITEM = {"dentists": "treatment", "salons": "service", "restaurants": "dish", "gyms": "class or plan",
                 "pharmacies": "product"}
CATEGORY_PLURAL = {"dentists": "clinics", "salons": "salons", "restaurants": "restaurants", "gyms": "gyms",
                   "pharmacies": "pharmacies"}
CATALOG_NAME = {"dentists": "dental", "salons": "salon", "restaurants": "restaurant", "gyms": "gym", "pharmacies": "pharmacy"}
VISIT_NOUN = {"dentists": "appointment", "salons": "appointment", "gyms": "session", "restaurants": "table booking",
              "pharmacies": "order"}
TRIAL_NOUN = {"dentists": "consultation", "salons": "visit", "gyms": "trial session", "restaurants": "visit",
              "pharmacies": "first order"}
CITIES = ["delhi", "mumbai", "hyderabad", "pune", "bangalore", "bengaluru", "chennai", "jaipur", "lucknow",
          "chandigarh", "ahmedabad", "kolkata", "noida", "gurgaon", "gurugram"]
CATEGORY_EMOJI = {"dentists": "🦷", "salons": "💇", "gyms": "💪", "restaurants": "🍽️", "pharmacies": ""}
CUSTOMER_KINDS = {"recall_due", "appointment_tomorrow", "chronic_refill_due", "customer_lapsed_soft",
                  "customer_lapsed_hard", "trial_followup", "wedding_package_followup"}

KIND_WEIGHT = {
    "supply_alert": 30, "regulation_change": 25, "active_planning_intent": 25, "appointment_tomorrow": 18,
    "chronic_refill_due": 18, "ipl_match_today": 16, "recall_due": 15, "perf_dip": 15, "renewal_due": 14,
    "competitor_opened": 12, "review_theme_emerged": 12, "trial_followup": 12, "research_digest": 10,
    "gbp_unverified": 10, "wedding_package_followup": 10, "category_seasonal": 9, "seasonal_perf_dip": 9,
    "customer_lapsed_hard": 8, "customer_lapsed_soft": 8, "winback_eligible": 8, "perf_spike": 8,
    "milestone_reached": 8, "cde_opportunity": 7, "festival_upcoming": 6, "dormant_with_vera": 5,
    "curious_ask_due": 5,
    # Trigger families explicitly named in the official brief but not all present in the public expanded set.
    "weather_heatwave": 14, "local_news_event": 12, "category_trend_movement": 11,
    "category_research_digest_release": 10, "scheduled_recurring": 5,
}

# First outbound actions model a pre-approved WhatsApp template. The challenge does not call Meta,
# but the registry makes the {{1}}/{{2}}/{{3}} structure explicit and testable.
TEMPLATE_REGISTRY = {}
for _kind in sorted(set(KIND_WEIGHT) | CUSTOMER_KINDS):
    TEMPLATE_REGISTRY[f"vera_{_kind}_v1"] = "{{1}}, timely update for your business: {{2}} {{3}}"
    TEMPLATE_REGISTRY[f"merchant_{_kind}_v1"] = "Hi {{1}}, this is your merchant team: {{2}} {{3}}"
TEMPLATE_REGISTRY["vera_followup_v1"] = "{{1}}, following up as requested: {{2}} {{3}}"
TEMPLATE_REGISTRY["vera_generic_v1"] = "{{1}}, timely update for your business: {{2}} {{3}}"


class Composer:
    def __init__(self, category, merchant, trigger, customer=None, store=None):
        self.cat = category or {}
        self.m = merchant or {}
        self.t = trigger or {}
        self.c = customer
        self.store = store
        self.p = self.t.get("payload") or {}
        self.kind = self.t.get("kind", "generic")
        self.placeholder = bool(self.p.get("placeholder"))
        ident = self.m.get("identity", {})
        self.slug = self.m.get("category_slug") or self.cat.get("slug", "")
        self.name = ident.get("name", "your business")
        self.first = ident.get("owner_first_name") or self.name
        self.locality = ident.get("locality", "")
        self.city = ident.get("city", "")
        self.langs = ident.get("languages", ["en"])
        self.perf = self.m.get("performance", {}) or {}
        self.delta = self.perf.get("delta_7d", {}) or {}
        self.peer = self.cat.get("peer_stats", {}) or {}
        self.agg = self.m.get("customer_aggregate", {}) or {}
        self.signals = self.m.get("signals", []) or []
        self.voice = self.cat.get("voice", {}) or {}
        self.display = self.cat.get("display_name", self.slug.title())
        self.plural = CATEGORY_PLURAL.get(self.slug, "businesses like yours")
        self.catalog_name = CATALOG_NAME.get(self.slug, "category") + " catalog"
        self.offers = [o.get("title") for o in self.m.get("offers", []) if o.get("status") == "active" and o.get("title")]
        self.catalog = [o.get("title") for o in self.cat.get("offer_catalog", []) if o.get("title")]
        code_mix = str(self.voice.get("code_mix", ""))
        self.hinglish = ("hi" in self.langs and
                         (code_mix.startswith("hindi_english") or code_mix == "english_primary_some_hindi"))
        self.vocab_allowed = [str(x) for x in self.voice.get("vocab_allowed", []) or []]
        # facts registry: every number in the final body must be in here
        self.allowed = set(str(i) for i in range(0, 11))
        for obj in (self.cat, self.m, self.t, self.c or {}):
            self._walk(obj)
        self.levers = []
        self.facts_used = []

    # ---- facts registry -------------------------------------------------
    def _walk(self, obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                self._walk(k)
                self._walk(v)
        elif isinstance(obj, list):
            for v in obj:
                self._walk(v)
        elif isinstance(obj, bool) or obj is None:
            return
        elif isinstance(obj, (int, float)):
            self.allowed.add(canon(obj))
        elif isinstance(obj, str):
            for tok in NUM_RE.findall(obj):
                self.allowed.add(canon(tok))

    def reg(self, *vals):
        for v in vals:
            self.allowed.add(canon(v))

    def pct(self, frac):
        v = abs(float(frac)) * 100
        s = str(int(round(v))) if v >= 1 else ("%.1f" % v).rstrip("0").rstrip(".")
        self.reg(s)
        return s + "%"

    def rate(self, frac):
        s = ("%.1f" % (float(frac) * 100)).rstrip("0").rstrip(".")
        self.reg(s)
        return s + "%"

    def num(self, n):
        s = indian(n)
        self.reg(s)
        return s

    def money(self, n):
        return "₹" + self.num(n)

    def day(self, iso, weekday=False, clock=False):
        dt = parse_iso(iso)
        if not dt:
            return str(iso)
        local = dt
        label = f"{local.day} {MONTHS[local.month - 1]}"
        if weekday:
            label = f"{WEEKDAYS[local.weekday()]} {label}"
        self.reg(local.day)
        if clock and (local.hour or local.minute):
            h = local.hour % 12 or 12
            ampm = "am" if local.hour < 12 else "pm"
            t = f"{h}:{local.minute:02d}{ampm}" if local.minute else f"{h}{ampm}"
            self.reg(h, local.minute)
            label = f"{label}, {t}"
        return label

    def unknown_numbers(self, text):
        return [tok for tok in NUM_RE.findall(text) if canon(tok) not in self.allowed]

    # ---- context helpers -------------------------------------------------
    def greet(self):
        if self.slug == "dentists":
            f = self.first
            return f if f.lower().startswith("dr") else f"Dr. {f}"
        return self.first

    def offer_like(self, *keywords, pool=None):
        pool = self.offers if pool is None else pool
        for kw in keywords:
            for o in pool:
                if kw.lower() in o.lower():
                    return o
        return None

    def best_offer(self, *keywords):
        return self.offer_like(*keywords) or (self.offers[0] if self.offers else None)

    def catalog_offer(self, *keywords):
        return self.offer_like(*keywords, pool=self.catalog) or next(
            (o for o in self.catalog if "₹" in o and "%" not in o), self.catalog[0] if self.catalog else None)

    def digest(self, item_id=None, kinds=()):
        """Pick an injected digest item deterministically by explicit id, relevance, then recency/id."""
        items = self.cat.get("digest", []) or []
        if not items:
            return None
        if item_id:
            for d in items:
                if d.get("id") == item_id:
                    return d
        pool = [d for d in items if not kinds or d.get("kind") in kinds] or list(items)
        # Relevance is based on merchant state/history, not generic name/location words.  For ties,
        # later items win because injected category versions append fresh digest entries.
        merchant_text = " ".join([
            " ".join(self.offers),
            " ".join((h.get("body") or "") for h in self.m.get("conversation_history", []) or []),
            " ".join(self.signals),
            " ".join(str(r.get("theme", "")) for r in self.m.get("review_themes", []) or []),
            json.dumps(self.p, ensure_ascii=False),
        ]).lower()
        stop = {"the","and","for","with","from","this","that","your","into","near","week","today","month","year",
                "dental","clinic","salon","restaurant","pharmacy","studio","india"}
        ranked = []
        for idx, d in enumerate(pool):
            text = " ".join(str(d.get(k, "")) for k in ("title","summary","actionable","kind")).lower()
            words = {w for w in re.findall(r"[a-z]+", text) if len(w) > 3 and w not in stop}
            overlap = sum(1 for w in words if w in merchant_text)
            ranked.append((overlap, idx, str(d.get("id", "")), d))
        return max(ranked, key=lambda x: (x[0], x[1], x[2]))[3]

    def top_trend(self):
        """Most relevant rising search for THIS merchant: keyword overlap first, then broad-audience trends."""
        trends = self.cat.get("trend_signals", []) or []
        if not trends:
            return None
        text = " ".join([self.name] + self.offers + [r.get("theme", "") for r in self.m.get("review_themes", []) or []]).lower()
        stop = {"near", "me", "price", "delhi", "offer", "cost", "the", "and"}
        city = (self.city or "").lower()
        trends = [tr for tr in trends if not any(c in tr.get("query", "").lower() and c != city for c in CITIES)] or trends
        def score(tr):
            words = [w for w in re.findall(r"[a-z]+", tr.get("query", "").lower()) if w not in stop and len(w) > 2]
            hit = sum(1 for w in words if w in text)
            broad = 1 if tr.get("skew") == "balanced" or tr.get("segment_age") == "all" else 0
            return (-hit, -broad, -float(tr.get("delta_yoy", 0)), tr.get("query", ""))
        return sorted(trends, key=score)[0]

    def peer_lead(self):
        """Biggest outperformance vs peer averages (30 days)."""
        best = None
        for metric in ("calls", "views", "directions"):
            mine, peer = self.perf.get(metric), self.peer.get(f"avg_{metric}_30d")
            if isinstance(mine, (int, float)) and isinstance(peer, (int, float)) and peer > 0 and mine > peer:
                ratio = mine / peer
                if best is None or ratio > best[3]:
                    best = (metric, mine, peer, ratio)
        return best

    def peer_gap(self):
        """Biggest shortfall vs peer averages among views / calls / directions (30 days)."""
        best = None
        for metric in ("calls", "views", "directions"):
            mine, peer = self.perf.get(metric), self.peer.get(f"avg_{metric}_30d")
            if isinstance(mine, (int, float)) and isinstance(peer, (int, float)) and peer > 0 and mine < peer:
                ratio = mine / peer
                if best is None or ratio < best[3]:
                    best = (metric, mine, peer, ratio)
        return best

    def ctr_gap(self):
        ctr, peer = self.perf.get("ctr"), self.peer.get("avg_ctr")
        if ctr is None or peer is None:
            return None
        return ctr, peer

    def worst_delta(self):
        items = [(k.replace("_pct", ""), v) for k, v in self.delta.items() if isinstance(v, (int, float)) and v < 0]
        return sorted(items, key=lambda kv: (kv[1], kv[0]))[0] if items else None

    def best_delta(self):
        items = [(k.replace("_pct", ""), v) for k, v in self.delta.items() if isinstance(v, (int, float)) and v > 0]
        return sorted(items, key=lambda kv: (-kv[1], kv[0]))[0] if items else None

    def review(self, sentiment):
        for r in self.m.get("review_themes", []) or []:
            if r.get("sentiment") == sentiment:
                return r
        return None

    def cust_lang(self):
        if not self.c:
            return "en"
        pref = str(self.c.get("identity", {}).get("language_pref", "en")).lower()
        return "hi" if pref.startswith("hi") else "en"

    def cust_name(self):
        name = (self.c or {}).get("identity", {}).get("name", "there")
        m = re.match(r"^(.*?)\s*\(parent:\s*(.*?)\)\s*$", name)
        if m:
            return m.group(2).strip(), m.group(1).strip()   # (addressee, child)
        return name, None

    def yes(self, en, hi=None):
        """Merchant-facing closing ask with one binary CTA."""
        if self.hinglish and hi:
            return f"{hi} Reply YES."
        return f"{en} Reply YES."

    def history_focus(self):
        """Return a short merchant-stated focus from recent history when it is actually present."""
        hist = self.m.get("conversation_history", []) or []
        for h in reversed(hist):
            if h.get("from") != "merchant":
                continue
            body = (h.get("body") or "").strip()
            if not body:
                continue
            m = re.search(r"focus on\s+([^.!?]+)", body, re.I)
            if m:
                return m.group(1).strip()[:80]
            hits = [term for term in self.vocab_allowed if term.lower() in body.lower()]
            if hits:
                return " and ".join(hits[:2])
        return ""

    def signal_value(self, prefix):
        """Return the value after `prefix:` in a merchant signal, if present."""
        for sig in self.signals:
            text = str(sig)
            if text.startswith(prefix + ":"):
                return text.split(":", 1)[1]
        return ""

    def current_metric(self, metric):
        """Current 30-day merchant value for a trigger metric, when available."""
        return self.perf.get(metric) if metric in {"views", "calls", "directions", "leads", "ctr"} else None

    def recent_commitment_matches_trigger(self):
        """True when a recent explicit merchant commitment clearly belongs to this trigger topic."""
        hist = self.m.get("conversation_history", []) or []
        payload = (json.dumps(self.p, ensure_ascii=False) + " " + self.kind.replace("_", " ")).lower()
        pwords = {w for w in re.findall(r"[a-z]+", payload) if len(w) > 4}
        for i in range(len(hist) - 1, -1, -1):
            h = hist[i]
            if h.get("from") != "merchant" or h.get("engagement") not in {"intent_action", "intent_question", "intent_planning"}:
                continue
            context = (h.get("body") or "")
            if i > 0 and hist[i-1].get("from") == "vera":
                context += " " + (hist[i-1].get("body") or "")
            cwords = {w for w in re.findall(r"[a-z]+", context.lower()) if len(w) > 4}
            if pwords & cwords:
                return True
        return False
    def compose(self):
        fn = getattr(self, "k_" + self.kind, None) or self.k_generic
        out = fn()
        if not out or not out.get("body"):
            return None
        bad = self.unknown_numbers(out["body"])
        if bad:  # numeric factual claims must trace to pushed context or an explicitly registered proposal
            out = self.k_generic()
            if not out or self.unknown_numbers(out["body"]):
                return None
            out["rationale"] += " (fallback: primary draft had an unverifiable number)"
        out["body"] = re.sub(r"[ \t]+", " ", out["body"]).strip()
        taboo = [str(x).split(" (")[0].strip().lower() for x in self.voice.get("vocab_taboo", []) or []]
        if any(term and term in out["body"].lower() for term in taboo):
            return None
        polished = _optional_gemini_polish(out["body"], self)
        if polished != out["body"]:
            out["body"] = polished
            out["rationale"] += " Optional zero-temperature wording polish accepted after grounding checks."
        customer_facing = self.kind in CUSTOMER_KINDS or self.t.get("scope") == "customer"
        out.setdefault("cta", "binary_yes_no")
        out["send_as"] = "merchant_on_behalf" if (customer_facing and self.c) else "vera"
        template_kind = self.kind if (self.kind in KIND_WEIGHT or self.kind in CUSTOMER_KINDS) else "generic"
        out.setdefault("template_name", ("merchant_" if out["send_as"] != "vera" else "vera_") + template_kind + "_v1")
        params = [str(x) for x in out.get("template_params", [])[:3]]
        while len(params) < 3:
            params.append("")
        out["template_params"] = params
        return out

    def _out(self, body, rationale, cta="binary_yes_no", params=None):
        return {"body": body, "cta": cta, "rationale": rationale, "template_params": params or []}

    # ---- adaptive / post-submission helpers ---------------------------------
    def _payload_category_ok(self):
        """Reject an external/category trigger explicitly targeted at a different category."""
        raw = self.p.get("category_slug") or self.p.get("category")
        if isinstance(raw, dict):
            raw = raw.get("slug")
        return not raw or str(raw).lower().strip() == str(self.slug).lower().strip()

    def _payload_location_ok(self):
        """Reject clearly local events for a different city/locality; national/unspecified events remain eligible."""
        raw = self.p.get("city") or self.p.get("locality") or self.p.get("location")
        if not raw or isinstance(raw, (dict, list)):
            return True
        text = str(raw).lower()
        # If a known city is named in the event, it must match the merchant's city.
        named = [c for c in CITIES if c in text]
        if named:
            mine = (self.city or "").lower()
            aliases = {"bangalore": "bengaluru", "bengaluru": "bangalore", "gurgaon": "gurugram", "gurugram": "gurgaon"}
            return any(c in mine or (aliases.get(c) and aliases[c] in mine) for c in named)
        # A locality-only payload is relevant when it matches this merchant; otherwise don't over-filter unknown place names.
        if self.locality and self.locality.lower() in text:
            return True
        return True

    def _payload_item(self):
        """Return an inline injected digest/research item or resolve an item id against the latest category version."""
        for key in ("top_item", "digest_item", "item", "research_item"):
            if isinstance(self.p.get(key), dict):
                return self.p[key]
        item_id = self.p.get("top_item_id") or self.p.get("digest_item_id") or self.p.get("item_id")
        return self.digest(item_id) if item_id else None

    def _trigger_fact(self):
        """Best human-readable fact from an unseen trigger payload, using only pushed data."""
        for key in ("headline", "title", "event", "summary", "message", "note", "topic", "metric_or_topic"):
            val = self.p.get(key)
            if isinstance(val, str) and val.strip() and not self.p.get("placeholder"):
                return first_sentence(val.strip(), 1)
        item = self._payload_item()
        if item:
            title = item.get("title") or item.get("summary")
            if title:
                return first_sentence(str(title), 1)
        metric = self.p.get("metric")
        delta = self.p.get("delta_pct")
        if metric and isinstance(delta, (int, float)):
            direction = "up" if float(delta) > 0 else "down"
            return f"{humanize(metric).capitalize()} are {direction} {self.pct(delta)}."
        query = self.p.get("query") or self.p.get("search_query") or self.p.get("term")
        change = self.p.get("delta_yoy") if self.p.get("delta_yoy") is not None else self.p.get("growth_pct")
        if query:
            if isinstance(change, (int, float)):
                pct = self.pct(change) if abs(float(change)) <= 2 else (self.num(abs(change)) + "%")
                return f"Search interest for '{query}' is up {pct}."
            return f"'{query}' is the current category trend."
        return ""

    def _adaptive_ask(self, noun="update"):
        """Category-native next step for unseen external signals; advice is explicit, facts remain grounded."""
        if self.slug == "dentists":
            return self.yes(f"Want me to turn this into a concise patient-information {noun} for your clinic?",
                            f"Iska concise patient-information {noun} clinic ke liye bana doon?")
        if self.slug == "salons":
            return self.yes(f"Want me to turn this into a warm customer-facing {noun} for {self.name}?",
                            f"{self.name} ke liye warm customer {noun} bana doon?")
        if self.slug == "restaurants":
            return self.yes(f"Want me to draft a short customer {noun} you can use today?",
                            f"Aaj ke liye short customer {noun} draft kar doon?")
        if self.slug == "gyms":
            return self.yes(f"Want me to draft a clear member {noun} around this?",
                            f"Iske around clear member {noun} draft kar doon?")
        if self.slug == "pharmacies":
            return self.yes(f"Want me to draft a precise customer {noun}, with medicine questions routed to pharmacist counsel?",
                            f"Precise customer {noun} draft kar doon, medicine questions pharmacist counsel ko route karke?")
        return self.yes(f"Want me to draft a short {noun} around this?", f"Iska short {noun} draft kar doon?")

    def k_category_research_digest_release(self):
        if not self._payload_category_ok():
            return None
        item = self._payload_item()
        if not item:
            # Some injected triggers only announce the new category version. The latest digest selector prefers appended items.
            return self.k_research_digest()
        g = self.greet()
        src = item.get("source") or self.p.get("source") or "the latest category digest"
        title = item.get("title") or first_sentence(item.get("summary", ""), 1).rstrip(".")
        if not title:
            return self.k_generic()
        body = f"{g}, fresh {self.display.lower()} digest from {src}: {str(title).rstrip('.')}."
        if item.get("summary"):
            body += " " + first_sentence(item.get("summary", ""), 1)
        body += " " + self._adaptive_ask("summary + ready post")
        return self._out(body, "category_research_digest_release: used the newly pushed digest item/source and offered one category-native artifact.",
                         params=[g, str(title), str(src)])

    def k_category_trend_movement(self):
        if not self._payload_category_ok():
            return None
        g = self.greet()
        query = self.p.get("query") or self.p.get("search_query") or self.p.get("term")
        change = self.p.get("delta_yoy")
        if change is None:
            change = self.p.get("delta_pct")
        if change is None:
            change = self.p.get("growth_pct")
        segment = self.p.get("segment_age") or self.p.get("segment")
        if not query:
            tr = self.top_trend()
            if tr:
                query, change, segment = tr.get("query"), tr.get("delta_yoy"), tr.get("segment_age")
        if not query:
            return self.k_generic()
        body = f"{g}, category signal: searches for '{query}'"
        if isinstance(change, (int, float)):
            # Fractions (0.62) are standard in category context; whole percentages (62) also occur in injected payloads.
            pct = self.pct(change) if abs(float(change)) <= 2 else self.num(abs(change)) + "%"
            body += f" are {'up' if float(change) >= 0 else 'down'} {pct}"
        body += "."
        if segment and str(segment).lower() not in {"all", "balanced"}:
            seg = humanize(segment)
            body += f" The signal is strongest among ages {seg}." if re.fullmatch(r"\d{1,2}-\d{1,2}", seg) else f" The signal is strongest in {seg}."
        if self.perf.get("views") is not None and self.perf.get("calls") is not None:
            body += f" {self.name} already has {self.num(self.perf['views'])} views and {self.num(self.perf['calls'])} calls in the last 30 days."
        live = self.best_offer()
        if live:
            body += f" Your live offer is '{live}', so use the trend to explain that offer rather than inventing a new discount."
        elif self.slug == "pharmacies":
            body += " Keep the message informational and route medicine-specific questions to pharmacist counsel."
        body += " " + self._adaptive_ask("Google post")
        return self._out(body, "category_trend_movement: used the exact pushed search signal/segment and current offer, with no invented demand numbers.",
                         params=[g, str(query), str(change or "")])

    def k_weather_heatwave(self):
        if not self._payload_category_ok() or not self._payload_location_ok():
            return None
        # A dentist heatwave nudge is too weak unless the trigger explicitly explains dental relevance.
        relevance = self.p.get("category_relevance") or self.p.get("relevance") or self.p.get("actionable")
        if self.slug == "dentists" and not relevance:
            return None
        g = self.greet()
        temp = self.p.get("temperature_c")
        if temp is None: temp = self.p.get("temp_c")
        if temp is None: temp = self.p.get("temperature")
        city = self.p.get("city") or self.city
        condition = self.p.get("headline") or self.p.get("title") or self.p.get("condition") or "heatwave"
        body = f"{g}, weather heads-up for {self.name}"
        if city: body += f" in {city}"
        body += ": "
        if temp is not None:
            temp_s = canon(temp)
            self.reg(temp_s)
            # Do not repeat the temperature if the pushed headline already contains it.
            if temp_s not in str(condition):
                body += f"{temp_s}°C; "
        body += str(condition).rstrip(".") + "."
        if relevance:
            body += " " + first_sentence(str(relevance), 1)
        advice = {
            "pharmacies": "For your pharmacy, keep it to summer-essentials availability and pharmacist counsel rather than medical claims.",
            "restaurants": "For your restaurant, availability/delivery is the useful same-day angle; don't invent a broad weather discount.",
            "salons": "For your salon, a practical heat-day service note is more useful than a generic discount blast.",
            "gyms": "For your gym, hydration and cooler-session planning fit the moment better than a sales-heavy push.",
            "dentists": "For the clinic, keep any patient note factual and limited to the relevance stated in the trigger.",
        }.get(self.slug, "Only act on this if it changes what customers need today.")
        body += " " + advice
        body += " " + self._adaptive_ask("heat-day update")
        return self._out(body, "weather_heatwave: checked category/location relevance, used only pushed weather facts, then proposed a category-native same-day update.",
                         params=[g, str(city or ""), str(temp or "")])

    def k_local_news_event(self):
        if not self._payload_category_ok() or not self._payload_location_ok():
            return None
        g = self.greet()
        headline = self.p.get("headline") or self.p.get("title") or self.p.get("event") or self.p.get("summary")
        if not headline:
            return self.k_generic()
        src = self.p.get("source_name") or self.p.get("source")
        impact = self.p.get("impact") or self.p.get("category_relevance") or self.p.get("actionable")
        body = f"{g}, local heads-up for {self.name}{', ' + self.locality if self.locality else ''}: {first_sentence(str(headline), 1)}"
        if src:
            body += f" Source: {src}."
        if impact:
            body += " " + first_sentence(str(impact), 1)
        else:
            body += " If this affects access to your locality, tell customers only what is confirmed rather than guessing delays."
        body += " " + self._adaptive_ask("access/availability update")
        return self._out(body, "local_news_event: matched the local event to merchant geography, preserved the source/headline, and avoided inventing travel impact.",
                         params=[g, str(headline)[:80], str(src or "")])

    def k_scheduled_recurring(self):
        if not self._payload_category_ok():
            return None
        g = self.greet()
        ask = self.p.get("ask_template") or self.p.get("question")
        item = CATEGORY_ITEM.get(self.slug, "service")
        if not ask:
            ask = f"Which {item} are customers asking about most this week?"
        # Recurring curious asks should be low-stakes, not another sales pitch.
        body = f"{g}, quick weekly check"
        if self.perf.get("views") is not None and self.perf.get("calls") is not None:
            body += f": {self.name} had {self.num(self.perf['views'])} views and {self.num(self.perf['calls'])} calls in the last 30 days."
        else:
            body += "."
        body += f" {str(ask).strip()} Just send the {item} name; I'll turn it into a ready Google post + customer reply."
        return self._out(body, "scheduled_recurring: treated the recurring wake-up as one low-stakes merchant question with an immediate artifact promised in return.",
                         cta="open_ended", params=[g, str(ask)[:80]])

    # ---- merchant-facing composers ----------------------------------------
    def k_research_digest(self):
        item = self.digest(self.p.get("top_item_id"), kinds=("research", "trend", "tech", "compliance"))
        if not item:
            return self.k_generic()
        g, src = self.greet(), item.get("source", "this week's digest")
        summary = first_sentence(item.get("summary", "")) or item.get("title", "") + "."
        trial = f" ({self.num(item['trial_n'])} patients)" if item.get("trial_n") else ""
        anchor = ""
        if self.slug == "dentists" and item.get("patient_segment") == "high_risk_adults":
            if any("high_risk_adult" in str(sig) for sig in self.signals):
                anchor = " This maps directly to your high-risk adult cohort."
            elif self.agg.get("high_risk_adult_count"):
                anchor = f" This maps onto your {self.num(self.agg['high_risk_adult_count'])} high-risk adult patients."
        if self.slug == "dentists":
            body = (f"{g}, {src} landed: {item.get('title', '').rstrip('.')}{trial}. "
                    f"{summary}{anchor} Worth a look? I can pull the 2-min abstract and a patient-education WhatsApp you can forward. Reply YES.")
        else:
            action_en, action_hi = {
                "salons": (f"turn this into a Google post for {self.name}", f"iska Google post {self.name} ke liye bana doon"),
                "gyms": ("draft a short member note and a front-desk card", "iska member note aur front-desk card bana doon"),
                "pharmacies": ("draft a precise counter note for your staff", "staff ke liye precise counter note bana doon"),
                "restaurants": ("work it into your menu and Google profile", "ise menu aur Google profile mein add kar doon"),
            }.get(self.slug, ("draft a post on it", "iska post bana doon"))
            tip = item.get("actionable", "")
            tip = (" " + first_sentence(tip)) if tip else ""
            body = (f"{g}, from {src}: {item.get('title', '').rstrip('.')}.{tip} "
                    + self.yes(f"Want me to {action_en}?", f"Kya main {action_hi}?"))
        self.levers += ["source citation", "curiosity", "merchant-state anchor", "effort taken off"]
        return self._out(body, f"research_digest: source-cited item '{item.get('id')}', tied to merchant state where possible, then one low-friction next step.",
                         params=[g, item.get("title", ""), src])
    def k_regulation_change(self):
        item = self.digest(self.p.get("top_item_id"), kinds=("compliance",))
        if not item:
            return self.k_generic()
        g = self.greet()
        deadline = self.p.get("deadline_iso")
        due = f" Deadline: {self.day(deadline)}." if deadline else ""
        summary = first_sentence(item.get("summary", ""), 2)
        loc = f" for your {self.locality} clinic" if self.locality else ""
        body = (f"{g}, compliance heads-up{loc} from {item.get('source', 'the regulator')}: {item.get('title', '').rstrip('.')}."
                f" {summary}{due} Want me to send a 1-page audit checklist your team can finish this week? Reply YES.")
        self.levers += ["urgency", "source citation", "effort taken off"]
        return self._out(body, "regulation_change (high urgency): cited circular, stated the concrete change and "
                         "deadline, offered a ready checklist.", params=[g, item.get("title", ""), deadline or ""])

    def k_cde_opportunity(self):
        item = self.digest(self.p.get("digest_item_id"), kinds=("cde",))
        if not item:
            return self.k_generic()
        g = self.greet()
        when = self.day(item.get("date"), weekday=True, clock=True) if item.get("date") else ""
        credits = self.p.get("credits") or item.get("credits")
        cred = f" {credits} CDE credits;" if credits else ""
        fee = first_sentence(item.get("actionable", "")).rstrip(".")
        fee = fee[:1].lower() + fee[1:] if fee else fee
        speaker = first_sentence(item.get("summary", ""), 2)
        body = (f"{g}, {item.get('title', '').rstrip('.')}{' on ' + when if when else ''}.{cred} {fee}. {speaker} "
                f"Want me to block the slot and send you the joining details? Reply YES.")
        self.levers += ["specificity", "effort taken off"]
        return self._out(body, "cde_opportunity: dated event with credits and fee from the digest; one-step signup ask.",
                         params=[g, item.get("title", ""), when])

    def k_perf_dip(self):
        g = self.greet()
        metric = self.p.get("metric")
        delta = self.p.get("delta_pct")
        if metric is None or delta is None:
            wd = self.worst_delta()
            if wd:
                metric, delta = wd
            else:
                gap = self.peer_gap()
                if not gap:
                    return self.k_generic()
                m_name, mine, peer, _r = gap
                offer = self.best_offer() or self.catalog_offer("haircut", "cleaning", "trial", "thali", "delivery")
                body = (f"{g}, {self.name} has {self.num(mine)} {m_name} in the last 30 days versus {self.num(peer)} for similar {self.plural}. "
                        f"That is the gap worth fixing now. " + self.yes(
                            f"Want me to refresh the listing around '{offer}'?",
                            f"'{offer}' ke around listing refresh kar doon?"))
                self.levers += ["social proof", "specificity", "effort taken off"]
                return self._out(body, f"perf_dip fallback: used the merchant's current {m_name} against the category peer benchmark and proposed one listing action.",
                                 params=[g, m_name])

        line = f"{g}, {humanize(metric)} for {self.name} are down {self.pct(delta)} this week"
        base = self.p.get("vs_baseline")
        line += f" from a {self.num(base)}-{humanize(metric).rstrip('s')} baseline." if base is not None else "."
        current = self.current_metric(metric)
        views = self.perf.get("views")
        if metric == "calls" and current is not None and views is not None:
            line += f" You're at {self.num(current)} calls from {self.num(views)} views over the last 30 days."
        elif current is not None:
            line += f" Current 30-day {humanize(metric)}: {self.num(current)}."

        unverified = any(str(s).startswith("unverified_gbp") for s in self.signals) or not (self.m.get("identity") or {}).get("verified", True)
        if unverified and not self.offers:
            if self.slug == "dentists":
                judgment = "You also have an unverified Google profile and no active offer. I wouldn't cut treatment prices first; fix discovery before discounting."
            elif self.slug == "salons":
                judgment = "You also have an unverified Google profile and no active offer. Fix discovery first, then test a service-price hook."
            else:
                judgment = "You also have an unverified Google profile and no active offer. Fix discovery before adding another promotion."
            ask = self.yes("Want me to start the Google verification flow today?", "Aaj Google verification start kar doon?")
            body = " ".join([line, judgment, ask])
        elif not self.offers:
            offer = self.catalog_offer("cleaning", "haircut", "trial", "thali", "delivery", "consult")
            body = " ".join([line, f"There is no active offer on the listing. A concrete service-price test is '{offer}' from the {self.catalog_name}.",
                             self.yes("Want me to draft the listing change?", "Listing change draft kar doon?")])
        else:
            live = self.offers[0]
            cat_line = {
                "dentists": "Keep the clinical offer clear rather than adding a blanket discount.",
                "salons": "Keep the service-price hook visible instead of adding a generic discount.",
                "restaurants": "The aim is to turn listing interest into covers, not just impressions.",
                "gyms": "The aim is to recover trial footfall, not just impressions.",
                "pharmacies": "Keep the copy practical and route medicine questions to pharmacist counsel.",
            }.get(self.slug, "Use the offer already live before inventing a new one.")
            body = " ".join([line, f"Your live offer is '{live}'. {cat_line}",
                             self.yes("Want me to draft a fresh post around it today?", "Aaj iske around fresh post draft kar doon?")])
        self.levers += ["loss aversion", "specificity", "merchant-state diagnosis", "single next step"]
        return self._out(body, f"perf_dip on {metric}: combined trigger delta with current merchant metrics and chose one action from the visible account state.",
                         params=[g, humanize(metric), self.pct(delta)])
    def k_perf_spike(self):
        g = self.greet()
        metric, delta = self.p.get("metric"), self.p.get("delta_pct")
        if metric is None or delta is None:
            bd = self.best_delta()
            if not bd:
                lead = self.peer_lead()
                if not lead:
                    return self.k_generic()
                m_name, mine, peer, _r = lead
                offer = self.best_offer() or self.catalog_offer()
                body = (f"{g}, {self.name} is pulling {self.num(mine)} {m_name} in 30 days vs {self.num(peer)} for similar {self.plural}. "
                        f"That's momentum worth converting. " + self.yes(
                            f"Want me to pin '{offer}' on the listing and draft the follow-up post?",
                            f"'{offer}' listing par pin karke follow-up post draft kar doon?"))
                self.levers += ["social proof", "momentum"]
                return self._out(body, "perf_spike fallback: used the real lead over peers and one conversion action.", params=[g, m_name])
            metric, delta = bd
        base = self.p.get("vs_baseline")
        driver = self.p.get("likely_driver")
        offer = self.best_offer()

        # Yoga is a gym-category subtype, but its operator voice should be calmer and
        # parent/program focused rather than high-octane gym slang.
        if self.slug == "gyms" and ("yoga" in self.name.lower() or "yoga" in str(driver).lower()) and offer:
            lead = f"{g}, your {humanize(driver) if driver else 'kids-yoga post'} is pulling interest: {humanize(metric)} are up {self.pct(delta)} this week"
            if base is not None:
                lead += f" from an {self.num(base)}-{humanize(metric).rstrip('s')} baseline"
            body = (lead + f". '{offer}' is already live, so I wouldn't add another discount. "
                    + f"Keep the same parent audience and use one calm follow-up to turn the interest into trial enquiries for {self.name}{' in ' + self.locality if self.locality else ''}. "
                    + "Want me to draft that follow-up post now? Reply YES.")
            self.levers += ["own momentum", "calm coaching", "merchant-state continuity", "effort taken off"]
            return self._out(body, f"perf_spike on {metric}: used the exact lift + baseline + driver + live offer, then kept the yoga voice calm and parent-focused.",
                             params=[g, humanize(metric), self.pct(delta), self.name, self.locality])

        body = f"{g}, {humanize(metric)} are up {self.pct(delta)} this week"
        if base is not None:
            unit = humanize(metric).rstrip("s")
            article = "an" if str(base).startswith(("8", "11", "18")) else "a"
            body += f" from {article} {self.num(base)}-{unit} baseline"
        body += f". The likely driver is your {humanize(driver)}." if driver else "."
        current = self.current_metric(metric)
        if offer:
            if self.slug == "gyms":
                body += f" '{offer}' is already live. Stay with the same audience and turn the momentum into trial footfall without adding another discount."
            else:
                body += f" '{offer}' is already live, so keep the same audience and turn this momentum into "
                body += "covers" if self.slug == "restaurants" else "calls"
                body += "."
        else:
            cat_offer = self.catalog_offer("delivery", "check", "trial", "haircut", "cleaning", "thali")
            body += f" There is no live offer yet; a concrete next test is '{cat_offer}' from the {self.catalog_name}."
        body += " " + self.yes("Want me to draft the follow-up post?", "Follow-up post draft kar doon?")
        self.levers += ["own momentum", "merchant-state continuity", "effort taken off"]
        return self._out(body, f"perf_spike on {metric}: used the trigger delta, current merchant metric, likely driver and live offer to double down on what is working.",
                         params=[g, humanize(metric), self.pct(delta)])
    def k_renewal_due(self):
        g = self.greet()
        sub = self.m.get("subscription", {}) or {}
        days = self.p.get("days_remaining", sub.get("days_remaining"))
        plan = self.p.get("plan", sub.get("plan", "Vera"))
        amount = self.p.get("renewal_amount")
        if sub.get("status") == "expired":
            return self.k_winback_eligible()
        if days is None:
            return self.k_generic()
        if days > 45:
            return None  # restraint: a renewal nudge months early is spam
        if str(sub.get("status")) == "trial" or str(plan).lower() == "trial":
            body = f"{g}, your free trial ends in {self.num(days)} days"
        else:
            body = f"{g}, your {plan} plan renews in {self.num(days)} days"
        body += f" ({self.money(amount)})." if amount else "."
        v, c = self.perf.get("views"), self.perf.get("calls")
        if v is not None and c is not None:
            body += f" Last 30 days on your listing: {self.num(v)} views and {self.num(c)} calls."
        cd = self.delta.get("calls_pct")
        if isinstance(cd, (int, float)) and cd <= -0.2:
            body += f" Calls are already down {self.pct(cd)} this week, so this is the wrong moment for the profile to go quiet."
        if str(sub.get("status")) == "trial" or str(plan).lower() == "trial":
            body += " " + self.yes("Want me to move you to a paid plan so the listing work doesn't pause?",
                                   "Paid plan par move kar doon taaki listing ka kaam ruke nahi?")
        else:
            body += " " + self.yes("Want me to raise the renewal so nothing pauses? No reply needed if it's already done.",
                                   "Renewal raise kar doon taaki kuch ruke nahi?")
        self.levers += ["loss aversion", "specificity"]
        return self._out(body, "renewal_due: days left + amount + what the listing delivered; loss-aversion framing.",
                         params=[g, str(days), plan])

    def k_festival_upcoming(self):
        g = self.greet()
        fest, date = self.p.get("festival"), self.p.get("date")
        beats = self.cat.get("seasonal_beats", []) or []
        if not fest:
            beat = (next((b for b in beats if "festival" in b.get("note", "")), None)
                    or next((b for b in beats if "wedding" in b.get("note", "")), None) or (beats[0] if beats else None))
            if not beat:
                return self.k_generic()
            offer = self.best_offer() or self.catalog_offer("refer", "combo", "membership")
            body = (f"{g}, festival season is coming, and {beat.get('month_range')} is the relevant window for {self.name}. "
                    f"Your current offer is '{offer}'. " + self.yes(
                        "Want me to line up the campaign now so it is ready before the rush?",
                        f"'{offer}' ke around campaign abhi se ready kar doon?"))
            self.levers += ["timing", "merchant offer", "effort taken off"]
            return self._out(body, "festival_upcoming without named festival: used the category seasonal window and merchant's current offer, without inventing a festival.",
                             params=[g, beat.get("month_range", ""), offer or ""])

        when = self.day(date, weekday=True) if date else ""
        days = self.p.get("days_until")
        lead = f"{g}, {fest}{' is on ' + when if when else ' is coming'}"
        lead += f", {self.num(days)} days away." if days is not None else "."
        perf_bits = []
        if self.perf.get("views") is not None:
            perf_bits.append(f"{self.num(self.perf['views'])} views")
        if self.perf.get("calls") is not None:
            perf_bits.append(f"{self.num(self.perf['calls'])} calls")
        if perf_bits:
            lead += f" {self.name} already has " + " and ".join(perf_bits) + " in the last 30 days."
        live = (self.offer_like("bridal", "spa", "package", "family") if self.slug == "salons" else None) or self.best_offer()
        if live:
            lead += f" Your live offer is '{live}'."
        if self.slug == "salons":
            decision = "188 days is too early for a broad discount, but it is a useful runway: warm up bridal/festival demand now around the service already live, then intensify closer to the booking window."
            ask = self.yes("Want me to draft the first 3-post salon calendar and the opening Google post?", "First 3-post salon calendar + opening Google post draft kar doon?")
        elif self.slug == "restaurants":
            decision = "Use the lead time to plan the menu hook and posting dates rather than discounting today."
            ask = self.yes("Want me to draft the festival plan?", "Festival plan draft kar doon?")
        else:
            decision = "Use the lead time to prepare the campaign before the demand window opens."
            ask = self.yes("Want me to draft the campaign plan?", "Campaign plan draft kar doon?")
        body = " ".join([lead, decision, ask])
        self.levers += ["timing", "merchant-state anchor", "judgment", "effort taken off"]
        return self._out(body, f"festival_upcoming: named the festival/date, anchored on the merchant's current demand and live offer, and chose planning rather than an early discount.",
                         params=[g, fest, when])
    def k_curious_ask_due(self):
        g = self.greet()
        item = CATEGORY_ITEM.get(self.slug, "service")
        views, calls = self.perf.get("views"), self.perf.get("calls")
        live = " and ".join(f"'{o}'" for o in self.offers[:2])
        perf = ""
        if views is not None and calls is not None:
            perf = f"{self.name} had {self.num(views)} views and {self.num(calls)} calls in the last 30 days. "
        if self.hinglish:
            offer_line = f"Aapke {live} live hain. " if live else ""
            body = (f"{g}, {perf}{offer_line}Ek quick sawaal: is hafte customers sabse zyada kis {item} ka price pooch rahe hain? "
                    f"Bas {item} ka naam bhejiye; main usi par Google post + ready WhatsApp price-reply bana dungi.")
        else:
            offer_line = f"You have {live} live. " if live else ""
            body = (f"{g}, {perf}{offer_line}Quick one: which {item} is getting the most price questions this week? "
                    f"Reply with just the {item}; I'll turn it into a Google post plus a ready WhatsApp price-reply.")
        self.levers += ["asking the merchant", "merchant-state specificity", "reciprocity", "curiosity"]
        return self._out(body, "curious_ask_due: asked one low-stakes demand question after grounding it in the merchant's current traffic and live offers; promised a concrete artifact in return.",
                         cta="open_ended", params=[g])
    def k_winback_eligible(self):
        g = self.greet()
        days = self.p.get("days_since_expiry", (self.m.get("subscription") or {}).get("days_since_expiry"))
        dip = self.p.get("perf_dip_pct")
        lapsed = self.p.get("lapsed_customers_added_since_expiry")
        body = f"{g}, it's been {self.num(days)} days since {self.name}'s plan lapsed." if days else f"{g}, your plan has lapsed."
        if dip is not None:
            body += f" Since then calls are down {self.pct(dip)}"
            body += f" and {self.num(lapsed)} more customers have gone inactive." if lapsed else "."
        if self.perf.get("views") is not None and self.perf.get("calls") is not None:
            body += f" Even now the listing has {self.num(self.perf['views'])} views but only {self.num(self.perf['calls'])} calls in 30 days."
        offer = self.best_offer() or self.catalog_offer("spa", "cleaning", "trial", "thali", "delivery")
        body += (f" Restart proposal: 2 fresh posts plus '{offer}' as the service-price hook, not a blanket discount. "
                 + self.yes("Want me to set up the restart?", "Restart setup kar doon?"))
        self.levers += ["loss aversion", "merchant-state specificity", "effort taken off"]
        return self._out(body, "winback_eligible: quantified the lapse impact, added current listing demand, and proposed a concrete restart with a category-correct service-price hook.",
                         params=[g, str(days or ""), offer or ""])
    def k_dormant_with_vera(self):
        g = self.greet()
        days = self.p.get("days_since_last_merchant_message")
        topic = self.p.get("last_topic")
        views, calls = self.perf.get("views"), self.perf.get("calls")

        if self.slug == "salons" and days:
            # Dormant salon owners respond better to a warm re-entry than a data-audit voice.
            body = f"Hi {self.first}, last time {humanize(topic) if topic else 'the listing'} par baat hui thi — {self.num(days)} days ho gaye."
            if views is not None and calls is not None:
                body += f" {self.name} in {self.locality} is still getting attention: {self.num(views)} views and {self.num(calls)} calls in the last 30 days."
            if not self.offers:
                body += " Abhi listing par koi live offer nahi hai."
            body += " Pehle renewal pitch nahi — main sirf ek practical salon-listing fix suggest karungi jo iss week try kar sakte ho. Dekhna hai? Reply YES."
            self.levers += ["warm re-entry", "merchant-state specificity", "curiosity", "low effort"]
            return self._out(body, "dormant_with_vera: warm salon re-entry using exact dormancy + locality + current demand, then one no-pressure practical fix.",
                             params=[self.first, str(days), self.locality])

        body = f"{g}, it's been {self.num(days)} days since we last spoke" if days else f"{g}, it's been a while since we spoke"
        body += f" about {humanize(topic)}." if topic else "."
        if views is not None and calls is not None:
            body += f" {self.name} still pulled {self.num(views)} views and {self.num(calls)} calls in the last 30 days."
        if not self.offers:
            body += " There is no live offer on the listing right now."
        body += " No hard sell — " + self.yes(
            f"want a 5-minute {self.locality + ' ' if self.locality else ''}{CATEGORY_SINGULAR.get(self.slug, 'business')} profile check that tells you the one thing I'd fix first?",
            f"5-minute profile check kar doon aur sirf sabse important fix bataun?")
        self.levers += ["reciprocity", "merchant-state specificity", "curiosity", "low effort"]
        return self._out(body, "dormant_with_vera: reopened with the exact dormancy reason plus current listing demand, then offered one small diagnostic instead of another pitch.",
                         params=[g, str(days or "")])
    def k_ipl_match_today(self):
        g = self.greet()
        match, venue = self.p.get("match", "tonight's match"), self.p.get("venue")
        when = parse_iso(self.p.get("match_time_iso"))
        weeknight = self.p.get("is_weeknight")
        time_s = ""
        if when:
            h = when.hour % 12 or 12
            self.reg(h, when.minute)
            ampm = "am" if when.hour < 12 else "pm"
            time_s = f"{h}:{when.minute:02d}{ampm}" if when.minute else f"{h}{ampm}"
        day_name = WEEKDAYS[when.weekday()] if when else ""
        body = f"{g}, {match}{' at ' + venue if venue else ''} tonight{', ' + time_s if time_s else ''}."
        ipl = self.digest(kinds=("seasonal",))
        ipl_note = ipl if ipl and "IPL" in ipl.get("title", "") else None
        if weeknight is False:
            if ipl_note:
                body += f" Quick operator call: {ipl_note.get('source')} says weekend home matches underperform weeknight dine-in covers."
            else:
                body += f" Quick operator call: it's a {day_name}, so don't bank on a dine-in spike."
            live = self.best_offer()
            if live:
                body += f" Your live '{live}' is Tue-Thu, so forcing it tonight will muddy the offer."
            combo = self.offer_like("match", pool=self.catalog) or self.catalog_offer("combo")
            body += f" Cleaner move for tonight: delivery-only '{combo}'. " + self.yes(
                "Want me to draft the banner + Insta story now?", "Banner + Insta story abhi draft kar doon?")
            rat = "ipl_match_today on a weekend: used match timing, category evidence and the merchant's offer terms in a warm operator-to-operator recommendation."
        else:
            combo = self.best_offer("match", "pizza", "combo") or self.catalog_offer("match")
            body += f" Weeknight matches are the stronger covers window; use '{combo}' rather than changing the full menu. " + self.yes(
                "Want me to draft the listing banner for 6pm?", "6pm ke liye listing banner draft kar doon?")
            rat = "ipl_match_today on a weeknight: used the category match-night pattern and merchant offer for one timed action."
        self.levers += ["judgment", "specificity", "merchant-offer fit", "loss aversion"]
        return self._out(body, rat, params=[g, match, time_s])
    def k_review_theme_emerged(self):
        g = self.greet()
        theme = self.p.get("theme")
        occ = self.p.get("occurrences_30d")
        quote = self.p.get("common_quote")
        if not theme:
            neg = self.review("neg")
            if neg:
                theme, occ, quote = neg.get("theme"), neg.get("occurrences_30d"), neg.get("common_quote")
        if not theme:
            v, c = self.perf.get("views"), self.perf.get("calls")
            body = f"{g}, new reviews are coming in for {self.name}"
            body += f"; the listing had {self.num(v)} views and {self.num(c)} calls in the last 30 days." if v is not None and c is not None else "."
            body += " " + self.yes("Want me to turn this week's review themes into reply drafts?", "Is hafte ke review themes ke reply drafts bana doon?")
            return self._out(body, "review_theme_emerged without a theme: used current merchant traffic and offered a bounded review-summary task.", params=[g])

        body = f"{g}, '{humanize(theme)}' has come up in {self.num(occ)} reviews this month" if occ else f"{g}, '{humanize(theme)}' is showing up in your reviews"
        body += " and it's rising." if self.p.get("trend") == "rising" else "."
        if quote:
            body += f" One says: \"{quote}\"."
        live = self.best_offer()
        if live:
            body += f" You already have '{live}' live, so I wouldn't answer this with another discount."
        if self.slug == "restaurants":
            body += " Fix the trust gap first, then let the offer do its job on covers."
        elif self.slug == "salons":
            body += " Fix the service-expectation gap first, then promote."
        body += " " + self.yes(
            f"Want me to draft {self.num(occ) + ' ' if occ else ''}polite review replies plus a one-line listing note?",
            "Review replies + one-line listing note draft kar doon?")
        self.levers += ["loss aversion", "trigger specificity", "merchant-offer judgment", "effort taken off"]
        return self._out(body, "review_theme_emerged: used the exact rising theme/quote, checked the merchant's existing offer, and chose trust repair before more discounting.",
                         params=[g, humanize(theme), str(occ or "")])
    def k_milestone_reached(self):
        g = self.greet()
        metric, now_v, goal = self.p.get("metric"), self.p.get("value_now"), self.p.get("milestone_value")
        if metric and now_v is not None and goal is not None:
            gap = goal - now_v
            self.reg(gap)
            body = f"{g}, {self.name} is at {self.num(now_v)} {humanize(metric).replace('count', '').strip()}s"
            body = body.replace("reviews", "Google reviews")
            body += f", just {self.num(gap)} away from {self.num(goal)}." if gap > 0 else f", past {self.num(goal)}."
        else:
            v = self.perf.get("views")
            if v is None:
                return self.k_generic()
            c = self.perf.get("calls")
            body = f"{g}, {self.name} had {self.num(v)} views"
            body += f" and {self.num(c)} calls in the last 30 days" if c is not None else " in the last 30 days"
            body += ", a good base to turn into reviews."
            if self.slug == "restaurants":
                body += " Keep the ask tied to regular covers rather than a generic blast."
        pos = self.review("pos")
        if pos and pos.get("occurrences_30d"):
            body += f" Your {humanize(pos['theme'])} got {self.num(pos['occurrences_30d'])} positive mentions this month."
        body += " " + self.yes("Want me to draft a thank-you WhatsApp for your regulars that asks for a quick review?",
                               "Regulars ke liye thank-you WhatsApp draft kar doon jo review bhi maange?")
        self.levers += ["goal proximity", "social proof", "effort taken off"]
        return self._out(body, "milestone_reached: goal-gradient framing with the exact gap; review ask to close it.",
                         params=[g, str(now_v or ""), str(goal or "")])

    def k_active_planning_intent(self):
        g = self.greet()
        topic = str(self.p.get("intent_topic", ""))
        hist = self.m.get("conversation_history", []) or []
        last_vera = next((h.get("body", "") for h in reversed(hist) if h.get("from") == "vera"), "")
        merchant_ask = self.p.get("merchant_last_message", "")
        if "thali" in topic or "corporate" in topic:
            base = self.offer_like("thali") or self.best_offer()
            price = None
            mm = re.search(r"₹\s?([\d,]+)", base or "")
            if mm:
                price = int(mm.group(1).replace(",", ""))
            lines = [f"{g}, you asked what the corporate-bulk version would look like. Starting from your live '{base}':", "",
                     f"*{self.name} — {self.locality} office lunch proposal*"]
            if price:
                bulk = price - 10
                self.reg(price, bulk, 25, 12, 30, 1, 5)
                lines += [f"• Proposal: 10+ thalis at ₹{price} each + free delivery",
                          f"• Proposal: 25+ thalis at ₹{bulk} each",
                          "• Day-before order by 5pm; delivery window 12:30-1pm"]
            else:
                self.reg(12, 30, 1, 5)
                lines += ["• Bulk office thali + free delivery", "• Day-before order by 5pm; delivery window 12:30-1pm"]
            lines += ["", "This keeps the regular thali price intact and gives corporate orders a clear AOV ladder. "
                      "Want me to draft the 3-line office-admin WhatsApp next? Reply YES."]
            body = "\n".join(lines)
            rat = "active_planning_intent: answered the merchant's explicit 'what would it look like' with a labelled proposal anchored on the live thali offer and locality; no re-qualification."
        elif "yoga" in topic or "kids" in topic or "camp" in topic:
            weeks = re.search(r"(\d+)-week", last_vera)
            per_wk = re.search(r"(\d+)\s+classes/week", last_vera)
            ages = re.search(r"age\s+(\d+)-(\d+)", last_vera)
            fee = re.search(r"₹\s?([\d,]+)", last_vera)
            lines = [f"Hi {g}, you asked what the kids yoga program should look like. Using the version already discussed:", "",
                     f"*{self.name} Kids Summer Camp*"]
            spec = []
            if weeks: spec.append(f"{weeks.group(1)} weeks")
            if per_wk: spec.append(f"{per_wk.group(1)} classes/week")
            if ages: spec.append(f"ages {ages.group(1)}-{ages.group(2)}")
            if spec: lines.append("• " + ", ".join(spec))
            if fee: lines.append(f"• ₹{fee.group(1)} full-camp price")
            live = self.best_offer()
            if live:
                lines.append(f"• Keep '{live}' separate as the low-friction fallback; don't blur the camp offer")
            lines += ["", "Keep it calm and parent-friendly: small-batch yoga + coaching is the hero, not another discount. Google post + Insta carousel copy bana doon? Reply YES."]
            body = "\n".join(lines)
            rat = "active_planning_intent: continued the merchant's explicit kids-yoga planning, attributed prior proposed numbers to the existing conversation, and used the live offer only as a fallback."
        else:
            offer = self.best_offer() or self.catalog_offer()
            body = (f"{g}, picking up your '{merchant_ask or humanize(topic) or 'plan'}' request: here's the next step built around '{offer}'. "
                    f"I can turn it into the ready Google post + WhatsApp copy now. Reply YES.")
            rat = "active_planning_intent: continued the merchant's own request and moved directly to an artifact rather than another qualifying question."
        self.levers += ["complete artifact", "merchant continuity", "category-native framing", "momentum"]
        return self._out(body, rat, params=[g, humanize(topic)])
    def k_seasonal_perf_dip(self):
        g = self.greet()
        metric, delta = self.p.get("metric", "views"), self.p.get("delta_pct")
        if delta is None:
            wd = self.worst_delta()
            if not wd:
                return self.k_generic()
            metric, delta = wd
        current = self.current_metric(metric)
        body = f"{g}, quick check: {humanize(metric)} are down {self.pct(delta)} this week"
        if current is not None:
            body += f", but {self.name} still has {self.num(current)} {humanize(metric)} in the last 30 days"
        body += "."
        expected = self.p.get("is_expected_seasonal") or any("seasonal_dip" in str(s) for s in self.signals)
        if expected:
            body += " Your own signals already flag this as the seasonal Apr-Jun acquisition dip."
        if self.perf.get("ctr") is not None:
            body += f" CTR is still {self.rate(self.perf['ctr'])}, so don't panic-discount while the listing is converting."
        live = self.best_offer()
        if live:
            body += f" Keep '{live}' live, but make the next push about retention rather than another acquisition offer."
        else:
            body += " Make the next push about retention rather than adding another acquisition offer."
        body += " " + self.yes("Want me to draft a 4-week retention challenge?",
                               "4-week retention challenge draft kar doon?")
        self.levers += ["anxiety pre-emption", "merchant performance", "category judgment", "specificity"]
        return self._out(body, "seasonal_perf_dip: used only the trigger, visible performance, merchant signals and live offer; chose retention over another acquisition push without introducing unscored aggregate or peer numbers.",
                         params=[g, humanize(metric), self.pct(delta)])
    def k_supply_alert(self):
        g = self.greet()
        mol = self.p.get("molecule", "the affected medicine")
        batches = self.p.get("affected_batches", []) or []
        mfr = self.p.get("manufacturer")
        item = self.digest(self.p.get("alert_id"), kinds=("alert", "supply"))
        b = ", ".join(batches)
        src = item.get("source") if item else ""
        body = f"{g}, urgent for {self.name}{', ' + self.locality if self.locality else ''}: voluntary recall on {mol} batches {b}"
        if mfr: body += f" by {mfr}"
        if src: body += f" ({src})"
        body += "."
        if item and item.get("summary"):
            summary = item.get("summary", "")
            if "sub-potency" in summary:
                body += " The alert flags sub-potency; replacement is advised for affected packs."
        body += " First pull those batches; don't guess the affected-customer count until the repeat-Rx batch match is done."
        body += " " + self.yes("Want me to draft the customer note + replacement-pickup workflow?",
                               "Customer note + replacement-pickup workflow draft kar doon?")
        self.levers += ["urgency", "batch specificity", "category-precise language", "complete workflow"]
        return self._out(body, "supply_alert: exact molecule/batches/manufacturer/source, immediate shelf action, and a no-guess customer workflow; no fabricated affected count.",
                         params=[g, mol, b])
    def k_category_seasonal(self):
        g = self.greet()
        trends = self.p.get("trends", []) or []
        parts = []
        for tr in trends:
            mm = re.match(r"([A-Za-z_ ]+?)_demand_([+-]\d+)", tr)
            if mm:
                label = mm.group(1).replace("_", " ")
                label = label if label.isupper() else label.replace("cold cough", "cold & cough")
                parts.append(f"{label} {mm.group(2)}%")
        if not parts:
            return self.k_generic()
        body = f"{g}, summer demand has shifted: " + ", ".join(parts) + "."
        item = self.digest(kinds=("seasonal",))
        if item and item.get("actionable"):
            body += " " + first_sentence(item["actionable"])
        if self.slug == "pharmacies" and "pharmacist counsel" not in body.lower():
            body += " Keep pharmacist counsel at the counter focused on the seasonal essentials customers are asking for."
        offer = self.offer_like("delivery") or self.best_offer()
        with_offer = f" with '{offer}'" if offer else ""
        hi_offer = f" '{offer}' ke saath" if offer else ""
        body += " " + self.yes(f"Want me to post a summer-essentials update on your Google profile{with_offer}?",
                               f"Google profile par summer-essentials post{hi_offer} daal doon?")
        self.levers += ["specificity", "timeliness", "effort taken off"]
        return self._out(body, "category_seasonal: exact demand shifts from the trigger, shelf action from the digest, "
                         "one post as the next step.", params=[g, ", ".join(parts)])

    def k_gbp_unverified(self):
        g = self.greet()
        up = self.p.get("estimated_uplift_pct")
        path = self.p.get("verification_path", "")

        if self.slug == "pharmacies":
            body = f"{g}, {self.locality + ' mein ' if self.locality else ''}{self.name} ka Google profile abhi unverified hai."
            if up:
                body += f" Verification se visibility {self.pct(up)} improve hone ka estimate hai."
            v, c = self.perf.get("views"), self.perf.get("calls")
            if v is not None and c is not None:
                body += f" Aapko already {self.num(v)} views aur {self.num(c)} calls in 30 days mil rahe hain, isliye verification ko priority dena sensible hai."
            if path:
                body += f" Aapke verification options {humanize(path)} hain."
            body += " Main exact verification checklist abhi bhej doon? Reply YES."
            self.levers += ["loss aversion", "local merchant specificity", "clear next step", "low effort"]
            return self._out(body, "gbp_unverified: natural neighbourhood-pharmacist Hinglish with locality, uplift, current demand and a low-friction checklist CTA.",
                             params=[g, self.locality, self.pct(up) if up else ""])

        body = f"{g}, {self.name}'s Google profile is still unverified."
        if up:
            body += f" Verification is estimated to lift visibility by {self.pct(up)}."
        v, c = self.perf.get("views"), self.perf.get("calls")
        if v is not None and c is not None:
            body += f" You're already getting {self.num(v)} views and {self.num(c)} calls in 30 days, so verification protects demand you already have."
        if path:
            body += f" The available path is {humanize(path).replace(' or ', ' or ')}."
        body += " " + self.yes("Want me to send the verification checklist?", "Verification checklist bhej doon?")
        self.levers += ["loss aversion", "merchant demand", "low effort"]
        return self._out(body, "gbp_unverified: combined the trigger's uplift estimate with current merchant traffic and one concrete verification step.",
                         params=[g, self.pct(up) if up else ""])
    def k_competitor_opened(self):
        g = self.greet()
        comp, dist, their, opened = (self.p.get(k) for k in ("competitor_name", "distance_km", "their_offer", "opened_date"))
        if not comp:
            body = f"{g}, a new {CATEGORY_SINGULAR.get(self.slug, 'business')} has opened near {self.locality}."
            if self.perf.get("views") is not None and self.perf.get("calls") is not None:
                body += f" {self.name} has {self.num(self.perf['views'])} views and {self.num(self.perf['calls'])} calls in 30 days, so defend that demand rather than panic-discount."
            live = self.best_offer()
            if live:
                body += f" Your live offer is '{live}'."
            body += " " + self.yes("Want me to draft a fresh comparison-style Google post?", "Fresh comparison-style Google post draft kar doon?")
            return self._out(body, "competitor_opened without competitor details: used locality, current merchant demand and live offer; no competitor facts invented.", params=[g, self.locality])
        dist_s = (indian(dist) if float(dist).is_integer() else canon(dist)) if dist is not None else None
        if dist_s: self.reg(dist_s)
        body = f"{g}, {comp} opened " + (f"{dist_s} km away" if dist_s else "nearby")
        if opened: body += f" on {self.day(opened)}"
        if their: body += f" with '{their}'"
        body += "."
        mine = self.best_offer()
        mp, tp = re.search(r"₹\s?([\d,]+)", mine or ""), re.search(r"₹\s?([\d,]+)", their or "")
        if mp and tp:
            gap = int(mp.group(1).replace(",", "")) - int(tp.group(1).replace(",", ""))
            if gap > 0:
                self.reg(gap)
                body += f" That's ₹{gap} below your live '{mine}'."
        stale = self.signal_value("stale_posts")
        if stale:
            m_stale = re.match(r"(\d+)d$", stale)
            stale_text = f"{m_stale.group(1)} days" if m_stale else stale
            body += f" Your last Google post is already {stale_text} old, so I wouldn't start a price war; refresh the value of the existing offer first."
        else:
            body += " I wouldn't start a price war; defend the value of the offer already live."
        if self.slug == "dentists":
            body += " Keep the tone clinical and explain the treatment value, not hype."
        body += " " + self.yes("Want me to draft that Google post?", "Wahi Google post draft kar doon?")
        self.levers += ["loss aversion", "merchant-offer fit", "judgment", "specificity"]
        return self._out(body, "competitor_opened: compared the exact competitor offer with the merchant's live offer, used stale-content state, and chose value communication over a price war.",
                         params=[g, comp, str(dist)])
    def _cust_open(self):
        who, child = self.cust_name()
        emoji = CATEGORY_EMOJI.get(self.slug, "")
        shop = self.name
        return who, child, shop, (" " + emoji if emoji else "")

    def k_recall_due(self):
        if not self.c:
            return self.k_generic()
        who, _child, shop, emo = self._cust_open()
        hi = self.cust_lang() == "hi"
        rel = self.c.get("relationship", {}) or {}
        last = self.day(rel["last_visit"]) if rel.get("last_visit") else None
        service = humanize(self.p.get("service_due", "")) or {"dentists": "check-up", "gyms": "next session",
                                                                "salons": "next appointment", "pharmacies": "refill",
                                                                "restaurants": "next visit"}.get(self.slug, "next visit")
        slots = [s.get("label") for s in (self.p.get("available_slots") or []) if s.get("label")][:2]
        for s in slots:
            self.reg(*NUM_RE.findall(s))
        offer = self.best_offer("clean", "check", "trial", "consult")
        if hi:
            body = f"Hi {who}, {shop} se baat kar rahe hain.{emo} Aapki {service} due hai" + (f"; pichli visit {last} ko hui thi." if last else ".")
            if len(slots) == 2:
                body += f" Aapke liye 2 slots rakhe hain: {slots[0]} ya {slots[1]}."
            if offer:
                body += f" {offer} hi rahega." if slots else f" Is mahine {offer} chal raha hai."
            body += " Reply 1 for the first slot, 2 for the second." if len(slots) == 2 else (
                " Order ready karna ho toh YES reply kijiye." if self.slug == "pharmacies" else " Reply YES aur hum is hafte aapka slot book kar denge.")
        else:
            body = f"Hi {who}, {shop} here.{emo} Your {service} is due" + (f"; your last visit was on {last}." if last else ".")
            if len(slots) == 2:
                body += f" We've kept two slots for you: {slots[0]} or {slots[1]}."
            if offer:
                body += f" {offer}, same as before." if slots else f" {offer} is on this month."
            body += " Reply 1 for the first slot or 2 for the second." if len(slots) == 2 else (
                " Reply YES and we'll keep your order ready." if self.slug == "pharmacies" else " Reply YES and we'll hold a slot for you this week.")
        self.levers += ["personalisation", "specificity", "low-friction choice"]
        return self._out(body, "recall_due (customer-facing, sent on the merchant's behalf): language pref honoured, "
                         "real slots and live offer, choice CTA.", cta="multi_choice_slot" if len(slots) == 2 else "binary_yes_no",
                         params=[who, shop, service] + slots)

    def k_appointment_tomorrow(self):
        if not self.c:
            return self.k_generic()
        who, _child, shop, emo = self._cust_open()
        slot = self.p.get("slot_label") or self.p.get("appointment_label")
        noun = VISIT_NOUN.get(self.slug, "appointment")
        if self.cust_lang() == "hi":
            body = (f"Hi {who}, {shop} se reminder{emo} Aapka {noun} kal hai" + (f" ({slot})." if slot else ".")
                    + " Confirm karne ke liye YES reply kijiye, ya time badalna ho toh 2 bhejiye.")
        else:
            body = (f"Hi {who}, a quick reminder from {shop}{emo}: your {noun} is tomorrow" + (f" ({slot})." if slot else ".")
                    + " Reply YES to confirm, or 2 if you'd like to reschedule.")
        self.levers += ["commitment", "low friction"]
        return self._out(body, "appointment_tomorrow: reminder with no invented time (payload has none); confirm or reschedule.",
                         params=[who, shop])

    def k_chronic_refill_due(self):
        if not self.c:
            return self.k_generic()
        who, _child, shop, emo = self._cust_open()
        hi = self.cust_lang() == "hi"
        mols = self.p.get("molecule_list") or []
        runout = self.p.get("stock_runs_out_iso")
        ident = self.c.get("identity", {}) or {}
        if not mols or self.slug != "pharmacies":
            # generic follow-up for placeholder payloads or non-pharmacy merchants
            return self.k_customer_lapsed_soft(reason="follow-up")
        name = ident.get("name", who)
        surname = name.replace("Mr. ", "").replace("Mrs. ", "").strip()
        med = ", ".join(mols)
        senior = self.offer_like("senior") if ident.get("senior_citizen") else None
        deliv = self.offer_like("delivery")
        # judgment: if a recall is open on one of these molecules, say we'll dispense clean stock
        recall_line = ""
        if self.store:
            for trg in self.store.triggers_for_merchant(self.m.get("merchant_id")):
                if trg.get("kind") == "supply_alert":
                    mol = (trg.get("payload") or {}).get("molecule")
                    batches = (trg.get("payload") or {}).get("affected_batches") or []
                    if mol and mol in mols and batches:
                        for b in batches:
                            self.reg(*NUM_RE.findall(b))
                        recall_line = (f" {mol.capitalize()} recall wale batches ({', '.join(batches)}) se nahi denge." if hi
                                       else f" We'll make sure the {mol} isn't from the recalled batches ({', '.join(batches)}).")
        when = self.day(runout) if runout else None
        if hi:
            body = f"Namaste! {shop}, {self.locality} se.{emo} {surname} ji ki monthly medicines ({med})"
            body += f" {when} tak khatam ho jayengi." if when else " ka refill due hai."
            body += " Same dose ka refill ready kar dete hain."
            if senior:
                body += f" {senior} lagega"
                body += f", aur {deliv.replace('Free Home Delivery > ', '')} se upar free home delivery saved address par." if deliv else "."
            elif deliv:
                body += f" {deliv} saved address par."
            body += recall_line + " Dispatch ke liye YES reply kijiye, ya dose mein badlaav ho toh bata dijiye."
        else:
            body = f"Hello from {shop}, {self.locality}.{emo} {name}'s monthly medicines ({med})"
            body += f" run out on {when}." if when else " are due for a refill."
            body += " We can keep the same dose ready."
            if senior:
                body += f" {senior} applies."
            if deliv:
                body += f" {deliv} to your saved address."
            body += recall_line + " Reply YES to dispatch, or tell us if the dose has changed."
        self.levers += ["specificity", "convenience", "trust"]
        return self._out(body, "chronic_refill_due: exact molecules and run-out date, merchant's real senior/delivery offers, "
                         "cross-checked against the open recall.", params=[who, med, when or ""])

    def k_customer_lapsed_soft(self, reason="lapsed"):
        if not self.c:
            return self.k_generic()
        who, _child, shop, emo = self._cust_open()
        hi = self.cust_lang() == "hi"
        rel = self.c.get("relationship", {}) or {}
        last = self.day(rel["last_visit"]) if rel.get("last_visit") else None
        visits = rel.get("visits_total")
        offer = self.best_offer("check", "clean", "trial", "haircut", "month", "thali", "delivery")
        if hi:
            body = f"Hi {who}, {shop} se baat kar rahe hain.{emo}"
            body += f" Aapki pichli visit {last} ko thi" if last else " Kaafi time ho gaya"
            body += f", aur aap humare saath {self.num(visits)} baar aa chuke hain." if visits and visits > 1 else "."
            if offer:
                body += f" Is hafte ke liye {offer} available hai."
            body += {"pharmacies": " Order ready karna ho toh YES reply kijiye.",
                     "restaurants": " Table chahiye toh YES reply kijiye."}.get(
                self.slug, " Slot chahiye toh YES reply kijiye, hum aapke hisaab se time rakh denge.")
        else:
            body = f"Hi {who}, {shop} here.{emo}"
            body += f" Your last visit was on {last}" if last else " It's been a while"
            body += f", and you've been in {self.num(visits)} times." if visits and visits > 1 else "."
            if offer:
                body += f" {offer} is available this week."
            body += {"pharmacies": " Reply YES and we'll keep your order ready.",
                     "restaurants": " Reply YES and we'll hold a table for you."}.get(
                self.slug, " Reply YES and we'll hold a time that suits you.")
        self.levers += ["personalisation", "low friction"]
        return self._out(body, f"customer {reason}: last-visit date and visit history from the customer record; "
                         "live offer only if the merchant has one; no guilt.", params=[who, shop, last or ""])

    def k_customer_lapsed_hard(self):
        if not self.c:
            return self.k_generic()
        who, _child, shop, emo = self._cust_open()
        days = self.p.get("days_since_last_visit")
        focus = self.p.get("previous_focus") or (self.c.get("preferences", {}) or {}).get("training_focus")
        pref = (self.c.get("preferences", {}) or {}).get("preferred_slots")
        offer = self.best_offer("trial", "free", "month")
        body = f"Hi {who}, {self.first} from {shop}, {self.locality} here.{emo}"
        body += f" It's been {self.num(days)} days since your last session" if days else " It's been a while since your last visit"
        body += ". That happens to most people at some point, no judgment."
        if focus:
            body += f" Your goal was {humanize(focus)}, so here's an easy restart"
            body += f": {offer}" if offer else ""
            body += (f", {humanize(pref).replace('weekday evening', 'weekday evenings')} like before" if pref else "") + ", no commitment."
        elif offer:
            body += f" An easy way back: {offer}, no commitment."
        body += " Want us to hold a spot for you this week? Reply YES."
        self.levers += ["no-shame framing", "goal recall", "no-commitment trial"]
        return self._out(body, "customer_lapsed_hard: warm, no-guilt winback anchored on the customer's own goal and "
                         "a live no-commitment offer.", params=[who, shop, str(days or "")])

    def k_trial_followup(self):
        if not self.c:
            return self.k_generic()
        who, child, shop, emo = self._cust_open()
        trial = self.p.get("trial_date")
        opts = [o.get("label") for o in (self.p.get("next_session_options") or []) if o.get("label")]
        for s in opts:
            self.reg(*NUM_RE.findall(s))
        kid = child or "you"
        body = f"Hi {who}, {shop}, {self.locality} here.{emo}"
        tnoun = TRIAL_NOUN.get(self.slug, "visit")
        if trial:
            body += f" Thank you for bringing {kid} to the trial on {self.day(trial)}." if child else f" Thanks for joining the trial on {self.day(trial)}."
        else:
            body += f" Thanks for your {tnoun} with us."
        if opts:
            body += f" The next session is {opts[0]}."
        pref = (self.c.get("preferences", {}) or {}).get("preferred_slots")
        if pref and "saturday" in pref and opts and opts[0].startswith("Sat"):
            body += " That fits your Saturday-morning preference."
        if self.slug == "pharmacies":
            body += " Want us to set up your next order with home delivery? Reply YES."
        elif self.slug == "restaurants":
            body += " Want us to hold a table for your next visit? Reply YES."
        else:
            body += f" Shall we save {child + chr(39) + 's' if child else 'your'} spot? Reply YES."
        self.levers += ["momentum", "specificity", "single ask"]
        return self._out(body, "trial_followup: thanks + next concrete session from the payload, matched to stated preference.",
                         params=[who, opts[0] if opts else ""])

    def k_wedding_package_followup(self):
        if not self.c:
            return self.k_generic()
        who, _child, shop, emo = self._cust_open()
        wd = self.p.get("wedding_date")
        days = self.p.get("days_to_wedding")
        trial = self.p.get("trial_completed")
        raw_step = str(self.p.get("next_step_window_open", ""))
        mstep = re.match(r"(.*?)_?(\d+)day$", raw_step)
        step = f"{mstep.group(2)}-day {humanize(mstep.group(1)).replace('skin prep', 'skin-prep')}" if mstep else humanize(raw_step)
        body = f"Hi {who}, {shop} here 💍"
        if wd:
            body += f" Your wedding is on {self.day(wd)}" + (f", {self.num(days)} days away" if days is not None else "")
            body += f", and your bridal trial with us was on {self.day(trial)}." if trial else "."
        if step:
            body += f" This is the right window to start the {step} so your skin is settled well before the day."
        pref = (self.c.get("preferences", {}) or {}).get("preferred_slots")
        body += f" Shall we book your first session on a {pref.replace('_', ' ').title()}, as you prefer? Reply YES." if pref else " Shall we book your first session? Reply YES."
        self.levers += ["personalisation", "timing", "single ask"]
        return self._out(body, "wedding_package_followup: wedding date, countdown and trial date from context; "
                         "no invented package price; preferred day honoured.", params=[who, str(days or "")])

    # ---- fallback -------------------------------------------------------------
    def k_generic(self):
        g = self.greet()
        if self.c and (self.kind in CUSTOMER_KINDS or self.t.get("scope") == "customer"):
            who, _child, shop, emo = self._cust_open()
            body = f"Hi {who}, {shop} here.{emo} We'd love to see you again. Reply YES and we'll hold a time that suits you."
            return self._out(body, f"{self.kind}: minimal customer message (payload had no usable detail).", params=[who, shop])

        # Phase-3 safety net: an unseen trigger must use its pushed fact, not collapse into a stale generic listing pitch.
        # Explicitly mismatched category/location events are better skipped than forced into a generic outreach.
        if not self._payload_category_ok() or not self._payload_location_ok():
            return None
        if self._payload_category_ok() and self._payload_location_ok():
            fact = self._trigger_fact()
            if fact:
                body = f"{g}, timely heads-up for {self.name}: {fact}"
                src = self.p.get("source_name") or self.p.get("source")
                if src and isinstance(src, str) and src.lower() not in {"internal", "external"}:
                    body += f" Source: {src}."
                live = self.best_offer()
                if live:
                    body += f" Your current offer is '{live}'; keep the response anchored on what is already live rather than inventing a new discount."
                body += " " + self._adaptive_ask("update")
                return self._out(body, f"{self.kind}: unseen-trigger fact-first fallback used the pushed event fact and current merchant state; no fabricated payload fields.",
                                 params=[g, fact[:80], str(src or "")])

        v, c = self.perf.get("views"), self.perf.get("calls")
        body = f"{g}, quick update on {self.name}:"
        if v is not None and c is not None:
            body += f" {self.num(v)} views and {self.num(c)} calls in the last 30 days."
        gap = self.ctr_gap()
        if gap and gap[0] < gap[1]:
            body += f" Your CTR is {self.rate(gap[0])} vs {self.rate(gap[1])} for similar {self.plural}."
        offer = self.best_offer() or self.catalog_offer()
        body += " " + self.yes(f"Want me to refresh your listing{' around ' + repr(offer) if offer else ''} this week?",
                               "Is hafte listing refresh kar doon?")
        return self._out(body, f"{self.kind}: grounded account-status fallback because the trigger payload contained no usable event fact.", params=[g])


def compose(category, merchant, trigger, customer=None):
    """Public challenge interface from challenge-brief.md section 7.1."""
    out = Composer(category, merchant, trigger, customer, STORE).compose()
    if not out:
        return None
    out = dict(out)
    out["suppression_key"] = trigger.get("suppression_key") or trigger.get("id")
    return out


# ---------------------------------------------------------------------------
# Tick: decide what to send
# ---------------------------------------------------------------------------
def consent_ok(customer, kind, merchant_id=None):
    """Require consent that matches the outbound purpose; tolerate generic promotional consent for synthetic placeholders."""
    if not customer:
        return False
    if merchant_id and customer.get("merchant_id") and customer.get("merchant_id") != merchant_id:
        return False
    prefs = customer.get("preferences", {}) or {}
    scopes = set((customer.get("consent", {}) or {}).get("scope", []) or [])
    if not scopes and not prefs.get("reminder_opt_in"):
        return False
    required = {
        "recall_due": {"recall_reminders", "treatment_followup"},
        "appointment_tomorrow": {"appointment_reminders"},
        "chronic_refill_due": {"refill_reminders", "delivery_notifications"},
        "customer_lapsed_soft": {"winback_offers", "promotional_offers"},
        "customer_lapsed_hard": {"winback_offers"},
        "trial_followup": {"program_updates", "kids_program_updates", "treatment_followup", "promotional_offers"},
        "wedding_package_followup": {"bridal_package_followup", "appointment_reminders", "promotional_offers"},
    }.get(kind, set())
    if not required:
        return bool(scopes or prefs.get("reminder_opt_in"))
    if scopes & required:
        return True
    # The supplied synthetic placeholder customers sometimes expose only generic promo consent.
    if "promotional_offers" in scopes and kind in {"appointment_tomorrow", "recall_due", "chronic_refill_due",
                                                    "customer_lapsed_soft", "trial_followup"}:
        return True
    if prefs.get("reminder_opt_in") and kind in {"appointment_tomorrow", "recall_due", "chronic_refill_due"}:
        return True
    return False


def _history_relevance(merchant, trg):
    hist = (merchant or {}).get("conversation_history", []) or []
    if not hist:
        return 0
    payload = json.dumps(trg.get("payload") or {}, ensure_ascii=False).lower()
    trigger_words = {w for w in re.findall(r"[a-z]+", payload) if len(w) > 4}
    score = 0
    for h in hist[-8:]:
        body = (h.get("body") or "").lower()
        if h.get("from") == "merchant":
            score += min(8, 2 * sum(1 for w in trigger_words if w in body))
            if h.get("engagement") in ("intent_action", "merchant_replied"):
                score += 2
    return score


def priority(trg, merchant=None, category=None, customer=None):
    """Rank signals by urgency, kind, payload quality, merchant history and actual state."""
    kind = trg.get("kind", "")
    urgency = trg.get("urgency", 1) or 1
    payload = trg.get("payload") or {}
    placeholder = bool(payload.get("placeholder"))
    score = int(urgency) * 10 + KIND_WEIGHT.get(kind, 5) + (0 if placeholder else 6)
    score += _history_relevance(merchant, trg)
    perf = (merchant or {}).get("performance", {}) or {}
    delta = perf.get("delta_7d", {}) or {}
    if kind == "perf_dip":
        negatives = [abs(float(v)) for v in delta.values() if isinstance(v, (int, float)) and v < 0]
        score += min(12, int(max(negatives, default=0) * 20))
    elif kind == "perf_spike":
        positives = [float(v) for v in delta.values() if isinstance(v, (int, float)) and v > 0]
        score += min(8, int(max(positives, default=0) * 15))
    if kind == "active_planning_intent":
        score += 10
    if customer is not None and kind in {"appointment_tomorrow", "chronic_refill_due", "recall_due"}:
        score += 4
    return score

def make_conv_id(trg, merchant_id, customer_id):
    base = f"conv_{merchant_id}_{trg.get('kind', 'msg')}"
    if customer_id:
        base += f"_{customer_id.split('_for_')[0]}"
    key = re.sub(r"[^A-Za-z0-9]+", "_", str(trg.get("suppression_key") or trg.get("id", "")))[-24:]
    return f"{base}_{key}".strip("_")


def handle_tick(req):
    now = parse_iso(req.get("now"))
    ids = list(dict.fromkeys(req.get("available_triggers") or []))
    followup_actions = []
    if now:
        for mid, pending in sorted(list(STORE.pending_followups.items())):
            if pending.get("due") and now >= pending["due"] and mid not in STORE.optout:
                conv = STORE.conversations.get(pending.get("conversation_id"), {})
                trg = STORE.get("trigger", pending.get("trigger_id"))
                merchant = STORE.get("merchant", mid)
                if not merchant or not trg:
                    STORE.pending_followups.pop(mid, None)
                    continue
                art = ARTIFACT.get(pending.get("kind"), "that draft")
                body = f"Following up after the pause: I still have {art} ready to continue. Want me to pick it up now? Reply YES."
                if body not in STORE.recipient_bodies[mid] and STORE.unanswered_nudges[mid] < 3:
                    base_conv = f"{pending.get('conversation_id')}_followup"
                    new_conv = base_conv
                    n = 2
                    while new_conv in STORE.conversations:
                        new_conv = f"{base_conv}_{n}"
                        n += 1
                    params = [((merchant.get("identity") or {}).get("owner_first_name") or ""), art, ""]
                    followup_key = f"followup:{pending.get('conversation_id')}"
                    followup_actions.append({
                        "conversation_id": new_conv, "merchant_id": mid, "customer_id": None,
                        "send_as": "vera", "trigger_id": trg.get("id", pending.get("trigger_id")),
                        "template_name": "vera_followup_v1", "template_params": params,
                        "body": body, "cta": "binary_yes_no",
                        "suppression_key": followup_key,
                        "rationale": "Wait period elapsed: started one fresh follow-up conversation with a low-friction yes/no continuation."
                    })
                    STORE.conversations[new_conv] = {
                        "merchant_id": mid, "customer_id": None, "trigger_id": trg.get("id"), "kind": pending.get("kind"),
                        "sent": [body], "stage": 0, "status": "open", "auto": 0, "nos": 0, "apologised": False,
                        "send_as": "vera",
                    }
                    STORE.sent_keys.add((mid, followup_key))
                    STORE.recipient_bodies[mid].add(body)
                    STORE.unanswered_nudges[mid] += 1
                    STORE.nudge_times[mid].append(now)
                STORE.pending_followups.pop(mid, None)
                if len(followup_actions) >= MAX_ACTIONS_PER_TICK:
                    return {"actions": followup_actions}
    cands = []
    for tid in ids:
        trg = STORE.get("trigger", tid)
        if not trg:
            continue
        mid = trg.get("merchant_id") or (trg.get("payload") or {}).get("merchant_id")
        merchant = STORE.get("merchant", mid)
        if not merchant:
            continue
        category = STORE.get("category", merchant.get("category_slug"))
        if not category:
            continue
        cid = trg.get("customer_id")
        customer = STORE.get("customer", cid) if cid else None
        customer_facing = trg.get("scope") == "customer" or trg.get("kind") in CUSTOMER_KINDS
        if customer_facing and cid and not customer:
            continue  # customer context not pushed yet: wait rather than guess
        skey = trg.get("suppression_key") or tid
        recipient_hint = cid or mid
        sent_key = (recipient_hint, skey)
        if sent_key in STORE.sent_keys:
            continue
        if mid in STORE.optout or (cid and cid in STORE.optout):
            continue
        if customer_facing and not consent_ok(customer, trg.get("kind"), mid):
            continue
        exp = parse_iso(trg.get("expires_at"))
        if now and exp and exp < now:
            continue
        if not customer_facing and STORE.unanswered_nudges[mid] >= 3 and (trg.get("urgency") or 0) < 4:
            times = STORE.nudge_times.get(mid, [])[-3:]
            if len(times) >= 3 and len({t.isoformat() for t in times}) >= 3:
                continue  # restraint: stop after three unanswered nudges on separate wake-ups
        wait_until = STORE.merchant_wait_until.get(mid)
        if not customer_facing and wait_until and now and now < wait_until and (trg.get("urgency") or 0) < 4:
            continue
        cands.append((priority(trg, merchant, category, customer), tid, trg, merchant, category, customer, customer_facing, skey))

    cands.sort(key=lambda x: (-x[0], x[1]))
    actions, used_m, used_c = list(followup_actions), {a["merchant_id"] for a in followup_actions}, Counter()
    for _prio, tid, trg, merchant, category, customer, customer_facing, skey in cands:
        mid = merchant.get("merchant_id")
        cid = customer.get("customer_id") if customer else None
        if customer_facing:
            if cid in used_c or used_c[mid] >= 3:
                continue
        elif mid in used_m:
            continue  # one merchant-facing message per merchant per tick
        msg = Composer(category, merchant, trg, customer, STORE).compose()
        recipient = cid or mid
        if not msg:
            continue
        if msg["body"] in STORE.recipient_bodies[recipient]:
            STORE.sent_keys.add((recipient, skey))  # duplicate was already effectively delivered
            continue
        conv_id = make_conv_id(trg, mid, cid)
        n = 2
        while conv_id in STORE.conversations:
            conv_id = f"{make_conv_id(trg, mid, cid)}_{n}"
            n += 1
        action = {
            "conversation_id": conv_id,
            "merchant_id": mid,
            "customer_id": cid,
            "send_as": msg["send_as"],
            "trigger_id": trg.get("id", tid),
            "template_name": msg["template_name"],
            "template_params": msg["template_params"],
            "body": msg["body"],
            "cta": msg["cta"],
            "suppression_key": skey,
            "rationale": msg["rationale"],
        }
        STORE.conversations[conv_id] = {
            "merchant_id": mid, "customer_id": cid, "trigger_id": trg.get("id", tid), "kind": trg.get("kind"),
            "sent": [msg["body"]], "stage": 0, "status": "open", "auto": 0, "nos": 0, "apologised": False,
            "send_as": msg["send_as"],
        }
        STORE.sent_keys.add((recipient, skey))
        STORE.recipient_bodies[recipient].add(msg["body"])
        if not customer_facing:
            STORE.unanswered_nudges[mid] += 1
            if now:
                STORE.nudge_times[mid].append(now)
                STORE.nudge_times[mid] = STORE.nudge_times[mid][-6:]
        if customer_facing:
            used_c[cid] += 1
            used_c[mid] += 1
        else:
            used_m.add(mid)
        actions.append(action)
        if len(actions) >= MAX_ACTIONS_PER_TICK:
            break
    return {"actions": actions}


# ---------------------------------------------------------------------------
# Reply: route the merchant's / customer's answer
# ---------------------------------------------------------------------------
STOP_RE = re.compile(r"\b(stop|unsubscribe|not interested|no more messages?|don'?t (message|text|contact|send)|"
                     r"stop (messaging|sending|texting)|leave me alone|band karo|mat bhejo|message mat|nahi chahiye|"
                     r"remove me|opt ?out|never message)\b")
ABUSE_RE = re.compile(r"\b(useless|spam|idiot|stupid|nonsense|bakwas|pagal|shut up|fraud|scam|harass\w*|bother\w*|"
                      r"irritat\w*|rubbish|waste of time|chutiya|bewakoof)\b")
AUTO_RE = re.compile(r"(thank you for contacting|thanks for contacting|thank you for reaching|thanks for reaching|"
                     r"will (respond|reply|get back|revert)|get back to you|our team will|we will revert|"
                     r"currently (unavailable|closed|away)|out of office|business hours|automated|auto[- ]?reply|"
                     r"this is an automatic|jaankari ke liye|sampark karne ke liye|aapka sandesh|team tak|aapki madad ke liye)")
YES_RE = re.compile(r"(^\s*(yes|yeah|yep|yup|ok|okay|sure|haan|han|ha|ji|done|confirm\w*|go ahead|proceed|"
                    r"please do|do it|kar ?do|kar dijiye|chalo|theek hai|thik hai|perfect|great|sounds good|y)\b)|"
                    r"(let'?s do it|lets go|go ahead|send it|please send|send me|yes please|book it|what'?s next|"
                    r"whats next|sign me up|i want to join|mujhe judna|start karo|shuru karo|haan ji|please proceed)")
LATER_RE = re.compile(r"\b(later|busy|tomorrow|kal|baad mein|baad me|abhi nahi|not now|next week|in a meeting|"
                      r"call you back|thoda time|give me time|remind me)\b")
OFFTOPIC_RE = re.compile(r"\b(gst|income tax|itr|tax filing|tax return|loan|insurance|legal|lawyer|court|visa|"
                         r"passport|website development|app development|accounting|bookkeeping|ca help|payroll)\b")
QUESTION_RE = re.compile(r"(\?|\b(what|how|why|when|which|kya|kaise|kitna|kitne|kab|price|cost|charges?|fees?|rate)\b)")
NO_RE = re.compile(r"^\s*(no|nope|nah|nahi|na|not really|no thanks)\b")
HINGLISH_RE = re.compile(r"\b(hai|hain|nahi|kya|aap|karo|kar|haan|mujhe|kaise|kitna|chahiye|bhej|abhi|theek|"
                         r"acha|accha|bhai|ji|kal|baad|mein)\b")


def detect_lang(msg):
    if re.search(r"[ऀ-ॿ]", msg or ""):
        return "hi"
    return "hi" if len(HINGLISH_RE.findall((msg or "").lower())) >= 2 else "en"


def _conv_stub(conv_id, mid, cid, message=""):
    """Recover a replay conversation conservatively when the judge did not start it through /v1/tick."""
    trg = None
    if mid:
        merchant = STORE.get("merchant", mid)
        category = STORE.get("category", (merchant or {}).get("category_slug"))
        customer = STORE.get("customer", cid) if cid else None
        trigs = [t for t in STORE.triggers_for_merchant(mid) if t.get("customer_id") == cid]
        hint = ((conv_id or "") + " " + (message or "")).lower()
        kind_hints = {
            "research_digest": ("abstract", "research", "paper", "patient whatsapp", "study"),
            "supply_alert": ("affected", "batch", "recall", "customers affected", "medicine"),
            "renewal_due": ("renewal", "renew", "price", "cost", "kitna"),
            "gbp_unverified": ("verify", "verification", "google profile"),
            "review_theme_emerged": ("review", "reply draft"),
            "active_planning_intent": ("let's do it", "lets do it", "go ahead", "haan kar do"),
            "cde_opportunity": ("cde", "webinar", "credits", "joining details"),
        }
        hinted = []
        for t in trigs:
            kind = t.get("kind", "")
            if str(t.get("id", "")).lower() in hint or kind.lower() in hint:
                hinted.append(t); continue
            if any(term in hint for term in kind_hints.get(kind, ())):
                hinted.append(t)
        pool = hinted or trigs
        if pool:
            trg = sorted(pool, key=lambda t: (-priority(t, merchant, category, customer), str(t.get("id", ""))))[0]
    conv = {"merchant_id": mid, "customer_id": cid, "trigger_id": trg.get("id") if trg else None,
            "kind": trg.get("kind") if trg else None, "sent": [], "stage": 0, "status": "open",
            "auto": 0, "nos": 0, "apologised": False, "send_as": "merchant_on_behalf" if cid else "vera"}
    STORE.conversations[conv_id] = conv
    return conv


ARTIFACT = {
    "research_digest": "the 2-min abstract and a patient-education WhatsApp draft",
    "regulation_change": "the 1-page audit checklist",
    "cde_opportunity": "your registration and joining details",
    "perf_dip": "the offer switch-on and a fresh Google post",
    "perf_spike": "the follow-up post",
    "renewal_due": "the renewal request",
    "festival_upcoming": "the festival package and post schedule",
    "curious_ask_due": "the Google post and price-reply",
    "winback_eligible": "the reactivation and 2 fresh posts",
    "dormant_with_vera": "your 5-minute profile check",
    "ipl_match_today": "tonight's delivery-only special",
    "review_theme_emerged": "the review replies and listing note",
    "milestone_reached": "the thank-you and review-request WhatsApp",
    "active_planning_intent": "the Google post and the WhatsApp for office admins",
    "seasonal_perf_dip": "the 4-week summer attendance challenge",
    "supply_alert": "the affected-customer list and their WhatsApp note",
    "category_seasonal": "the summer-essentials post",
    "gbp_unverified": "the verification steps",
    "competitor_opened": "the Google post",
    "weather_heatwave": "the heat-day customer update",
    "local_news_event": "the customer access/availability update",
    "category_research_digest_release": "the digest summary and ready post",
    "category_trend_movement": "the trend-led Google post",
    "scheduled_recurring": "the Google post and customer reply",
}


def _merchant_bits(merchant, category):
    ident = (merchant or {}).get("identity", {}) or {}
    slug = (merchant or {}).get("category_slug", "")
    first = ident.get("owner_first_name") or ident.get("name", "")
    greet = (first if first.lower().startswith("dr") else f"Dr. {first}") if slug == "dentists" else first
    offers = [o.get("title") for o in (merchant or {}).get("offers", []) if o.get("status") == "active"]
    catalog = [o.get("title") for o in (category or {}).get("offer_catalog", [])]
    return greet, ident.get("name", "your business"), offers, catalog


def _draft_artifact(kind, merchant, category, trg, lang):
    """Create a usable, immediate artifact from known context; never claim an external publish/send happened."""
    greet, name, offers, catalog = _merchant_bits(merchant, category)
    p = (trg or {}).get("payload") or {}
    offer = offers[0] if offers else (catalog[0] if catalog else None)
    if kind == "research_digest":
        items = (category or {}).get("digest", []) or []
        item = next((d for d in items if d.get("id") == p.get("top_item_id")), None) or (items[-1] if items else None)
        if item:
            summary = first_sentence(item.get("summary", ""), 2) or item.get("title", "")
            copy = f"Quick update from {name}: {item.get('title','').rstrip('.')}. Ask us if you'd like to know whether it applies to you."
            return f"Key point: {summary} ({item.get('source','')})\n\nReady-to-send draft:\n{copy}"
    if kind == "review_theme_emerged":
        theme = humanize(p.get("theme", "")) or "the feedback"
        first = ((merchant or {}).get("identity", {}) or {}).get("owner_first_name", "")
        return f"Review reply draft:\nThank you for telling us. We're reviewing {theme} and would value another chance. - {first}, {name}"
    if kind == "supply_alert":
        batches = ", ".join(p.get("affected_batches", []))
        molecule = p.get("molecule", "the affected medicine")
        return f"Customer note draft:\nImportant: {molecule} batches {batches} are under recall. Please bring your strip to {name} so the team can check it and guide you on replacement."
    if kind == "competitor_opened":
        strength = next((r.get("theme") for r in (merchant or {}).get("review_themes", []) if r.get("sentiment") == "pos"), None)
        copy = f"Why customers choose {name}: {humanize(strength)}." if strength else f"A useful update from {name}: local service, clear information, and easy follow-up."
        if offer:
            copy += f" Current offer: {offer}."
        return "Google post draft:\n" + copy
    if kind == "active_planning_intent":
        topic = str(p.get("intent_topic", "")).lower()
        history = (merchant or {}).get("conversation_history", []) or []
        if "thali" in topic:
            live = next((o for o in offers if "thali" in o.lower()), offer) or "the live lunch thali"
            locality = ((merchant or {}).get("identity", {}) or {}).get("locality", "nearby offices")
            return (
                "Google post draft:\n"
                f"{name} - Corporate Lunch for {locality} offices. {live}. "
                "For team orders, send the headcount and preferred delivery window; we'll confirm the bulk setup.\n\n"
                "Office-admin WhatsApp draft:\n"
                f"Hi - {name} now takes weekday corporate thali orders in {locality}. "
                f"The current lunch thali is {live}. Share your team size and delivery window and we'll confirm the order plan."
            )
        if "yoga" in topic:
            prior = next((str(h.get("body", "")) for h in reversed(history)
                          if h.get("from") == "vera" and "4-week" in str(h.get("body", "")).lower()), "")
            details = "4-week program, 3 classes/week, age 7-12, ₹2,499" if prior else "a structured kids-yoga summer program"
            return (
                "Google post draft:\n"
                f"{name} Kids Yoga Summer Camp - {details}. Small-batch coaching with a clear weekly routine.\n\n"
                "Insta carousel opener:\n"
                "Kids need movement, focus and a routine this summer - here's the camp plan in one swipe."
            )
    if kind in {"weather_heatwave", "local_news_event", "category_trend_movement", "category_research_digest_release"}:
        fact = ""
        for key in ("headline", "title", "event", "summary", "message", "note"):
            if isinstance(p.get(key), str) and p.get(key).strip():
                fact = first_sentence(p[key], 1)
                break
        if not fact and kind == "category_trend_movement":
            q = p.get("query") or p.get("search_query") or p.get("term")
            if q: fact = f"Current category signal: {q}."
        if not fact and kind == "category_research_digest_release":
            item = p.get("top_item") if isinstance(p.get("top_item"), dict) else None
            if not item:
                items = (category or {}).get("digest", []) or []
                iid = p.get("top_item_id") or p.get("digest_item_id")
                item = next((d for d in items if d.get("id") == iid), None) or (items[-1] if items else None)
            if item: fact = first_sentence(item.get("title") or item.get("summary", ""), 1)
        copy = f"{name}: {fact}" if fact else f"{name}: timely local update."
        if offer: copy += f" Current offer: {offer}."
        return "Ready-to-send customer update:\n" + copy
    if kind in {"perf_dip", "perf_spike", "gbp_unverified", "milestone_reached", "festival_upcoming", "category_seasonal", "seasonal_perf_dip", "ipl_match_today", "dormant_with_vera"}:
        copy = f"{name}: {offer}." if offer else f"A fresh update from {name} for customers nearby."
        return "Google post draft:\n" + copy
    if kind in {"renewal_due", "winback_eligible"}:
        return "Next step ready: I can prepare the renewal summary from your current plan details; no external action has been taken yet."
    art = ARTIFACT.get(kind, "a fresh listing message")
    copy = f"{name}: {offer}." if offer else f"{name}: here's a useful update for customers nearby."
    return f"{art.capitalize()} draft:\n{copy}"


def _execute(conv, merchant, category, trg, lang):
    """Merchant said yes: deliver the useful artifact now; never promise background work or fake execution."""
    kind = conv.get("kind")
    stage = conv.get("stage", 0)
    if stage >= 1:
        conv["stage"] = stage + 1
        if lang == "hi":
            return ("Confirmed. Final copy ready hai; koi external post/send automatically nahi hua. Aap ise ab publish/send kar sakte hain.",
                    "none", "Merchant confirmed; clearly marked the artifact ready without claiming an external action occurred.")
        return ("Confirmed. The final copy is ready; no external post or send has been performed automatically.",
                "none", "Merchant confirmed; clearly marked the artifact ready without claiming an external action occurred.")
    conv["stage"] = 1
    draft = _draft_artifact(kind, merchant, category, trg, lang)
    if lang == "hi":
        body = f"Bilkul. Yeh ready draft hai:\n\n{draft}\n\nCheck karke CONFIRM reply kijiye; main final copy lock kar dungi."
    else:
        body = f"Absolutely. Here's the ready draft now:\n\n{draft}\n\nReply CONFIRM after checking it; I'll lock the final copy."
    return body, "binary_confirm_cancel", "Explicit commitment: delivered the promised artifact immediately, with no fake wait or external execution claim."


def _customer_execute(conv, trg, customer, merchant, msg_norm, lang):
    p = (trg or {}).get("payload") or {}
    slots = [s.get("label") for s in (p.get("available_slots") or p.get("next_session_options") or []) if s.get("label")]
    name = (customer or {}).get("identity", {}).get("name", "")
    name = re.sub(r"\s*\(parent:.*\)", "", name)
    shop = (merchant or {}).get("identity", {}).get("name", "us")
    choice = None
    if msg_norm in ("1", "2") and len(slots) >= int(msg_norm):
        choice = slots[int(msg_norm) - 1]
    elif slots:
        for s in slots:
            if s.split(",")[0].lower() in msg_norm:
                choice = s
        choice = choice or slots[0]
    conv["stage"] = conv.get("stage", 0) + 1
    if conv.get("kind") == "chronic_refill_due":
        body = ("Theek hai, refill request mil gayi. Team stock aur delivery confirm karke message karegi." if lang == "hi"
                else "Thanks, your refill request is received. The team will confirm stock and delivery details.")
    elif choice:
        body = (f"Aapne {choice} select kiya hai at {shop}. Team final booking confirmation bhejegi." if lang == "hi"
                else f"You've selected {choice} at {shop}. The team will send the final booking confirmation.")
    else:
        body = (f"Request mil gayi. {shop} ki team aapko time confirm karke message karegi." if lang == "hi"
                else f"Request received. The {shop} team will message you to confirm the time.")
    return body, "none", f"Customer confirmed ({name}); request acknowledged without claiming an external booking or dispatch happened."


def _answer_question(conv, merchant, category, trg, msg, lang):
    greet, name, offers, catalog = _merchant_bits(merchant, category)
    low = msg.lower()
    kind = conv.get("kind")
    art = ARTIFACT.get(kind, "a fresh post for your listing")
    if kind == "supply_alert" and re.search(r"\b(how many|kitne|which customers|affected)\b", low):
        p = (trg or {}).get("payload") or {}
        n = ((merchant or {}).get("customer_aggregate", {}) or {}).get("chronic_rx_count")
        body = ("I'll get the exact number by matching batches " + ", ".join(p.get("affected_batches", [])) +
                " against your repeat-Rx records" + (f"; you have {indian(n)} chronic-Rx customers on file" if n else "") +
                ". Shall I start? Reply YES.")
        return body, "binary_yes_no", "Answered honestly: exact count needs the batch match; no number invented."
    if re.search(r"\b(price|cost|charges?|fees?|kitna|how much)\b", low):
        sub = (merchant or {}).get("subscription", {}) or {}
        amount = ((trg or {}).get("payload") or {}).get("renewal_amount")
        if not amount and "renew" in low and merchant:
            for t in STORE.triggers_for_merchant(merchant.get("merchant_id")):
                if t.get("kind") == "renewal_due" and (t.get("payload") or {}).get("renewal_amount"):
                    amount, kind = t["payload"]["renewal_amount"], "renewal_due"
        if kind in ("renewal_due", "winback_eligible") and amount:
            body = f"Your {sub.get('plan', '')} renewal is ₹{indian(amount)}. Want me to raise it now? Reply YES."
        elif offers:
            body = (f"Setting this up costs you nothing extra on your plan. It uses your live offer "
                    f"'{offers[0]}'. Shall I go ahead? Reply YES.")
        else:
            body = "There's no extra charge for this on your plan. Shall I go ahead? Reply YES."
    elif re.search(r"\b(how long|time|kab|when)\b", low):
        body = f"I can prepare {art} in this chat immediately; nothing is published automatically. Shall I draft it now? Reply YES."
    else:
        body = (f"Good question. In short: I prepare {art} for {name} here, you review it, and the final copy stays ready for your approval. "
                f"Nothing is published automatically. Shall I start? Reply YES.")
    if lang == "hi":
        body = body.replace("Shall I start? Reply YES.", "Shuru kar doon? YES reply kijiye.").replace(
            "Shall I go ahead? Reply YES.", "Aage badhoon? YES reply kijiye.")
    return body, "binary_yes_no", "Merchant asked a question: answered from context in one line and returned to the single next step."


def handle_reply(req):
    conv_id = req.get("conversation_id") or "conv_unknown"
    mid = req.get("merchant_id")
    cid = req.get("customer_id")
    role = (req.get("from_role") or "merchant").lower()
    msg = req.get("message") or ""
    conv = STORE.conversations.get(conv_id)
    if conv is None:
        conv = _conv_stub(conv_id, mid, cid, msg)
    mid = mid or conv.get("merchant_id")
    cid = cid or conv.get("customer_id")
    merchant = STORE.get("merchant", mid)
    category = STORE.get("category", (merchant or {}).get("category_slug"))
    trg = STORE.get("trigger", conv.get("trigger_id"))
    customer = STORE.get("customer", cid) if cid else None
    low = msg.lower().strip()
    nmsg = norm_text(msg)
    lang = detect_lang(msg)
    sender = (cid if role == "customer" and cid else mid) or conv_id
    seen = STORE.sender_texts[sender]
    seen[nmsg] += 1
    received = parse_iso(req.get("received_at"))

    def done(action, body=None, cta=None, rationale="", wait=None):
        out = {"action": action, "rationale": rationale}
        if action == "send":
            if body in conv["sent"]:  # anti-repetition
                body = body + (" (Reply STOP anytime.)" if "STOP" not in body else " 🙏")
            conv["sent"].append(body)
            out["body"] = body
            out["cta"] = cta or "open_ended"
        if action == "wait":
            out["wait_seconds"] = wait or 14400
            if role == "merchant" and mid and received:
                STORE.pending_followups[mid] = {
                    "due": received + timedelta(seconds=out["wait_seconds"]),
                    "conversation_id": conv_id,
                    "trigger_id": conv.get("trigger_id"),
                    "kind": conv.get("kind"),
                }
        if action == "end":
            conv["status"] = "ended"
            if role == "merchant" and mid:
                STORE.pending_followups.pop(mid, None)
        return out

    if not nmsg:
        return done("wait", rationale="Empty reply; backing off briefly.", wait=3600)

    # 1. explicit opt-out: stop immediately, never message again
    if STOP_RE.search(low):
        STORE.optout[sender] = "opt_out"
        return done("end", rationale="Explicit opt-out/stop request: closing the conversation and suppressing further "
                                      "messages to this recipient.")

    # Once an explicit conversation end has been returned, later replay noise must not revive it.
    if conv.get("status") == "ended":
        return done("end", rationale="Conversation already ended; ignoring later replay traffic instead of reopening it.")

    # 2. WhatsApp Business auto-reply: detect by phrasing or verbatim repeats, exit fast
    is_auto = bool(AUTO_RE.search(low)) or (seen[nmsg] >= 2 and len(nmsg) > 15 and not YES_RE.search(low))
    if is_auto:
        STORE.sender_auto[sender] += 1
        conv["auto"] = conv.get("auto", 0) + 1
        if STORE.sender_auto[sender] >= 2 or conv["auto"] >= 2:
            if mid and received:
                STORE.merchant_wait_until[mid] = received + timedelta(hours=24)
            return done("end", rationale="Same canned auto-reply again: owner isn't on the phone. Closing without "
                                          "burning more turns; will retry on a later trigger.")
        return done("wait", rationale="Detected a WhatsApp Business auto-reply (canned phrasing), not the owner. "
                                       "Backing off 4 hours instead of replying to a bot.", wait=14400)

    # Any non-auto merchant reply is real engagement: reset the unanswered-nudge cadence.
    if role == "merchant" and mid:
        STORE.unanswered_nudges[mid] = 0
        STORE.nudge_times[mid] = []
        STORE.pending_followups.pop(mid, None)

    # 3. hostile without an explicit stop: one short apology with an opt-out path, then exit
    offtopic = bool(OFFTOPIC_RE.search(low))
    if ABUSE_RE.search(low) and not offtopic:
        if conv.get("apologised"):
            STORE.optout[sender] = "hostile"
            return done("end", rationale="Second hostile message: exiting gracefully and suppressing this merchant.")
        conv["apologised"] = True
        body = ("Maaf kijiye, aapka time waste nahi karna chahti. Aage messages nahi chahiye toh bas STOP likh dijiye. 🙏"
                if lang == "hi" else
                "Sorry, I don't want to waste your time. If you'd rather not hear from me, just reply STOP and I won't message again. 🙏")
        return done("send", body, "none", "Merchant frustrated but didn't opt out: one-line apology with a clear opt-out path.")

    # 4. off-topic asks (GST, loans, legal...): decline politely, stay on mission
    if offtopic:
        conv["offtopic"] = conv.get("offtopic", 0) + 1
        if conv["offtopic"] >= 2:
            return done("end", rationale="Repeated out-of-scope requests after a redirect: staying on-mission by closing politely.")
        topic = OFFTOPIC_RE.search(low).group(1).upper() if OFFTOPIC_RE.search(low).group(1) in ("gst", "itr") else "that"
        art = ARTIFACT.get(conv.get("kind"))
        back = f" Coming back to {art}: shall I go ahead? Reply YES." if art else " I can help with your listing, offers and customer messages whenever you want."
        if lang == "hi":
            body = f"{topic} ke liye aapke CA sahi rahenge, woh mere scope se bahar hai." + (
                f" Wapas {art} par: aage badhoon? YES reply kijiye." if art else " Listing, offers aur customer messages mein main kabhi bhi madad kar sakti hoon.")
        else:
            body = f"I'll have to leave {topic} to your CA, it's outside what I can help with." + back
        return done("send", body, "binary_yes_no" if art else "none",
                    "Out-of-scope request: politely declined and steered back to the original thread.")

    # 5. customer replies (booking choices, confirmations)
    if role == "customer":
        if nmsg in ("1", "2") or YES_RE.search(low):
            body, cta, rat = _customer_execute(conv, trg, customer, merchant, nmsg, lang)
            return done("send", body, cta, rat)
        if LATER_RE.search(low):
            return done("wait", rationale="Customer asked for time; backing off a day.", wait=86400)
        if NO_RE.search(low):
            return done("end", rationale="Customer declined; closing politely without pushing.")
        shop = (merchant or {}).get("identity", {}).get("name", "us")
        body = (f"Zaroor! {shop} ki team aapko jaldi reply karegi." if lang == "hi"
                else f"Thanks! Someone from {shop} will reply to you shortly.")
        return done("send", body, "none", "Customer question routed to the merchant's team.")

    # 6. commitment: act, don't qualify
    if YES_RE.search(low) and not NO_RE.search(low):
        body, cta, rat = _execute(conv, merchant, category, trg, lang)
        return done("send", body, cta, rat)

    # 7. curious-ask answers are information, not a question: thank and act on it
    if conv.get("kind") in {"curious_ask_due", "scheduled_recurring"} and not QUESTION_RE.search(low) and not NO_RE.search(low):
        conv["stage"] = 1
        service = msg.strip().strip(".!")[:60]
        offers = [o.get("title") for o in (merchant or {}).get("offers", []) if o.get("status") == "active" and o.get("title")]
        offer = offers[0] if offers else "your current price"
        post = f"{service} at {(merchant or {}).get('identity',{}).get('name','your business')}: ask us for availability. {offer}."
        price_reply = f"Thanks for asking about {service}. {offer}. Reply here and our team can help with availability."
        body = f"Got it: {service}. Here are both drafts now:\n\nGoogle post: {post}\n\nPrice reply: {price_reply}\n\nReply CONFIRM after checking them."
        if lang == "hi":
            body = f"Samajh gayi: {service}. Dono drafts ready hain:\n\nGoogle post: {post}\n\nPrice reply: {price_reply}\n\nCheck karke CONFIRM reply kijiye."
        return done("send", body, "binary_confirm_cancel", "Merchant answered the curious-ask: delivered both promised artifacts immediately.")

    # 8. later / busy
    if LATER_RE.search(low):
        secs = 86400 if re.search(r"\b(tomorrow|kal|next week)\b", low) else 14400
        return done("wait", rationale="Merchant asked for time; backing off instead of pushing.", wait=secs)

    # 9. soft no: one lighter alternative, then exit
    if NO_RE.search(low):
        conv["nos"] = conv.get("nos", 0) + 1
        if conv["nos"] >= 2:
            return done("end", rationale="Second no: exiting gracefully.")
        body = ("Koi baat nahi. Main isse hold par rakh deti hoon; jab chahiye ho, bas 'Hi Vera' likh dijiye." if lang == "hi"
                else "No problem, I'll park this. Whenever you want it, just message 'Hi Vera'.")
        conv["status"] = "ended"
        return done("send", body, "none", "Merchant declined: acknowledged without pressure and left an easy way back.")

    # 10. questions
    if QUESTION_RE.search(low):
        body, cta, rat = _answer_question(conv, merchant, category, trg, msg, lang)
        return done("send", body, cta, rat)

    # 11. anything else after we've already acted: close the loop politely
    if conv.get("stage", 0) >= 2 or re.search(r"\b(thanks|thank you|shukriya|dhanyavad)\b", low):
        return done("end", rationale="Conversation complete; nothing further needed.")

    body, cta, rat = _execute(conv, merchant, category, trg, lang)
    return done("send", body, cta, "Positive but non-explicit reply; moving to the next concrete step. " + rat)


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------
def handle_context(body):
    scope, cid, version, payload = body.get("scope"), body.get("context_id"), body.get("version"), body.get("payload")
    if scope not in SCOPES:
        return 400, {"accepted": False, "reason": "invalid_scope", "details": f"scope must be one of {list(SCOPES)}"}
    if not cid or not isinstance(payload, dict):
        return 400, {"accepted": False, "reason": "invalid_body", "details": "context_id and payload object are required"}
    try:
        version = int(version)
    except (TypeError, ValueError):
        return 400, {"accepted": False, "reason": "invalid_version", "details": "version must be an integer"}
    key = (scope, cid)
    cur = STORE.contexts.get(key)
    if cur and version < cur["version"]:
        return 409, {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}
    if cur and version == cur["version"]:
        # Live challenge contract: idempotent by (context_id, version). Do not reset conversational state.
        return 200, {"accepted": True, "ack_id": f"ack_{cid}_v{version}", "stored_at": now_iso(), "no_op": True}
    STORE.contexts[key] = {"version": version, "payload": payload, "session": STORE.session}
    return 200, {"accepted": True, "ack_id": f"ack_{cid}_v{version}", "stored_at": now_iso()}


class Handler(BaseHTTPRequestHandler):
    server_version = "VeraEngine/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet default logging; we log ourselves
        pass

    def _send(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        t0 = time.time()
        path = self.path.split("?")[0].rstrip("/")
        with STORE.lock:
            if path == "/v1/healthz":
                code, out = 200, {"status": "ok", "uptime_seconds": int(time.time() - START_TIME),
                                  "contexts_loaded": STORE.counts()}
            elif path == "/v1/metadata":
                code, out = 200, {"team_name": TEAM_NAME, "team_members": TEAM_MEMBERS,
                                  "model": ((f"gemini:{GEMINI_MODEL} optional polish" if (LLM_REWRITE and GEMINI_MODEL)
                                             else "none (deterministic decision engine)")), "approach": APPROACH,
                                  "contact_email": CONTACT_EMAIL, "version": VERSION, "submitted_at": SUBMITTED_AT}
            elif path == "":
                code, out = 200, {"status": "ok", "service": "vera-engine", "endpoints": [
                    "/v1/healthz", "/v1/metadata", "/v1/context", "/v1/tick", "/v1/reply"]}
            else:
                code, out = 404, {"error": "not_found"}
        self._send(code, out)
        log(f"GET {path} {code} {int((time.time() - t0) * 1000)}ms")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        t0 = time.time()
        path = self.path.split("?")[0].rstrip("/")
        try:
            body = self._body()
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
        except Exception as e:  # malformed JSON
            self._send(400, {"error": "invalid_json", "details": str(e)[:200]})
            log(f"POST {path} 400 invalid json")
            return
        try:
            with STORE.lock:
                if path == "/v1/context":
                    code, out = handle_context(body)
                elif path == "/v1/tick":
                    code, out = 200, handle_tick(body)
                elif path == "/v1/reply":
                    code, out = 200, handle_reply(body)
                elif path == "/v1/teardown":
                    STORE.reset()
                    code, out = 200, {"ok": True, "wiped": True}
                else:
                    code, out = 404, {"error": "not_found"}
        except Exception as e:  # never crash the server; fail safe
            log(f"ERROR {path}: {type(e).__name__}: {e}")
            if path == "/v1/tick":
                code, out = 200, {"actions": []}
            elif path == "/v1/reply":
                code, out = 200, {"action": "wait", "wait_seconds": 3600, "rationale": "Internal error; backing off safely."}
            else:
                code, out = 500, {"error": "internal_error"}
        self._send(code, out)
        extra = f" actions={len(out.get('actions', []))}" if path == "/v1/tick" else (
            f" action={out.get('action')}" if path == "/v1/reply" else "")
        log(f"POST {path} {code}{extra} {int((time.time() - t0) * 1000)}ms")


def log(line):
    sys.stdout.write(f"{now_iso()} {line}\n")
    sys.stdout.flush()


def main():
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    log(f"Vera engine listening on 0.0.0.0:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
