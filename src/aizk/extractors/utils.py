# ruff: NOQA: E731
import logging
import os
from pathlib import Path
from subprocess import CalledProcessError, run

from aizk.utilities.file_helpers import AtomicWriter
from aizk.utilities.path_helpers import path_is_file

logger = logging.getLogger(__name__)


def bin_version(bin_path: Path | str) -> str | None:
    """Get version a specified binary."""
    bin_path = Path(bin_path)
    if not bin_path.exists():
        logger.debug(f"{bin_path} does not exist")
        return None

    cmd = [bin_path, "--version"]
    result = run(  # NOQA: S603
        cmd,
        # env=os.environ | {"LANG": "C"},
        capture_output=True,
    )

    try:
        result.check_returncode()  # raises error if failed
    except CalledProcessError:
        logger.debug(f"{' '.join(cmd)} failed with non-zero exit code")
        return None

    version_str = result.stdout.strip().decode()
    # take first 3 columns of first line of version info
    return " ".join(version_str.split("\n")[0].strip().split()[:3])


def get_write_mode(data):
    """Determine whether data is bytes vs string."""
    if isinstance(data, bytes):
        return "wb"
    elif isinstance(data, str):
        return "w"
    else:
        raise TypeError("Data must be either string or bytes")


def download_file(url: str, file_path: Path, timeout: int = 600):
    """Download a file."""
    import requests
    from tqdm.auto import tqdm

    try:
        # First, send a HEAD request to get file size
        head_response = requests.head(url, timeout=timeout)
        head_response.raise_for_status()
        total_size = int(head_response.headers.get("content-length", 0))
    except requests.exceptions.RequestException:
        logger.exception("Could not query file head")
        raise

    try:
        with requests.get(url, stream=True, timeout=timeout) as response:
            # Raise an exception for bad status codes
            response.raise_for_status()

            # Get the total file size
            total_size = int(response.headers.get("content-length", 0))

            with (
                AtomicWriter(file_path, binary_mode=True) as f,
                tqdm(total=total_size, unit="iB", unit_scale=True, desc=str(file_path), leave=False) as progress_bar,
            ):
                for chunk in response.iter_content(chunk_size=8192):
                    size = f.write(chunk)
                    progress_bar.update(size)

        logger.info(f"File downloaded successfully: {file_path}")

    except requests.exceptions.RequestException:
        logger.exception("Download failed")
        raise


def validate_file(filepath: Path | str, min_bytes: int = 1) -> bool:
    """Validate that downloaded file exists and is of minimum size."""
    if min_bytes < 1:
        raise ValueError("Minimum file size must be at least 1 byte")

    filepath = path_is_file(filepath)

    # Get file size
    file_size = filepath.stat().st_size

    # Check minimum size
    if file_size < min_bytes:
        logger.error(f"File is too small: found {file_size} bytes, expected at least {min_bytes} bytes.")
        return False

    return True
