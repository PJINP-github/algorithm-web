# -*- coding: utf-8 -*-
"""HTML 转 XLSX 的稳定入口，负责 5 天老数据窗口和文件落位。"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_HTML = BASE_DIR / "0Work" / "page.html"
DEFAULT_OUTPUT = BASE_DIR / "0Work" / "result-2.xlsx"
CONVERTER = BASE_DIR / "html_to_xlsx.py"
FIVE_DAYS_SECONDS = 5 * 24 * 60 * 60
VALID_MODES = {"y", "n", "a", "b"}


def configure_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def parse_args(argv):
    positional = []
    for arg in argv[1:]:
        if arg == "--no-pause":
            continue
        positional.append(arg)

    input_path = positional[0] if positional else ""
    mode_input = positional[1] if len(positional) >= 2 else ""
    output_path = positional[2] if len(positional) >= 3 else ""

    if input_path.lower() in VALID_MODES:
        mode_input = input_path.lower()
        input_path = ""

    input_file = Path(input_path).expanduser() if input_path else DEFAULT_HTML
    output_file = Path(output_path).expanduser() if output_path else DEFAULT_OUTPUT
    mode = (mode_input or "n").strip().lower()
    if mode not in VALID_MODES:
        raise ValueError("模式必须是 y、n、a 或 b。")
    return input_file.resolve(), output_file.resolve(), mode


def remove_file_if_present(path):
    if path.is_file():
        path.unlink()


def is_within_five_days(path):
    age_seconds = time.time() - path.stat().st_ctime
    return age_seconds <= FIVE_DAYS_SECONDS


def prepare_old_data(final_result, table_result):
    old_file = final_result.with_name("old.xlsx")
    unused_old_file = final_result.with_name("old.xlxs")
    unused_old_bak_file = final_result.with_name("old-bak.xlsx")

    # Only old.xlsx and result-2.xlsx are retained as data sources.
    remove_file_if_present(unused_old_file)
    remove_file_if_present(unused_old_bak_file)

    backup_source = table_result if table_result.is_file() else final_result
    if not backup_source.is_file():
        return "no_source"

    if old_file.is_file() and is_within_five_days(old_file):
        return "preserved"

    if old_file.is_file():
        old_file.unlink()
    shutil.copy2(backup_source, old_file)
    return "updated"


def table_output_path(output_file):
    return output_file.with_name(
        "%s-2%s" % (output_file.stem, output_file.suffix)
    )


def run_converter(input_file, output_file, mode):
    return subprocess.run([
        sys.executable,
        str(CONVERTER),
        str(input_file),
        str(output_file),
        mode,
    ], cwd=str(BASE_DIR)).returncode


def main(argv=None):
    configure_console()
    argv = argv or sys.argv
    try:
        input_file, output_file, mode = parse_args(argv)
    except ValueError as exc:
        print("[ERROR] %s" % exc)
        return 1

    if not input_file.is_file():
        print("[ERROR] HTML不存在:")
        print(input_file)
        return 1
    if not CONVERTER.is_file():
        print("[ERROR] Python脚本不存在:")
        print(CONVERTER)
        return 1

    output_file.parent.mkdir(parents=True, exist_ok=True)
    generated_table = table_output_path(output_file)
    had_previous_result = output_file.is_file()

    try:
        old_status = prepare_old_data(output_file, generated_table)
    except OSError as exc:
        print("[ERROR] 无法准备 old.xlsx，请确认文件未被 Excel 或其他程序占用。")
        print(exc)
        return 1

    print()
    print("==============================================")
    print("              HTML 转 XLSX")
    print("==============================================")
    print()
    print("输入文件:")
    print(input_file)
    print()
    print("模式: %s" % mode)
    print("输出:")
    print(output_file)
    print()

    return_code = run_converter(input_file, output_file, mode)
    if return_code != 0:
        print()
        print("[ERROR] 执行失败")
        return return_code

    if generated_table.is_file():
        try:
            os.replace(generated_table, output_file)
        except OSError as exc:
            print()
            print("[ERROR] 无法将 %s 改名为 %s，可能文件被占用。" % (
                generated_table.name,
                output_file.name,
            ))
            print(exc)
            return 1
        print("[INFO] 已落位新表: %s 改名为 %s" % (
            generated_table.name,
            output_file.name,
        ))
        if old_status == "preserved":
            print("[INFO] old.xlsx：创建时间在 5 天内，已保留，未覆盖。")
        elif old_status == "updated":
            print("[INFO] old.xlsx：已用刷新前的旧数据覆盖更新。")
        else:
            print("[INFO] old.xlsx：刷新前没有可备份的旧 result-2.xlsx，本次未更新。")

        if had_previous_result:
            print("[INFO] 旧 result-2.xlsx：已被新 result-2-2.xlsx 替换，旧文件已移除。")
        else:
            print("[INFO] 旧 result-2.xlsx：刷新前不存在，本次直接生成。")
    elif not output_file.is_file():
        print()
        print("[ERROR] 未找到新数据表 %s 或 %s。" % (
            generated_table.name,
            output_file.name,
        ))
        return 1

    print()
    print("==============================================")
    print("完成")
    print("==============================================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
