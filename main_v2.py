from datetime import datetime, timezone
import traceback

import main
import news_engine

news_engine.install(main)
_original_enrich = main.enrich_event


def _safe_enrich_event(event_id: str, payload: dict) -> None:
    try:
        _original_enrich(event_id, payload)
    except Exception as exc:
        traceback.print_exc()
        gate = main.technical_gate(payload)
        fallback_news = {
            "event_time_et": str(payload.get("timestamp_et", "")),
            "lookback_minutes": int(payload.get("lookback_minutes", 120) or 120),
            "fresh_confirmed": [],
            "background": [],
            "older": [],
            "items": [],
            "error": f"{type(exc).__name__}: {exc}",
        }
        result = news_engine.deterministic_analysis(main, payload, gate, fallback_news)
        result["driver_type"] = "TECHNICAL_FLOW"
        result["driver"] = "No major confirmed fresh headline found — likely technical/flow catalyst."
        result["headline"] = "NONE"
        result["source"] = "NONE"
        result["published_at"] = None
        result["age_minutes"] = None
        result["enrichment_error"] = f"{type(exc).__name__}: {exc}"
        result["cost"] = "$0"
        result["news_method"] = "Safe fallback after enrichment error"
        result["news_window_minutes"] = fallback_news["lookback_minutes"]
        result["event_time_et"] = fallback_news["event_time_et"]

        with main._lock:
            for rec in main._alerts:
                if rec["id"] == event_id:
                    rec["status"] = "READY"
                    rec["technical_gate"] = gate
                    rec["analysis"] = result
                    rec["completed_at"] = datetime.now(timezone.utc).isoformat()
                    main._persist(rec)
                    break


main.enrich_event = _safe_enrich_event
app = main.app
