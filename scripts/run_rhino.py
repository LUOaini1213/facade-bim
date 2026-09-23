"""调起本机 Rhino 8 跑 rhino/build_model.py，等它退出后打印构建日志。

Rhino 命令行对非 ASCII 路径不可靠（仓库在「求职」目录下），所以先把脚本复制到
纯 ASCII 的临时目录，再通过环境变量 FACADE_BIM_ROOT 告诉它仓库在哪。

    python scripts/run_rhino.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RHINO = os.environ.get("RHINO_EXE", r"C:\Program Files\Rhino 8\System\Rhino.exe")


def main():
    tmp = os.path.join(tempfile.gettempdir(), "facade_bim_run")
    os.makedirs(tmp, exist_ok=True)
    script = os.path.join(tmp, "build_model.py")
    shutil.copyfile(os.path.join(ROOT, "rhino", "build_model.py"), script)
    if not script.isascii():
        sys.exit("临时路径含非 ASCII 字符：%s，请把 TMP 设到纯英文目录" % script)
    log = os.path.join(ROOT, "model", "build_log.json")
    if os.path.exists(log):
        os.remove(log)
    env = dict(os.environ, FACADE_BIM_ROOT=ROOT)
    if " " in script:
        sys.exit("临时脚本路径含空格：%s" % script)
    # 手工拼命令行：整段 /runscript 外包一层引号、脚本路径不再加引号。交给 subprocess
    # 按列表拼的话，内层引号会被转义成 \"，Rhino 收到畸形路径后会停在命令行等输入。
    cmdline = '"%s" /nosplash /notemplate /runscript="_-RunPythonScript %s _-Exit"' % (RHINO, script)
    t0 = time.time()
    proc = subprocess.Popen(cmdline, env=env)
    try:
        proc.wait(timeout=900)
    except subprocess.TimeoutExpired:
        proc.kill()
        sys.exit("Rhino 900 秒未退出，已终止")
    if not os.path.exists(log):
        sys.exit("Rhino 退出了但没写日志（%.0f 秒）——脚本多半没被执行" % (time.time() - t0))
    data = json.load(open(log, encoding="utf-8"))
    for s in data.get("steps", []):
        print(s)
    if not data.get("ok"):
        print(data.get("error", "（无错误信息）"))
        sys.exit(1)
    print("完成：%s 秒，%s 个对象，Rhino %s" % (data["seconds"], data["objects"], data["rhino"]))


if __name__ == "__main__":
    main()
