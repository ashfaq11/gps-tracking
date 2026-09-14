"""
Backwards-compatible entry point.

The gateway now lives in the `gps_gateway` package; this shim keeps the
documented `python gateway.py` command working. Prefer:

    python -m gps_gateway
"""

from gps_gateway.server import main

if __name__ == "__main__":
    main()
