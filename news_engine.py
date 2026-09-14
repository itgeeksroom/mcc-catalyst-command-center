import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

ET_ZONE = ZoneInfo("America/New_York")


def _safe_num(payload: Dict[str, Any], key: str, default: float = 0.0) -> float:
    value = payload.get(key)
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _parse_event_time(value: str) -> datetime:
    raw = (value or "").strip()
    if not raw:
        return datetime.now(ET_ZONE)
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=ET_ZONE)
        return dt.astimezone(ET_ZONE)
    except ValueError:
        return datetime.now(ET_ZONE)


def _parse_rss_time(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ET_ZONE)
    except Exception:
        return None


def _source_tier(source: str) -> int:
    s = (source or "").lower()
    tier1 = [
        "reuters", "associated press", "ap news", "sec.gov",
        "securities and exchange commission", "business wire",
        "pr newswire", "globenewswire", "investor relations",
    ]
    tier2 = [
        "bloomberg", "cnbc", "wall street journal", "wsj",
        "financial times", "barron's", "marketwatch",
        "investor's business daily", "yahoo finance", "fortune", "forbes",
    ]
    tier3 = ["benzinga", "thefly", "seeking alpha", "motley fool", "tipranks", "stocktwits"]
    low = ["timothy sykes", "beststocks", "ainvest", "quiver quantitative"]
    if any(x in s for x in tier1):
        return 1
    if any(x in s for x in tier2):
        return 2
    if any(x in s for x in tier3):
        return 3
    if any(x in s for x in low):
        return 5
    return 4


def _news_queries(ticker: str) -> List[str]:
    t = ticker.upper()
    queries = [f'"{t}" stock earnings guidance analyst acquisition partnership regulatory lawsuit sector']
    if t == "SPY":
        queries.extend([
            "S&P 500 market Fed Treasury yields oil geopolitics inflation jobs stocks",
            "Nvidia semiconductors AI megacap market S&P 500 stocks",
            "Iran Israel Middle East oil Trump markets S&P 500",
        ])
    elif t == "QQQ":
        queries.extend([
            "Nasdaq 100 market Fed Treasury yields technology stocks",
            "Nvidia semiconductors AI megacap Nasdaq stocks",
        ])
    return queries


def _google_news_rss_query(query: str) -> List[Dict[str, str]]:
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 MCC-Catalyst/1.1"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        root = ET.fromstring(resp.read())
    rows: List[Dict[str, str]] = []
    for item in root.findall(".//item")[:25]:
        src = item.find("source")
        rows.append({
            "title": (item.findtext("title") or "").strip(),
            "source": ((src.text or "").strip() if src is not None else ""),
            "published": (item.findtext("pubDate") or "").strip(),
            "link": (item.findtext("link") or "").strip(),
        })
    return rows


def _headline_score(title: str, ticker: str) -> int:
    text = title.lower()
    score = 3 if ticker.lower() in text else 0
    strong = [
        "earnings", "guidance", "raises", "cuts", "upgrade", "downgrade",
        "acquire", "acquisition", "merger", "partnership", "contract",
        "investigation", "lawsuit", "regulatory", "forecast", "outlook",
        "breach", "hack", "cyberattack", "offering", "buyback",
        "fed", "rate", "treasury", "yield", "inflation", "jobs", "oil",
        "iran", "israel", "tariff", "nvidia", "semiconductor", "chips",
        "ai", "s&p 500", "nasdaq",
    ]
    score += sum(2 for word in strong if word in text)
    score += sum(1 for word in ["surge", "soar", "jump", "rally", "plunge", "slump", "shares", "stock", "market"] if word in text)
    return score


def free_news_context(p: Dict[str, Any]) -> Dict[str, Any]:
    ticker = str(p.get("ticker", "")).upper()
    event_dt = _parse_event_time(str(p.get("timestamp_et", "")))
    lookback = max(15, int(_safe_num(p, "lookback_minutes", 120)))
    raw_items: List[Dict[str, str]] = []
    errors: List[str] = []

    for query in _news_queries(ticker):
        try:
            raw_items.extend(_google_news_rss_query(query))
        except Exception as exc:
            errors.append(str(exc))

    dedup: Dict[str, Dict[str, Any]] = {}
    for raw in raw_items:
        title = raw.get("title", "").strip()
        if not title:
            continue
        key = title.lower()
        if key in dedup:
            continue
        published_dt = _parse_rss_time(raw.get("published", ""))
        age_minutes = None
        if published_dt is not None:
            age_minutes = (event_dt - published_dt).total_seconds() / 60.0
        tier = _source_tier(raw.get("source", ""))
        relevance = _headline_score(title, ticker)
        fresh = age_minutes is not None and -5 <= age_minutes <= lookback
        same_day_background = age_minutes is not None and lookback < age_minutes <= 24 * 60
        threshold = 2 if ticker in {"SPY", "QQQ"} else 4
        confirmed_eligible = fresh and tier <= 2 and relevance >= threshold
        rank = relevance * 10 + (40 if fresh else 0) + (20 if confirmed_eligible else 0) - tier * 5
        if age_minutes is not None:
            rank -= min(max(age_minutes, 0), 1440) / 60
        dedup[key] = {
            **raw,
            "published_et": published_dt.isoformat() if published_dt else None,
            "age_minutes": round(age_minutes, 1) if age_minutes is not None else None,
            "source_tier": tier,
            "relevance": relevance,
            "fresh": fresh,
            "same_day_background": same_day_background,
            "confirmed_eligible": confirmed_eligible,
            "rank": round(rank, 2),
        }

    ranked = sorted(dedup.values(), key=lambda x: x["rank"], reverse=True)
    return {
        "event_time_et": event_dt.isoformat(),
        "lookback_minutes": lookback,
        "fresh_confirmed": [x for x in ranked if x["confirmed_eligible"]][:8],
        "background": [x for x in ranked if not x["confirmed_eligible"] and x["same_day_background"]][:5],
        "older": [x for x in ranked if not x["confirmed_eligible"] and not x["same_day_background"]][:5],
        "items": ranked[:12],
        "error": "; ".join(errors) if errors and not ranked else None,
    }


def _extract_json(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                pass
    return {}


def ollama_analyze(main_module, p: Dict[str, Any], gate: Dict[str, Any], news: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not main_module.USE_OLLAMA:
        return None
    fresh = news.get("fresh_confirmed", [])
    background = news.get("background", [])
    prompt = (
        "You are a concise intraday catalyst analyst running locally at $0. "
        "Catalyst is NOT automatically a trade. Never upgrade a deterministic WAIT. "
        "Only FRESH_CONFIRMED headlines are eligible to be called the current driver. "
        "BACKGROUND headlines are context only and must never be presented as the cause of the catalyst candle. "
        "Never invent causation. If FRESH_CONFIRMED is empty, driver_type MUST be TECHNICAL_FLOW and driver MUST be exactly: "
        "No major confirmed fresh headline found — likely technical/flow catalyst. "
        "Return ONLY JSON with keys driver_type, driver, headline, source, published_at, age_minutes, trade, confidence, trigger, invalidation, why, alert_text.\n"
        "EVENT:\n" + json.dumps(p) +
        "\nGATE:\n" + json.dumps(gate) +
        "\nFRESH_CONFIRMED:\n" + json.dumps(fresh) +
        "\nBACKGROUND:\n" + json.dumps(background)
    )
    body = json.dumps({"model": main_module.OLLAMA_MODEL, "prompt": prompt, "stream": False, "format": "json"}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            obj = json.loads(resp.read().decode())
        result = _extract_json(obj.get("response", ""))
        if not result or not fresh:
            return None
        selected = str(result.get("headline", "")).strip().lower()
        eligible = {str(x.get("title", "")).strip().lower() for x in fresh}
        if selected not in eligible:
            return None
        return result
    except Exception:
        return None


def deterministic_analysis(main_module, p: Dict[str, Any], gate: Dict[str, Any], news: Dict[str, Any]) -> Dict[str, Any]:
    ticker = str(p.get("ticker", ""))
    side = str(p.get("side", ""))
    ts = str(p.get("timestamp_et", ""))
    fresh = news.get("fresh_confirmed", [])
    background = news.get("background", [])
    best = fresh[0] if fresh else None

    if best:
        driver_type = "FRESH_CONFIRMED_NEWS"
        driver = best["title"]
        headline = best["title"]
        source = best.get("source", "RSS")
        published_at = best.get("published_et")
        age_minutes = best.get("age_minutes")
    else:
        driver_type = "TECHNICAL_FLOW"
        driver = "No major confirmed fresh headline found — likely technical/flow catalyst."
        headline = "NONE"
        source = "NONE"
        published_at = None
        age_minutes = None

    trade = gate["classification"]
    confidence = int(_safe_num(p, "catalyst_score", _safe_num(p, "score", 0)))
    if trade == "WAIT":
        confidence = min(confidence, 69)
    trigger = str(gate.get("trigger") or "WAIT FOR STRUCTURE")
    invalidation = str(gate.get("invalidation_reference") or "N/A")
    why = gate["reason"]
    icon = "🟡" if trade == "WAIT" else ("🔥" if trade.startswith("HIGH_CONFIDENCE") else ("🟢" if "LONG" in trade else "🔴"))
    time_text = ts[11:16] if len(ts) >= 16 else ts
    bg_text = background[0]["title"] if background else "NONE"
    alert_text = (
        f"⚡ {ticker} {side} CATALYST — {time_text} ET\n"
        f"Price: ${p.get('price')}\n"
        f"Driver type: {driver_type}\n"
        f"Driver: {driver}\n"
        f"Background: {bg_text}\n"
        f"Trade: {icon} {trade.replace('_', ' ')}\n"
        f"Trigger: {trigger}\n"
        f"Invalidation: {invalidation}\n"
        f"Confidence: {confidence}%\n"
        f"Why: {why}"
    )
    return {
        "driver_type": driver_type,
        "driver": driver,
        "headline": headline,
        "source": source,
        "published_at": published_at,
        "age_minutes": age_minutes,
        "background": background[:3],
        "trade": trade,
        "confidence": confidence,
        "trigger": trigger,
        "invalidation": invalidation,
        "why": why,
        "alert_text": alert_text,
        "rss_headlines": news.get("items", [])[:8],
    }


def post_validate(main_module, result: Dict[str, Any], p: Dict[str, Any], gate: Dict[str, Any], news: Dict[str, Any]) -> Dict[str, Any]:
    fresh = news.get("fresh_confirmed", [])
    if not fresh:
        result["driver_type"] = "TECHNICAL_FLOW"
        result["driver"] = "No major confirmed fresh headline found — likely technical/flow catalyst."
        result["headline"] = "NONE"
        result["source"] = "NONE"
        result["published_at"] = None
        result["age_minutes"] = None
    else:
        by_title = {str(x.get("title", "")).strip().lower(): x for x in fresh}
        chosen = str(result.get("headline", "")).strip().lower()
        if chosen not in by_title:
            return deterministic_analysis(main_module, p, gate, news)
        item = by_title[chosen]
        result["driver_type"] = "FRESH_CONFIRMED_NEWS"
        result["driver"] = item["title"]
        result["headline"] = item["title"]
        result["source"] = item.get("source", "RSS")
        result["published_at"] = item.get("published_et")
        result["age_minutes"] = item.get("age_minutes")

    result["background"] = news.get("background", [])[:3]
    result["rss_headlines"] = news.get("items", [])[:8]
    if gate["classification"] == "WAIT":
        result["trade"] = "WAIT"
        result["confidence"] = min(int(_safe_num(result, "confidence", _safe_num(p, "catalyst_score", 0))), 69)
        result["why"] = gate["reason"] + " News context does not override the technical entry gate."
    return result


def install(main_module) -> None:
    def patched_free_news_context(p: Dict[str, Any]) -> Dict[str, Any]:
        return free_news_context(p)

    def patched_ollama_analyze(p: Dict[str, Any], gate: Dict[str, Any], news: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return ollama_analyze(main_module, p, gate, news)

    def patched_deterministic(p: Dict[str, Any], gate: Dict[str, Any], news: Dict[str, Any]) -> Dict[str, Any]:
        return deterministic_analysis(main_module, p, gate, news)

    original_enrich = main_module.enrich_event

    def patched_enrich_event(event_id: str, payload: Dict[str, Any]) -> None:
        gate = main_module.technical_gate(payload)
        news = free_news_context(payload)
        result = ollama_analyze(main_module, payload, gate, news) or deterministic_analysis(main_module, payload, gate, news)
        result = post_validate(main_module, result, payload, gate, news)
        result["cost"] = "$0"
        result["news_method"] = "Free RSS + local Ollama" if main_module.USE_OLLAMA else "Free RSS + deterministic rules"
        result["news_window_minutes"] = news.get("lookback_minutes")
        result["event_time_et"] = news.get("event_time_et")
        with main_module._lock:
            for rec in main_module._alerts:
                if rec["id"] == event_id:
                    rec["status"] = "READY"
                    rec["technical_gate"] = gate
                    rec["analysis"] = result
                    rec["completed_at"] = datetime.now(timezone.utc).isoformat()
                    main_module._persist(rec)
                    break

    main_module._free_news_context = patched_free_news_context
    main_module._ollama_analyze = patched_ollama_analyze
    main_module._deterministic_free_analysis = patched_deterministic
    main_module.enrich_event = patched_enrich_event
    main_module._mcc_original_enrich_event = original_enrich
