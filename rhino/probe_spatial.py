"""Native Rhino API probe; writes all six fixture diagnostics, even on failure.

Run before quality_check.py in the same Rhino CPython process. No document is
opened, saved, or modified by this probe.
"""
import json
import os
import sys
import traceback

import Rhino

ROOT = os.environ.get("FACADE_BIM_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.join(ROOT, "rhino")
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import spatial_quality


def main():
    records = []
    report = {"ok": False, "rhino": str(Rhino.RhinoApp.Version), "diagnostics": records}
    try:
        report.update(spatial_quality.run_fixtures(records))
    except Exception:
        report["error"] = traceback.format_exc()
    destination = os.path.join(ROOT, "model", "quality", "spatial_probe.json")
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with open(destination, "w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    return report


if __name__ == "__main__":
    main()
