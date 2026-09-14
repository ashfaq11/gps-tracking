"""
Write the OpenAPI spec to a file without starting the server.

    python3 tools/export_openapi.py                  # -> openapi.json
    python3 tools/export_openapi.py --format yaml    # needs PyYAML

Useful for client codegen and for diffing the contract in review, so an
accidental breaking change shows up in the pull request.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.config import ApiConfig   
from api.main import create_app  


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the OpenAPI schema")
    parser.add_argument("-o", "--out", default="openapi.json")
    parser.add_argument("--format", choices=("json", "yaml"), default="json")
    args = parser.parse_args()

    # backend=memory so no database connection is needed just to read routes.
    spec = create_app(ApiConfig(backend="memory", ingest_api_key="documented")).openapi()

    out = Path(args.out)
    if args.format == "yaml":
        try:
            import yaml
        except ImportError:
            raise SystemExit("PyYAML is required for --format yaml: pip install pyyaml")
        out.write_text(yaml.safe_dump(spec, sort_keys=False))
    else:
        out.write_text(json.dumps(spec, indent=2) + "\n")

    paths = sum(len(ops) for ops in spec["paths"].values())
    print(f"Wrote {out} ({paths} operations, OpenAPI {spec['openapi']})")


if __name__ == "__main__":
    main()
