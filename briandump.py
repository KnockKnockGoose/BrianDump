"""BrianDump entry point: `py briandump.py <command>` (launch | serve | doctor | config | ask | agenda | dump | mics | version).

A file in the repo root so a beginner has one thing to run, the Start Menu shortcut has one target and the Claude Code
plugin has one stable path (install_dir in config.json). It puts the repo on sys.path and hands over to briandump.cli.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from briandump.cli import main  # noqa: E402  (after the path tweak on purpose)

if __name__ == "__main__":
    sys.exit(main())
