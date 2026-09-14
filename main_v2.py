from datetime import datetime, timezone
from pathlib import Path
import shutil
import traceback

import main
import news_engine

news_engine.install(main)


def _safe_enrich_event(event_id: str, payload: dict) -> None:
    """Always finish enrichment and never leave Driver=Unknown."""
    gate = main.technical_gate(payload)
    try:
        news = news_engine.free_news_context(payload)
        result = news_engine.deterministic_analysis(main, payload, gate, news)
        result = news_engine.post_validate(main, result, payload, gate, news)
        result["cost"] = "$0"
        result["news_method"] = "Free RSS + deterministic freshness/source gate"
        result["news_window_minutes"] = news.get("lookback_minutes")
        result["event_time_et"] = news.get("event_time_et")
        result["news_error"] = news.get("error")
    except Exception as exc:
        traceback.print_exc()
        trade = gate.get("classification", "WAIT")
        try:
            confidence = int(payload.get("catalyst_score") or payload.get("score") or 0)
        except Exception:
            confidence = 0
        if trade == "WAIT":
            confidence = min(confidence, 69)
        result = {
            "driver_type": "TECHNICAL_FLOW",
            "driver": "No major confirmed fresh headline found — likely technical/flow catalyst.",
            "headline": "NONE",
            "source": "NONE",
            "published_at": None,
            "age_minutes": None,
            "background": [],
            "trade": trade,
            "confidence": confidence,
            "trigger": str(gate.get("trigger") or "WAIT FOR STRUCTURE"),
            "invalidation": str(gate.get("invalidation_reference") or "N/A"),
            "why": gate.get("reason", "") + " News enrichment failed safely; technical gate preserved.",
            "alert_text": "",
            "rss_headlines": [],
            "cost": "$0",
            "news_method": "Fail-safe deterministic fallback",
            "news_error": f"{type(exc).__name__}: {exc}",
        }

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


@app.post("/admin/archive-alerts")
def archive_alerts() -> dict:
    """Archive the current alert log and clear the live dashboard feed."""
    archive_dir = Path(main.APP_DIR) / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_path = archive_dir / f"alerts-{stamp}.jsonl"

    with main._lock:
        count = len(main._alerts)
        if main.ALERT_LOG.exists() and main.ALERT_LOG.stat().st_size > 0:
            shutil.copy2(main.ALERT_LOG, archive_path)
        else:
            archive_path.write_text("", encoding="utf-8")

        main._alerts.clear()
        main.ALERT_LOG.write_text("", encoding="utf-8")

    return {
        "ok": True,
        "archived_count": count,
        "archive_file": str(archive_path),
        "live_alerts": 0,
    }


@app.get("/admin/archives")
def list_archives() -> dict:
    archive_dir = Path(main.APP_DIR) / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    files = sorted((p.name for p in archive_dir.glob("alerts-*.jsonl")), reverse=True)
    return {"archives": files[:50]}
