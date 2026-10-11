from pathlib import Path

import shutil
import sys

try:
    import jsonc
except ModuleNotFoundError as e:
    raise ImportError(
        "Missing dependency 'json-with-comments' (imported as 'jsonc').\n"
        f"Install it with:\n  {sys.executable} -m pip install json-with-comments\n"
        "Or add it to your project's requirements."
    ) from e

from configure import configure_ocr_model


working_dir = Path(__file__).parent.parent.resolve()
install_path = working_dir / Path("install")
version = len(sys.argv) > 1 and sys.argv[1] or "v0.0.1"

# the first parameter is self name
if sys.argv.__len__() < 4:
    print("Usage: python install.py <version> <os> <arch>")
    print("Example: python install.py v1.0.0 win x86_64")
    sys.exit(1)

os_name = sys.argv[2]
arch = sys.argv[3]


def install_deps():
    if not (working_dir / "deps" / "bin").exists():
        print('Please download the MaaFramework to "deps" first.')
        print('请先下载 MaaFramework 到 "deps"。')
        sys.exit(1)

    if os_name not in {"win", "linux"} or arch != "x86_64":
        raise ValueError("Release builds support win x64 and linux amd64 only")
    shutil.copytree(
        working_dir / "deps" / "bin",
        install_path / "maafw",
        ignore=shutil.ignore_patterns(
            "*MaaDbgControlUnit*", "*MaaThriftControlUnit*", "*MaaRpc*",
            "*MaaHttp*", "*.node", "*MaaPiCli*",
        ),
        dirs_exist_ok=True,
    )
    shutil.copytree(
        working_dir / "deps" / "share" / "MaaAgentBinary",
        install_path / "maafw" / "MaaAgentBinary",
        dirs_exist_ok=True,
    )



def install_resource():

    configure_ocr_model()

    shutil.copytree(
        working_dir / "assets" / "resource",
        install_path / "resource",
        dirs_exist_ok=True,
    )
    shutil.copy2(
        working_dir / "assets" / "interface.json",
        install_path,
    )

    with open(install_path / "interface.json", "r", encoding="utf-8") as f:
        interface = jsonc.load(f)

    interface["version"] = version
    interface["agent"]["child_exec"] = (
        "./python/python.exe" if os_name == "win" else "./python/bin/python3"
    )
    interface["agent"]["child_args"] = ["-u", "./agent/main.py"]

    with open(install_path / "interface.json", "w", encoding="utf-8") as f:
        jsonc.dump(interface, f, ensure_ascii=False, indent=4)


def install_chores():
    shutil.copy2(
        working_dir / "README.md",
        install_path,
    )
    shutil.copy2(
        working_dir / "LICENSE",
        install_path,
    )


def install_agent():
    shutil.copytree(
        working_dir / "agent",
        install_path / "agent",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        dirs_exist_ok=True,
    )
    shutil.copytree(
        working_dir / "web", install_path / "web",
        ignore=shutil.ignore_patterns("runtime.json", "runtime.json.tmp"),
        dirs_exist_ok=True,
    )
    for filename in ("rune-filter.default.json", "rune-filter.rules.sqlite3"):
        (install_path / "assets").mkdir(exist_ok=True)
        shutil.copy2(working_dir / "assets" / filename, install_path / "assets" / filename)


if __name__ == "__main__":
    install_deps()
    install_resource()
    install_chores()
    install_agent()

    print(f"Install to {install_path} successfully.")
