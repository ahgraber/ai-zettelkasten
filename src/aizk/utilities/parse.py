# ruff: NOQA: E731
import json
import logging
from pathlib import Path
import re
import typing as t
from typing import Any, Callable, List, Optional
from urllib.parse import quote, unquote, urlparse

from pydantic import HttpUrl, ValidationError

logger = logging.getLogger(__name__)


def detect_encoding(rawdata: bytes) -> str:
    """Detect the encoding of a byte string."""
    import chardet

    encoding = chardet.detect(rawdata)
    logger.info(encoding)
    return encoding["encoding"] or "utf-8"


# https://mathiasbynens.be/demo/url-regex
# https://gist.github.com/dperini/729294
URL_REGEX = (
    r"(?:http|ftp)s?://"  # http:// or https://
    r"(?![-_])(?:[-\w\u00a1-\uffff]{0,63}[^-_]\.)+"  # domain...
    r"(?:[a-z\u00a1-\uffff]{2,}\.?)"  # tld
    r"(?:[/?#]\S*)?"  # path
    # r"(?:[/?#]\S+?)?"  # path  uses +? to match as few as possible
)


def extract_url(text: str) -> list[str]:
    """Identify urls (url-like strings) from text."""
    pattern = re.compile(URL_REGEX, re.IGNORECASE | re.UNICODE)
    matches = re.findall(pattern, text)
    return matches


def validate_url(url: str) -> str:
    """Validate a URL."""
    try:
        _url = HttpUrl(url)
    except ValidationError:
        logger.exception(f"Invalid URL: {url}")
        raise

    return str(_url)


def extract_md_url(text: str) -> list[tuple[str, str]]:
    """Identify markdown-style urls (i.e., [title](url) ) and extract (title, url)."""
    pattern = re.compile(
        r"(?:\[|\\\[)"  # initial '['
        r"([\s\S]*?)"  # title text
        r"(?:(?:\]|\\\])\()"  # middle ']('
        f"({URL_REGEX})"  # url
        r"(?:\))",  # final ')'
        re.IGNORECASE | re.UNICODE,
    )
    # Find all matches
    matches = re.findall(pattern, text)

    # # clean multispaces / newlines
    matches = [(" ".join(title.split()), url) for title, url in matches]

    return matches


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
    while not check_matched_pairs(trimmed_url):
        trimmed_url = trimmed_url[:-1]

    # make sure trimmed url is still valid
    if extract_url(trimmed_url):
        return trimmed_url

    return url_str


def find_all_urls(urls_str: str):
    """Find all urls in text blob."""
    for url in extract_url(urls_str):
        yield fix_url_from_markdown(url)


def validate_arxiv_url(url: str) -> str:
    """Validate arXiv URL."""
    if "arxiv.org" not in url:
        raise ValueError("URL must be from arXiv.org")

    try:
        _url = HttpUrl(url)
    except ValidationError:
        logger.exception(f"Invalid URL: {url}")
        raise

    if not (_url.path.startswith("/pdf") or _url.path.startswith("/abs") or _url.path.startswith("/html")):
        raise ValueError("URL must be to PDF, abstract, or HTML page")
    else:
        return str(_url)


def check_matched_pairs(string: str, open_char="(", close_char=")"):
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


def extract_json(text: str) -> str:
    """Identify json from a text blob by matching '[]' or '{}'.

    Warning: This will identify the first json structure!
    """
    # check for markdown indicator; if present, start there
    md_json_idx = text.find("```json")
    if md_json_idx != -1:
        text = text[md_json_idx:]

    # search for json delimiter pairs
    left_bracket_idx = text.find("[")
    left_brace_idx = text.find("{")

    indices = [idx for idx in (left_bracket_idx, left_brace_idx) if idx != -1]
    start_idx = min(indices) if indices else None

    # If no delimiter found, return the original text
    if start_idx is None:
        return text

    # Identify the exterior delimiters defining JSON
    open_char = text[start_idx]
    close_char = "]" if open_char == "[" else "}"

    # Initialize a count to keep track of delimiter pairs
    count = 0
    for i, char in enumerate(text[start_idx:], start=start_idx):
        if char == open_char:
            count += 1
        elif char == close_char:
            count -= 1

        # When count returns to zero, we've found a complete structure
        if count == 0:
            return text[start_idx : i + 1]

    return text  # In case of unbalanced JSON, return the original text


# def parse_date(date: t.Any) -> datetime:
#     """Parse unix timestamps, iso format, and human-readable strings."""
#     if date is None:
#         return None  # type: ignore

#     if isinstance(date, datetime):
#         if date.tzinfo is None:
#             return date.replace(tzinfo=timezone.utc)

#         if date.tzinfo.utcoffset(datetime.now()).seconds != 0:
#             raise ValueError("Refusing to load a non-UTC date!")
#         return date

#     if isinstance(date, (float, int)):
#         date = str(date)

#     if isinstance(date, str):
#         return dateparser(date, settings={"TIMEZONE": "UTC"}).astimezone(timezone.utc)

#     raise ValueError("Tried to parse invalid date! {}".format(date))
