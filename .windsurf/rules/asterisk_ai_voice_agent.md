---
trigger: always_on
description: Development rules and guidelines for the Asterisk AI Voice Agent v4.x project
globs: src/**/*.py, *.py, docker-compose.yml, Dockerfile, config/ai-agent.yaml
---

# Asterisk AI Voice Agent v4.x — Windsurf Rules

> For high-level planning, onboarding, and “what to build next”, defer to the AVA project manager persona defined in `AVA.mdc`. Use this file to constrain *how* code and configs are changed.

## GA Scope & Architecture

- Maintain the two-container design (`ai-engine`, `local-ai-server`) with AudioSocket-first ingest and streaming transport downstream; file playback remains an automatic fallback. ExternalMedia/RTP remains a safety path and is exercised during transition.
- Preserve the Hybrid ARI lifecycle in `engine.py`; extend `_handle_caller_stasis_start_hybrid()` and related helpers rather than bypassing originate/bridge steps.
- Continue migrating state into `SessionStore`, `PlaybackManager`, and `ConversationCoordinator`; new logic should interact through these abstractions so `/health` and metrics stay trustworthy.
 - Respect newer transport abstractions (e.g., TransportOrchestrator and audio profiles) as documented in `docs/ROADMAP.md` and milestone files.

## Workflow Essentials

- For **public contributors**:
  - It is acceptable to run Docker locally following `README.md` and `docs/INSTALLATION.md` (e.g., `./install.sh`, `docker compose up`, and `agent init/doctor/demo` from `docs/CLI_TOOLS_GUIDE.md`).
  - Prefer small, well-scoped branches from `develop` and keep changes aligned with the documented architecture.
- For **maintainers using the shared lab server**:
  - Work on `develop`, push, then on `root@mypbx.server.com:/root/Asterisk-AI-Voice-Agent` run `git pull` and `docker-compose up -d --build --force-recreate ai-engine local-ai-server`.
- Use `scripts/rca_collect.sh` for RCA evidence on server.
- When `vad.use_provider_vad` is enabled, keep local WebRTC/Enhanced VAD disabled and document any follow-on tuning in the config + milestone notes.
- Ensure OpenAI Realtime commits operate on 24 kHz PCM (`openai_realtime.provider_input_sample_rate_hz=24000`; advertise `pcm_s16le_24000` in session.update) before converting back to μ-law for playback.

## Streaming Transport Guardrails

- Treat `config/ai-agent.yaml` as the source of truth for streaming defaults (`streaming.min_start_ms`, `low_watermark_ms`, `fallback_timeout_ms`, `provider_grace_ms`, `jitter_buffer_ms`) and `barge_in.post_tts_end_protection_ms`.
- Keep `StreamingPlaybackManager` pacing provider frames in 20 ms slices, sustaining jitter buffer depth before playback and pausing when depth drops below the low watermark.
- Preserve post-TTS guard windows so the agent does not hear its own playback; ConversationCoordinator must stay responsible for gating toggles and metrics.

## Pipelines & Providers

- Register new STT/LLM/TTS adapters via `src/pipelines/orchestrator.py`, extend the YAML schema, update examples, and refresh milestone docs.
- Providers must honour μ-law/8 kHz input from AudioSocket, emit events compatible with Prometheus metrics, and expose readiness through `/health`.
- Capture regression details (call IDs, tuning outcomes) in `docs/regressions/` and link or compare to golden baselines under `docs/baselines/golden/`.
- Local provider: rely on the new ~1.2 s idle-finalized STT, async TinyLlama execution, and the engine’s ingest/transcript queues with transcript aggregation (≥ 3 words or ≥ 12 chars) so slow local LLM responses never starve AudioSocket or fire premature replies.

## Testing & Observability

- Regression loop: place an AudioSocket call on the server, watch streaming depth/fallback logs, scrape `/metrics` for latency histograms, then archive findings with `scripts/rca_collect.sh`.
- Remote ai-engine logs: from the repo run `timestamp=$(date +%Y%m%d-%H%M%S); ssh root@vmypbx.server.com" cd /root/Asterisk-AI-Voice-Agent && docker-compose logs ai-engine --since 30m --no-color" > logs/ai-engine-voiprnd-$timestamp.log` to capture the latest container output for RCA.
- Update `docs/Architecture.md`, `docs/ROADMAP.md`, and milestone instructions when architecture or workflow changes ship; rules across IDEs must stay synchronized.

## GPT-5 Prompting Guidance

- **Precision & consistency**: Align guidance with Cursor rules and any Codex/Gemini rule files when present; avoid conflicts when editing prompts or workflows.
- **Structured prompts**: Use XML-style wrappers, for example:

  ```xml
  <code_editing_rules>
    <guiding_principles>
      - audio transport stays AudioSocket-first with file fallback
    </guiding_principles>
    <tool_budget max_calls="5"/>
  </code_editing_rules>
  ```

- **Reasoning effort**: Reserve `high` for milestone-level changes (streaming transport, pipeline orchestration); choose medium or low for incremental updates.
- **Tone calibration**: Keep language collaborative; avoid all-caps or overly forceful mandates that encourage overcorrection.
- **Planning & self-reflection**: For zero-to-one work, embed a `<self_reflection>` block prompting the agent to outline a brief plan before execution.
- **Eagerness control**: Bound exploration with explicit tool budgets or `<persistence>` directives, stating when to assume reasonable defaults versus re-checking.

 When Codex/CLI (`Agents.md`) or Gemini rule files exist, keep their development guidance conceptually aligned with this file and `.cursor/rules/asterisk_ai_voice_agent.mdc`; avoid divergent architectural instructions.

## Provider/Pipeline Resolution Precedence

- Provider precedence: `AI_PROVIDER` (Asterisk channel var) > `contexts.*.provider` > `default_provider`.
- Per-call overrides read from: `AI_PROVIDER`, `AI_AUDIO_PROFILE`, `AI_CONTEXT`.

## MCP Tools

- Active servers: `linear-mcp-server`, `mcp-playwright`, `memory`, `perplexity-ask`, `sequential-thinking`.
- Prefer MCP resources over web search; discover via `list_mcp_resources` / `list_mcp_resource_templates`, read via `read_mcp_resource`.
- Use `mcp-playwright` for dashboard UI validations (Grafana/Prometheus) as part of regressions.

## Deploy Details (Server)

- Host: `root@mypbx.server.com`
- Repo: `/root/Asterisk-AI-Voice-Agent` (branch `develop`)
- Deploy: `git pull && docker-compose up -d --build --force-recreate ai-engine local-ai-server`
- Config: `.env` on server with `ASTERISK_HOST`, ARI creds, provider API keys
- Dialplan: AudioSocket + Stasis contexts configured; `app_audiosocket.so` loaded

## Change Safety & Review

- Review and research thoroughly before fixes; validate against golden baselines in `docs/baselines/golden/` and capture RCA with `scripts/rca_collect.sh`.