#!/usr/bin/env bash
set -euo pipefail
# Load environment variables from .env if present (local only; not committed)
ENV_FILE="${ENV_FILE:-.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
fi
SERVER_USER="${SERVER_USER:-root}"
SERVER_HOST="${SERVER_HOST:-voiprnd.nemtclouddispatch.com}"
PROJECT_PATH="${PROJECT_PATH:-/root/Asterisk-AI-Voice-Agent}"
SINCE_MIN="${SINCE_MIN:-60}"
TS=$(date -u +%Y%m%d-%H%M%S)
BASE="logs/remote/rca-$TS"
mkdir -p "$BASE"/{taps,recordings,logs,transcripts}
mkdir -p "$BASE"/config
echo "$BASE" > logs/remote/rca-latest.path
ssh "$SERVER_USER@$SERVER_HOST" "docker logs --since ${SINCE_MIN}m ai_engine > /tmp/ai-engine.latest.log" || true
scp "$SERVER_USER@$SERVER_HOST:/tmp/ai-engine.latest.log" "$BASE/logs/ai-engine.log"
# Clean up remote tmp log
ssh "$SERVER_USER@$SERVER_HOST" "rm -f /tmp/ai-engine.latest.log" || true
CID=$(grep -o '"call_id": "[^"]*"' "$BASE/logs/ai-engine.log" | awk -F '"' '{print $4}' | tail -n 1 || true)
echo -n "$CID" > "$BASE/call_id.txt"
ssh "$SERVER_USER@$SERVER_HOST" "docker exec ai_engine sh -lc 'cd /tmp/ai-engine-taps 2>/dev/null || exit 0; tar czf /tmp/ai_taps_${CID}.tgz *${CID}*.wav 2>/dev/null || true'; docker cp ai_engine:/tmp/ai_taps_${CID}.tgz /tmp/ai_taps_${CID}.tgz 2>/dev/null || true" || true
scp "$SERVER_USER@$SERVER_HOST:/tmp/ai_taps_${CID}.tgz" "$BASE/" 2>/dev/null || true
# Clean up remote tmp tap bundle
ssh "$SERVER_USER@$SERVER_HOST" "rm -f /tmp/ai_taps_${CID}.tgz" 2>/dev/null || true
if [ -f "$BASE/ai_taps_${CID}.tgz" ]; then tar xzf "$BASE/ai_taps_${CID}.tgz" -C "$BASE/taps"; fi
REC_LIST=$(ssh "$SERVER_USER@$SERVER_HOST" "find /var/spool/asterisk/monitor -type f -name '*${CID}*.wav' -printf '%p\\n' 2>/dev/null | head -n 10") || true
if [ -n "$REC_LIST" ]; then
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    scp "$SERVER_USER@$SERVER_HOST:$f" "$BASE/recordings/" || true
  done <<< "$REC_LIST"
fi

# Fetch ARI channel recordings by parsing rec name from engine logs (name field)
REC_NAME=$(grep -o '"name": "out-[^"]*"' "$BASE/logs/ai-engine.log" | awk -F '"' '{print $4}' | tail -n 1 || true)
if [ -n "$REC_NAME" ]; then
  # Default ARI recording directory
  scp "$SERVER_USER@$SERVER_HOST:/var/spool/asterisk/recording/${REC_NAME}.wav" "$BASE/recordings/" 2>/dev/null || true
  # Some installs use 'recordings' (plural)
  scp "$SERVER_USER@$SERVER_HOST:/var/spool/asterisk/recordings/${REC_NAME}.wav" "$BASE/recordings/" 2>/dev/null || true
fi
TAPS=$(ls "$BASE"/taps/*.wav 2>/dev/null || true)
RECS=$(ls "$BASE"/recordings/*.wav 2>/dev/null || true)
if [ -n "$TAPS" ]; then python3 scripts/wav_quality_analyzer.py "$BASE"/taps/*.wav --json "$BASE/wav_report_taps.json" --frame-ms 20 || true; fi
if [ -n "$RECS" ]; then python3 scripts/wav_quality_analyzer.py "$BASE"/recordings/*.wav --json "$BASE/wav_report_rec.json" --frame-ms 20 || true; fi
# Build call timeline with key events for the captured call
if [ -n "$CID" ]; then
  egrep -n "ADAPTIVE WARM-UP|Wrote .*200ms|call-level summary|STREAMING TUNING SUMMARY" "$BASE/logs/ai-engine.log" | grep "$CID" > "$BASE/logs/call_timeline.log" || true
fi

# Offline transcription of outbound audio when available
OUT_WAVS=$(ls "$BASE"/recordings/out-*.wav 2>/dev/null | head -n 1 || true)
if [ -n "$OUT_WAVS" ]; then
  python3 scripts/transcribe_call.py "$BASE"/recordings/out-*.wav --json "$BASE/transcripts/out.json" || true
fi

IN_WAVS=$(ls "$BASE"/recordings/in-*.wav 2>/dev/null | head -n 1 || true)
if [ -n "$IN_WAVS" ]; then
  python3 scripts/transcribe_call.py "$BASE"/recordings/in-*.wav --json "$BASE/transcripts/in.json" || true
fi

if [ -n "$CID" ]; then
  ssh "$SERVER_USER@$SERVER_HOST" "CID=$CID; SRC=/tmp/ai-engine-captures/\$CID; TMP=/tmp/ai-capture-\$CID; TAR=/tmp/ai-capture-\$CID.tgz; if docker exec ai_engine test -d \"\$SRC\"; then docker cp ai_engine:\"\$SRC\" \"\$TMP\" 2>/dev/null && tar czf \"\$TAR\" -C /tmp ai-capture-\$CID && rm -rf \"\$TMP\"; fi" || true
  if scp "$SERVER_USER@$SERVER_HOST:/tmp/ai-capture-$CID.tgz" "$BASE/" 2>/dev/null; then
    ssh "$SERVER_USER@$SERVER_HOST" "rm -f /tmp/ai-capture-$CID.tgz" || true
    mkdir -p "$BASE/captures"
    tar xzf "$BASE/ai-capture-$CID.tgz" -C "$BASE/captures" && rm "$BASE/ai-capture-$CID.tgz"
  fi
fi

# Fetch server-side ai-agent.yaml for transport/provider troubleshooting
scp "$SERVER_USER@$SERVER_HOST:$PROJECT_PATH/config/ai-agent.yaml" "$BASE/config/" 2>/dev/null || true

# Fetch Asterisk dialplan custom file and full log for context
scp "$SERVER_USER@$SERVER_HOST:/etc/asterisk/extensions_custom.conf" "$BASE/config/" 2>/dev/null || true
scp "$SERVER_USER@$SERVER_HOST:/var/log/asterisk/full" "$BASE/logs/asterisk-full.log" 2>/dev/null || true

# Fetch Deepgram usage for this call when credentials are available (robust Python fallback).
DG_PROJECT_ID="${DG_PROJECT_ID:-}"
DEEPGRAM_API_KEY="${DEEPGRAM_API_KEY:-}"
DEEPGRAM_LOG_API_KEY="${DEEPGRAM_LOG_API_KEY:-}"
if [ -n "$DEEPGRAM_API_KEY" ] || [ -n "$DEEPGRAM_LOG_API_KEY" ]; then
  RCA_BASE="$BASE" DG_PROJECT_ID="$DG_PROJECT_ID" DEEPGRAM_API_KEY="$DEEPGRAM_API_KEY" DEEPGRAM_LOG_API_KEY="$DEEPGRAM_LOG_API_KEY" DG_REQUEST_ID="${DG_REQUEST_ID:-}" DEEPGRAM_REQUEST_ID="${DEEPGRAM_REQUEST_ID:-}" python3 - <<'PY'
import os, re, json, datetime as dt, urllib.request, pathlib, sys

base = pathlib.Path(os.environ.get('RCA_BASE', ''))
dg_proj = os.environ.get('DG_PROJECT_ID') or os.environ.get('DEEPGRAM_PROJECT_ID')
# Prefer dedicated logging key if provided
dg_key = os.environ.get('DEEPGRAM_LOG_API_KEY') or os.environ.get('DEEPGRAM_API_KEY')
logs_dir = base / 'logs'
logs_dir.mkdir(parents=True, exist_ok=True)

def parse_call_ts(log_path: pathlib.Path):
    try:
        txt = log_path.read_text(errors='ignore')
    except Exception:
        return None
    m = re.findall(r'"event": "\\ud83c\\udfb5 STREAMING OUTBOUND - First frame".*?"timestamp": "([^"]+)"', txt)
    ts = m[-1] if m else None
    if not ts:
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

def http_get(url: str):
    req = urllib.request.Request(url, headers={'Authorization': f'Token {dg_key}', 'accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode('utf-8', 'ignore'))

if not (dg_key and base.exists()):
    sys.exit(0)

logp = logs_dir / 'ai-engine.log'
call_ts = parse_call_ts(logp)
# Attempt to derive Deepgram request_id from ai-engine log for this call
cid = None
try:
    cid = (base / 'call_id.txt').read_text(errors='ignore').strip()
except Exception:
    cid = None

rid_from_log = None
try:
    if logp.exists():
        import re as _re
        call_tag = f'"call_id": "{cid}"' if cid else None
        rid_pat = _re.compile(r'"request_id"\s*:\s*"([0-9a-fA-F\-]{36})"')
        last = None
        with logp.open('r', errors='ignore') as fh:
            for line in fh:
                if (not call_tag or call_tag in line) and ('Deepgram' in line or 'providers.deepgram' in line) and 'request_id' in line:
                    m = rid_pat.search(line)
                    if m:
                        last = m.group(1)
        rid_from_log = last
except Exception:
    rid_from_log = None
rid_env = os.environ.get('DG_REQUEST_ID') or os.environ.get('DEEPGRAM_REQUEST_ID') or rid_from_log
now = dt.datetime.utcnow()
if call_ts:
    start = call_ts - dt.timedelta(hours=2)
    end = call_ts + dt.timedelta(hours=1)
else:
    start = now - dt.timedelta(minutes=60)
    end = now

all_reqs = []

# If a request id is provided and we have a project id, fetch detail directly
det_written = False
if rid_env and dg_proj:
    try:
        det = http_get(f"https://api.deepgram.com/v1/projects/{dg_proj}/requests/{rid_env}")
        (logs_dir / 'deepgram_request_detail.json').write_text(json.dumps(det, indent=2))
        det_written = True
    except Exception:
        det_written = False

# If we didn't fetch by id, list requests within the time window (single or multiple projects)
if not det_written:
    projects = []
    if dg_proj:
        projects = [dg_proj]
    else:
        # Try to list projects when project id not provided
        try:
            pdata = http_get('https://api.deepgram.com/v1/projects')
            for p in (pdata.get('projects') or []):
                pid = p.get('project_id') or p.get('id')
                if pid:
                    projects.append(pid)
        except Exception:
            projects = []

    for pid in projects or []:
        try:
            data = http_get(f"https://api.deepgram.com/v1/projects/{pid}/requests?start={iso(start)}&end={iso(end)}&limit=200")
            reqs = data.get('requests') or []
            for it in reqs:
                it['_project_id'] = pid
            all_reqs.extend(reqs)
        except Exception:
            continue

    # If specific project set but yielded nothing, try all accessible projects as fallback
    if dg_proj and not all_reqs:
        try:
            pdata = http_get('https://api.deepgram.com/v1/projects')
            for p in (pdata.get('projects') or []):
                pid = p.get('project_id') or p.get('id')
                if not pid or pid == dg_proj:
                    continue
                try:
                    data = http_get(f"https://api.deepgram.com/v1/projects/{pid}/requests?start={iso(start)}&end={iso(end)}&limit=200")
                    reqs = data.get('requests') or []
                    for it in reqs:
                        it['_project_id'] = pid
                    all_reqs.extend(reqs)
                except Exception:
                    pass
        except Exception:
            pass

(logs_dir / 'deepgram_requests.json').write_text(json.dumps(all_reqs, indent=2))

def best_match(reqs, ref_ts):
    def ts_of(it):
        for k in ('created','start','completed'):
            v = it.get(k)
            if v:
                try:
                    return dt.datetime.fromisoformat(v.replace('Z','+00:00'))
                except Exception:
                    pass
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

best = best_match(all_reqs, call_ts)
if (not det_written) and best and best.get('request_id') and best.get('_project_id'):
    rid = best['request_id']
    pid = best['_project_id']
    try:
        det = http_get(f"https://api.deepgram.com/v1/projects/{pid}/requests/{rid}")
        (logs_dir / 'deepgram_request_detail.json').write_text(json.dumps(det, indent=2))
        det_written = True
    except Exception:
        det_written = False
# Compute time mapping between Deepgram and engine timeline when detail exists
try:
    det_path = logs_dir / 'deepgram_request_detail.json'
    if det_path.exists():
        det = json.loads(det_path.read_text(errors='ignore') or '{}')
        dg_created = det.get('created') or det.get('response', {}).get('created')
        dg_completed = (det.get('response') or {}).get('completed')
        def to_dt(s):
            try:
                return dt.datetime.fromisoformat((s or '').replace('Z','+00:00'))
            except Exception:
                return None
        created_dt = to_dt(dg_created)
        completed_dt = to_dt(dg_completed)
        engine_dt = call_ts  # already computed above
        offset_seconds = None
        if created_dt and engine_dt:
            offset_seconds = (engine_dt - created_dt).total_seconds()
        mapping = {
            'deepgram_request_id': det.get('request_id'),
            'deepgram_project_id': det.get('project_uuid') or det.get('project_id'),
            'deepgram_created_utc': dg_created,
            'deepgram_completed_utc': dg_completed,
            'engine_first_frame_utc': engine_dt.strftime('%Y-%m-%dT%H:%M:%SZ') if engine_dt else None,
            'offset_seconds_engine_minus_deepgram': offset_seconds,
        }
        (logs_dir / 'deepgram_time_map.json').write_text(json.dumps(mapping, indent=2))
except Exception:
    pass

print("Deepgram snapshot captured:", len(all_reqs), "requests; detail written:", det_written)
PY
fi
echo "RCA_BASE=$BASE"
echo "CALL_ID=$CID"
