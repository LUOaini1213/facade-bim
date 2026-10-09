"""批量调用本机 Rhino 8 输出日期 .3dm / PNG / JSON；原始模型保持可重复使用。"""
import argparse
from datetime import date
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
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dates", nargs="+", default=["2026-10-30", "2026-11-02", "2026-11-23", "2026-12-20"])
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    for value in args.dates:
        try:
            date.fromisoformat(value)
        except ValueError:
            parser.error("日期应为 YYYY-MM-DD：" + value)
    rhino = Path(os.environ.get("RHINO_EXE", r"C:\Program Files\Rhino 8\System\Rhino.exe"))
    if not rhino.is_file():
        parser.error("找不到 Rhino 8；请设置 RHINO_EXE")
    if not (ROOT / "model" / "facade_bim.3dm").is_file():
        parser.error("缺 model/facade_bim.3dm；先运行 scripts/run_rhino.py")
    log = ROOT / "model" / "replay" / "run_log.json"
    log.parent.mkdir(parents=True, exist_ok=True)
    if log.exists():
        log.unlink()
    with tempfile.TemporaryDirectory(prefix="facade_replay_") as temp:
        script = Path(temp) / "replay_install.py"
        if not str(script).isascii() or " " in str(script):
            parser.error("请把 TMP/TEMP 设为不含空格的英文路径")
        shutil.copyfile(ROOT / "rhino" / "replay_install.py", script)
        env = dict(os.environ, FACADE_BIM_ROOT=str(ROOT), FACADE_REPLAY_DATES=",".join(args.dates))
        cmd = '"%s" /nosplash /notemplate /runscript="_-RunPythonScript %s _-Exit"' % (rhino, script)
        startup = None
        if os.name == "nt":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
        proc = subprocess.Popen(cmd, env=env, startupinfo=startup)
        try:
            proc.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            sys.exit("Rhino 日期回放超时；仅终止本次启动的进程")
    if not log.is_file():
        sys.exit("Rhino 未写回放日志；检查许可证与脚本运行环境")
    data = json.loads(log.read_text(encoding="utf-8"))
    if not data.get("ok"):
        sys.exit(data.get("error", "Rhino 日期回放失败"))
    for state in data["snapshots"]:
        print(json.dumps(state, ensure_ascii=False))
    print("Rhino %s：日期模型、截图与统计已存入 model/replay/" % data["rhino"])


if __name__ == "__main__":
    main()
