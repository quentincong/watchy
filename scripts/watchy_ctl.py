#!/usr/bin/env python
"""Watchy 2.0 operator controls — see `python scripts/watchy_ctl.py --help`.

Run on the VPS with the daemon's interpreter:
  /home/watchy/.pyenv/versions/3.11.9/envs/trading/bin/python scripts/watchy_ctl.py status
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from watchy.ctl import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
