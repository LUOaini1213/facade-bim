"""Validate the checked-in IDS 1.0 delivery requirements; write portable JSON/HTML reports."""
import argparse
import hashlib
import html
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
import ifcopenshell
from ifctester import ids, reporter


def profile():
    return json.loads((ROOT / "quality" / "profile.json").read_text(encoding="utf-8"))


def restriction(rule):
    if "min_exclusive" in rule:
        return ids.Restriction({"minExclusive": rule["min_exclusive"]}, base="double")
    if "pattern" in rule:
        return ids.Restriction({"pattern": rule["pattern"]})
    if "values" in rule:
        return ids.Restriction({"enumeration": rule["values"]})
    return rule.get("value")


def make_rules():
    config = profile()
    document = ids.Ids(title=config["title"], description=config["description"])
    for entry in config["specifications"]:
        spec = ids.Specification(name=entry["name"], identifier=entry["id"],
                                 minOccurs=1, maxOccurs="unbounded", ifcVersion=config["ifc_versions"])
        entity = entry["entity"]
        spec.applicability.append(ids.Entity(name=entity if isinstance(entity, str) else ids.Restriction({"enumeration": entity})))
        for prop in entry.get("applicability_properties", []):
            spec.applicability.append(ids.Property(propertySet=prop["pset"], baseName=prop["name"], value=restriction(prop)))
        for attr in entry.get("applicability_attributes", []):
            spec.applicability.append(ids.Attribute(name=attr["name"], value=restriction(attr)))
        for attr in entry.get("attributes", []):
            spec.requirements.append(ids.Attribute(name=attr["name"], value=restriction(attr), cardinality="required"))
        for prop in entry.get("properties", []):
            spec.requirements.append(ids.Property(propertySet=prop["pset"], baseName=prop["name"],
                                                  value=restriction(prop), dataType=prop.get("datatype"), cardinality="required"))
        if entry.get("material"):
            spec.requirements.append(ids.Material(cardinality="required"))
        document.specifications.append(spec)
    return document


def validate_model(model=None, write_reports=False):
    config = profile()
    rules_path = ROOT / "quality" / "delivery.ids"
    rules = ids.open(str(rules_path), validate=True)
    model_path = ROOT / "model" / config["model"]
    source_model = model is None
    model = model if model is not None else ifcopenshell.open(str(model_path))
    rules.validate(model)
    full = reporter.Json(rules).report()
    result = {key: full[key] for key in ("status", "total_specifications", "total_specifications_pass",
                                        "total_specifications_fail", "total_checks", "total_checks_pass", "total_checks_fail")}
    result.update(title=config["title"], model="model/" + config["model"], schema=model.schema_identifier,
                  ids="quality/delivery.ids", ids_sha256=hashlib.sha256(rules_path.read_bytes()).hexdigest(),
                  ifctester_version="0.8.5", specifications=[])
    if source_model:
        result["model_sha256"] = hashlib.sha256(model_path.read_bytes()).hexdigest()
    for spec in full["specifications"]:
        compact = {key: spec[key] for key in ("name", "status", "is_ifc_version", "total_applicable",
                                              "total_applicable_fail", "total_checks", "total_checks_fail")}
        compact["requirements"] = [{key: requirement[key] for key in
                                     ("label", "description", "status", "total_applicable", "total_fail", "failed_entities")}
                                    for requirement in spec["requirements"]]
        result["specifications"].append(compact)
    if write_reports:
        out = ROOT / "model" / "quality"
        out.mkdir(parents=True, exist_ok=True)
        (out / "ids_report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        rows = []
        for spec in result["specifications"]:
            for req in spec["requirements"]:
                rows.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%d</td><td>%d</td></tr>" % (
                    html.escape(spec["name"]), html.escape(req["label"]), "PASS" if req["status"] else "FAIL",
                    req["total_applicable"], req["total_fail"]))
        failures = "\n".join(html.escape(json.dumps(req["failed_entities"], ensure_ascii=False, indent=2, default=str))
                             for spec in result["specifications"] for req in spec["requirements"] if req["failed_entities"])
        document = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>IDS delivery report</title>'
                    '<style>body{font:15px system-ui;margin:32px;color:#203045}table{border-collapse:collapse;width:100%%}'
                    'td,th{border:1px solid #ccd6df;padding:8px;text-align:left}th{background:#eaf1f5}pre{white-space:pre-wrap}</style>'
                    '<h1>%s</h1><p>%s · %s · %d checks, %d failures</p>'
                    '<table><tr><th>Specification</th><th>Requirement</th><th>Status</th><th>Entities</th><th>Failures</th></tr>%s</table>'
                    '<pre>%s</pre></html>') % (html.escape(config["title"]), html.escape(result["model"]),
                        "PASS" if result["status"] else "FAIL", result["total_checks"], result["total_checks_fail"], "".join(rows), failures)
        (out / "ids_report.html").write_text(document, encoding="utf-8")
    return result


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-rules", action="store_true", help="Regenerate delivery.ids from quality/profile.json")
    parser.add_argument("--no-write-reports", action="store_true", help="Validate without rewriting JSON/HTML reports")
    args = parser.parse_args()
    if args.write_rules:
        path = ROOT / "quality" / "delivery.ids"
        if not make_rules().to_xml(str(path)):
            sys.exit("IDS XML schema validation failed")
    result = validate_model(write_reports=not args.no_write_reports)
    print("%s IDS: %d specifications, %d checks, %d failures" % (
        "PASS" if result["status"] else "FAIL", result["total_specifications"], result["total_checks"], result["total_checks_fail"]))
    for spec in result["specifications"]:
        print("  %s %s (%d applicable)" % ("PASS" if spec["status"] else "FAIL", spec["name"], spec["total_applicable"]))
    if not result["status"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
