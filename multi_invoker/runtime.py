"""运行时路径解析：源码运行、单文件运行（zipapp / PyInstaller）、环境变量覆盖。

这个模块只回答三个问题，别的一概不管：

1. **网页资源在哪** —— 源码运行就在仓库的 `web/`；打包成单文件后资源被压进归档，
   需要通过 `read_asset()` 从归档里读。
2. **配置数据写哪** —— 源码运行沿用仓库的 `config/`；单文件运行时改用用户目录
   （`~/.multi-invoker/config`），这样可执行文件本身可以放在任何位置、随时替换。
3. **当前是哪种运行形态** —— 供 `/api/meta` 与 `info` 子命令告诉调用方，便于排障。

优先级（从高到低）：
    `--config-dir` 参数  >  `MULTI_INVOKER_HOME` 环境变量  >  单文件默认目录 / 仓库目录
"""

from __future__ import annotations

import os
import sys
import zipfile

APP_NAME = "multi-invoker"
ARCHIVE_ASSET_PREFIX = "web"


# --------------------------------------------------------------- 运行形态 --
def _main_file() -> str | None:
    """当前进程的入口文件；zipapp 下形如 `/path/app.pyz/__main__.py`。"""
    main = sys.modules.get("__main__")
    path = getattr(main, "__file__", None)
    if path:
        return path
    return sys.argv[0] if sys.argv else None


def archive_path() -> str | None:
    """若运行在 zipapp 单文件里，返回该归档的绝对路径；否则 None。"""
    candidates = []
    main_file = _main_file()
    if main_file:
        candidates.append(main_file)
    if sys.argv and sys.argv[0]:
        candidates.append(sys.argv[0])
    for candidate in sys.path[:2]:
        if candidate:
            candidates.append(candidate)

    for candidate in candidates:
        # zipapp 里 `__main__.__file__` 是 "<archive>/__main__.py"，要往上剥一层
        probe = os.path.abspath(candidate)
        while probe and not os.path.isfile(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        if probe and os.path.isfile(probe) and zipfile.is_zipfile(probe):
            return probe
    return None


def is_pyinstaller() -> bool:
    return bool(getattr(sys, "frozen", False))


def is_bundled() -> bool:
    """是否运行在「一个执行文件」里（zipapp 或 PyInstaller 单文件）。"""
    return is_pyinstaller() or archive_path() is not None


def repo_root() -> str:
    """源码仓库根目录（`multi_invoker/` 的上一级）。打包后该目录不存在。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def describe() -> str:
    if is_pyinstaller():
        return "单文件可执行程序（PyInstaller）"
    if archive_path():
        return "单文件可执行程序（zipapp）"
    return "源码目录"


# ----------------------------------------------------------------- 路径 --
def bundle_dir() -> str | None:
    """PyInstaller 解包出的临时目录；非 PyInstaller 运行返回 None。"""
    base = getattr(sys, "_MEIPASS", None)
    return base if base else None


def home_dir() -> str:
    """数据根目录：配置、导出等都挂在它下面。"""
    override = (os.environ.get("MULTI_INVOKER_HOME") or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if is_bundled():
        return os.path.join(os.path.expanduser("~"), f".{APP_NAME}")
    return repo_root()


def default_config_dir() -> str:
    return os.path.join(home_dir(), "config")


def exports_dir() -> str:
    return os.path.join(home_dir(), "exports")


def web_dir() -> str | None:
    """网页资源的真实目录；资源只在归档里时返回 None（改用 read_asset）。"""
    base = bundle_dir()
    if base:
        candidate = os.path.join(base, ARCHIVE_ASSET_PREFIX)
        if os.path.isdir(candidate):
            return candidate
    candidate = os.path.join(repo_root(), ARCHIVE_ASSET_PREFIX)
    if os.path.isdir(candidate):
        return candidate
    return None


def read_asset(name: str) -> bytes | None:
    """读取网页资源：先看磁盘目录，再从单文件归档里取。

    `name` 是 `web/` 下的相对路径，例如 `index.html`、`style.css`。
    取不到返回 None，由调用方决定回什么错。
    """
    name = name.lstrip("/").replace(os.sep, "/")
    directory = web_dir()
    if directory:
        path = os.path.join(directory, *name.split("/"))
        if os.path.isfile(path):
            try:
                with open(path, "rb") as handle:
                    return handle.read()
            except OSError:
                return None

    archive = archive_path()
    if archive:
        try:
            with zipfile.ZipFile(archive) as bundle:
                return bundle.read(f"{ARCHIVE_ASSET_PREFIX}/{name}")
        except (KeyError, OSError, zipfile.BadZipFile):
            return None
    return None


def has_asset(name: str) -> bool:
    directory = web_dir()
    if directory and os.path.isfile(os.path.join(directory, *name.split("/"))):
        return True
    archive = archive_path()
    if archive:
        try:
            with zipfile.ZipFile(archive) as bundle:
                return f"{ARCHIVE_ASSET_PREFIX}/{name}" in bundle.namelist()
        except (OSError, zipfile.BadZipFile):
            return False
    return False


def snapshot() -> dict:
    """给 `/api/meta` 和 `info` 子命令用的运行时信息。"""
    return {
        "app": APP_NAME,
        "version": _version(),
        "runtime": describe(),
        "bundled": is_bundled(),
        "home": home_dir(),
        "configDir": default_config_dir(),
        "webDir": web_dir(),
        "archive": archive_path(),
        "python": sys.version.split()[0],
        "platform": sys.platform,
    }


def _version() -> str:
    from . import __version__
    return __version__
