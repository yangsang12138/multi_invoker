#!/usr/bin/env python3
"""Multi-Service Invoker 入口。

用法：
    python3 query.py platforms
    python3 query.py services
    python3 query.py query S_BE_AM_19 --env prod
    python3 query.py query S_BE_AM_19 --env both --csv result.csv --full
    python3 query.py serve
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from multi_invoker.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
