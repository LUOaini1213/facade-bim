"""Run the GUID-preserving U5 coping repair in native Rhino 8."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--end-joints", action="store_true", help="应用配置的压顶端缝并更新真实材料用量，保留全部GUID")
    args = parser.parse_args()
    rhino = Path(os.environ.get("RHINO_EXE", r"C:\Program Files\Rhino 8\System\Rhino.exe"))
    if not rhino.is_file():
        parser.error("找不到 Rhino 8；请设置 RHINO_EXE")
    script_name = "repair_coping_joints.py" if args.end_joints else "repair_roof_coping.py"
    log = ROOT / "model/quality" / ("coping_joint_repair.json" if args.end_joints else "roof_coping_repair.json")
    log.parent.mkdir(parents=True, exist_ok=True)
    if log.exists():
        log.unlink()
    with tempfile.TemporaryDirectory(prefix="facade_roof_") as folder:
        script = Path(folder) / script_name
        if not str(script).isascii() or " " in str(script):
            parser.error("请把 TMP/TEMP 设为不含空格的英文路径")
        shutil.copyfile(ROOT / "rhino" / script_name, script)
        env = dict(os.environ, FACADE_BIM_ROOT=str(ROOT))
        startup = None
        if os.name == "nt":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
        command = '"%s" /nosplash /notemplate /runscript="_-RunPythonScript %s _-Exit"' % (rhino, script)
        process = subprocess.Popen(command, env=env, startupinfo=startup)
        try:
            process.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            sys.exit("Rhino 压顶修补超时；仅终止本次进程")
    if not log.is_file():
        sys.exit("Rhino 未写修补报告")
    report = json.loads(log.read_text(encoding="utf-8"))
    if not report.get("ok"):
        sys.exit(report.get("error", "压顶修补失败"))
    if args.end_joints:
        print("PASS 压顶端缝 %.1fmm，净长 %.1fmm；保留 %d 个对象GUID / %d 个定义GUID" % (
            report["coping_end_joint_mm"], report["coping_length_mm"], report["preserved_object_guids"], report["preserved_definition_guids"]))
        print("真实铝板 %.4fkg/块；10mm检查：接触 %d → %d；请重导IFC、数据和正式日期快照。" % (
            report["physical_coping_kg_per_panel"], report["before_caps_and_posts"]["clearance_counts"]["contact"],
            report["after_caps_and_posts"]["clearance_counts"]["contact"]))
        return
    print("PASS U5 压顶修补：%d 块共享原定义；保留 %d 个对象 GUID" % (
        report["affected_panels"], report["preserved_object_guids"]))
    print("250 mm 总宽 / 180 mm 背边 / 70 mm 外伸；转角体积穿透 %d → %d" % (
        len(report["before_corners"]["clashes"]), len(report["after_corners"]["clashes"])))
    print("请重导 IFC 并刷新质量报告与日期快照。")


if __name__ == "__main__":
    main()
