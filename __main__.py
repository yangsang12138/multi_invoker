#!/usr/bin/env python3
"""单文件部署的入口。

这个文件既服务于 `python3 <仓库目录>`，也是 `tools/build_single_file.py`
打进 zipapp 归档的那份 `__main__.py`。

行为：不带参数直接运行 = 启动本地网页界面（自动开浏览器）。
其余子命令与命令行用法完全一致，见 `multi_invoker/cli.py`。
"""

import os
import sys

# 直接 `python3 这个文件` 时要保证包能被导入；zipapp 里 zip 本身已在 sys.path 上。
_here = os.path.dirname(os.path.abspath(__file__))
if os.path.isdir(os.path.join(_here, "multi_invoker")) and _here not in sys.path:
    sys.path.insert(0, _here)

from multi_invoker.cli import main  # noqa: E402

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
