"""
Handoff to Human Tool - Atomic promotion of human participant + AI disconnect.

Promotes the human from listen to talk, then disconnects the AI from the call.
If promotion fails, the AI remains connected.
"""

from typing import Dict, Any
import structlog

from src.tools.base import Tool, ToolDefinition, ToolParameter, ToolCategory
from src.tools.context import ToolExecutionContext

logger = structlog.get_logger(__name__)


class HandoffToHumanTool(Tool):
    """
    Perform an atomic handoff from AI to human participant.

    Use when:
    - A human participant is already connected in listen mode
    - The AI has reached the final state of the conversation
    - The call should continue human-to-human without AI
    """

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="handoff_to_human",
            description=(
                "Hand off the call to the connected human participant. "
                "Promotes the human from listen to talk mode, then disconnects the AI. "
                "Use this when the AI conversation is complete and a human should take over."
            ),
            category=ToolCategory.TELEPHONY,
            requires_channel=True,
            max_execution_time=15,
            parameters=[
                ToolParameter(
                    name="cleanup_media_channels",
                    type="boolean",
                    description=(
                        "If true, hang up detached AI media channels after removing them from bridge. "
                        "Recommended true."
                    ),
                    required=False,
                    default=True,
                ),
            ],
        )

    async def execute(
        self,
        parameters: Dict[str, Any],
        context: ToolExecutionContext,
    ) -> Dict[str, Any]:
        cleanup_media_channels = parameters.get("cleanup_media_channels", True)
        if isinstance(cleanup_media_channels, str):
            cleanup_media_channels = cleanup_media_channels.strip().lower() in {"1", "true", "yes", "on"}
        else:
            cleanup_media_channels = bool(cleanup_media_channels)

        logger.info(
            "Handoff to human requested",
            call_id=context.call_id,
            cleanup_media_channels=cleanup_media_channels,
        )

        engine = getattr(getattr(context, "ari_client", None), "engine", None)
        if not engine or not hasattr(engine, "handoff_to_human"):
            return {
                "status": "error",
                "message": "Handoff to human is not available in this runtime.",
                "human_promoted": False,
                "ai_disconnected": False,
                "ai_should_speak": True,
            }

        try:
            result = await engine.handoff_to_human(
                context.call_id,
                cleanup_media_channels=cleanup_media_channels,
            )
            status = result.get("status", "error")
            return {
                "status": status,
                "message": result.get("message", "Handoff completed."),
                "human_promoted": bool(result.get("human_promoted", False)),
                "ai_disconnected": bool(result.get("ai_disconnected", False)),
                "human_channel_id": result.get("human_channel_id"),
                "detached_channels": result.get("detached_channels", []),
                "provider_stopped": bool(result.get("provider_stopped", False)),
                "ai_should_speak": False if status in ("success", "partial") else True,
            }
        except Exception as exc:
            logger.error("Handoff to human tool failed", call_id=context.call_id, error=str(exc), exc_info=True)
            return {
                "status": "error",
                "message": "I could not complete the handoff to the human participant.",
                "error": str(exc),
                "human_promoted": False,
                "ai_disconnected": False,
                "ai_should_speak": True,
            }
