# ruff: NOQA: E731
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import file_digest
from html import escape, unescape
import logging
import os
from pathlib import Path
import re
import shutil
from subprocess import PIPE, CalledProcessError, CompletedProcess, Popen, TimeoutExpired, run
import sys
from tempfile import NamedTemporaryFile
from typing import List
from urllib.parse import quote, unquote, urlparse

from rag_zk.utilities.path_helpers import path_is_file

logger = logging.getLogger(__name__)


# ### Parsing Helpers

# # All of these are (str) -> str
# # shortcuts to: https://docs.python.org/3/library/urllib.parse.html#url-parsing
# scheme = lambda url: urlparse(url).scheme.lower()
# without_scheme = lambda url: urlparse(url)._replace(scheme="").geturl().removeprefix("//").removesuffix("//")
# without_query = lambda url: urlparse(url)._replace(query="").geturl().removeprefix("//").removesuffix("//")
# without_fragment = lambda url: urlparse(url)._replace(fragment="").geturl().removeprefix("//").removesuffix("//")
# without_path = (
#     lambda url: urlparse(url)._replace(path="", fragment="", query="").geturl().removeprefix("//").removesuffix("//")
# )
# path = lambda url: urlparse(url).path
# basename = lambda url: urlparse(url).path.rsplit("/", 1)[-1]
# domain = lambda url: urlparse(url).netloc
# query = lambda url: urlparse(url).query
# fragment = lambda url: urlparse(url).fragment
# extension = lambda url: basename(url).rsplit(".", 1)[-1].lower() if "." in basename(url) else ""
# base_url = lambda url: without_scheme(url)  # uniq base url used to dedupe links

# without_www = lambda url: url.replace("://www.", "://", 1)
# without_trailing_slash = lambda url: url[:-1] if url[-1] == "/" else url.replace("/?", "?")
# # hashurl = lambda url: base32_encode(int(sha256(base_url(url).encode("utf-8")).hexdigest(), 16))[:20]

# urlencode = lambda s: s and quote(s, encoding="utf-8", errors="replace")
# urldecode = lambda s: s and unquote(s)
# htmlencode = lambda s: s and escape(s, quote=True)
# htmldecode = lambda s: s and unescape(s)

# short_ts = lambda ts: str(parse_date(ts).timestamp()).split(".")[0]
# ts_to_date_str = lambda ts: ts and parse_date(ts).strftime("%Y-%m-%d %H:%M")
# ts_to_iso = lambda ts: ts and parse_date(ts).isoformat()

# URL_REGEX = re.compile(
#     r"(?=("
#     r"http[s]?://"  # start matching from allowed schemes
#     r"(?:[a-zA-Z]|[0-9]"  # followed by allowed alphanum characters
#     r"|[-_$@.&+!*\(\),]"  #    or allowed symbols (keep hyphen first to match literal hyphen)
#     r"|(?:%[0-9a-fA-F][0-9a-fA-F]))"  #    or allowed unicode bytes
#     r'[^\]\[\(\)<>"\'\s]+'  # stop parsing at these symbols
#     r"))",
#     re.IGNORECASE,
# )

# STATICFILE_EXTENSIONS = {
#     # 99.999% of the time, URLs ending in these extensions are static files,
#     # and can be downloaded as-is, not html pages that need to be rendered
#     "gif", "jpeg", "jpg", "png", "tif", "tiff", "wbmp", "ico", "jng", "bmp",
#     "svg", "svgz", "webp", "ps", "eps", "ai", "mp3", "mp4", "m4a", "mpeg",
#     "mpg", "mkv", "mov", "webm", "m4v", "flv", "wmv", "avi", "ogg", "ts", "m3u8",
#     "pdf", "txt", "rtf", "rtfd", "doc", "docx", "ppt", "pptx", "xls", "xlsx",
#     "atom", "rss", "css", "js", "json", "dmg", "iso", "img", "rar", "war",
#     "hqx", "zip", "gz", "bz2", "7z",
#     # Less common extensions to consider adding later
#     # jar, swf, bin, com, exe, dll, deb
#     # ear, hqx, eot, wmlc, kml, kmz, cco, jardiff, jnlp, run, msi, msp, msm,
#     # pl pm, prc pdb, rar, rpm, sea, sit, tcl tk, der, pem, crt, xpi, xspf,
#     # ra, mng, asx, asf, 3gpp, 3gp, mid, midi, kar, jad, wml, htc, mml
#     # These are always treated as pages, not as static files, never add them:
#     # html, htm, shtml, xhtml, xml, aspx, php, cgi
# }  # fmt: skip


# def is_static_file(url: str):
#     """Determine whether file is static or requires rendering."""
#     # TODO: the proper way is with MIME type detection + ext, not only extension
#     return extension(url).lower() in STATICFILE_EXTENSIONS


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


def dedupe(options: List[str]) -> List[str]:
    """Deduplicate the given CLI args by key=value. Options that come later override earlier."""
    deduped = {}

    for option in options:
        key = option.split("=")[0]
        deduped[key] = option

    return list(deduped.values())


# def find_node_binary(binary: str) -> Path | None:
#     """Find path to specified node package binary."""
#     binary_path = NODE_BIN_PATH / binary
#     if binary_path.exists():
#         return binary_path.expanduser().resolve()

#     binary_path = shutil.which(binary)
#     if binary_path:
#         return Path(binary_path).expanduser().resolve()


# def find_chrome_binary() -> Path | None:
#     """Find any installed chrome binaries in the default locations."""
#     # Precedence: Chromium, Chrome, Beta, Canary, Unstable, Dev
#     # make sure data dir finding precedence order always matches binary finding order
#     default_executable_paths = (
#         "chromium-browser",
#         "chromium",
#         "/Applications/Chromium.app/Contents/MacOS/Chromium",
#         "chrome",
#         "google-chrome",
#         "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
#         # "google-chrome-stable",
#         # "google-chrome-beta",
#         # "google-chrome-canary",
#         # "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
#         # "google-chrome-unstable",
#         # "google-chrome-dev",
#     )
#     for name in default_executable_paths:
#         chrome_path = shutil.which(name)
#         if chrome_path:
#             return Path(chrome_path).expanduser().resolve()


# %%
# readability
# - https://github.com/mozilla/readability
# - https://github.com/ArchiveBox/ArchiveBox/blob/v0.7.2/archivebox/extractors/readability.py
#
# singlefile
# - https://github.com/gildas-lormeau/SingleFile
# - https://github.com/ArchiveBox/ArchiveBox/blob/v0.7.2/archivebox/extractors/singlefile.py

# POSTLIGHTPARSER_BINARY = find_node_binary("postlight-parser")
# READABILITY_BINARY = find_node_binary("readability-extractor")
# SINGLEFILE_BINARY = find_node_binary("single-file")


def download_file(url, filename, timeout: int = 600):
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

    try:
        with requests.get(url, stream=True, timeout=timeout) as response:
            # Raise an exception for bad status codes
            response.raise_for_status()

            # Get the total file size
            total_size = int(response.headers.get("content-length", 0))

            with (
                atomic_write(filename, is_binary=True) as f,
                tqdm(total=total_size, unit="iB", unit_scale=True, desc=filename) as progress_bar,
            ):
                for chunk in response.iter_content(chunk_size=8192):
                    size = f.write(chunk)
                    progress_bar.update(size)

        logger.info(f"File downloaded successfully: {filename}")

    except requests.exceptions.RequestException:
        logger.exception("Download failed")


# atomic write
# https://stackoverflow.com/questions/2333872/how-to-make-file-creation-an-atomic-operation
@contextmanager
def atomic_write(filepath: Path | str, is_binary: bool = False):
    """Write to temporary file object that atomically moves to destination upon exiting."""
    filepath = Path(filepath)

    dirpath, fname = filepath.parent, filepath.name
    dirpath.mkdir(parents=True, exist_ok=True)

    with NamedTemporaryFile(
        mode="wb" if is_binary else "w",
        dir=dirpath,
        prefix=fname,
        suffix=".tmp",
        delete_on_close=False,
    ) as tmp:
        try:
            yield tmp
        finally:
            tmp.flush()  # libc -> OS
            os.fsync(tmp.fileno())  # OS -> disc
        os.replace(tmp.name, filepath)


def validate_download(filepath: Path | str, min_bytes: int = 1) -> bool:
    """Validate that downloaded file exists and is of minimum size."""
    filepath = path_is_file(filepath)

    # Get file size
    file_size = filepath.stat().st_size

    # Check minimum size
    if file_size < min_bytes:
        logger.error(f"File is too small: found {file_size} bytes, expected at least {min_bytes} bytes.")
        return False

    return True


# def save_and_hash(filepath: Path | str, content: str):
#     """Save content to file and get file hash."""
#     filepath = Path(filepath)

#     with atomic_write(filepath) as f:
#         f.write(content)

#     with filepath.open("rb", buffering=0) as f:
#         content_hash = file_digest(f, "sha256").hexdigest()  # req. py>=3.11

#     return content_hash
