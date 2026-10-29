"""Run ptt.py hidden (no console window). The logon task "BrianDump PTT" starts this with pythonw; output goes to
<state_dir>/logs/ptt-<host>.log (one rotation at 5 MB). Any ptt.py arguments pass through: `pythonw ptt_service.pyw --key f8`.
"""
import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))       # the repo root, so `briandump` imports from any cwd

from briandump.config import CONF  # noqa: E402

LOG = CONF.logs / f"ptt-{CONF.host}.log"
try:
    CONF.logs.mkdir(parents=True, exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > 5_000_000:              # one rotation, no log ever grows without bound
        LOG.replace(LOG.with_suffix(".log.1"))
except OSError:
    pass
try:
    log = open(LOG, "a", encoding="utf-8", buffering=1)
except OSError:                                                      # an unwritable log must not stop the recorder
    log = open(os.devnull, "w", encoding="utf-8")
sys.stdout = sys.stderr = log
print(f"\n=== BrianDump PTT start {dt.datetime.now():%Y-%m-%d %H:%M:%S} args={sys.argv[1:]} ===", flush=True)

from briandump import ptt  # noqa: E402

sys.exit(ptt.main(sys.argv[1:]))
