import logging
import os
from pathlib import Path
import platform
import shutil
import sys
from typing import Annotated, List

from pydantic import AfterValidator, BeforeValidator, TypeAdapter, ValidationError

logger = logging.getLogger(__name__)


### abx-pkg
OPERATING_SYSTEM = platform.system().lower()
DEFAULT_PATH: str = ":".join(
    [
        "/usr/local/bin",
        "/usr/local/sbin",
        "/usr/bin",
        "/usr/sbin",
        "/bin",
        "/sbin",
    ]
)
if OPERATING_SYSTEM == "darwin":
    DEFAULT_PATH = ":".join(["/opt/homebrew/bin", DEFAULT_PATH])

DEFAULT_ENV_PATH: str = os.environ.get("PATH", DEFAULT_PATH)


def path_is_valid(path: Path | str) -> Path:
    """Check whether full path can be resolved."""
    path = Path(path) if isinstance(path, str) else path
    path = path.expanduser().absolute()  # resolve ~/ -> /home/<username/ and ../../
    _ = path.resolve()  # make sure symlinks can be resolved, but dont return resolved link
    return path


def path_is_dir(path: Path | str) -> Path:
    """Test whether path is dir."""
    path = path_is_valid(path)
    if os.path.isdir(path):
        if os.access(path, os.R_OK):
            return path
        else:
            raise PermissionError(f"Path is not readable: {path}")
    else:
        raise NotADirectoryError(f"Path is not a directory: {path}")


DirPath = Annotated[Path, AfterValidator(path_is_dir)]


def path_is_file(path: Path | str) -> Path:
    """Test whether path is file."""
    path = path_is_valid(path)
    if os.path.isfile(path):
        if os.access(path, os.R_OK):
            return path
        else:
            raise PermissionError(f"Path is not readable: {path}")
    else:
        raise FileNotFoundError(f"Path is not a file: {path}")


FilePath = Annotated[Path, AfterValidator(path_is_file)]


def path_is_executable(path: FilePath) -> FilePath:
    """Test whether path is executable."""
    if path_is_file(path) and os.access(path, os.X_OK):
        return path
    else:
        raise PermissionError(f"Path is not executable (fix by running `chmod +x {path}`)")


def path_is_script(path: FilePath) -> FilePath:
    """Check whether path is script."""
    SCRIPT_EXTENSIONS = (".py", ".js", ".sh")  # NOQA: N806
    if path.suffix.lower() in SCRIPT_EXTENSIONS:
        return path
    else:
        raise TypeError(f"Path is not a script (does not end in {', '.join(SCRIPT_EXTENSIONS)})")


ExecPath = Annotated[FilePath, AfterValidator(path_is_executable)]
ScriptPath = Annotated[FilePath, AfterValidator(path_is_script)]


class PATHValidationError(Exception):
    """System PATH did not validate."""


def validate_syspath(syspath: Path | str | None = None) -> str:
    """Test whether all PATH paths are valid."""
    syspath = str(syspath) if syspath else DEFAULT_ENV_PATH
    paths = syspath.split(":")
    if all(Path(bin_dir) for bin_dir in paths):
        return ":".join(paths).strip(":")
    else:
        raise PATHValidationError(f"At least one System PATH did not validate: {syspath}")


SysPATH = Annotated[str, BeforeValidator(validate_syspath)]


def get_local_bin_dir() -> DirPath:
    """Identify local directory for binary files."""
    local_bin_dir = Path.cwd().expanduser().absolute() / ".local" / "bin"
    local_bin_dir.mkdir(parents=True, exist_ok=True)
    return TypeAdapter(DirPath).validate_python(local_bin_dir)


def add_local_bindir_to_syspath(syspath: SysPATH = DEFAULT_ENV_PATH) -> SysPATH:
    """Add local bin directory to path."""
    local_bin_dir = str(get_local_bin_dir())
    if local_bin_dir not in syspath:
        syspath = ":".join([local_bin_dir, syspath])
    return TypeAdapter(SysPATH).validate_python(syspath)


def add_node_bindir_to_syspath(syspath: SysPATH = DEFAULT_ENV_PATH) -> SysPATH:
    """Add Node / NPM bin to path."""
    node_bin_dir = str(Path.cwd().expanduser().absolute() / "node_modules" / ".bin")

    if node_bin_dir not in syspath:
        syspath = ":".join([node_bin_dir, syspath])
    return TypeAdapter(SysPATH).validate_python(syspath)


def add_python_bindir_to_syspath(syspath: SysPATH = DEFAULT_ENV_PATH) -> SysPATH:
    """Add Python bin to path."""
    python_bin_dir = str(Path(sys.executable).parent)

    if python_bin_dir not in syspath:
        syspath = ":".join([python_bin_dir, syspath])
    return TypeAdapter(SysPATH).validate_python(syspath)


def find_binary_abspath(bin_path_or_name: Path | str, syspath: Path | str | None = None) -> ExecPath:
    """Identify abspath for specified binary."""
    if not bin_path_or_name:
        raise ValueError("'bin_path_or_name' must be provided")
    bin_path_or_name = str(bin_path_or_name)
    syspath = validate_syspath(syspath)

    if bin_path_or_name.startswith("/"):
        bin_path = Path(bin_path_or_name)
    else:
        bin_path = shutil.which(bin_path_or_name, mode=os.X_OK, path=syspath)

        if bin_path:
            bin_path = Path(bin_path)
        else:
            # some bins dont show up with shutil.which()
            for path in syspath.split(":"):
                bin_dir = Path(path)

                try:
                    bin_path = path_is_file(bin_dir / bin_path_or_name)
                    break
                except FileNotFoundError:
                    pass

            raise FileNotFoundError(f"Could not find {bin_path_or_name} in PATH")

    try:
        return TypeAdapter(ExecPath).validate_python(bin_path)
    except ValidationError as e:
        # return None
        raise FileNotFoundError from e


def symlink_to_bin(binary: Path | str, bin_dir: Path | None = None) -> Path:
    """Create executable symlink to binary."""
    binary = find_binary_abspath(binary)
    bin_dir = path_is_dir(bin_dir or get_local_bin_dir())

    _ = path_is_file(binary)  # validate binary exists

    symlink = bin_dir / binary.name.lower().replace(" ", "-")

    if platform.system().lower() != "darwin":
        # if on macOS, binary may be inside a .app (such as chrome), so create a tiny bash script instead of a symlink
        symlink.unlink(missing_ok=True)
        symlink.write_text(f"""#!/usr/bin/env bash\nexec '{binary}' "$@"\n""")
        symlink.chmod(0o777)  # make sure its executable by everyone
    else:
        # otherwise on linux we can symlink directly to binary executable
        symlink.unlink(missing_ok=True)
        symlink.symlink_to(binary)

    return symlink
