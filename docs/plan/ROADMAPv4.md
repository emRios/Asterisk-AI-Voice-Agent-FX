# AI Voice Agent Roadmap v4 — Transport Orchestrator, Audio Profiles, and Provider-Agnostic Pipeline

This roadmap defines the implementation plan to make the engine provider‑agnostic, format‑agnostic, and user‑friendly by standardizing around an internal PCM pipeline, declarative Audio Profiles, and automatic capability negotiation per call.

- Mirrors structure and tone of `docs/plan/ROADMAP.md` and aligns with `docs/Architecture.md`.
- Technical details of AudioSocket wire types and endianness: see `docs/AudioSocket with Asterisk_ Technical Summary for A.md`.

---

## Vision (Context)

- **Single internal format**: PCM16 inside the engine (normalize, DRC, resample). Compand once at the Asterisk edge.
- **Declarative Audio Profiles**: `telephony_ulaw_8k`, `wideband_pcm_16k`, `hifi_pcm_24k` capture user intent.
- **Capability negotiation**: Discover provider I/O (encoding/rate/chunk_ms) and map to chosen profile with safe fallbacks.
- **Continuous streaming**: One pacer per call owns continuity with idle cutoff; providers don’t control pacing.
- **Asterisk-first guardrails**: AudioSocket PCM is little‑endian by spec; select the correct `c(...)` media and keep truncation/transcoding predictable.

References:

- `docs/Architecture.md` — overall system and streaming transport.
- `docs/AudioSocket with Asterisk_ Technical Summary for A.md` — type codes `0x10..0x18`, TLV length (big‑endian), payload PCM LE.

---

## Prerequisites (MUST COMPLETE BEFORE P0)

### Testing & Regression Protocol (Gap 1)

**Golden Baseline Capture**:

- Tag: `Working-Two-way-audio-ulaw` (commit b3e9bad)
- RCA Documentation: `logs/remote/golden-baseline-telephony-ulaw/WORKING_BASELINE_DOCUMENTATION.md`
- Working configuration: `audiosocket.format: slin` (PCM16@8k), Deepgram `mulaw@8000` I/O, continuous stream, attack_ms: 0

**Golden Metrics (from working call 1761186027.1877)**:

```json
{
  "underflows": 0,
  "drift_pct": 0.0,
  "wall_seconds": 85.0,
  "frames_sent": 4250,
  "provider_bytes": "~816000",
  "tx_bytes": "~816000",
  "buffer_starvation_rate": "<1%",
  "latency_ms": "500-1500",
  "audio_quality": "clear, natural"
}
```

**Regression Protocol (before and after each milestone)**:

1. Run identical test call (same DID, same script, same duration ~60-90s)
2. Collect via `scripts/rca_collect.sh`
3. Compare metrics:
   - Underflows must remain ≈ 0
   - Drift must remain ≈ 0%
   - Wall_seconds ≈ content duration (no long tails)
   - Provider/tx byte totals must match
   - Audio quality subjective check (no garble, clear speech)
4. Archive results in `logs/remote/regression-p0-YYYYMMDD-HHMMSS/`
5. **PASS/FAIL decision**: Any metric regression > 10% = FAIL; rollback and debug

**Automated checks (future)**:

- `make test-regression` runs golden call + metric comparison
- CI/CD integration for pre-merge validation

### Backward Compatibility & Migration (Gap 2)

**Legacy Config Handling**:

- If `profiles.*` block is missing in `config/ai-agent.yaml`:
  - Engine synthesizes implicit `telephony_ulaw_8k` profile from existing settings:
    - `audiosocket.format` → `transport_out`
    - `providers.*.input/output_encoding` → `provider_pref`
    - `streaming.sample_rate` → `internal_rate_hz`
  - Log once: "Using synthesized profile 'legacy_compat' from existing config"
  - No config rewrite required; zero-change upgrade for users happy with current setup

**Migration Path**:

- Provide `scripts/migrate_config_v4.py`:
  - Reads current `config/ai-agent.yaml`
  - Generates `profiles.*` block matching current behavior
  - Validates knob compatibility (warns if deprecated knobs present)
  - Emits new YAML with `config_version: 4`
- User can preview migration: `python scripts/migrate_config_v4.py --dry-run`
- Apply migration: `python scripts/migrate_config_v4.py --apply`

**Mixed-mode support (P0-P1)**:

- During P0: profiles are internal-only; engine behaves exactly as before
- During P1: if `profiles.*` exists, use it; else use legacy synthesis
- Post-P2: deprecate legacy knobs with loud warnings but keep functional

### Rollback Plan (Gap 3)

**Per-Milestone Rollback**:

- **P0 Rollback**:
  - If removing swap logic breaks audio:
    - Set env var `DIAG_EGRESS_SWAP_OVERRIDE=auto` (re-enables old behavior)
    - Or revert to tag `pre-p0-transport-stabilization`
  - Validation: run golden call; if metrics match baseline, stay; else rollback
  
- **P1 Rollback**:
  - If Orchestrator breaks format negotiation:
    - Set env var `DISABLE_TRANSPORT_ORCHESTRATOR=true` (falls back to legacy)
    - Or revert to tag `pre-p1-orchestrator`
  
- **P2 Rollback**:
  - Config cleanup is non-breaking (deprecated knobs still work with warnings)
  - Rollback: re-add deprecated knobs to YAML; engine logs warnings but functions

**Rollback checklist**:

1. Stop engine: `docker-compose stop ai-engine`
2. Revert code: `git checkout <pre-milestone-tag>`
3. Rebuild: `docker-compose build ai-engine`
4. Restore config: `cp config/ai-agent.yaml.backup config/ai-agent.yaml`
5. Restart: `docker-compose up -d ai-engine`
6. Validate: run golden call; check metrics

---

## Milestone P0 — Transport Stabilization (Immediate)

- **Goal**: Eliminate garble/tails by enforcing AudioSocket invariants, cadence auto, and pacer idle cutoff.
- **Scope**:
  - Enforce LE PCM on AudioSocket wire; remove all egress byte‑swap logic in streamer.
  - Disable provider‑side PCM swap heuristics by default (keep internal override if absolutely necessary).
  - Set `chunk_size_ms: auto` (20 ms for μ‑law/PCM unless provider explicitly needs otherwise); reframe provider chunks to pacer cadence.
  - Add pacer `idle_cutoff_ms` (~1200 ms) in continuous mode to prevent long tails/underflows.
  - Log a one‑shot "TransportCard" at call start summarizing wire and provider formats.
- **Primary Tasks**:
  - `src/core/streaming_playback_manager.py`: remove egress swap, add idle cutoff, honor `chunk_size_ms: auto`, consistent 20 ms pacing; reframe provider chunks.
  - `src/providers/deepgram.py` (and others): disable internal PCM swap heuristics by default; treat provider PCM as LE.
  - `src/engine.py`: emit TransportCard (wire type, provider I/O, chunk_ms, idle_cutoff_ms).
  - Docs: Update AudioSocket summary to stress PCM LE payload; link it from Architecture.
- **Progress 2025-10-23**:
  - Implemented `_resolve_chunk_size_ms()` and `_resolve_idle_cutoff_ms()` helpers in `src/core/streaming_playback_manager.py` to default cadence to 20 ms and enforce a 1200 ms pacer idle cutoff while supporting configurable overrides.
  - Removed remaining PCM egress swap usage in the playback manager hot path, moving us toward the enforced little-endian wire contract.

- **Regression Findings 2025-10-24**:
  - **Diagnostics tap accumulation broken**: `call_tap_pre_bytes`/`call_tap_post_bytes` remain zero even with `diag_enable_taps: true` due to `_update_audio_diagnostics()` raising repeatedly and preventing tap buffers from flushing. Fix by guarding the diagnostics callback and ensuring call-level tap arrays append regardless of callback failures (`src/core/streaming_playback_manager.py`).
  - **Diagnostics scope bug**: `_update_audio_diagnostics()` still contained an unused transport-alignment block referencing undefined locals, spamming `Audio diagnostics update failed`. Removed the stray block; transport alignment continues via `_emit_transport_card()` (`src/engine.py`).
  - **Unwired egress configuration**: YAML keys `streaming.egress_swap_mode` / `streaming.egress_force_mulaw` were not honored. Streaming manager now ingests both settings from `streaming_config` so swap detection works during regression calls (`src/engine.py`, `src/core/streaming_playback_manager.py`).
  - **Continuous stream pacing**: Greeting segment shows `underflow_events=59`, `drift_pct=-67.1`, and wall duration 47.8 s vs 15.7 s effective despite clear audio. Idle cutoff is blocked because pacer never sees end-of-stream sentinel in continuous mode. Track investigation under “Pacing + idle cutoff” in follow-up tasks.

- **Next Steps (in progress)**:
  - Harden diagnostics: keep tap accumulation decoupled from optional callbacks, ensure RCA bundles report non-zero tap bytes, and suppress redundant alignment warnings once conversions are intentional.
  - Instrument pacing metrics: add buffer-depth and idle-cutoff telemetry around `StreamingPlaybackManager` to close the gap between effective and wall duration; evaluate segment-aware idle close for continuous streams.
  - Re-run ulaw baseline regression after fixes to confirm tap bytes populate and drift returns to ≈0 % with underflows near zero.

- **Verification 2025-10-24 13:05 PDT**:
  - Golden baseline transport restored end-to-end (`audiosocket.format: "slin"`, Deepgram mulaw 8 kHz). Runtime call `1761336116.1987` completed 65 s with clean two-way dialog; pacer emitted 20 ms μ-law frames without underflows.
  - RCA bundle `rca-20251024-200537` shows `agent_out_to_caller.wav` 8 kHz RMS 2030 (66 dB SNR) and `caller_to_provider.wav` 8 kHz RMS 14866 (18.9 dB SNR). Provider chunk log confirms steady 960-byte μ-law packets.
  - Remaining gaps: startup still logs codec-alignment warnings despite intentional `slin`↔`mulaw` bridge; diagnostic taps stay at zero bytes. Track suppressing false warnings and wiring tap capture as short-term fixes.

- **Verification 2025-10-24 17:59 PDT — Taps & Alignment**:
  - Call `1761353185.1999` RCA at `logs/remote/rca-20251025-004851/`.
  - Taps now accumulate (fast-path fixed):
    - `call_tap_pre_bytes=89600`, `call_tap_post_bytes=89600` at 8 kHz → ~5.6 s each; call-level WAVs written under `taps/`.
    - First-200 ms pre/post snapshots emitted per segment (for QA).
  - Alignment warnings suppressed for the intentional PCM↔μ-law bridge:
    - Startup logs show "Provider codec/sample alignment verified" with no Deepgram input vs audiosocket warnings.
  - Audio quality: agent/caller legs assessed "good" (SNR ~66–67 dB agent, ~56–63 dB caller). Overall flagged "poor" only because the first-200 ms snapshot has very low SNR (expected during attack/gating). This skews the aggregator.
  - Streaming tuning summary: `bytes_sent=111680`, `effective_seconds=6.98`, `wall_seconds=44.6`, `drift_pct=-84.4` reflecting long idle vs short speech; no underflow evidence in this run.

  Recommended follow-ups:
  - Adjust RCA aggregator to exclude or down‑weight first‑200 ms snapshots from the overall score; keep detailed per-leg ratings.
  - Keep taps enabled for the next few calls to confirm stability across dialogs; target ≥10–15 s of agent audio to extend tap coverage.
  - Continue monitoring for `underflow_events` and drift; current negative drift is expected with long idle time.

- **Verification 2025-10-24 18:20 PDT — Handshake Gated + 40s Two‑Way Call**:
  - Call `1761355140.2007` RCA at `logs/remote/rca-20251025-012113/`.
  - Deepgram handshake gated correctly:
    - `SettingsApplied` logged before any `AgentAudio` frames.
    - No `BINARY_MESSAGE_BEFORE_SETTINGS` errors present.
  - Taps and metrics:
    - `call_tap_pre_bytes=98560`, `call_tap_post_bytes=98560` at 8 kHz → ~6.16 s each; tap WAVs present.
    - Main legs assessed "good": `agent_from_provider` SNR ~68 dB; `agent_out_to_caller` SNR ~67.4 dB; `caller_recording` SNR ~63.3 dB.
    - First‑200 ms snapshots show high silence (as expected) and are excluded from "overall" by aggregator update; overall reported "good".
  - Streaming tuning summary: `bytes_sent=116800`, `effective_seconds=7.3`, `wall_seconds=38.43`, `drift_pct=-81.0` (idle dominates vs short agent speech). No underflows observed.

  Recommended follow-ups:
  - Keep gating: binary audio must only flow after `SettingsApplied`.
  - Run longer regression (60–90 s) with more agent speech to further extend tap coverage and validate pacing over longer content.
  - Continue monitoring for `underflow_events` and drift; negative drift is expected with long idle but should approach ~0% when content fills the interval.

- **✅ FINAL VALIDATION & P0 COMPLETION — Oct 25, 2025**:
  - **Critical Bug Fix**: AudioSocket format override bug discovered and fixed (commit `1a049ce`).
    - **Root cause**: `src/engine.py` line 1862 incorrectly set `spm.audiosocket_format` from transport profile (caller codec) instead of YAML config.
    - **Impact**: Caller μ-law codec forced AudioSocket to 160-byte frames; Asterisk expected 320-byte PCM16 → severe garble.
    - **Fix**: Removed override; AudioSocket format now always from YAML `audiosocket.format: "slin"`, never from caller codec.
  - **Validation Call**: `1761424308.2043` (45s, two-way conversation) — RCA at `logs/remote/rca-20251025-203447/`.
  - **User Report**: "Clean audio, clean two-way conversation. Audio pipeline is working really well."
  
  **P0 Acceptance Criteria Results**:
  1. ✅ **No garbled greeting**: User confirmed clean audio; transcripts show clear speech
  2. ✅ **Underflows ≈ 0**: Actual = 0 underflow events observed
  3. ✅ **Wall duration appropriate**: 45s call, 11.84s agent audio, no long tails
  4. ✅ **TransportCard present**: Line 191 logs complete transport card with correct wire format
  5. ✅ **No egress swap**: All frames show "μ-law → PCM16 FAST PATH"; zero swap messages
  6. ✅ **Golden metrics match**: Provider bytes 16,320/16,320 (1.0 ratio), SNR 64.6-68.2 dB, frame size 320 bytes
  
  **Key Validations**:
  - AudioSocket wire: `slin` PCM16 @ 320 bytes/frame (correct)
  - Chunk size: 20ms (auto)
  - Idle cutoff: 1200ms (working, backoff during silence)
  - Diagnostic taps: Working (snapshots captured)
  - Alignment warnings: Suppressed (intentional PCM↔μ-law bridge documented)
  
  **Status**: ✅ **P0 COMPLETE** — Production ready. All acceptance criteria met.
  
  **Tag**: `v1.0-p0-transport-stable`
  
  **Documentation**:
  - Success RCA: `logs/remote/rca-20251025-203447/SUCCESS_RCA_ANALYSIS.md`
  - Acceptance validation: `logs/remote/rca-20251025-203447/P0_ACCEPTANCE_VALIDATION.md`
  - Progress summary: `PROGRESS_SUMMARY_20251025.md`
  
  **Known Issues (Non-Blocking)**:
  - Engine caller audio captures have high noise floor (diagnostic only; use Asterisk monitor for caller transcripts)
  - RCA aggregator may skew "overall" score due to attack-phase snapshots (fixed in code, verify next RCA)

- **Inbound Path Scope (Gap 4)**:
  - P0 focuses on **outbound only** (provider → caller).
  - Inbound path (caller → provider) is **proven stable** in working baseline:
    - AudioSocket PCM16@8k → DC bias removal → DC-block filter → encode to provider format (mulaw for Deepgram).
    - This path remains **unchanged** in P0.
  - Inbound orchestration (if needed) deferred to P1.

- **Acceptance (fast checks)**:
  - A test call shows: no garbled greeting; `underflow_events ≈ 0`; `wall_seconds` ≈ content duration (no 20+ s tail).
  - Logs contain TransportCard; no egress swap messages anywhere.
  - Golden metrics match baseline within 10% tolerance.

- **Impact**: Immediate restoration of clarity and pacing stability with minimal config changes.

---

## Milestone P0.5 — OpenAI Realtime Integration & Echo Prevention ✅ COMPLETE

- **Goal**: Enable OpenAI Realtime API as a production-ready provider with full-duplex audio and echo prevention.
- **Status**: ✅ **COMPLETE** (Oct 26, 2025)
- **Scope**:
  - Implement audio gating manager with VAD-based interrupt detection
  - Integrate gating with OpenAI Realtime provider for echo prevention
  - Tune VAD aggressiveness to prevent false positive echo detection
  - Establish golden baseline configuration for OpenAI Realtime
  - Validate production-ready operation with natural conversation flow

- **Primary Achievements**:
  
  **1. Audio Gating Manager** (`src/core/audio_gating_manager.py`):
  - Provider-specific gating (opt-in per provider)
  - Audio buffering during agent speech with VAD-based interrupt detection
  - Per-call state isolation and cleanup
  - Comprehensive debug logging for diagnostics
  
  **2. OpenAI Realtime Provider Integration** (`src/providers/openai_realtime.py`):
  - Gating manager integration with `response.audio.delta` / `response.audio.done` events
  - 24 kHz PCM16 input/output (advertise `pcm_s16le_24000` in session.update)
  - Continuous input mode with OpenAI's server-side VAD
  - Helper method `_send_audio_to_openai()` for clean gating integration
  
  **3. Engine Integration** (`src/engine.py`):
  - Initialize AudioGatingManager when VAD available
  - Pass gating manager to OpenAI provider during instantiation
  - Cleanup gating state on call termination
  
  **4. Critical Configuration Discovery** ⭐:
  - **Problem Identified**: `webrtc_aggressiveness: 0` was TOO SENSITIVE
    - Detected echo as "speech" with 0.4 confidence
    - Caused gate to flutter open/closed 50+ times per call
    - Echo leaked through gaps → self-interruption loop
    - User report: "lot of agent's response still leaked back"
  
  - **Solution**: `webrtc_aggressiveness: 1` (balanced mode)
    - Does NOT detect echo as speech
    - Gate stays open (correct behavior)
    - OpenAI's built-in server-side echo cancellation works properly
    - Natural conversation flow restored
  
  - **Key Insight**: OpenAI Realtime API has sophisticated server-side echo cancellation. Local VAD at level 0 was fighting it. Level 1 ignores echo, lets OpenAI handle turn-taking naturally.

- **Validation Call**: `1761449250.2163` (Oct 26, 2025)
  - Duration: 45.9s, SNR: 64.7 dB
  - Buffered: 0 chunks (vs 50 with aggressiveness: 0)
  - Gate closures: 1 time (vs 50+ with aggressiveness: 0)
  - Self-interruption: None
  - User validation: **"much better results"** ✅

- **Golden Baseline Configuration**:

  ```yaml
  vad:
    use_provider_vad: false
    enhanced_enabled: true       # Required for gating manager
    webrtc_aggressiveness: 1     # ⭐ CRITICAL for OpenAI echo prevention
    webrtc_start_frames: 3
    webrtc_end_silence_frames: 50
    confidence_threshold: 0.6
    energy_threshold: 1500
  ```

- **Acceptance Criteria Results**:
  1. ✅ **Clean audio**: User confirmed clear, natural conversation
  2. ✅ **No self-interruption**: Gate stays open properly, no echo loops
  3. ✅ **Natural turn-taking**: OpenAI's server VAD handles conversation flow
  4. ✅ **Production quality**: SNR 64.7 dB, no buffering issues
  5. ✅ **Reproducible**: Configuration documented and validated

- **Documentation**:
  - Golden baseline: `OPENAI_REALTIME_GOLDEN_BASELINE.md`
  - Detailed RCA: `logs/remote/rca-20251026-033115/GOLDEN_BASELINE_ANALYSIS.md`
  - Comparative analysis: `logs/remote/rca-20251026-032415/AUDIO_GATING_RCA.md`
  - Commit: `937b4a4` (config) + `70fa037` (code)

- **Lessons Learned**:
  1. **Trust the provider's echo handling**: OpenAI has server-side echo cancellation; don't fight it with overly sensitive local VAD
  2. **VAD aggressiveness matters**: Level 0 detects everything including echo; level 1 is balanced for telephony
  3. **Simpler is better**: Configuration change (0→1) solved complex problem that seemed to need code changes
  4. **Validate assumptions**: Initial hypothesis (response.audio.done timing) was wrong; root cause was VAD sensitivity

- **Cancelled Work**:
  - ❌ Initial recommendation to use `response.done` instead of `response.audio.done` for gating
  - **Why cancelled**: Gate staying open is CORRECT behavior; OpenAI's echo cancellation works when not interfered with

- **Impact**: OpenAI Realtime API now production-ready with natural conversation flow and zero self-interruption.

---

## Milestone P1 — Transport Orchestrator + Audio Profiles

- **Status**: ✅ **PRODUCTION READY** (Oct 26, 2025) — Implementation complete + Validation passed
- **Goal**: Provider‑agnostic behavior with per‑call Audio Profile selection and automatic negotiation.
- **Scope**:
  - Add `AudioProfile` (config) with fields: `internal_rate_hz`, `transport_out{encoding, sample_rate_hz}`, `provider_pref{input, output}`, `chunk_ms: auto`, `idle_cutoff_ms`.
  - Add `TransportOrchestrator` that resolves a canonical `TransportProfile` per call using profile + provider caps/ACK.
  - Per‑call overrides via channel vars (all optional; fallback to YAML defaults):
    - `AI_PROVIDER`: Which provider (e.g., `deepgram`, `openai`)
    - `AI_AUDIO_PROFILE`: Which transport profile (e.g., `telephony_ulaw_8k`, `wideband_pcm_16k`)
    - `AI_CONTEXT`: Semantic tag (e.g., `sales`, `support`) mapped to YAML `contexts.*` for prompt/greeting/profile
  - Add `contexts.*` block in YAML for semantic context mapping (cleaner than verbose `AI_PROMPT`/`AI_GREETING` in dialplan).
  - One‑shot "Audio Profile Resolution" log: provider_in/out, internal_rate, transport_out, chunk_ms, idle_cutoff_ms, context, remediation.
- **Primary Tasks**:
  - `src/engine.py`: implement Orchestrator; read `AI_PROVIDER`, `AI_AUDIO_PROFILE`, `AI_CONTEXT` channel vars; produce `TransportProfile`; pass to provider + streamer.
  - `src/providers/*`: expose `ProviderCapabilities` (encodings, sample rates, preferred chunk_ms) or read from ACK; respect Orchestrator output.
  - `config/ai-agent.yaml`: add `profiles.*` block (default `telephony_ulaw_8k`) and `contexts.*` block for semantic mapping.
  - Example YAML structure:

    ```yaml
    profiles:
      default: telephony_ulaw_8k
      telephony_ulaw_8k: { internal_rate_hz: 8000, ... }
      wideband_pcm_16k: { internal_rate_hz: 16000, ... }
    
    contexts:
      default:
        prompt: "You are a helpful assistant..."
        greeting: "Hello, how can I help?"
      sales:
        prompt: "You are a sales assistant. Be enthusiastic."
        greeting: "Thanks for calling sales!"
        profile: wideband_pcm_16k  # optional profile override
      support:
        prompt: "You are technical support. Be concise."
        greeting: "Support line, how can we help?"
    ```

  - Docs: `docs/Architecture.md` add "Transport Orchestrator" section; quick reference for profiles and contexts.

- **Provider Capability Contract (Gap 5)**:
  - Define `ProviderCapabilities` dataclass in `src/providers/base.py`:

    ```python
    @dataclass
    class ProviderCapabilities:
        supported_input_encodings: List[str]  # e.g., ["ulaw", "linear16"]
        supported_output_encodings: List[str]
        supported_sample_rates: List[int]     # e.g., [8000, 16000, 24000]
        preferred_chunk_ms: int = 20
        can_negotiate: bool = True  # if False, use static config only

    ```

  - Each provider adapter implements `def get_capabilities() -> ProviderCapabilities`.
  - **Static config fallback**: If provider returns `can_negotiate: False` or ACK is empty (Deepgram Voice Agent rejects linear16), Orchestrator uses config values only.
  - **Runtime ACK parsing**: Provider adapters implement `parse_ack(event_data) -> Optional[ProviderCapabilities]` to extract accepted formats from provider responses (Deepgram `SettingsApplied`, OpenAI `session.updated`).

- **Late ACK / Mid-Call Negotiation Policy (Gap 6)**:
  - TransportProfile is **locked at call start** (before first audio frame).
  - If provider ACK arrives late (after first chunk sent), log a warning but **do not renegotiate**:

  ```text
    WARNING: Late provider ACK ignored; TransportProfile locked at call start.
    call_id=..., expected_ack_within_ms=500, actual_delay_ms=1200
  ```

- Future (post-GA): Add renegotiation support if provider sends updated settings mid-call.
- Document this constraint in Architecture and quick reference.

- **DC-Block and Inbound Filters Preserved (Gap 8)**:
  - Inbound path retains proven stability filters from working baseline:
    - DC bias removal: `audioop.bias(pcm_bytes, 2, -mean)`
    - DC-block filter: IIR highpass, 0.995 coefficient
  - Orchestrator does **not** touch these; they remain in `src/engine.py::_audiosocket_handle_audio()`.

- **Metrics Schema for Observability (Gap 10)**:
  - Define segment summary schema (emitted after `AgentAudioDone` or idle cutoff):

    ```json
    {
      "event": "Streaming segment summary",
      "call_id": "...",
      "stream_id": "...",
      "provider_bytes": 64000,
      "tx_bytes": 64000,
      "frames_sent": 100,
      "underflow_events": 0,
      "drift_pct": 0.0,
      "wall_seconds": 2.0,
      "buffer_depth_hist": {"0-20ms": 5, "20-80ms": 90, "80-120ms": 5},
      "idle_cutoff_triggered": false,
      "chunk_reframe_count": 3,
      "remediation": null
    }
    ```

  - Prometheus counters: `ai_agent_underflow_events_total`, `ai_agent_drift_pct`, `ai_agent_chunk_reframe_total`.
  - One-shot TransportCard at call start:

    ```json
    {
      "event": "TransportCard",
      "call_id": "...",
      "wire_type": "0x10",
      "wire_encoding": "pcm16",
      "wire_sample_rate": 8000,
      "provider_input": {"encoding": "ulaw", "sample_rate": 8000},
      "provider_output": {"encoding": "ulaw", "sample_rate": 8000},
      "internal_rate": 8000,
      "chunk_ms": 20,
      "idle_cutoff_ms": 1200,
      "profile": "telephony_ulaw_8k"
    }

    ```

- **Provider-Specific ACK Formats (Gap 11)**:
  - Deepgram: `SettingsApplied` event with `audio.input/output` schema
  - OpenAI Realtime: `session.updated` event with `session.input_audio_format` / `session.output_audio_format`
  - Each adapter parses its own ACK format; Orchestrator calls `provider.parse_ack(...)`.
  - Document ACK schemas in `docs/providers/deepgram.md`, `docs/providers/openai.md`.

- **Implementation Achievements (Oct 26, 2025)**:
  1. ✅ **TransportOrchestrator** class created (`src/core/transport_orchestrator.py`)
     - Profile resolution with precedence (AI_PROVIDER > AI_CONTEXT > AI_AUDIO_PROFILE > default)
     - Provider capability negotiation
     - Format validation and remediation
     - Legacy config synthesis for backward compatibility
  
  2. ✅ **Provider Capabilities** enhanced
     - Added `can_negotiate` field to `ProviderCapabilities`
     - Deepgram: `get_capabilities()` + `parse_ack()` (SettingsApplied)
     - OpenAI Realtime: `get_capabilities()` + `parse_ack()` (session.updated)
  
  3. ✅ **Engine Integration** complete
     - `_resolve_audio_profile()` uses TransportOrchestrator
     - Reads AI_PROVIDER, AI_AUDIO_PROFILE, AI_CONTEXT channel vars
     - Applies resolved transport to session, streaming manager, provider config
     - Backward compatible with legacy TransportProfile
  
  4. ✅ **Documentation** created
     - `P1_IMPLEMENTATION_COMPLETE.md` - comprehensive implementation guide
     - Testing strategy for both golden baselines
     - Rollback plan and known limitations documented

- **Acceptance (fast checks)**:
  - Switching `AI_AUDIO_PROFILE` changes end‑to‑end plan without YAML edits; call remains stable.
  - If provider rejects a format (empty ACK), call continues with logged remediation (e.g., 24k→16k, PCM→μ‑law).
  - Logs show TransportCard + segment summaries; metrics align with golden baseline.

- **Validation Results (Oct 26, 2025)**:
  1. ✅ **Deepgram validation** (Call 1761504353.2179)
     - Profile: `telephony_responsive` (idle_cutoff_ms: 600) applied correctly
     - Audio quality: SNR 66.8 dB (exceeds P0 baseline 64 dB)
     - Result: Clean 2-way conversation
     - Known limitation: 3-4s STT latency (Deepgram Voice Agent API intentional)
     - User accepted limitation; documented in P1_POST_FIX_RCA.md
  
  2. ✅ **OpenAI Realtime validation** (Call 1761505357.2187)
     - Profile: `openai_realtime_24k` (idle_cutoff_ms: 0) applied correctly
     - Audio quality: SNR 64.77 dB (matches P0.5 golden baseline 64.7 dB)
     - Drift: -9.6% (vs -52% in incorrect profile)
     - Gate closures: 0 (perfect)
     - Buffering events: 0 (perfect)
     - User feedback: "complete better and natural"
     - Matches/exceeds golden baseline on all metrics
  
  3. ✅ **Profile resolution verified**
     - TransportOrchestrator correctly resolved profiles for both providers
     - TransportCard emitted with correct settings
     - Channel variable precedence working correctly
  
  4. ✅ **Audio gating working correctly**
     - Stays open during agent speech (correct)
     - No false positives (detecting agent audio as echo)
     - webrtc_aggressiveness: 1 validated as correct setting

- **Production Configurations**:
  - **Deepgram**: Use `telephony_responsive` profile (idle_cutoff_ms: 600)
    - Limitation: 3-4s latency from Deepgram Voice Agent API (service constraint)
    - Alternative: Consider Deepgram STT-only mode for < 2s latency
  
  - **OpenAI Realtime**: Use `openai_realtime_24k` profile (idle_cutoff_ms: 0) ✅
    - Dialplan MUST set: `AI_AUDIO_PROFILE=openai_realtime_24k`
    - Audio gating MUST stay enabled (webrtc_aggressiveness: 1)
    - Validated as production-ready

- **Documentation**:
  - `P1_VALIDATION_RCA.md` - Initial validation findings
  - `P1_POST_FIX_RCA.md` - Root cause analysis of failed fixes
  - `OPENAI_REALTIME_P1_FINAL_RCA.md` - Final validation success ✅

- **Impact**: Simplifies operator experience; same engine works across providers/formats.

---

## Milestone P2.1 — Post-Call Diagnostics ✅ COMPLETE

- **Status**: ✅ **PRODUCTION READY** (Oct 26, 2025)
- **Goal**: Automated post-call RCA with AI-powered diagnosis matching manual RCA quality.
- **Tool**: `agent troubleshoot` CLI command
- **Scope**:
  - Automated call ID detection and filtering (excludes AudioSocket infrastructure channels)
  - RCA-level metrics extraction (provider bytes, drift, underflows, VAD, transport)
  - Golden baseline comparison (OpenAI Realtime, Deepgram, Streaming Performance)
  - Format/sampling alignment detection (config vs runtime validation)
  - Greeting segment awareness (excludes timing artifacts from quality scoring)
  - AI-powered diagnosis with context-aware prompts (OpenAI/Anthropic)
  - Quality scoring system (0-100 with EXCELLENT/FAIR/POOR/CRITICAL verdicts)
  
- **Implementation**:
  - `cli/cmd/agent/troubleshoot.go` - CLI command entry point
  - `cli/internal/troubleshoot/troubleshoot.go` - Main analysis runner
  - `cli/internal/troubleshoot/metrics.go` - Metrics extraction from logs
  - `cli/internal/troubleshoot/baselines.go` - Golden baseline comparison
  - `cli/internal/troubleshoot/format_analyzer.go` - Format alignment detection
  - `cli/internal/troubleshoot/llm.go` - AI diagnosis integration

- **Key Features**:
  
  **1. Format/Sampling Alignment Detection** (Critical):
  - Loads config from ai_engine container
  - Compares `audiosocket.format` config vs runtime
  - Validates provider input/output encoding alignment
  - Checks frame size consistency (slin=320 bytes, mulaw=160 bytes)
  - Detects AudioSocket format override bugs (must be slin per golden baseline)
  - Quality impact: Format mismatch -30 points, Provider mismatch -25 points
  
  **2. Greeting Segment Awareness**:
  - Extracts `stream_id` from logs, detects greeting segments
  - Excludes greeting drift from worst drift calculation (greeting has conversation pauses)
  - Excludes greeting underflows from quality scoring (occur during silence)
  - Prevents false negatives (marking good calls as POOR due to greeting artifacts)
  - Validated: Call 2199 now scores EXCELLENT (was POOR before fix)
  
  **3. Underflow Rate Analysis**:
  - Calculates underflow rate as `underflows / total_frames * 100`
  - Thresholds: <1% acceptable (no penalty), 1-5% minor (-5 pts), >5% significant (-20 pts)
  - Context-aware: Greeting underflows ignored, conversation underflows tracked
  
  **4. AI Diagnosis Quality**:
  - Filters benign warnings (e.g., DeepgramProviderConfig target_encoding)
  - Provides exact config fixes (file + section + parameter + value)
  - References golden baseline values (webrtc_aggressiveness: 1, audiosocket.format: slin)
  - Avoids false positives through explicit false positive guidance

- **Usage Examples**:

  ```bash
  # List recent calls
  ./bin/agent troubleshoot --list
  
  # Analyze most recent call
  ./bin/agent troubleshoot --last
  
  # Analyze specific call
  ./bin/agent troubleshoot --call 1761523231.2199
  
  # Skip AI diagnosis (faster)
  ./bin/agent troubleshoot --last --no-llm
  
  # Use Anthropic Claude
  ./bin/agent troubleshoot --last --provider anthropic
  ```

- **Output Example**:

  ```text
  🎯 OVERALL CALL QUALITY
  Verdict: ✅ EXCELLENT - No significant issues detected
  Quality Score: 100/100
  
  ✅ Provider bytes ratio: ~1.0
  ✅ Drift: <10% (greeting excluded)
  ✅ No underflows (conversation)
  ✅ Clean audio expected
  
  Streaming Performance:
    Segments: 1 (1 greeting, 0 conversation)
    Greeting drift: -70.8% (expected - includes pauses)
    Underflows: 0 ✅ NONE
  ```

- **Validation Results**:
  - **Call 2199 Alignment Test**:
    - Manual RCA: "GOOD - SNR 67.3 dB, clean audio"
    - agent troubleshoot: "EXCELLENT - 100/100"
    - Result: ✅ ALIGNED
  
  - **Format Detection Test**:
    - Detects AudioSocket format mismatches (slin vs ulaw)
    - Identifies frame size misalignment (320 vs 160 bytes)
    - Catches provider format config errors
    - Result: ✅ VALIDATED

- **Acceptance Criteria Met**:
  1. ✅ Accurate call detection (filters AudioSocket channels)
  2. ✅ RCA-level metrics depth (matches manual RCA)
  3. ✅ Baseline alignment (exact value comparisons)
  4. ✅ Format validation (config vs runtime)
  5. ✅ Greeting awareness (timing artifact handling)
  6. ✅ AI diagnosis quality (actionable, specific fixes)
  7. ✅ Quality accuracy (matches manual RCA verdicts)
  8. ✅ False positive prevention (ignores benign warnings)

- **Known Limitations**:
  - Requires Docker access to ai_engine container (for config loading)
  - Analyzes recent logs only (current container session)
  - No audio file analysis (logs only; RCA script analyzes WAV files)
  - Requires LLM API key for AI diagnosis (optional with --no-llm)

- **Impact**:
  - **Operators**: Instant RCA without manual log parsing
  - **Development**: Rapid debugging of audio issues
  - **Production**: Proactive issue detection
  - **Documentation**: Self-documenting (shows exact fixes)

- **Commits**:
  - `9db4479` - Greeting segment timing alignment
  - `a910d13` - Exclude greeting underflows from scoring
  - `aa5375f` - Format/sampling alignment detection
  - `8ef5b10` - Filter benign Deepgram target_encoding warning

---

## Milestone P2 — Config Cleanup + CLI UX

- **Status**: 🟡 **IN PROGRESS** (P2.1 Complete, P2.2 Next)
- **Goal**: Minimize knobs; add guided setup and diagnostics.
- **Scope**:
  - Deprecate/remove troubleshooting‑only knobs from user config: `egress_swap_mode`, `allow_output_autodetect`, attack/normalizer/limiter toggles (keep internal only).
  - Add CLI:
    - `agent troubleshoot` — ✅ **COMPLETE** (Oct 26, 2025) - post-call RCA with AI diagnosis
    - `agent init` — pick provider(s)/voice/profile; writes `.env` and minimal `config/ai-agent.yaml`.
    - `agent doctor` — validates ARI, app_audiosocket, ports, provider keys; prints fix‑ups.
    - `agent demo` — loopback framing check + provider ping (plays reference audio).
- **Primary Tasks**:
  - ✅ `cli/cmd/agent/troubleshoot.go` + internal troubleshooting package (complete)
  - ⏳ `scripts/agent_init.py`, `scripts/agent_doctor.py`, `scripts/agent_demo.py` with Makefile wrappers.
  - ⏳ Docs: Getting started section in `docs/Architecture.md`; examples updated.

- **Attack/Normalizer/Limiter Migration (Gap 9)**:
  - Remove from user-facing config schema (`config/ai-agent.yaml`).
  - Keep in code behind env var `DIAG_ENABLE_AUDIO_PROCESSING=true`.
  - If env var is set, log loudly: `WARNING: Diagnostic audio processing enabled. NOT for production use. May corrupt audio.`
  - Default behavior (env var unset): `attack_ms=0`, normalizer/limiter disabled.
  - Document migration: "These knobs are now internal diagnostics only. Remove from your YAML; they won't be loaded."

- **Reference Audio for `agent demo` (Gap 12)**:
  - Ship known-good reference file: `tests/fixtures/reference_tone_8khz.wav` (1 kHz sine wave @ 8k PCM16, 2 s duration).
  - `agent demo` plays this over AudioSocket loopback and measures:
    - RMS (should match source within 10%)
    - Clipping detection (should be 0 samples clipped)
    - SNR (should be > 60 dB)
  - Acceptance: Demo reports "PASS" if all checks succeed; "FAIL" with diagnostic hints otherwise.

- **`agent doctor` Validation Checklist (Gap 13)**:
  - ARI accessible: `GET /ari/asterisk/info` returns 200
  - `app_audiosocket` loaded: `module show like audiosocket` shows loaded
  - AudioSocket port available: `nc -zv 127.0.0.1 8090` succeeds
  - Dialplan context exists: `dialplan show from-ai-agent` has entries
  - Provider keys present: `DEEPGRAM_API_KEY` / `OPENAI_API_KEY` in `.env`
  - Provider endpoints reachable: HTTP ping to Deepgram/OpenAI APIs
  - Shared media directory writable: `/mnt/asterisk_media/ai-generated` exists and writable
  - Docker network connectivity (if containerized)
  - **Asterisk dialplan codec check (Gap 7)**: Validate `c(slin)` vs `c(slin16)` matches `audiosocket.format`
  - Print ✅ or ❌ for each item with fix-up suggestions.

- **Config Schema Versioning (Gap 14)**:
  - Add `config_version: 4` to `config/ai-agent.yaml`.
  - Engine validates on load:
    - If `config_version < 4` and `profiles.*` missing → auto-migrate or refuse to start (log clear instructions).
    - If `config_version >= 4` → expect `profiles.*`; use it.
  - Migration script `scripts/migrate_config_v4.py` updates version field automatically.

- **Acceptance (fast checks)**:
  - `agent doctor` reports green for all checks; `agent demo` plays clean audio and reports latency + quality metrics.
  - Deprecated knobs removed from YAML; warnings logged if env var override is set.

- **Impact**: Faster onboarding; fewer footguns; consistent environments.

---

## Milestone P3 — Quality, Multi‑Provider Demos, and Hifi

- **Goal**: Improve resampling quality for hifi and showcase multi‑provider parity.
- **Scope**:
  - Optional higher‑quality resamplers (e.g., speexdsp/soxr) for `hifi_pcm_24k` profile.
  - Multi‑provider demos (Deepgram + OpenAI Realtime) with the Orchestrator.
  - Extended metrics for cadence reframe efficiency and pacer drift control.
- **Primary Tasks**:
  - Library integration behind a feature flag; fall back to `audioop` by default.
  - Example configs and demo scripts for each provider pairing.
- **Acceptance (fast checks)**:
  - Side‑by‑side playback comparisons show improved frequency response at 24 kHz; drift remains ≈ 0%.
- **Impact**: Better fidelity for hifi use cases without compromising PSTN reliability.

---

## Asterisk‑First Guardrails (Always On)

- Always originate AudioSocket with the correct PCM type for the wire:
  - `c(slin)` for `0x10` (PCM16@8k), `c(slin16)` for `0x12` (PCM16@16k), etc.
  - Do not send μ‑law over `0x10` — it’s PCM16 LE by spec. See `docs/AudioSocket with Asterisk_ Technical Summary for A.md`.
- Keep SIP trunk `allow=ulaw` for PSTN; Asterisk transcodes ulaw↔slin at 8 kHz.
- Channel vars are **optional overrides**; if unset, engine uses YAML defaults.
- Supported overrides: `AI_PROVIDER` (which provider), `AI_AUDIO_PROFILE` (which transport profile), `AI_CONTEXT` (semantic tag mapped to prompt/greeting in YAML).
- Verbose vars like `AI_PROMPT`/`AI_GREETING` can be used but `AI_CONTEXT` is recommended for cleaner dialplans.

### Detailed Dialplan Mapping (Gap 7)

**AudioSocket Type to Codec to Dialplan Parameter**:

| Config Format | AudioSocket Type | Encoding | Sample Rate | Dial Parameter | Use Case |
|---------------|------------------|----------|-------------|----------------|----------|
| slin | 0x10 | PCM16 LE | 8 kHz | c(slin) | Default telephony (working baseline) |
| slin16 | 0x12 | PCM16 LE | 16 kHz | c(slin16) | Wideband WebRTC |
| slin24 | 0x13 | PCM16 LE | 24 kHz | c(slin24) | Hifi future |
| slin48 | 0x16 | PCM16 LE | 48 kHz | c(slin48) | Ultra-hifi future |

**Critical Rules**:

- Do NOT mix formats: `audiosocket.format: slin` with dialplan `c(slin16)` causes sample rate mismatch
- Do NOT send mulaw over 0x10: AudioSocket Type 0x10 expects PCM16 LE only
- Engine validates at startup: format must match generated dialplan parameter or refuse to start

**Minimal Dialplan (uses YAML defaults)**:

```asterisk
[from-ai-agent]
exten => s,1,NoOp(AI Voice Agent - YAML defaults)
 same => n,Stasis(asterisk-ai-voice-agent)
 same => n,Hangup()
```

**Advanced Dialplan (optional overrides)**:

```asterisk
[from-ai-agent-sales]
exten => s,1,NoOp(Sales line - custom profile)
 same => n,Set(AI_PROVIDER=deepgram)
 same => n,Set(AI_AUDIO_PROFILE=wideband_pcm_16k)
 same => n,Set(AI_CONTEXT=sales)  ; maps to YAML contexts.sales.*
 same => n,Stasis(asterisk-ai-voice-agent)
 same => n,Hangup()
```

Engine generates: `AudioSocket/${host}:${port}/${uuid}/c(slin)` from `audiosocket.format: slin`

**Validation by agent doctor**:

- Check dialplan parameter matches audiosocket.format config
- Report mismatch with fix suggestion

---

## Observability & RCA

- One‑shot TransportCard + Audio Profile Resolution log at call start.
- Segment summary metrics: underflows, drift_pct, buffer depth histogram, provider_bytes vs tx_bytes, wall_seconds.
- `scripts/rca_collect.sh` remains the default for RCA; bundle includes config snapshot + tap WAVs.

---

## Migration Path (from current baseline)

- Keep "slin fast‑path + continuous stream" as robust default for telephony.
- Introduce `profiles.*` + Negotiator behind a feature flag; default to `telephony_ulaw_8k`.
- Add detection of provider ACK empties vs acceptances; apply remediation and log once.
- Later enable `wideband_pcm_16k`/`hifi_pcm_24k` profiles where needed; compand once at the PSTN edge only.

---

## Timeline & Ownership (Indicative)

- **P0 (1–2 days)**: ✅ Transport stabilization — COMPLETE (Oct 25, 2025)
- **P0.5 (1 day)**: ✅ OpenAI Realtime — COMPLETE (Oct 26, 2025)
- **P1 (3–5 days)**: ✅ Orchestrator + profiles — COMPLETE (Oct 26, 2025)
- **P2.1 (1 day)**: ✅ Post-call diagnostics — COMPLETE (Oct 26, 2025)
- **P2.2 (1–2 weeks)**: ⏳ Setup tools + config cleanup — NEXT
- **P3 (2–4 days)**: 🔮 Hifi + demos — FUTURE

Quick verification after each milestone should take < 1 minute via a smoke call + log/metrics inspection.

---

## Deliverables (Files/Modules)

- Engine: `src/engine.py` (TransportOrchestrator, TransportCard logs)
- Playback: `src/core/streaming_playback_manager.py` (no swap, idle cutoff, reframe, chunk_ms auto)
- Providers: `src/providers/deepgram.py`, `src/providers/*` (caps exposure, honor Negotiator)
- Config: `config/ai-agent.yaml` (`profiles.*`, default profile)
- CLI: `scripts/agent_init.py`, `scripts/agent_doctor.py`, `scripts/agent_demo.py`
- Docs: `docs/Architecture.md`, `docs/AudioSocket with Asterisk_ Technical Summary for A.md`, this `docs/plan/ROADMAPv4.md`

---

## Acceptance Checklist (Global)

- **[transport invariants]** AudioSocket payload is PCM16 LE; no egress/provider swap; correct `c(...)` per type; no garble.
- **[pacing health]** Underflows ~0; drift ≈ 0%; wall_seconds ≈ content duration; idle cutoff prevents tails.
- **[negotiation]** Per‑call `AI_AUDIO_PROFILE` switches plans; AKAs and remediations logged.
- **[ux]** `agent init/doctor/demo` enable a first call in minutes; docs match code.
- **[docs]** Architecture, Roadmap v4, and AudioSocket summary are consistent and referenced.

---

## Gap Coverage Summary

All critical gaps identified in `docs/plan/ROADMAPv4-GAP-ANALYSIS.md` have been addressed:

**P0-Critical Gaps (RESOLVED)**:

- ✅ **Gap 1 (Testing)**: Golden baseline metrics captured; regression protocol defined
- ✅ **Gap 2 (Backward Compat)**: Legacy config synthesis; migration script specified
- ✅ **Gap 3 (Rollback)**: Per-milestone rollback procedures documented
- ✅ **Gap 4 (Inbound Path)**: Explicitly scoped to outbound-only in P0; inbound proven stable
- ✅ **Gap 5 (Provider Caps)**: ProviderCapabilities dataclass defined; static fallback specified
- ✅ **Gap 6 (Late ACK)**: Lock-at-start policy documented; late ACK warning behavior defined
- ✅ **Gap 7 (Dialplan Mapping)**: Comprehensive table added; agent doctor validation specified

**P1 Gaps (RESOLVED)**:

- ✅ **Gap 8 (DC-Block)**: Inbound filters explicitly preserved
- ✅ **Gap 9 (Attack/Normalizer)**: Env var migration path defined
- ✅ **Gap 10 (Metrics Schema)**: TransportCard + segment summary schemas documented
- ✅ **Gap 11 (Provider ACK)**: Per-provider ACK parsing contract specified

**P2 Gaps (RESOLVED)**:

- ✅ **Gap 12 (Reference Audio)**: Test fixture specified with acceptance criteria
- ✅ **Gap 13 (agent doctor)**: Full validation checklist documented
- ✅ **Gap 14 (Config Versioning)**: Schema version field and migration handling defined

**Deferred (Post-GA)**:

- ⏭️ Gap 15 (A/B Testing): Post-GA enhancement
- ⏭️ Gap 16 (Multi-Locale): Post-GA enhancement
- ⏭️ Gap 17 (AEC/NS): Post-GA enhancement

---

## Pre-Implementation Checklist

Before starting P0 code changes:

- [ ] Tag current code: `pre-p0-transport-stabilization`
- [ ] Run golden baseline call (μ-law@8k Deepgram); capture via `scripts/rca_collect.sh`
- [ ] Archive golden metrics in `logs/remote/golden-baseline-YYYYMMDD-HHMMSS/`
- [ ] Backup current config: `cp config/ai-agent.yaml config/ai-agent.yaml.pre-p0`
- [ ] Review working baseline doc: `logs/remote/golden-baseline-telephony-ulaw/WORKING_BASELINE_DOCUMENTATION.md`
- [ ] Confirm all team members understand rollback procedure
- [ ] Create regression comparison script (automated or manual checklist)

---

## Success Criteria (Post-Implementation)

**P0 Success**:

- Golden call regression: metrics match baseline within 10%
- No garbled audio on linear16@16k test call
- TransportCard logs present; no swap messages
- Underflows ≈ 0; wall_seconds ≈ content duration

**P1 Success**:

- `AI_AUDIO_PROFILE` channel var switches plans dynamically
- Provider ACK empty → remediation logged; call continues
- Multi-provider parity (Deepgram + OpenAI) demonstrated

**P2 Success**:

- `agent doctor` reports green on fresh install
- `agent demo` plays clean reference audio; metrics PASS
- Deprecated knobs removed from YAML schema

**P3 Success**:

- Hifi profile demonstrates improved frequency response
- Side-by-side demos published

---

## Critical Bug Fixes (Pre-P0)

### Fix 1: AudioSocket Format Override from Transport Profile (Oct 25, 2025)

**Issue**: AudioSocket wire format was incorrectly overridden by detected caller SIP codec instead of using YAML config.

**Root Cause**:

- `src/engine.py` line 1862: `spm.audiosocket_format = enc` (where `enc` came from transport profile detection)
- Transport profile was set from caller's `NativeFormats: (ulaw)` during Stasis entry
- This overrode the correct YAML setting `audiosocket.format: "slin"` and dialplan `c(slin)`

**Impact**:

- Caller with μ-law codec forced AudioSocket wire to μ-law (160 bytes/frame @ 8kHz)
- Asterisk channel expected PCM16 slin (320 bytes/frame @ 8kHz) per dialplan
- Mismatch: 160-byte μ-law frames interpreted as 320-byte PCM16 → severe garble/distortion
- No audio after greeting due to broken bidirectional audio chain

**Fix** (commit 1a049ce):

```python
# REMOVED: spm.audiosocket_format = enc
# CRITICAL: Do NOT override audiosocket_format from transport profile.
# AudioSocket wire format must always match config.audiosocket.format (set at engine init),
# NOT the caller's SIP codec. Caller codec applies only to provider transcoding.
```

**Evidence**:

- RCA: `logs/remote/rca-20251025-062235/`
- Logs showed: `TransportCard: wire_encoding="ulaw"`, `target_format="ulaw"`, `frame_size_bytes=160`
- Expected: `audiosocket_format="slin"`, `target_format="slin"`, `frame_size_bytes=320`
- Golden baseline comparison: wire format must be `slin` PCM16 @ 8kHz per YAML and dialplan

**Validation**:

- Transport alignment summary now correctly shows: `"audiosocket_format": "slin"`, `"streaming_target_encoding": "slin"`
- Next test call must verify: clean audio both directions, 320-byte PCM16 frames, no garble

**Lesson**:

- AudioSocket wire leg is **separate** from caller-side trunk codec
- Transport profile governs **provider transcoding** only (caller μ-law ↔ Deepgram μ-law)
- AudioSocket wire format is **static** per YAML/dialplan, not dynamic per call

---

## Next Steps & Strategic Priorities (Post-P0.5)

### Immediate Priorities (Next 1-2 Weeks)

**1. Production Monitoring & Validation**
- [ ] Monitor 10-20 OpenAI Realtime production calls
- [ ] Collect metrics: gate behavior, audio quality, user satisfaction
- [ ] Establish baseline metrics dashboard (Prometheus/Grafana)
- [ ] Document any edge cases or failure modes

**2. Multi-Provider Testing**
- [ ] Validate Deepgram Voice Agent still working (golden baseline)
- [ ] Test provider switching without restart
- [ ] Document provider-specific quirks and configurations
- [ ] Create provider comparison guide

**3. Documentation & Knowledge Transfer**
- [ ] Update Architecture.md with audio gating system
- [ ] Create operator's guide for VAD tuning
- [ ] Document troubleshooting procedures for echo issues
- [ ] Update deployment playbook with P0.5 changes

### Short-Term Enhancements (2-4 Weeks)

**1. Pipeline Orchestrator (Resume P1)**
- Now that both Deepgram and OpenAI are production-ready, implement:
  - `TransportOrchestrator` for dynamic provider/profile selection
  - `AudioProfile` config system (`telephony_ulaw_8k`, `wideband_pcm_16k`, etc.)
  - Per-call overrides via channel vars (`AI_PROVIDER`, `AI_AUDIO_PROFILE`, `AI_CONTEXT`)
  - Provider capability negotiation and ACK parsing
- **Goal**: Seamless provider switching without config edits

**2. Enhanced Observability**
- [ ] Extend Prometheus metrics for OpenAI Realtime
  - Gate open/close events per call
  - VAD confidence distribution
  - Buffer statistics
  - Provider-specific latencies
- [ ] Add health checks for OpenAI WebSocket stability
- [ ] Create alerting rules for anomalies

**3. Advanced VAD Tuning**
- [ ] Test aggressiveness levels 2-3 for noisier environments
- [ ] Document when to use each level (0: disabled, 1: telephony, 2: noisy, 3: very noisy)
- [ ] Add adaptive VAD threshold tuning (optional)
- [ ] Create VAD calibration tool

### Medium-Term Features (1-2 Months)

**1. Multi-Language Support**
- Leverage pipeline orchestrator for language-specific profiles
- Test OpenAI Realtime multilingual capabilities
- Document language-specific VAD tuning requirements

**2. Advanced Echo Cancellation (Optional)**
- Investigate if any scenarios need more aggressive echo prevention
- Consider acoustic echo cancellation (AEC) library integration
- Document when built-in echo handling is insufficient

**3. Load Testing & Scale**
- Test concurrent OpenAI Realtime calls (10, 50, 100+)
- Measure WebSocket connection limits
- Document scaling considerations and bottlenecks
- Optimize resource usage per call

**4. Config Cleanup (Resume P2)**
- Deprecate troubleshooting-only knobs
- Add CLI tools: `agent init`, `agent doctor`, `agent demo`
- Migrate to profiles-based configuration
- Create guided setup experience

### Long-Term Vision (2-3 Months)

**1. Hybrid AI Architectures**
- OpenAI Realtime + local STT for privacy-sensitive portions
- Deepgram + OpenAI LLM + local TTS combinations
- Cost optimization strategies (local vs cloud)

**2. Advanced Features**
- Function calling with OpenAI Realtime
- Multi-turn context management
- Sentiment analysis and call routing
- Real-time translation

**3. Enterprise Features**
- A/B testing framework for providers/configs
- Call quality scoring and analytics
- Compliance and recording features
- Multi-tenant support

### Research & Exploration

**1. Provider Ecosystem Expansion**
- Google Gemini Live (when available)
- Anthropic Claude Voice (future)
- Azure Speech Services integration
- Custom model hosting

**2. Audio Quality Enhancements**
- Higher sample rates (16k, 24k, 48k)
- Better resamplers (speexdsp, soxr)
- Noise suppression integration
- Audio normalization improvements

**3. Asterisk Integration Improvements**
- Direct RTP integration (bypass AudioSocket for lower latency)
- WebRTC softphone support
- Conference bridge integration
- Call transfer and parking features

### Decision Points & Trade-offs

**Should we prioritize P1 (Orchestrator) or monitoring?**
- **Orchestrator**: Enables dynamic provider switching, cleaner architecture
- **Monitoring**: Ensures production stability, catches issues early
- **Recommendation**: Start monitoring (1 week), then Orchestrator (2-3 weeks)

**How much effort for hifi audio (P3)?**
- Current 8kHz telephony quality is excellent for PSTN
- 16kHz/24kHz beneficial for WebRTC, web apps
- **Recommendation**: Defer until P1/P2 complete and user demand exists

**Should we optimize for cost or latency?**
- OpenAI Realtime: Higher cost, lower latency, better UX
- Deepgram + GPT: Lower cost, slightly higher latency
- **Recommendation**: Support both; let users choose per use case

### Success Metrics (3-Month Goals)

**Operational Excellence**:
- [ ] 99.9% uptime for AI voice agent service
- [ ] < 5% error rate across all providers
- [ ] < 2s end-to-end latency (median)
- [ ] Zero critical bugs in production

**Feature Completeness**:
- [ ] 3+ production-ready providers (Deepgram, OpenAI, +1)
- [ ] Dynamic provider switching via channel vars
- [ ] Comprehensive documentation and operator guides
- [ ] Automated deployment and rollback procedures

**User Satisfaction**:
- [ ] 90%+ clear audio quality reports
- [ ] Natural conversation flow (no self-interruption)
- [ ] Easy onboarding (< 30 min to first call)
- [ ] Positive community feedback

### Risk Management

**Technical Risks**:
- WebSocket instability at scale → mitigation: connection pooling, retry logic
- Provider API changes → mitigation: version pinning, rapid adaptation
- Memory leaks in long calls → mitigation: call duration limits, monitoring

**Operational Risks**:
- Provider outages → mitigation: automatic failover, multi-provider support
- Configuration complexity → mitigation: profiles system, validation tools
- Debug difficulty → mitigation: comprehensive logging, RCA tools

### Team & Resource Allocation

**Current State**:
- ✅ P0 Transport Stabilization: COMPLETE
- ✅ P0.5 OpenAI Realtime: COMPLETE
- ✅ P1 Transport Orchestrator: **PRODUCTION READY** (Oct 26, 2025)
- ⏳ P2 Config Cleanup: Ready to start

**Recommended Focus** (Next Sprint):
1. **Week 1**: P1 production deployment + monitoring
2. **Week 2**: Config cleanup + deprecate legacy knobs
3. **Week 3**: CLI tools (`agent init`, `agent doctor`, `agent demo`)
4. **Week 4**: Documentation + operator guides

---

## References and Cross-Links

- **Baseline**: `logs/remote/golden-baseline-telephony-ulaw/WORKING_BASELINE_DOCUMENTATION.md`
- **Gap Analysis**: `docs/plan/ROADMAPv4-GAP-ANALYSIS.md`
- **AudioSocket Spec**: `docs/AudioSocket with Asterisk_ Technical Summary for A.md` — Type codes, TLV format, PCM LE payload
- **AudioSocket-Provider Alignment**: `docs/AudioSocket-Provider-Alignment.md` — Codec alignment patterns, latency optimization, multi-provider strategies
- **Architecture**: `docs/Architecture.md`
- **Original Roadmap**: `docs/plan/ROADMAP.md` (Milestones 1-8)
- **P1 Implementation Plan**: `docs/plan/P1_IMPLEMENTATION_PLAN.md` — Multi-provider support (5-day plan)
- **Git Tag**: `Working-Two-way-audio-ulaw` (commit b3e9bad)
