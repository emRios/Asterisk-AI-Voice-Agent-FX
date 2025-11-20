---
description: Gemini rules for the Asterisk AI Voice Agent repository
---

# Asterisk AI Voice Agent – Gemini Rules

## AVA as Project Manager

- Treat `AVA.mdc` as the primary definition of the project manager persona (AVA).
- When users ask what to build, how to onboard, or how to extend the system (providers/tools/pipelines), defer to the guidance and playbooks in `AVA.mdc`.
- Use this file and the IDE rule files for technical constraints and context.

## Canonical Project Context

Gemini should consider the following files as key context:

- Project overview and onboarding:
  - `README.md`
  - `docs/README.md`
  - `docs/contributing/README.md`
  - `docs/contributing/quickstart.md`
- Architecture and roadmap:
  - `docs/Architecture.md`
  - `docs/ROADMAP.md`
  - `docs/baselines/golden/`
  - `docs/regressions/`
  - `docs/contributing/milestones/`
- Operations and CLI:
  - `docs/INSTALLATION.md`
  - `docs/FreePBX-Integration-Guide.md`
  - `docs/Configuration-Reference.md`
  - `docs/MONITORING_GUIDE.md`
  - `cli/README.md`
- Tool calling and integrations:
  - `docs/TOOL_CALLING_GUIDE.md`

## Respect IDE Guardrails

Gemini should align with the existing Cursor/Windsurf rule files:

- `.cursor/rules/asterisk_ai_voice_agent.mdc`
- `.cursor/rules/architecture-overview.mdc`
- `.cursor/rules/project-roadmap.mdc`
- `.windsurf/rules/asterisk_ai_voice_agent.md`
- `.windsurf/rules/architecture-overview.md`
- `.windsurf/rules/project-roadmap.md`
- `.windsurf/rules/document-creation-rule.md`

These define:

- The expected architecture (two-container design, AudioSocket-first, transport orchestrators).
- How to interact with `SessionStore`, `PlaybackManager`, `ConversationCoordinator`.
- Streaming defaults, gating, and metrics/health expectations.
- How to maintain `docs/Architecture.md` and `docs/ROADMAP.md` without losing historical context.

Gemini must not contradict these rules when proposing code or documentation changes.

## MCP and External Tools

- If MCP servers (e.g., Linear, Perplexity) are available:
  - Prefer them over generic web search to fetch structured project context (issues, docs).
  - Use Linear MCP to read/update AAVA issues when configured, but never handle API keys directly in prompts.
- Always fall back gracefully to local repository docs when MCP is not available.

## Typical Gemini Tasks

- Help users understand the project:
  - Summarize architecture or roadmap based on the canonical docs.
  - Explain how golden baselines and regressions are organized.
- Support contributors:
  - Direct new developers to `docs/DEVELOPER_ONBOARDING.md` and suggest using an AI IDE with AVA.
  - When implementing features, base plans on `AVA.mdc` plus the appropriate AAVA issue/spec.
- Assist with debugging:
  - Suggest using `agent troubleshoot`, `scripts/rca_collect.sh`, and relevant docs.
  - Interpret outputs in the context of streaming/gating/transport rules.

Keep answers concise, grounded in the repository, and let AVA drive the overall project workflow.
