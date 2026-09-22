from __future__ import annotations

import os
from pathlib import Path
import sys

from dotenv import load_dotenv


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8001


def runtime_root() -> Path:
    """Return the external Market AI runtime root in source and frozen builds."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def prepare_runtime_environment() -> Path:
    """Pin mutable/runtime resources to the external market-ai directory."""
    root = runtime_root()

    # A frozen application may execute imported modules from PyInstaller's
    # internal bundle directory.  Keep all mutable files outside that bundle.
    os.environ.setdefault("MARKET_AI_HOME", str(root))
    os.environ.setdefault("MARKET_AI_DB_PATH", str(root / "db" / "market_signal.db"))

    # Load the user's local secrets/settings explicitly from the external root.
    # Existing process environment values win over .env values.
    load_dotenv(dotenv_path=root / ".env", override=False)

    # Preserve existing relative-path behavior for modules that still open local
    # project resources by a relative path.
    os.chdir(root)
    return root


def main() -> None:
    prepare_runtime_environment()

    # Import after runtime paths/.env are fixed.  This ordering is intentional
    # and is required for the future frozen executable.
    import uvicorn
    from app import app

    # Security boundary: the full FastAPI surface is local-only. Remote access
    # must go through Investment Local Suite :8002 GET-only proxy, never by
    # rebinding MarketAI itself through environment variables.
    uvicorn.run(
        app,
        host=DEFAULT_HOST,
        port=DEFAULT_PORT,
        log_level="info",
        access_log=True,
    )


if __name__ == "__main__":
    main()
