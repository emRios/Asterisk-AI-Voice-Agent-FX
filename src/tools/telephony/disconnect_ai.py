"""
Disconnect AI Tool - Detach AI media/provider from an active call bridge.

Keeps caller and any human participant connected while removing AI from audio.
"""

from typing import Dict, Any
import structlog

from src.tools.base import Tool, ToolDefinition, ToolParameter, ToolCategory
from src.tools.context import ToolExecutionContext

logger = structlog.get_logger(__name__)


class DisconnectAITool(Tool):
    """
    Disconnect AI from the current call without hanging up the caller.

    Use when:
    - A human participant has joined and should continue without AI
    - User asks to continue with a human only
    - Supervisor wants AI monitoring/generation stopped mid-call
    """

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="disconnect_ai",
            description=(
                "Disconnect the AI agent from the current call while keeping the call active. "
                "Use this when a human participant is connected and AI should leave the conversation."
            ),
            category=ToolCategory.TELEPHONY,
            requires_channel=True,
            max_execution_time=10,
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
                )
            ],
        )

    async def execute(
        self,
        parameters: Dict[str, Any],
        context: ToolExecutionContext,
    ) -> Dict[str, Any]:
        cleanup_media_channels = parameters.get("cleanup_media_channels", True)
        if isinstance(cleanup_media_channels, str):
            cleanup_media_channels = cleanup_media_channels.strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        else:
            cleanup_media_channels = bool(cleanup_media_channels)

        logger.info(
            "Disconnect AI requested",
            call_id=context.call_id,
            cleanup_media_channels=cleanup_media_channels,
        )

        engine = getattr(getattr(context, "ari_client", None), "engine", None)
        if not engine or not hasattr(engine, "disconnect_ai_from_call"):
            return {
                "status": "error",
                "message": "AI disconnect is not available in this runtime.",
                "ai_disconnected": False,
                "ai_should_speak": True,
            }

        try:
            result = await engine.disconnect_ai_from_call(
                context.call_id,
                cleanup_media_channels=cleanup_media_channels,
            )
            status = result.get("status", "error")
            return {
                "status": status,
                "message": result.get("message", "AI disconnected from call."),
                "ai_disconnected": bool(result.get("ai_disconnected", status == "success")),
                "detached_channels": result.get("detached_channels", []),
                "failed_detach_channels": result.get("failed_detach_channels", []),
                "provider_stopped": bool(result.get("provider_stopped", False)),
                "ai_should_speak": False if status == "success" else True,
            }
        except Exception as exc:
            logger.error("Disconnect AI tool failed", call_id=context.call_id, error=str(exc), exc_info=True)
            return {
                "status": "error",
                "message": "I could not disconnect AI from this call.",
                "error": str(exc),
                "ai_disconnected": False,
                "ai_should_speak": True,
            }
