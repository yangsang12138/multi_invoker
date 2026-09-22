#!/usr/bin/env python3
"""生成 macOS 桌面客户端（.app）。

思路和 worklog 的「一个核心 + 多个外壳」一致：这里不复制任何业务代码，
只是把**同一个单文件核心**塞进一个原生外壳里，让它可以被双击打开、
在程序坞里有个图标、关掉窗口就结束进程。

产物：
    dist/Multi-Service Invoker.app
      Contents/
        Info.plist
        MacOS/launcher                  ← clients/desktop/macos/launcher.sh
        Resources/multi-invoker         ← tools/build_single_file.py 的产物

用法：
    python3 tools/build_single_file.py           # 先生成核心（未生成会自动调用）
    python3 tools/make_desktop_app.py
    python3 tools/make_desktop_app.py --port 9000 --name "Multi-Service Invoker"
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import stat
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER_TEMPLATE = os.path.join(ROOT, "clients", "desktop", "macos", "launcher.sh")
DEFAULT_CORE = os.path.join(ROOT, "dist", "multi-invoker")
DEFAULT_NAME = "Multi-Service Invoker"
DISPLAY_NAME_ZH = "多服务调用器"   # 写入 Info.plist 的 CFBundleDisplayName
BUNDLE_ID = "local.multi.invoker"


def ensure_core(path: str) -> str:
    """核心不存在就先按标准流程构建一次，避免用户手动串两条命令。"""
    if os.path.isfile(path):
        return path
    builder = os.path.join(ROOT, "tools", "build_single_file.py")
    print(f"未找到核心 {path}，先执行 {os.path.relpath(builder, ROOT)} …")
    subprocess.run([sys.executable, builder], check=True)
    if not os.path.isfile(path):
        raise SystemExit(f"构建后仍未生成核心：{path}")
    return path


def write_info_plist(path: str, name: str, version: str, exe: str) -> None:
    info = {
        # 文件名 / Bundle 标识一律英文；中文只作为展示名出现在 Dock / 启动台
        "CFBundleName": name,
        "CFBundleDisplayName": DISPLAY_NAME_ZH,
        "CFBundleExecutable": exe,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "CFBundleInfoDictionaryVersion": "6.0",
        "LSMinimumSystemVersion": "10.13",
        "NSHighResolutionCapable": True,
        # 这是个网页外壳，不是文档编辑器，别在「打开方式」里出现
        "LSApplicationCategoryType": "public.app-category.developer-tools",
        "NSRequiresAquaSystemAppearance": False,
    }
    with open(path, "wb") as handle:
        plistlib.dump(info, handle)


def build_app(out_dir: str, name: str, core: str, port: int) -> str:
    from multi_invoker import __version__ as version  # 需要 sys.path 上有仓库根

    app_path = os.path.join(os.path.abspath(out_dir), f"{name}.app")
    contents = os.path.join(app_path, "Contents")
    macos_dir = os.path.join(contents, "MacOS")
    resources = os.path.join(contents, "Resources")

    if os.path.exists(app_path):
        shutil.rmtree(app_path)
    os.makedirs(macos_dir, exist_ok=True)
    os.makedirs(resources, exist_ok=True)

    # 核心：原样复制单文件可执行程序
    core_target = os.path.join(resources, os.path.basename(core))
    shutil.copy2(core, core_target)
    os.chmod(core_target, os.stat(core_target).st_mode | stat.S_IXUSR)

    # 启动器：替换模板里的两个占位符
    with open(LAUNCHER_TEMPLATE, "r", encoding="utf-8") as handle:
        script = handle.read()
    script = script.replace("__CORE_BIN__", os.path.basename(core))
    script = script.replace("__PORT__", str(port))
    launcher = os.path.join(macos_dir, "launcher")
    with open(launcher, "w", encoding="utf-8") as handle:
        handle.write(script)
    os.chmod(launcher, 0o755)

    write_info_plist(os.path.join(contents, "Info.plist"), name, version, "launcher")

    # 去掉隔离标记，避免从网盘/压缩包解出来后首次双击被拦
    subprocess.run(["xattr", "-cr", app_path], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return app_path


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 macOS 桌面客户端（.app）")
    parser.add_argument("--out", default=os.path.join(ROOT, "dist"), help="产物目录，默认 dist/")
    parser.add_argument("--name", default=DEFAULT_NAME, help=f"应用名，默认 {DEFAULT_NAME}")
    parser.add_argument("--core", default=DEFAULT_CORE, help="单文件核心路径")
    parser.add_argument("--port", type=int, default=8765, help="核心监听端口，默认 8765")
    args = parser.parse_args()

    if sys.platform != "darwin":
        print("这个脚本只用于生成 macOS 的 .app；其它平台见 clients/desktop/ 下的模板。",
              file=sys.stderr)
        return 2

    sys.path.insert(0, ROOT)
    core = ensure_core(args.core)
    app_path = build_app(args.out, args.name, core, args.port)
    print("已生成桌面客户端：")
    print(f"  {app_path}")
    print()
    print("双击即可打开；窗口关闭后核心进程一并结束。")
    print(f"日志：~/Library/Logs/multi-invoker.log    数据：~/.multi-invoker/config")
    return 0


if __name__ == "__main__":
    sys.exit(main())
