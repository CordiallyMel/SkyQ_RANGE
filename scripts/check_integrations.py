"""Print the optional integration readiness report.

No network call is made and no credential is printed.  A missing package or
unset environment variable is reported as ``not ready``, not as a crash.
"""

from __future__ import annotations

import argparse
import json

from integrations import doctor


def main() -> int:
    parser = argparse.ArgumentParser(description="SkyQ RANGE integration doctor")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()
    rows = [row.as_dict() for row in doctor()]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        marker = "READY" if row["configured"] and row["reachable"] else "not ready"
        print(f"{row['name']:<10} {marker:<10} {row['detail']}")
        print(f"{'':<10} {'role':<10} {row['role']}")
        if row["env_vars"]:
            print(f"{'':<10} {'env':<10} {', '.join(row['env_vars'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
