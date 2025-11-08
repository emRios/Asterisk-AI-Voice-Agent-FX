"""
Hangup Call Tool - End the current call.

Allows full AI agents to end calls when appropriate (e.g., after goodbye).
"""

from typing import Dict, Any
from src.tools.base import Tool, ToolDefinition, ToolParameter, ToolCategory
from src.tools.context import ToolExecutionContext
import structlog

logger = structlog.get_logger(__name__)


class HangupCallTool(Tool):
    """
    End the current call.
    
    Use when:
    - Caller says goodbye/thank you/that's all
    - Call purpose is complete
    - Caller explicitly asks to end the call
    
    Only available to full agents (not partial/assistant agents).
    """
    
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="hangup_call",
            description="End the current call. Use when the caller says goodbye, thank you, or indicates they're done.",
            category=ToolCategory.TELEPHONY,
            requires_channel=True,
            max_execution_time=5,
            parameters=[
                ToolParameter(
                    name="farewell_message",
                    type="string",
                    description="Optional farewell message to speak before hanging up",
                    required=False
                )
            ]
        )
    
    async def execute(
        self,
        parameters: Dict[str, Any],
        context: ToolExecutionContext
    ) -> Dict[str, Any]:
        """
        End the call.
        
        Args:
            parameters: {farewell_message: Optional[str]}
            context: Tool execution context
        
        Returns:
            {
                status: "success" | "error",
                message: "Farewell message",
                will_hangup: true
            }
        """
        farewell = parameters.get('farewell_message')
        
        if not farewell:
            # Use default from config or hardcoded
            farewell = context.get_config_value(
                'tools.hangup_call.farewell_message',
                "Thank you for calling. Goodbye!"
            )
        
        logger.info("📞 Hangup requested", 
                   call_id=context.call_id,
                   farewell=farewell)
        
        try:
            session = await context.get_session()
            if not session:
                return {
                    "status": "error",
                    "message": "Session not found"
                }
            
            # Mark session for cleanup after farewell TTS
            session.cleanup_after_tts = True
            await context.session_store.upsert_call(session)
            
            logger.info("✅ Call will hangup after farewell", call_id=context.call_id)
            
            # Return farewell message - AI will speak it, then engine will hangup
            return {
                "status": "success",
                "message": farewell,
                "will_hangup": True
            }
            
        except Exception as e:
            logger.error(f"Error preparing hangup: {e}", exc_info=True)
            return {
                "status": "error",
                "message": "Goodbye!",
                "will_hangup": True,
                "error": str(e)
            }
