from __future__ import annotations

import atexit
import os
from pathlib import Path
import shutil
import sys
import tempfile


_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_TEST_RUNTIME = Path(tempfile.mkdtemp(prefix="market-ai-pytest-"))
(_TEST_RUNTIME / "db").mkdir(parents=True, exist_ok=True)
os.environ["MARKET_AI_HOME"] = str(_TEST_RUNTIME)
os.environ["MARKET_AI_DB_PATH"] = str(_TEST_RUNTIME / "db" / "market_signal.db")
os.environ["MARKET_AI_TEST_ISOLATED"] = "1"


@atexit.register
def _cleanup_test_runtime() -> None:
    shutil.rmtree(_TEST_RUNTIME, ignore_errors=True)
