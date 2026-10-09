"""批量原生 Rhino 3D 碰撞、净距和 Eto 时间轴检查。"""
import argparse
from datetime import date
import hashlib
import json
import math
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
    parser.add_argument("--mode", choices=["all", "spatial", "timeline"], default="all")
    parser.add_argument("--clearance-mm", type=float, default=10)
    parser.add_argument("--date", type=date.fromisoformat)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    if not math.isfinite(args.clearance_mm) or args.clearance_mm < 0:
        parser.error("净距必须是有限非负数")
    rhino = Path(os.environ.get("RHINO_EXE", r"C:\Program Files\Rhino 8\System\Rhino.exe"))
    if not rhino.is_file():
        parser.error("找不到 Rhino 8；请设置 RHINO_EXE")
    source = ROOT / "model" / "facade_bim.3dm"
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    log = ROOT / "model" / "quality" / ("native_" + args.mode + ".json")
    log.parent.mkdir(parents=True, exist_ok=True)
    if log.exists():
        log.unlink()
    with tempfile.TemporaryDirectory(prefix="facade_quality_") as temp:
        script = Path(temp) / "quality_check.py"
        if not str(script).isascii() or " " in str(script):
            parser.error("请把 TMP/TEMP 设为不含空格的英文路径")
        shutil.copyfile(ROOT / "rhino" / "quality_check.py", script)
        env = dict(os.environ, FACADE_BIM_ROOT=str(ROOT), FACADE_QUALITY_MODE=args.mode,
                   FACADE_CLEARANCE_MM=str(args.clearance_mm))
        env.pop("FACADE_QUALITY_DATE", None)
        if args.date:
            env["FACADE_QUALITY_DATE"] = args.date.isoformat()
        startup = None
        if os.name == "nt":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
        cmd = '"%s" /nosplash /notemplate /runscript="_-RunPythonScript %s _-Exit"' % (rhino, script)
        proc = subprocess.Popen(cmd, env=env, startupinfo=startup)
        try:
            proc.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            sys.exit("Rhino 质量检查超时；仅终止本次启动的进程")
    if hashlib.sha256(source.read_bytes()).hexdigest() != before:
        sys.exit("源模型意外改变")
    if not log.is_file():
        sys.exit("Rhino 未写日志；请检查许可证和脚本环境")
    data = json.loads(log.read_text(encoding="utf-8"))
    if data.get("source_preserved"):
        from check_quality import verify
        verify(log)  # Keep an actionable HTML report even when a real clash makes the check fail.
    if not data.get("ok"):
        print("FAIL Rhino 原生质量检查：" + str(log.relative_to(ROOT)))
        if data.get("error"):
            print(data["error"])
        if "spatial" in data:
            q = data["spatial"]
            print("体积碰撞 %d，净距/接触事件 %d，未决 %d；完整定位见同名 HTML/JSON" % (
                len(q["clashes"]), len(q["clearance_events"]), len(q["unresolved"])))
        sys.exit(1)
    print("PASS Rhino 原生质量检查：" + str(log.relative_to(ROOT)))
    if "spatial" in data:
        q = data["spatial"]
        print("%d 构件 / %d 实体零件；体积碰撞 %d，净距/接触事件 %d，未决 %d" % (
            q["units"], q["solid_parts"], len(q["clashes"]), len(q["clearance_events"]), len(q["unresolved"])))
    if "timeline" in data:
        print("实际 Eto 滑块/定时器/查询/暂停/关闭检查通过")


if __name__ == "__main__":
    main()
