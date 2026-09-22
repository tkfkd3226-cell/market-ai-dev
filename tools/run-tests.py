from __future__ import annotations

import importlib.metadata as metadata
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
TEST_REQUIREMENTS = ROOT / "requirements-test.txt"
_REQUIREMENT_NAME = re.compile(r"^([A-Za-z0-9_.-]+)")
_OPTIONAL_SOURCE_QA_DISTRIBUTIONS = {"yfinance"}


def _distribution_names(path: Path, seen: set[Path] | None = None) -> list[str]:
    seen = set() if seen is None else seen
    resolved = path.resolve()
    if resolved in seen:
        return []
    seen.add(resolved)

    names: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-r ", "--requirement ")):
            include = line.split(maxsplit=1)[1].strip()
            names.extend(_distribution_names(path.parent / include, seen))
            continue
        match = _REQUIREMENT_NAME.match(line)
        if match:
            names.append(match.group(1))
    return names


def _missing_distributions() -> list[str]:
    missing: list[str] = []
    for name in _distribution_names(TEST_REQUIREMENTS):
        try:
            metadata.version(name)
        except metadata.PackageNotFoundError:
            missing.append(name)
    return sorted(set(missing), key=str.lower)


def main() -> int:
    missing = _missing_distributions()
    optional_missing = [
        name for name in missing
        if name.lower() in _OPTIONAL_SOURCE_QA_DISTRIBUTIONS
    ]
    required_missing = [name for name in missing if name not in optional_missing]

    if required_missing:
        print("Market AI test dependencies are incomplete.", file=sys.stderr)
        print("Missing: " + ", ".join(required_missing), file=sys.stderr)
        print("Install them with:", file=sys.stderr)
        print(
            f'  "{sys.executable}" -m pip install -r "{TEST_REQUIREMENTS}"',
            file=sys.stderr,
        )
        return 2

    if optional_missing:
        print(
            "Market AI source QA: optional dependency unavailable; "
            "yfinance-dependent tests will be skipped (no install attempt).",
            flush=True,
        )

    command = [sys.executable, "-m", "pytest", *sys.argv[1:]]
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
