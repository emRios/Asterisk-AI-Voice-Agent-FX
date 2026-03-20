"""
Conference participant tool - prepare/register a third human leg for the active bridge.

The marker remains responsible for originating the human leg. This tool only
validates the call state, stores control-plane state in the session, and returns
the originate contract the marker should use.
"""

from typing import Any, Dict, Optional
import time
import structlog

from src.tools.base import Tool, ToolCategory, ToolDefinition, ToolParameter
from src.tools.context import ToolExecutionContext

logger = structlog.get_logger(__name__)


class ConferenceParticipantTool(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="conference_participant",
            description=(
                "Prepare or register a configured human participant for the current call while the AI stays connected. "
                "Use this when an external marker will originate the PJSIP leg and the engine must later bridge it."
            ),
            category=ToolCategory.TELEPHONY,
            requires_channel=True,
            max_execution_time=30,
            parameters=[
                ToolParameter(
                    name="destination",
                    type="string",
                    description=(
                        "Configured extension destination or direct extension number to add. "
                        "Examples: 'support_agent', 'sales_agent', '6000'."
                    ),
                    required=True,
                ),
                ToolParameter(
                    name="mode",
                    type="string",
                    description="Participant audio mode: 'talk' for two-way audio or 'listen' for listen-only.",
                    enum=["talk", "listen"],
                    default="talk",
                ),
                ToolParameter(
                    name="channel_id",
                    type="string",
                    description="Optional ARI channel id if the marker already originated the participant leg and wants to register it for timeout/cancel tracking.",
                    required=False,
                ),
            ],
        )

    async def execute(self, parameters: Dict[str, Any], context: ToolExecutionContext) -> Dict[str, Any]:
        destination = parameters.get("destination") or parameters.get("target")
        mode = str(parameters.get("mode") or "talk").strip().lower()
        channel_id = str(parameters.get("channel_id") or parameters.get("participant_channel_id") or "").strip() or None

        if not destination:
            return {"status": "failed", "message": "Missing destination"}
        if mode not in {"talk", "listen"}:
            return {"status": "failed", "message": "Invalid mode. Use 'talk' or 'listen'."}

        cfg = context.get_config_value("tools.conference_participant") or {}
        if isinstance(cfg, dict) and cfg.get("enabled") is False:
            return {"status": "failed", "message": "Conference participant service is disabled"}

        session = await context.get_session()
        existing_action = getattr(session, "current_action", None) or {}
        if existing_action and existing_action.get("type") not in {None, "conference-participant"}:
            return {
                "status": "failed",
                "message": "Another call action is already in progress. Please wait until it completes.",
            }
        if existing_action and existing_action.get("type") == "conference-participant" and bool(existing_action.get("answered", False)):
            return {
                "status": "failed",
                "message": "The invited participant is already connected.",
            }
        if not session.bridge_id:
            return {"status": "failed", "message": "Active bridge not found for this call"}

        resolved = self._resolve_destination(str(destination).strip(), context)
        if not resolved:
            return {"status": "failed", "message": f"Unknown destination: {destination}"}

        extension = str(resolved["extension"])
        description = str(resolved["description"])
        dial_endpoint = str(resolved["dial_endpoint"])
        destination_key = resolved.get("destination_key")
        dial_timeout_sec = int(resolved.get("dial_timeout_seconds") or cfg.get("dial_timeout_seconds") or 30)
        caller_id = self._build_ai_caller_id(context)
        app = str(context.get_config_value("asterisk.app_name", "asterisk-ai-voice-agent") or "asterisk-ai-voice-agent")

        previous_channel_id = str(existing_action.get("channel_id") or "").strip() or None

        session.current_action = {
            "type": "conference-participant",
            "destination_key": destination_key,
            "target": extension,
            "target_name": description,
            "dial_endpoint": dial_endpoint,
            "dial_timeout_seconds": dial_timeout_sec,
            "mode": mode,
            "started_at": float(existing_action.get("started_at") or time.time()),
            "channel_id": channel_id or previous_channel_id,
            "answered": bool(existing_action.get("answered", False)),
        }
        await context.session_store.upsert_call(session)

        effective_channel_id = session.current_action.get("channel_id")
        if effective_channel_id and effective_channel_id != previous_channel_id:
            try:
                engine = getattr(context.ari_client, "engine", None)
                if engine and hasattr(engine, "start_conference_participant_timeout_guard"):
                    engine.start_conference_participant_timeout_guard(
                        session.call_id,
                        effective_channel_id,
                        timeout_sec=dial_timeout_sec,
                    )
            except Exception:
                logger.debug("Failed to register conference participant timeout guard", call_id=session.call_id, exc_info=True)

        logger.info(
            "Conference participant prepared",
            call_id=session.call_id,
            destination_key=destination_key,
            extension=extension,
            dial_endpoint=dial_endpoint,
            participant_channel_id=effective_channel_id,
            mode=mode,
        )

        return {
            "status": "success",
            "message": f"Conference participant prepared for {description}.",
            "destination": destination_key or extension,
            "target": extension,
            "type": "conference_participant",
            "mode": mode,
            "bridge_id": session.bridge_id,
            "channel_id": effective_channel_id,
            "originate": {
                "endpoint": dial_endpoint,
                "callerId": caller_id,
                "timeout": dial_timeout_sec,
                "app": app,
                "appArgs": self._build_app_args(session.call_id, extension, mode),
                "variables": {
                    "AGENT_ACTION": "conference-participant",
                    "AGENT_CALL_ID": session.call_id,
                    "AGENT_BRIDGE_ID": session.bridge_id,
                    "AGENT_TARGET": extension,
                    "AAVA_PARTICIPANT_MODE": mode,
                    "AAVA_DESTINATION_KEY": destination_key or "",
                },
            },
        }

    def _resolve_destination(self, destination: str, context: ToolExecutionContext) -> Optional[Dict[str, Any]]:
        transfer_cfg = context.get_config_value("tools.transfer") or {}
        destinations = (transfer_cfg.get("destinations") or {}) if isinstance(transfer_cfg, dict) else {}

        dest_key = self._resolve_destination_key(destination, destinations)
        if dest_key:
            dest_cfg = destinations.get(dest_key) or {}
            if dest_cfg.get("type") != "extension":
                return None
            extension = str(dest_cfg.get("target") or "").strip()
            if not extension:
                return None
            return {
                "destination_key": dest_key,
                "extension": extension,
                "description": str(dest_cfg.get("description") or dest_key),
                "dial_endpoint": self._resolve_dial_endpoint(extension, dest_cfg, transfer_cfg, context),
                "dial_timeout_seconds": int(dest_cfg.get("timeout") or 30),
            }

        legacy = self._resolve_legacy_internal_extension(destination, context)
        if legacy:
            return legacy
        return None

    def _resolve_destination_key(self, user_value: str, destinations: Dict[str, Any]) -> Optional[str]:
        raw = str(user_value or "").strip()
        if not raw:
            return None
        raw_lower = raw.lower()

        if raw in destinations:
            return raw

        for key in destinations.keys():
            if str(key).lower() == raw_lower:
                return str(key)

        extension_candidates = {
            str(key): cfg
            for key, cfg in (destinations or {}).items()
            if isinstance(cfg, dict) and cfg.get("type") == "extension"
        }

        for key, cfg in extension_candidates.items():
            target = str(cfg.get("target") or "").strip()
            if target and (target == raw or target.lower() == raw_lower):
                return key

        matches = []
        for key, cfg in extension_candidates.items():
            key_lower = key.lower()
            desc_lower = str(cfg.get("description") or "").lower()
            if raw_lower in key_lower or raw_lower in desc_lower:
                matches.append(key)

        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            preferred = [key for key in matches if key.lower().endswith("_agent")]
            if len(preferred) == 1:
                return preferred[0]
        return None

    def _resolve_legacy_internal_extension(self, destination: str, context: ToolExecutionContext) -> Optional[Dict[str, Any]]:
        extensions = context.get_config_value("tools.extensions.internal") or {}
        if not isinstance(extensions, dict) or not extensions:
            return None

        raw = str(destination or "").strip()
        raw_lower = raw.lower()

        if raw in extensions:
            return self._legacy_result(raw, extensions[raw], context)

        for ext, cfg in extensions.items():
            if str(ext).lower() == raw_lower:
                return self._legacy_result(str(ext), cfg, context)

            name = str((cfg or {}).get("name") or "").strip().lower()
            if name and name == raw_lower:
                return self._legacy_result(str(ext), cfg, context)

            aliases = [str(alias).lower() for alias in ((cfg or {}).get("aliases") or [])]
            if raw_lower in aliases:
                return self._legacy_result(str(ext), cfg, context)

        return None

    def _legacy_result(self, extension: str, cfg: Dict[str, Any], context: ToolExecutionContext) -> Dict[str, Any]:
        transfer_cfg = context.get_config_value("tools.transfer") or {}
        return {
            "destination_key": None,
            "extension": extension,
            "description": str((cfg or {}).get("name") or extension),
            "dial_endpoint": str((cfg or {}).get("dial_string") or self._resolve_dial_endpoint(extension, cfg or {}, transfer_cfg, context)),
            "dial_timeout_seconds": int((cfg or {}).get("timeout") or 30),
        }

    def _resolve_dial_endpoint(
        self,
        extension: str,
        dest_cfg: Dict[str, Any],
        transfer_cfg: Dict[str, Any],
        context: ToolExecutionContext,
    ) -> str:
        if isinstance(dest_cfg, dict):
            dial_string = dest_cfg.get("dial_string")
            if dial_string:
                return str(dial_string)

        ext_cfg = context.get_config_value(f"tools.extensions.internal.{extension}") or {}
        if isinstance(ext_cfg, dict) and ext_cfg.get("dial_string"):
            return str(ext_cfg.get("dial_string"))

        technology = transfer_cfg.get("technology") if isinstance(transfer_cfg, dict) else None
        technology = str(technology or "PJSIP")
        return f"{technology}/{extension}"

    def _build_app_args(self, call_id: str, extension: str, mode: str) -> str:
        return f"conference-participant,{call_id},{extension},mode={mode}"

    def _build_ai_caller_id(self, context: ToolExecutionContext) -> str:
        ai_name = str(context.get_config_value("tools.ai_identity.name", "AI Agent") or "AI Agent")
        ai_number = str(context.get_config_value("tools.ai_identity.number", "6789") or "6789")
        return f"\"{ai_name}\" <{ai_number}>"
