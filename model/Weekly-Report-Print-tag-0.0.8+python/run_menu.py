import os
import subprocess
import sys


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATE_REVIEW_DIR = os.path.join(BASE_DIR, "date_review")
WORK_DIR = os.path.join(DATE_REVIEW_DIR, "0Work")


def ask_choice(prompt, choices, default):
    normalized = {choice.lower() for choice in choices}
    while True:
        value = input(prompt).strip().lower()
        if not value:
            return default
        if value in normalized:
            return value
        print("输入无效，请重新选择。")


def run_command(args):
    completed = subprocess.run(args, cwd=BASE_DIR)
    if completed.returncode != 0:
        raise RuntimeError("命令执行失败: %s" % " ".join(args))


def refresh_data():
    print()
    print("[INFO] 模式 y：先采集页面 HTML...")
    run_command([os.path.join(DATE_REVIEW_DIR, "start.bat"), "--no-pause"])

    print()
    print("[INFO] 模式 y：再将 HTML 转为 XLSX，固定使用 n（完整数据 + 不合并）...")
    run_command([
        os.path.join(DATE_REVIEW_DIR, "run_html2xlsx.bat"),
        os.path.join(WORK_DIR, "page.html"),
        "n",
        os.path.join(WORK_DIR, "result-2.xlsx"),
        "--no-pause",
    ])

    print()
    print("[INFO] 数据已刷新，继续生成周报...")


def run_weekly(site, clipboard_mode, accuracy_source):
    args = [
        sys.executable,
        os.path.join(BASE_DIR, "weekly_report.py"),
        site,
        clipboard_mode,
        accuracy_source,
        "--local-clipboard",
    ]
    run_command(args)


def run_lan_web_service():
    if os.name == "nt":
        os.system("cls")
    return subprocess.run(
        [sys.executable, os.path.join(BASE_DIR, "lan_weekly_server.py")],
        cwd=BASE_DIR,
    ).returncode


def main():
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    initial_site = sys.argv[1].strip() if len(sys.argv) > 1 else ""

    print()
    print("==============================================")
    print("             输出方式")
    print("==============================================")
    print()
    print(" 1 : 本机终端模式（直接复制到本机剪贴板，不开启局域网网页）")
    print(" 2 : 局域网 Web 模式（启动独立网页服务）")
    print()
    delivery_mode = ask_choice("请选择输出方式 [1/2] (默认1): ", {"1", "2"}, "1")

    if delivery_mode == "2":
        return run_lan_web_service()

    output_label = "本机终端模式"

    while True:
        print()
        print("==============================================")
        print("             周报生成入口")
        print("==============================================")
        print()
        print("当前输出方式: %s" % output_label)
        print()
        print(" y : 刷新数据后输出周报")
        print(" n : 不刷新数据输出周报")
        print(" q : 退出")
        print()
        refresh_choice = ask_choice("是否刷新数据 [y/n/q] (默认n): ", {"y", "n", "q"}, "n")

        if refresh_choice == "q":
            print()
            print("Bye.")
            return 0
        if refresh_choice == "y":
            refresh_data()

        if initial_site:
            site = initial_site
            initial_site = ""
        else:
            site = input("Enter site keyword: ").strip()
        if not site:
            print("Error: site keyword cannot be empty.")
            continue

        print()
        print("Accuracy data source:")
        print("  y = accuracy")
        print("  n = audited accuracy")
        accuracy_choice = ask_choice("Choose y/n (default y): ", {"y", "n"}, "y")
        accuracy_source = "audited" if accuracy_choice == "n" else "raw"

        print()
        print("Clipboard mode:")
        print("  y = text body + table images")
        print("  n = full report as one image")
        image_choice = ask_choice("Choose y/n (default y): ", {"y", "n"}, "y")
        clipboard_mode = "image" if image_choice == "n" else "text"

        try:
            run_weekly(site, clipboard_mode, accuracy_source)
        except RuntimeError as exc:
            print()
            print(str(exc))
            return 1

        print()
        print("[INFO] 本次周报已完成。")
        print("[INFO] 可继续选择 y 刷新数据后输出，或选择 n 直接输出。")


if __name__ == "__main__":
    raise SystemExit(main())
