import asyncio
import os
import structlog
from src.engine import main

logger = structlog.get_logger()

if __name__ == "__main__":
    try:
        import uvloop
        uvloop.install()
    except ImportError:
        pass
    loop = asyncio.new_event_loop()
    logger.info("Event loop type", loop_class=loop.__class__.__name__)
    loop.close()
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
