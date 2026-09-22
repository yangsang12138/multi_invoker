#!/usr/bin/env python3
"""把整个项目打成一个可执行文件（zipapp）。

产物：
    dist/multi-invoker        带 shebang 的可执行文件，`./dist/multi-invoker` 即可启动网页
    dist/multi-invoker.pyz    同样的归档，`.pyz` 后缀，Windows 下 `python multi-invoker.pyz`

只要目标机器有 Python 3.9+，就**不需要**任何 pip 依赖，也不需要在旁边放
`web/`、`multi_invoker/` 这些目录 —— 全部压在这一个文件里。

用法：
    python3 tools/build_single_file.py
    python3 tools/build_single_file.py --out dist --name multi-invoker
    python3 tools/build_single_file.py --no-compress      # 调试用，构建更快
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import tempfile
import zipapp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 打进归档的东西：够跑起来就行，测试、文档、配置、.git 都不进去。
INCLUDE = ("__main__.py", "multi_invoker", "web")
SKIP_DIRS = {"__pycache__", ".git", ".mypy_cache", ".pytest_cache"}
SKIP_SUFFIX = (".pyc", ".pyo", ".DS_Store")


def _copy_tree(src: str, dst: str) -> None:
    for entry in sorted(os.listdir(src)):
        if entry in SKIP_DIRS or entry.endswith(SKIP_SUFFIX):
            continue
        source = os.path.join(src, entry)
        target = os.path.join(dst, entry)
        if os.path.isdir(source):
            os.makedirs(target, exist_ok=True)
            _copy_tree(source, target)
        else:
            shutil.copy2(source, target)


def stage(target_dir: str) -> None:
    """把要打包的文件复制到临时目录，保持 zipapp 需要的目录结构。"""
    for name in INCLUDE:
        source = os.path.join(ROOT, name)
        if not os.path.exists(source):
            raise SystemExit(f"缺少打包所需的文件：{name}")
        if os.path.isdir(source):
            os.makedirs(os.path.join(target_dir, name), exist_ok=True)
            _copy_tree(source, os.path.join(target_dir, name))
        else:
            shutil.copy2(source, os.path.join(target_dir, name))
    # __pycache__ 如果随源码目录一起进来，会显著变大且没有意义
    for current, dirs, _ in os.walk(target_dir):
        for name in list(dirs):
            if name in SKIP_DIRS:
                shutil.rmtree(os.path.join(current, name), ignore_errors=True)
                dirs.remove(name)


def build(out_dir: str, name: str, compress: bool, keep_stage: bool) -> list:
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    stage_dir = tempfile.mkdtemp(prefix="multi-invoker-build-")
    try:
        stage(stage_dir)
        plain = os.path.join(out_dir, name)
        zipapp.create_archive(
            stage_dir, target=plain, interpreter="/usr/bin/env python3",
            compressed=compress,
        )
        os.chmod(plain, os.stat(plain).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        pyz = os.path.join(out_dir, name + ".pyz")
        shutil.copyfile(plain, pyz)

        if keep_stage:
            print(f"暂存目录保留在：{stage_dir}")
        return [plain, pyz]
    finally:
        if not keep_stage:
            shutil.rmtree(stage_dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="打成一个可执行文件（zipapp）")
    parser.add_argument("--out", default=os.path.join(ROOT, "dist"), help="产物目录，默认 dist/")
    parser.add_argument("--name", default="multi-invoker", help="可执行文件名，默认 multi-invoker")
    parser.add_argument("--no-compress", action="store_true", help="不压缩（构建更快、体积更大）")
    parser.add_argument("--keep-stage", action="store_true", help="保留临时暂存目录，便于排查")
    args = parser.parse_args()

    if sys.version_info < (3, 9):
        print("需要 Python 3.9 及以上", file=sys.stderr)
        return 2

    outputs = build(args.out, args.name, not args.no_compress, args.keep_stage)
    print("已生成单文件部署产物：")
    for path in outputs:
        size = os.path.getsize(path) / 1024
        print(f"  {path}    {size:.0f} KB")
    print()
    print("用法：")
    print(f"  {outputs[0]}                 # 直接启动网页（自动开浏览器）")
    print(f"  {outputs[0]} serve --port 9000")
    print(f"  {outputs[0]} info             # 查看数据目录 / 运行形态")
    print(f"  {outputs[0]} query <调用项编码>")
    print()
    print("数据目录：单文件运行时默认在 ~/.multi-invoker/config，"
          "可用 MULTI_INVOKER_HOME 或 --config-dir 改。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
