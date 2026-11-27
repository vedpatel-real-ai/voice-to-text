"""Repair py2app 0.28 alias resources in a newly built candidate before signing."""
from pathlib import Path
import py_compile
import shutil
import sys
import sysconfig


def prepare(bundle):
    contents = bundle / "Contents"
    resources = contents / "Resources"
    executable = contents / "MacOS" / "python"
    # The alias launcher checks lstat(python) to select the external framework.
    # Preserve that contract with a relative link to the same interpreter binary.
    shutil.copy2(executable.resolve(strict=True), executable.with_name("python3.12"))
    executable.unlink()
    executable.symlink_to("python3.12")
    icon = resources / "PythonApplet.icns"
    if icon.is_symlink():
        source = icon.resolve(strict=True)
        icon.unlink()
        shutil.copy2(source, icon)
    config = resources / "lib/python3.12/config"
    if config.is_symlink():
        config.unlink()
        shutil.copytree(sysconfig.get_config_var("LIBPL"), config)
    py_compile.compile(str(resources / "site.py"), cfile=str(resources / "site.pyc"), doraise=True)


if __name__ == "__main__":
    prepare(Path(sys.argv[1]))
