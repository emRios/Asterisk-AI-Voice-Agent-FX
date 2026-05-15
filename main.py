import asyncio
import os
from src.engine import main

if __name__ == "__main__":
    try:
        import uvloop
        uvloop.install()
    except ImportError:
        pass
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
