"""重算全部数据产物，写到 data/。不需要 Rhino。

    python scripts/build_data.py            # 重写 data/
    python scripts/build_data.py --check    # 只比对：重算结果与已提交的 data/ 逐字节一致，否则退出 1
"""
import csv, io, json, os, sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from facade import config as C, site          # noqa: E402
from facade.pipeline import (all_checks, compute, panel_rows,  # noqa: E402
                             sensitivity_rows, takeoff_rows)

DATA = os.path.join(ROOT, "data")


def csv_text(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def outputs():
    r = compute()
    sm = r["summary"]
    daily = Counter(d for _, d in r["install"].values())
    hours = Counter()
    for p in r["panels"]:
        hours[r["install"][p.pid][1]] += C.INSTALL_HOURS[p.ptype]
    files = {
        "panels.csv": csv_text(panel_rows(r)),
        "takeoff.csv": csv_text(takeoff_rows(r["panels"])),
        "stillages.csv": csv_text([{
            "stillage": s["stillage"], "type": s["type"], "n": s["n"], "panels": " ".join(s["panels"]),
            "first_install": s["first_install"].isoformat(), "last_install": s["last_install"].isoformat(),
            "due": s["due"].isoformat(), "delivered": s["delivered"].isoformat(), "truck": s["truck"]}
            for s in r["stillages"]]),
        "trucks.csv": csv_text([{"truck": t["truck"], "date": t["date"].isoformat(),
                                 "stillages": " ".join(t["stillages"])} for t in r["trucks"]]),
        "install_daily.csv": csv_text([{"date": d.isoformat(), "panels": daily[d], "hours": "%.1f" % hours[d]}
                                       for d in sorted(daily)]),
        "site_stock.csv": csv_text([{"date": d.isoformat(), "stillages_on_site": n} for d, n in r["stock"]]),
        "sensitivity.csv": csv_text(sensitivity_rows()),
    }
    checks = all_checks(r)
    summary = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in sm.items()}
    summary.update({"panels": len(r["panels"]), "laydown_slots": len(site.slots()),
                    "checks_passed": sum(c["pass"] for c in checks), "checks_total": len(checks)})
    files["checks.json"] = json.dumps(checks, ensure_ascii=False, indent=1) + "\n"
    files["summary.json"] = json.dumps(summary, ensure_ascii=False, indent=1) + "\n"
    return files


def main():
    files = outputs()
    if "--check" in sys.argv:
        bad = []
        for name, text in files.items():
            path = os.path.join(DATA, name)
            old = open(path, encoding="utf-8", newline="").read() if os.path.exists(path) else None
            if old != text:
                bad.append(name)
        if bad:
            print("MISMATCH: data/ 与重算结果不一致：" + ", ".join(bad))
            sys.exit(1)
        print("PASS data/ 的 %d 个文件与重算结果逐字节一致" % len(files))
        return
    os.makedirs(DATA, exist_ok=True)
    for name, text in files.items():
        with open(os.path.join(DATA, name), "w", encoding="utf-8", newline="") as f:
            f.write(text)
    print("写出 data/ 下 %d 个文件" % len(files))


if __name__ == "__main__":
    main()
