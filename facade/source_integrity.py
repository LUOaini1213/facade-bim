"""Bind a source model to its inputs; permit only a coping end-joint repair.

This record describes demonstration inputs, not measured engineering values.
The repair also validates the actual panel dimensions, transforms and UserText;
the record alone never certifies geometry or clearance.
"""
from datetime import date
import hashlib
import json
import math
from pathlib import Path

from . import config as C
from .model import quantities, zones_for
from .pipeline import compute, panel_rows

ALLOWED_COPING_ATTRIBUTES = ("coping_alu_kg", "weight_kg")


def _normalise(value):
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _normalise(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (tuple, list)):
        return [_normalise(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("source configuration contains a non-finite value")
    return value


def configuration():
    return {name: _normalise(getattr(C, name)) for name in sorted(vars(C))
            if name.isupper() and name != "COPING_END_JOINT"}


def source_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_source_record(source_path):
    """Called after a real full build, or after a checked end-joint replacement."""
    source_path = Path(source_path)
    record = {"schema": 1, "source_sha256": source_digest(source_path),
              "configuration": configuration(), "allowed_repair_parameter": "COPING_END_JOINT",
              "basis": "fictional demonstration inputs; not measured or approved fabrication data"}
    destination = source_path.parent / "source_config.json"
    staged = destination.with_suffix(".json.tmp")
    staged.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    staged.replace(destination)
    return record


def read_source_record(source_path, compare_configuration=True):
    source_path = Path(source_path)
    path = source_path.parent / "source_config.json"
    if not path.is_file():
        raise ValueError("missing source_config.json; a full build must establish the source input baseline")
    record = json.loads(path.read_text(encoding="utf-8"))
    if (record.get("schema") != 1 or record.get("source_sha256") != source_digest(source_path)
            or record.get("allowed_repair_parameter") != "COPING_END_JOINT"):
        raise ValueError("source configuration baseline does not identify the current .3dm")
    if compare_configuration:
        current, original = configuration(), record.get("configuration", {})
        changed = sorted(key for key in set(current).union(original) if current.get(key) != original.get(key))
        if changed:
            raise ValueError("source/configuration mismatch: " + ", ".join(changed)
                             + "; only COPING_END_JOINT may be repaired; rebuild other inputs explicitly")
    return record


def coping_update_rows(records):
    """Check every source panel before deriving U5 updates from its own w/h.

    Each record is (UserText dict, 4x4 transform). Native callers invoke this
    before deleting objects, backups, candidate saves or source replacement.
    """
    expected = {row["pid"]: row for row in panel_rows(compute())}
    actual, updates = set(), {}
    for values, transform in records:
        pid = values.get("pid")
        if pid not in expected or pid in actual:
            raise ValueError("source panel identity set differs from configuration: " + str(pid))
        actual.add(pid)
        allowed = ALLOWED_COPING_ATTRIBUTES if values.get("type") == "U5" else ()
        target = expected[pid]
        for key in set(values).union(target):
            if key not in allowed and values.get(key) != str(target.get(key)):
                raise ValueError("source/configuration mismatch: %s %s" % (pid, key))
        angle = math.radians(float(target["rot"]))
        ca, sa = round(math.cos(angle)), round(math.sin(angle))
        exact = [[ca, -sa, 0, float(target["origin_x"])],
                 [sa, ca, 0, float(target["origin_y"])],
                 [0, 0, 1, float(target["origin_z"])], [0, 0, 0, 1]]
        if any(not math.isfinite(transform[i][j]) or abs(transform[i][j] - exact[i][j]) > 1e-7
               for i in range(4) for j in range(4)):
            raise ValueError("source/configuration transform mismatch: " + pid)
        if values.get("type") == "U5":
            w, h = float(values["w_mm"]), float(values["h_mm"])
            qty, mass = quantities("U5", w, h, zones_for("U5", h))
            updates[pid] = {"coping_alu_kg": "%.3f" % qty["coping_alu_kg"], "weight_kg": "%.1f" % round(mass, 1)}
    if actual != set(expected):
        raise ValueError("source panel identity set differs from configuration")
    return updates
