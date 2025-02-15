# ruff: NOQA: E731
import json
import logging
from pathlib import Path
import re
import typing as t
from typing import Any, Callable, List, Optional
from urllib.parse import parse_qs, unquote, unquote_plus, urlencode, urljoin, urlparse, urlunparse

from pydantic import HttpUrl, ValidationError

import requests

from aizk.utilities.parse import check_matched_pairs

logger = logging.getLogger(__name__)

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


def clean_link_title(title: str) -> str:
    r"""Clean titles.

    Some titles still need cleaning after parsing:
    "There's An AI: The Best AI Tools Directory\\]([https://theresanai.com/" --> "There's An AI: The Best AI Tools Directory"
    "\\[2407.20516\\] Machine Unlearning in Generative AI: A Survey\\]([https://arxiv.org/abs/2407.20516" --> "[2407.20516] Machine Unlearning in Generative AI: A Survey
    """
    # replace extra escapes
    title = title.replace("\\", "")
    # split on possible markdown-url divider ']('
    title = title.split("](")[0]

    return title


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


def clean_md_artifacts(url: str) -> str:
    """Clean url after identification."""
    # sometimes urls have weird markdown-like artifacts
    # "...)[–](...,    ...)[—](...,    ...)['](...,    ...)['](...,    ...)[\\](...,    ...)[�](..."
    # _split = re.split(r"\)\[[^\w\d]+?\]\(", url, flags=re.IGNORECASE)
    _split = re.split(r"\)\[[^\]\(]+?\]\(", url, flags=re.IGNORECASE)
    if _split:
        return _split[0]
    else:
        return url


def strip_utm_params(url: str) -> str:
    """Strip utm parameters from URL."""
    parsed = urlparse(url)

    params = parse_qs(parsed.query)
    cleaned_params = {k: v[0] for k, v in params.items() if not k.startswith("utm_")}
    cleaned = urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(cleaned_params),
            parsed.fragment,
        )
    )
    if cleaned != url:
        logger.debug(f"Stripped URL params: {url} -> {cleaned}")
        return cleaned
    else:
        return url


def safelink_to_url(url: str) -> str:
    """Convert safelinks to original url."""
    safelinks_str = "safelinks.protection.outlook.com"  # typos:disable
    if safelinks_str not in url:
        return url
    else:
        # Try unquote first (for general URL decoding)
        try:
            decoded = unquote(url)
        except ValueError:
            # If unquote fails, try unquote_plus (for '+' encoding)
            decoded = unquote_plus(url)

        pattern = re.compile(f"{safelinks_str}\\/\\?url=(.*?)&data=")
        matches = re.findall(pattern, decoded)

        if matches:
            return matches[0]
        else:
            raise ValueError(f"Could not find safelinks url in {decoded}")


def follow_redirects(url: str, timeout: int = 5) -> str:
    """Get the original URL after following redirections."""
    try:
        with requests.get(url, timeout=timeout, allow_redirects=True) as response:
            response.raise_for_status()
            return response.url

    except requests.exceptions.RequestException:
        logger.debug(f"Failed to follow redirects from original URL {url}")
        # raise
        return url


def emergentmind_to_arxiv(url: str) -> str:
    """Convert emergentmind links to arxiv.org."""
    pattern = re.compile(r"(?:emergentmind.com/papers/)(\d+\.\d+)", re.IGNORECASE)
    if matches := re.findall(pattern, url):
        return f"https://arxiv.org/abs/{matches[0]}"
    else:
        return url


def huggingface_to_arxiv(url: str) -> str:
    """Convert huggingface papers links to arxiv.org."""
    pattern = re.compile(r"(?:huggingface.co/papers/)(\d+\.\d+)", re.IGNORECASE)
    if matches := re.findall(pattern, url):
        return f"https://arxiv.org/abs/{matches[0]}"
    else:
        return url


def standardize_arxiv(url: str) -> str:
    """Point to standard arxiv abstract pages."""
    pattern = re.compile(r"(?:arxiv.org/[a-z]+?/)(\d+\.\d+)", re.IGNORECASE)
    if matches := re.findall(pattern, url):
        return f"https://arxiv.org/abs/{matches[0]}"
    else:
        return url


def standardize_github(url: str) -> str:
    """Point to repository root if possible.

    This attempts to retain any branch and/or file specification that exists, but may fail if commit links are given.
    """
    if not any(domain in url for domain in ["githubusercontent.com", "github.com"]):
        return url

    pattern = re.compile(
        r"/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)(?:/(?:refs/heads|blob|tree)/(?P<branch>[\w./-]+))?",
        re.IGNORECASE,
    )

    parsed = urlparse(url)
    match = pattern.match(parsed.path)

    if not match or not match.group("owner") or not match.group("repo"):
        return url

    if parsed.netloc == "gist.github.com":
        return urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                f"{match.group('owner')}/{match.group('repo')}",
                None,  # params
                None,  # query
                None,  # fragment
            )
        )
    elif parsed.netloc == "raw.githubusercontent.com":
        return urlunparse(
            (
                parsed.scheme,
                "github.com",
                f"{match.group('owner')}/{match.group('repo')}"
                + (f"/tree/{match.group('branch')}" if match.group("branch") else ""),
                None,  # params
                None,  # query
                None,  # fragment
            )
        )
    elif parsed.netloc == "github.com":
        return urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                f"{match.group('owner')}/{match.group('repo')}"
                + (f"/tree/{match.group('branch')}" if match.group("branch") else ""),
                None,  # params
                None,  # query
                None,  # fragment
            )
        )


processors = [
    fix_url_from_markdown,
    # clean_md_artifacts,
    safelink_to_url,
    # follow_redirects,
    strip_utm_params,
    emergentmind_to_arxiv,
    huggingface_to_arxiv,
    standardize_arxiv,
    standardize_github,
]


def _process_url(url: str, processors: List[Callable[[str], str]] = processors) -> str:
    """Apply a list of processors to a URL."""
    for processor in processors:
        url = processor(url)
    return url


def find_all_urls(urls_str: str):
    """Find all urls in text blob."""
    for url in extract_url(urls_str):
        yield _process_url(url)


# def validate_arxiv_url(url: str) -> str:
#     """Validate arXiv URL."""
#     if "arxiv.org" not in url:
#         raise ValueError("URL must be from arXiv.org")

#     try:
#         _url = HttpUrl(url)
#     except ValidationError:
#         logger.exception(f"Invalid URL: {url}")
#         raise

#     if not (_url.path.startswith("/pdf") or _url.path.startswith("/abs") or _url.path.startswith("/html")):
#         raise ValueError("URL must be to PDF, abstract, or HTML page")
#     else:
#         return str(_url)


def is_social_url(url: str) -> bool:
    """Determine whether the url is social media.

    Most social media requires login; it is insecure to have AIZK use personal logins.
    """
    socials = {"linkedin.com", "twitter.com", "x.com", "bsky.app", "facebook.com", "instagram.com", "threads.net"}

    return urlparse(url).netloc in socials


def is_github_url(url: str) -> bool:
    """Determine whether url is a github property.

    This is for use with the gitingest parser; github.io links are treated as normal webpages.
    """
    github = {"github.com", "gist.github.com", "raw.githubusercontent.com"}

    return urlparse(url).netloc in github
