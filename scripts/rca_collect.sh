#!/usr/bin/env bash
set -euo pipefail
SERVER_USER="${SERVER_USER:-root}"
SERVER_HOST="${SERVER_HOST:-voiprnd.nemtclouddispatch.com}"
PROJECT_PATH="${PROJECT_PATH:-/root/Asterisk-AI-Voice-Agent}"
SINCE_MIN="${SINCE_MIN:-60}"
TS=$(date -u +%Y%m%d-%H%M%S)
BASE="logs/remote/rca-$TS"
mkdir -p "$BASE"/{taps,recordings,logs}
echo "$BASE" > logs/remote/rca-latest.path
ssh "$SERVER_USER@$SERVER_HOST" "docker logs --since ${SINCE_MIN}m ai_engine > /tmp/ai-engine.latest.log" || true
scp "$SERVER_USER@$SERVER_HOST:/tmp/ai-engine.latest.log" "$BASE/logs/ai-engine.log"
CID=$(grep -o '"call_id": "[^"]*"' "$BASE/logs/ai-engine.log" | awk -F '"' '{print $4}' | tail -n 1 || true)
echo -n "$CID" > "$BASE/call_id.txt"
ssh "$SERVER_USER@$SERVER_HOST" "docker exec ai_engine sh -lc 'cd /tmp/ai-engine-taps 2>/dev/null || exit 0; tar czf /tmp/ai_taps_${CID}.tgz *${CID}*.wav 2>/dev/null || true'; docker cp ai_engine:/tmp/ai_taps_${CID}.tgz /tmp/ai_taps_${CID}.tgz 2>/dev/null || true" || true
scp "$SERVER_USER@$SERVER_HOST:/tmp/ai_taps_${CID}.tgz" "$BASE/" 2>/dev/null || true
if [ -f "$BASE/ai_taps_${CID}.tgz" ]; then tar xzf "$BASE/ai_taps_${CID}.tgz" -C "$BASE/taps"; fi
REC_LIST=$(ssh "$SERVER_USER@$SERVER_HOST" "find /var/spool/asterisk/monitor -type f -name '*${CID}*.wav' -printf '%p\\n' 2>/dev/null | head -n 10") || true
if [ -n "$REC_LIST" ]; then
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    scp "$SERVER_USER@$SERVER_HOST:$f" "$BASE/recordings/" || true
  done <<< "$REC_LIST"
fi
TAPS=$(ls "$BASE"/taps/*.wav 2>/dev/null || true)
RECS=$(ls "$BASE"/recordings/*.wav 2>/dev/null || true)
if [ -n "$TAPS" ]; then python3 scripts/wav_quality_analyzer.py "$BASE"/taps/*.wav --json "$BASE/wav_report_taps.json" --frame-ms 20 || true; fi
if [ -n "$RECS" ]; then python3 scripts/wav_quality_analyzer.py "$BASE"/recordings/*.wav --json "$BASE/wav_report_rec.json" --frame-ms 20 || true; fi
# Build call timeline with key events for the captured call
if [ -n "$CID" ]; then
  egrep -n "ADAPTIVE WARM-UP|Wrote .*200ms|call-level summary|STREAMING TUNING SUMMARY" "$BASE/logs/ai-engine.log" | grep "$CID" > "$BASE/logs/call_timeline.log" || true
fi

# Fetch Deepgram usage for this call when credentials are available (robust Python fallback).
DG_PROJECT_ID="${DG_PROJECT_ID:-}"
DG_API_KEY="${DEEPGRAM_API_KEY:-}"
if [ -n "$DG_PROJECT_ID" ] && [ -n "$DG_API_KEY" ]; then
  RCA_BASE="$BASE" DG_PROJECT_ID="$DG_PROJECT_ID" DEEPGRAM_API_KEY="$DG_API_KEY" python3 - <<'PY'
import os, re, json, datetime as dt, urllib.request, pathlib, sys

base = pathlib.Path(os.environ.get('RCA_BASE', ''))
dg_proj = os.environ.get('DG_PROJECT_ID')
dg_key = os.environ.get('DEEPGRAM_API_KEY')
logs_dir = base / 'logs'
logs_dir.mkdir(parents=True, exist_ok=True)

def parse_call_ts(log_path: pathlib.Path):
    try:
        txt = log_path.read_text(errors='ignore')
    except Exception:
        return None
    # Prefer the precise first outbound frame timestamp
    m = re.findall(r'"event": "\\ud83c\\udfb5 STREAMING OUTBOUND - First frame".*?"timestamp": "([^"]+)"', txt)
    ts = m[-1] if m else None
    if not ts:
        # Fallback to AudioSocket frame probe
        m2 = re.findall(r'"event": "AudioSocket frame probe".*?"timestamp": "([^"]+)"', txt)
        ts = m2[-1] if m2 else None
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts.replace('Z','+00:00'))
    except Exception:
        return None

def iso(dtobj):
    return dtobj.strftime('%Y-%m-%dT%H:%M:%SZ')

if not (dg_proj and dg_key and base.exists()):
    sys.exit(0)

logp = logs_dir / 'ai-engine.log'
call_ts = parse_call_ts(logp)

# Build time window: if call_ts known, use [call_ts-45m, call_ts+15m], else last 30m
now = dt.datetime.utcnow()
if call_ts:
    start = call_ts - dt.timedelta(minutes=45)
    end = call_ts + dt.timedelta(minutes=15)
else:
    start = now - dt.timedelta(minutes=30)
    end = now

list_url = f"https://api.deepgram.com/v1/projects/{dg_proj}/requests?start={iso(start)}&end={iso(end)}&status=succeeded"
req = urllib.request.Request(list_url, headers={'Authorization': f'Token {dg_key}', 'accept': 'application/json'})
try:
    with urllib.request.urlopen(req, timeout=45) as r:
        data = json.loads(r.read().decode('utf-8', 'ignore'))
except Exception as e:
    data = {'requests': []}

reqs = data.get('requests') or []
(logs_dir / 'deepgram_requests.json').write_text(json.dumps(reqs, indent=2))

def best_match(reqs, ref_ts):
    def ts_of(it):
        for k in ('created','start','completed'):
            v = it.get(k)
            if v:
                try: return dt.datetime.fromisoformat(v.replace('Z','+00:00'))
                except Exception: pass
        return None
    scored = []
    for it in reqs:
        t = ts_of(it)
        if not t and ref_ts is None:
            scored.append((0, it))
        elif t and ref_ts is not None:
            scored.append((abs((t - ref_ts).total_seconds()), it))
    scored.sort(key=lambda x: x[0])
    return scored[0][1] if scored else None

best = best_match(reqs, call_ts)
if best and best.get('request_id'):
    rid = best['request_id']
    det_url = f"https://api.deepgram.com/v1/projects/{dg_proj}/requests/{rid}"
    det_req = urllib.request.Request(det_url, headers={'Authorization': f'Token {dg_key}', 'accept': 'application/json'})
    try:
        with urllib.request.urlopen(det_req, timeout=45) as r:
            det = json.loads(r.read().decode('utf-8', 'ignore'))
        (logs_dir / 'deepgram_request_detail.json').write_text(json.dumps(det, indent=2))
    except Exception:
        pass
print("Deepgram snapshot captured:", len(reqs), "requests; detail written:", bool(best and best.get('request_id')))
PY
fi
echo "RCA_BASE=$BASE"
echo "CALL_ID=$CID"
