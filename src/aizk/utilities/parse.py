# ruff: NOQA: E731
from datetime import datetime, timezone
from functools import wraps
from hashlib import sha256
from html import escape, unescape
import http.cookiejar
from inspect import signature
import json as pyjson
from pathlib import Path
import re
import typing as t
from typing import Any, Callable, List, Optional
from urllib.parse import quote, unquote, urlparse

from base32_crockford import encode as base32_encode  # type: ignore
from dateparser import parse as dateparser

try:
    import chardet  # type:ignore

    detect_encoding = lambda rawdata: chardet.detect(rawdata)["encoding"]
except ImportError:
    detect_encoding = lambda rawdata: "utf-8"


### Parsing Helpers

# All of these are (str) -> str
# shortcuts to: https://docs.python.org/3/library/urllib.parse.html#url-parsing
scheme = lambda url: urlparse(url).scheme.lower()
without_scheme = lambda url: urlparse(url)._replace(scheme="").geturl().removeprefix("//").removesuffix("//")
without_query = lambda url: urlparse(url)._replace(query="").geturl().removeprefix("//").removesuffix("//")
without_fragment = lambda url: urlparse(url)._replace(fragment="").geturl().removeprefix("//").removesuffix("//")
without_path = (
    lambda url: urlparse(url)._replace(path="", fragment="", query="").geturl().removeprefix("//").removesuffix("//")
)
path = lambda url: urlparse(url).path
basename = lambda url: urlparse(url).path.rsplit("/", 1)[-1]
domain = lambda url: urlparse(url).netloc
query = lambda url: urlparse(url).query
fragment = lambda url: urlparse(url).fragment
extension = lambda url: basename(url).rsplit(".", 1)[-1].lower() if "." in basename(url) else ""
base_url = lambda url: without_scheme(url)  # uniq base url used to dedupe links

without_www = lambda url: url.replace("://www.", "://", 1)
without_trailing_slash = lambda url: url[:-1] if url[-1] == "/" else url.replace("/?", "?")
hashurl = lambda url: base32_encode(int(sha256(base_url(url).encode("utf-8")).hexdigest(), 16))[:20]

urlencode = lambda s: s and quote(s, encoding="utf-8", errors="replace")
urldecode = lambda s: s and unquote(s)
htmlencode = lambda s: s and escape(s, quote=True)
htmldecode = lambda s: s and unescape(s)

short_ts = lambda ts: str(parse_date(ts).timestamp()).split(".")[0]
ts_to_date_str = lambda ts: ts and parse_date(ts).strftime("%Y-%m-%d %H:%M")
ts_to_iso = lambda ts: ts and parse_date(ts).isoformat()

COLOR_REGEX = re.compile(r"\[(?P<arg_1>\d+)(;(?P<arg_2>\d+)(;(?P<arg_3>\d+))?)?m")


# https://mathiasbynens.be/demo/url-regex
URL_REGEX = re.compile(
    r"(?=("
    r"http[s]?://"  # start matching from allowed schemes
    r"(?:[a-zA-Z]|[0-9]"  # followed by allowed alphanum characters
    r"|[-_$@.&+!*\(\),]"  #   or allowed symbols (keep hyphen first to match literal hyphen)
    r"|[^\u0000-\u007F])+"  #   or allowed unicode bytes
    r'[^\]\[<>"\'\s]+'  # stop parsing at these symbols
    r"))",
    re.IGNORECASE | re.UNICODE,
)


def parens_are_matched(string: str, open_char="(", close_char=")"):
    """Check that all parentheses in a string are balanced and nested properly."""
    count = 0
    for c in string:
        if c == open_char:
            count += 1
        elif c == close_char:
            count -= 1
        if count < 0:
            return False
    return count == 0


def fix_url_from_markdown(url_str: str) -> str:
    """Clean up a regex-parsed url that may contain dangling trailing parens from markdown link syntax.

    helpful to fix URLs parsed from markdown e.g.
      input:  https://wikipedia.org/en/some_article_(Disambiguation).html?abc=def).somemoretext
      result: https://wikipedia.org/en/some_article_(Disambiguation).html?abc=def

    IMPORTANT ASSUMPTION: valid urls wont have unbalanced or incorrectly nested parentheses
    e.g. this will fail the user actually wants to ingest a url like 'https://example.com/some_wei)(rd_url'
         in that case it will return https://example.com/some_wei (truncated up to the first unbalanced paren)
    This assumption is true 99.9999% of the time, and for the rare edge case the user can use url_list parser.
    """
    trimmed_url = url_str

    # cut off one trailing character at a time
    # until parens are balanced e.g. /a(b)c).x(y)z -> /a(b)c
    while not parens_are_matched(trimmed_url):
        trimmed_url = trimmed_url[:-1]

    # make sure trimmed url is still valid
    if re.findall(URL_REGEX, trimmed_url):
        return trimmed_url

    return url_str


def find_all_urls(urls_str: str):
    """Find all urls in text blob."""
    for url in re.findall(URL_REGEX, urls_str):
        yield fix_url_from_markdown(url)


def is_static_file(url: str):
    """Determine whether file is static or requires rendering."""
    # TODO: the proper way is with MIME type detection + ext, not only extension
    STATICFILE_EXTENSIONS = { # NOQA:N806
        # 99.999% of the time, URLs ending in these extensions are static files,
        # and can be downloaded as-is, not html pages that need to be rendered
        "gif", "jpeg", "jpg", "png", "tif", "tiff", "wbmp", "ico", "jng", "bmp",
        "svg", "svgz", "webp", "ps", "eps", "ai", "mp3", "mp4", "m4a", "mpeg",
        "mpg", "mkv", "mov", "webm", "m4v", "flv", "wmv", "avi", "ogg", "ts", "m3u8",
        "pdf", "txt", "rtf", "rtfd", "doc", "docx", "ppt", "pptx", "xls", "xlsx",
        "atom", "rss", "css", "js", "json", "dmg", "iso", "img", "rar", "war",
        "hqx", "zip", "gz", "bz2", "7z",
        # Less common extensions to consider adding later
        # jar, swf, bin, com, exe, dll, deb
        # ear, hqx, eot, wmlc, kml, kmz, cco, jardiff, jnlp, run, msi, msp, msm,
        # pl pm, prc pdb, rar, rpm, sea, sit, tcl tk, der, pem, crt, xpi, xspf,
        # ra, mng, asx, asf, 3gpp, 3gp, mid, midi, kar, jad, wml, htc, mml
        # These are always treated as pages, not as static files, never add them:
        # html, htm, shtml, xhtml, xml, aspx, php, cgi
    }  # fmt: skip
    return extension(url).lower() in STATICFILE_EXTENSIONS


def parse_date(date: t.Any) -> datetime:
    """Parse unix timestamps, iso format, and human-readable strings."""
    if date is None:
        return None  # type: ignore

    if isinstance(date, datetime):
        if date.tzinfo is None:
            return date.replace(tzinfo=timezone.utc)

        if date.tzinfo.utcoffset(datetime.now()).seconds != 0:
            raise ValueError("Refusing to load a non-UTC date!")
        return date

    if isinstance(date, (float, int)):
        date = str(date)

    if isinstance(date, str):
        return dateparser(date, settings={"TIMEZONE": "UTC"}).astimezone(timezone.utc)

    raise ValueError("Tried to parse invalid date! {}".format(date))
