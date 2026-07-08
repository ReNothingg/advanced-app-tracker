import os
import sys

if sys.platform == "win32":
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app_tracker.app import main

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--guardian-helper":
        from app_tracker.security.guardian_helper import main as guardian_main

        sys.exit(guardian_main(sys.argv[2:]))
    main()
