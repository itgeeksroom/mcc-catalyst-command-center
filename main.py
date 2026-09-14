import json
import os
import threading
import uuid
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict

APP_DIR = Path(__file__).resolve().parent
ALERT_LOG = APP_DIR / "alerts.jsonl"
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:4b")
USE_OLLAMA = os.getenv("USE_OLLAMA", "true").lower() in {"1","true","yes","on"}
WEBHOOK_SECRET = os.getenv("TV_WEBHOOK_SECRET", "")
MAX_ALERTS = int(os.getenv("MAX_ALERTS", "200"))

app = FastAPI(title="MCC Catalyst Command Center", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_lock = threading.Lock()
_alerts: List[Dict[str, Any]] = []


class CatalystPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    source: str = "MCC"
    version: Optional[str] = None
    event: str
    ticker: str
    timeframe: str = "5m"
    side: str
    timestamp_et: str
    session: Optional[str] = None
    price: float
    catalyst_score: Optional[int] = None
    score: Optional[int] = None
    volume_ratio: Optional[float] = None
    range_atr: Optional[float] = None
    rsi: Optional[float] = None
    adx: Optional[float] = None
    plus_di: Optional[float] = None
    minus_di: Optional[float] = None
    market_state: Optional[int] = None
    market_state_text: Optional[str] = None
    long_readiness: Optional[int] = None
    short_readiness: Optional[int] = None
    profile_min_score: Optional[int] = None
    structure: Optional[str] = None
    breaks_recent_high: Optional[bool] = None
    breaks_recent_low: Optional[bool] = None
    ema9: Optional[float] = None
    ema21: Optional[float] = None
    vwap: Optional[float] = None
    ema200: Optional[float] = None
    ema9_above_ema21: Optional[bool] = None
    above_vwap: Optional[bool] = None
    above_ema200: Optional[bool] = None
    not_extended_long: Optional[bool] = None
    not_extended_short: Optional[bool] = None
    long_trigger: Optional[float] = None
    short_trigger: Optional[float] = None
    lookback_minutes: int = 120


def _load_existing() -> None:
    if not ALERT_LOG.exists():
        return
    try:
        lines = ALERT_LOG.read_text(encoding="utf-8").splitlines()[-MAX_ALERTS:]
        for line in lines:
            if line.strip():
                _alerts.append(json.loads(line))
    except Exception:
        pass


def _persist(record: Dict[str, Any]) -> None:
    with ALERT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _safe_num(payload: Dict[str, Any], key: str, default: float = 0.0) -> float:
    value = payload.get(key)
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def technical_gate(p: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic gate. A catalyst is NEVER automatically a trade."""
    side = str(p.get("side", "")).upper()
    state = int(_safe_num(p, "market_state", 0))
    long_ready = int(_safe_num(p, "long_readiness", 0))
    short_ready = int(_safe_num(p, "short_readiness", 0))
    score = int(_safe_num(p, "catalyst_score", _safe_num(p, "score", 0)))
    vol = _safe_num(p, "volume_ratio", 0)
    rng = _safe_num(p, "range_atr", 0)
    above_vwap = bool(p.get("above_vwap"))
    ema_bull = bool(p.get("ema9_above_ema21"))
    broke_high = bool(p.get("breaks_recent_high"))
    broke_low = bool(p.get("breaks_recent_low"))
    not_ext_long = p.get("not_extended_long") is not False
    not_ext_short = p.get("not_extended_short") is not False

    if side == "BULL":
        aligned = state >= 2 and above_vwap and ema_bull
        confirmed = aligned and broke_high and long_ready >= 80 and not_ext_long
        high = confirmed and state >= 3 and long_ready >= 90 and score >= 80 and vol >= 1.5 and rng >= 0.8
        if high:
            cls = "HIGH_CONFIDENCE_LONG"
            reason = "Bull catalyst + strong bull state + structure break + high readiness; not extended."
        elif confirmed:
            cls = "LONG_WATCH"
            reason = "Bull catalyst has technical confirmation, but not enough strength/quality for an automatic high-confidence long."
        else:
            cls = "WAIT"
            reason = "Bull catalyst detected, but trend/structure/readiness/extension gate is incomplete. Do not chase."
        trigger = p.get("long_trigger")
        invalidation = p.get("ema21") or p.get("vwap")
    elif side == "BEAR":
        aligned = state <= -2 and (not above_vwap) and (not ema_bull)
        confirmed = aligned and broke_low and short_ready >= 80 and not_ext_short
        high = confirmed and state <= -3 and short_ready >= 90 and score >= 80 and vol >= 1.5 and rng >= 0.8
        if high:
            cls = "HIGH_CONFIDENCE_SHORT"
            reason = "Bear catalyst + strong bear state + structure break + high readiness; not extended."
        elif confirmed:
            cls = "SHORT_WATCH"
            reason = "Bear catalyst has technical confirmation, but not enough strength/quality for an automatic high-confidence short."
        else:
            cls = "WAIT"
            reason = "Bear catalyst detected, but trend/structure/readiness/extension gate is incomplete. Do not chase."
        trigger = p.get("short_trigger")
        invalidation = p.get("ema21") or p.get("vwap")
    else:
        cls, reason, trigger, invalidation = "WAIT", "Unknown catalyst direction.", None, None

    return {
        "classification": cls,
        "reason": reason,
        "trigger": trigger,
        "invalidation_reference": invalidation,
    }


def _google_news_rss(ticker: str) -> List[Dict[str, str]]:
    q = f'"{ticker}" stock OR earnings OR guidance OR analyst OR acquisition OR partnership OR regulatory OR lawsuit OR sector'
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q":q,"hl":"en-US","gl":"US","ceid":"US:en"})
    req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0 MCC-Catalyst/1.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        root = ET.fromstring(resp.read())
    items=[]
    for item in root.findall(".//item")[:25]:
        src=item.find("source")
        items.append({"title":(item.findtext("title") or "").strip(),"source":((src.text or "").strip() if src is not None else ""),"published":(item.findtext("pubDate") or "").strip(),"link":(item.findtext("link") or "").strip()})
    return items

def _headline_score(title: str, ticker: str) -> int:
    t=title.lower(); score=3 if ticker.lower() in t else 0
    strong=["earnings","guidance","raises","cuts","upgrade","downgrade","acquire","acquisition","merger","partnership","contract","investigation","lawsuit","regulatory","forecast","outlook","breach","hack","cyberattack","offering","buyback"]
    return score + sum(2 for k in strong if k in t) + sum(1 for k in ["surge","soar","jump","rally","plunge","slump","shares","stock"] if k in t)

def _free_news_context(p: Dict[str, Any]) -> Dict[str, Any]:
    ticker=str(p.get("ticker","")).upper()
    try: items=_google_news_rss(ticker)
    except Exception as exc: return {"items":[],"error":str(exc)}
    return {"items":sorted(items,key=lambda x:_headline_score(x["title"],ticker),reverse=True)[:8],"error":None}

def _ollama_analyze(p: Dict[str, Any], gate: Dict[str, Any], news: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not USE_OLLAMA: return None
    prompt = "You are a concise intraday catalyst analyst running locally at $0. Catalyst is NOT automatically a trade. Never upgrade a deterministic WAIT. Use only supplied RSS headlines; do not invent causation. If no headline clearly explains the move say: No major confirmed headline found — likely technical/flow catalyst. Return ONLY JSON with keys driver_type, driver, headline, source, trade, confidence, trigger, invalidation, why, alert_text.\nEVENT:\n" + json.dumps(p) + "\nGATE:\n" + json.dumps(gate) + "\nHEADLINES:\n" + json.dumps(news.get("items",[]))
    body=json.dumps({"model":OLLAMA_MODEL,"prompt":prompt,"stream":False,"format":"json"}).encode()
    req=urllib.request.Request("http://127.0.0.1:11434/api/generate",data=body,headers={"Content-Type":"application/json"},method="POST")
    try:
        with urllib.request.urlopen(req,timeout=45) as resp: obj=json.loads(resp.read().decode())
        return _extract_json(obj.get("response",""))
    except Exception: return None

def _deterministic_free_analysis(p: Dict[str, Any], gate: Dict[str, Any], news: Dict[str, Any]) -> Dict[str, Any]:
    ticker=str(p.get("ticker","")); side=str(p.get("side","")); ts=str(p.get("timestamp_et","")); items=news.get("items",[])
    best=items[0] if items and _headline_score(items[0].get("title",""),ticker)>=4 else None
    driver=best["title"] if best else "No major confirmed headline found — likely technical/flow catalyst."
    trade=gate["classification"]; conf=int(_safe_num(p,"catalyst_score",_safe_num(p,"score",0))); conf=min(conf,69) if trade=="WAIT" else conf
    trigger=str(gate.get("trigger") or "WAIT FOR STRUCTURE"); inv=str(gate.get("invalidation_reference") or "N/A"); why=gate["reason"]
    icon="🟡" if trade=="WAIT" else ("🔥" if trade.startswith("HIGH_CONFIDENCE") else ("🟢" if "LONG" in trade else "🔴"))
    alert=f"⚡ {ticker} {side} CATALYST — {ts[11:16] if len(ts)>=16 else ts} ET\nPrice: ${p.get('price')}\nDriver: {driver}\nTrade: {icon} {trade.replace('_',' ')}\nTrigger: {trigger}\nInvalidation: {inv}\nConfidence: {conf}%\nWhy: {why}"
    return {"driver_type":"COMPANY_OR_SECTOR_NEWS" if best else "TECHNICAL_FLOW","driver":driver,"headline":best["title"] if best else "NONE","source":best.get("source","RSS") if best else "NONE","trade":trade,"confidence":conf,"trigger":trigger,"invalidation":inv,"why":why,"alert_text":alert,"rss_headlines":items[:5]}

def _extract_json(text: str) -> Dict[str, Any]:
    text = text.strip()
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
    return {"alert_text": text, "driver_type": "UNKNOWN", "trade": "WAIT"}


def enrich_event(event_id: str, payload: Dict[str, Any]) -> None:
    gate=technical_gate(payload); news=_free_news_context(payload)
    result=_ollama_analyze(payload,gate,news) or _deterministic_free_analysis(payload,gate,news)
    if gate["classification"]=="WAIT" and result.get("trade")!="WAIT":
        result["trade"]="WAIT"; result["why"]=gate["reason"] + " News context does not override the technical entry gate."
    result["cost"]="$0"; result["news_method"]="Free RSS + local Ollama" if USE_OLLAMA else "Free RSS + deterministic rules"
    with _lock:
        for rec in _alerts:
            if rec["id"]==event_id:
                rec["status"]="READY"; rec["technical_gate"]=gate; rec["analysis"]=result; rec["completed_at"]=datetime.now(timezone.utc).isoformat(); _persist(rec); break


@app.on_event("startup")
def startup() -> None:
    _load_existing()


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "cost": "$0", "ollama": USE_OLLAMA, "model": OLLAMA_MODEL if USE_OLLAMA else "deterministic", "alerts": len(_alerts)}


@app.post("/tradingview")
def tradingview(
    payload: CatalystPayload,
    background_tasks: BackgroundTasks,
    x_webhook_secret: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    if WEBHOOK_SECRET and x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    data = payload.model_dump()
    event_id = str(uuid.uuid4())
    gate = technical_gate(data)
    record = {
        "id": event_id,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "status": "ANALYZING",
        "payload": data,
        "technical_gate": gate,
        "analysis": None,
    }
    with _lock:
        _alerts.insert(0, record)
        del _alerts[MAX_ALERTS:]
        _persist(record)

    background_tasks.add_task(enrich_event, event_id, data)
    return {"accepted": True, "id": event_id, "technical_gate": gate}


@app.get("/alerts")
def alerts(limit: int = 20, ticker: Optional[str] = None) -> List[Dict[str, Any]]:
    rows = _alerts
    if ticker:
        t = ticker.upper()
        rows = [x for x in rows if str(x.get("payload", {}).get("ticker", "")).upper() == t]
    return rows[: max(1, min(limit, 100))]


@app.get("/alerts/latest/{ticker}")
def latest(ticker: str) -> Dict[str, Any]:
    t = ticker.upper()
    for row in _alerts:
        if str(row.get("payload", {}).get("ticker", "")).upper() == t:
            return row
    raise HTTPException(status_code=404, detail="No alert for ticker")


DASHBOARD_HTML = r'''<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MCC Catalyst Command Center</title>
<style>
:root{color-scheme:dark}body{margin:0;background:#07111d;color:#e6edf7;font:14px system-ui,Segoe UI,Arial}.wrap{max-width:1180px;margin:auto;padding:22px}.top{display:flex;justify-content:space-between;align-items:center}.card{background:#0d1b2a;border:1px solid #1f3a52;border-radius:14px;padding:16px;margin:14px 0;box-shadow:0 10px 30px #0004}.bull{border-left:5px solid #16a34a}.bear{border-left:5px solid #dc2626}.wait{border-left:5px solid #f59e0b}.title{font-size:20px;font-weight:800}.tag{display:inline-block;padding:4px 8px;border-radius:20px;background:#172a3a;margin-right:6px}.driver{font-size:16px;margin:10px 0}.trade{font-size:18px;font-weight:800;margin:8px 0}.muted{color:#94a3b8}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin-top:10px}.metric{background:#091521;border-radius:8px;padding:8px}button{background:#0ea5e9;color:#00111b;border:0;border-radius:9px;padding:9px 12px;font-weight:700;cursor:pointer}</style>
</head><body><div class="wrap"><div class="top"><div><div class="title">⚡ MCC Catalyst Command Center</div><div class="muted">Catalyst ≠ trade. $0 mode: free RSS + local analysis.</div></div><button onclick="enableNotifications()">Enable browser alerts</button></div><div id="feed"></div></div>
<script>
let seen=new Set();
function esc(x){return String(x??'').replace(/[&<>\"]/g,s=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[s]))}
async function enableNotifications(){if('Notification'in window) await Notification.requestPermission()}
function badge(trade){if(trade==='WAIT')return '🟡 WAIT — DO NOT CHASE';if(trade==='HIGH_CONFIDENCE_LONG')return '🔥 HIGH CONFIDENCE LONG';if(trade==='LONG_WATCH')return '🟢 LONG WATCH';if(trade==='HIGH_CONFIDENCE_SHORT')return '🔥 HIGH CONFIDENCE SHORT';if(trade==='SHORT_WATCH')return '🔴 SHORT WATCH';return trade||'ANALYZING'}
async function load(){let r=await fetch('/alerts?limit=30');let rows=await r.json();let html='';for(const x of rows){let p=x.payload||{},a=x.analysis||{},g=x.technical_gate||{};let trade=a.trade||g.classification||'ANALYZING';let cls=trade.includes('LONG')?'bull':trade.includes('SHORT')?'bear':'wait';let title=`${p.side==='BULL'?'⚡':'⚡'} ${esc(p.ticker)} ${esc(p.side)} CATALYST — ${esc((p.timestamp_et||'').slice(11))} ET`;html+=`<div class="card ${cls}"><div class="title">${title}</div><div><span class="tag">${esc(p.event)}</span><span class="tag">Score ${esc(p.catalyst_score??p.score??'-')}/100</span><span class="tag">Vol ${esc(p.volume_ratio??'-')}x</span></div><div class="driver"><b>Driver:</b> ${esc(a.driver|| (x.status==='ANALYZING'?'Searching major news…':'Unknown'))}</div><div class="trade">${badge(trade)}</div><div><b>Trigger:</b> ${esc(a.trigger||g.trigger||'WAIT FOR STRUCTURE')}</div><div><b>Invalidation:</b> ${esc(a.invalidation||g.invalidation_reference||'N/A')}</div><div><b>Why:</b> ${esc(a.why||g.reason||'')}</div><div class="grid"><div class="metric">Price<br><b>${esc(p.price)}</b></div><div class="metric">State<br><b>${esc(p.market_state_text||p.market_state||'-')}</b></div><div class="metric">Long ready<br><b>${esc(p.long_readiness??'-')}</b></div><div class="metric">Short ready<br><b>${esc(p.short_readiness??'-')}</b></div><div class="metric">RSI<br><b>${esc(p.rsi??'-')}</b></div><div class="metric">ADX<br><b>${esc(p.adx??'-')}</b></div></div></div>`;if(x.status==='READY'&&!seen.has(x.id)){seen.add(x.id);if(Notification.permission==='granted')new Notification(`${p.ticker} ${p.side} catalyst — ${badge(trade)}`,{body:a.driver||g.reason||''})}}
document.getElementById('feed').innerHTML=html||'<div class="card">Waiting for TradingView catalyst webhook…</div>'}
load();setInterval(load,2000);
</script></body></html>'''


@app.get("/command-center", response_class=HTMLResponse)
def command_center() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_HTML)
