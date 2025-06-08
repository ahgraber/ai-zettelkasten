# ruff: NOQA: E731
import json
import logging
from pathlib import Path
import re
import typing as t
from typing import Any, Callable, List, Optional
from urllib.parse import parse_qs, quote, unquote, unquote_plus, urlencode, urljoin, urlparse, urlunparse

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

# Compiled regex patterns for performance
URL_PATTERN = re.compile(URL_REGEX, re.IGNORECASE | re.UNICODE)
ARXIV_PATTERN = re.compile(r"(?:arxiv.org/[a-z]+?/)(\d+\.\d+)", re.IGNORECASE)
EMERGENTMIND_PATTERN = re.compile(r"(?:emergentmind.com/papers/)(\d+\.\d+)", re.IGNORECASE)
HUGGINGFACE_PATTERN = re.compile(r"(?:huggingface.co/papers/)(\d+\.\d+)", re.IGNORECASE)
GITHUB_PATTERN = re.compile(
    r"/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)(?:/(?:refs/heads|blob|tree)/(?P<branch>[\w./-]+))?",
    re.IGNORECASE,
)
SAFELINKS_PATTERN = re.compile(r"safelinks\.protection\.outlook\.com/\?url=(.*?)&data=")
MD_ARTIFACTS_PATTERN = re.compile(r"\)\[[^\]\(]+?\]\(", re.IGNORECASE)

# Domain constants
SOCIAL_MEDIA_DOMAINS = frozenset(
    {"linkedin.com", "twitter.com", "x.com", "bsky.app", "facebook.com", "instagram.com", "threads.net"}
)

GITHUB_DOMAINS = frozenset({"github.com", "gist.github.com", "raw.githubusercontent.com"})


def extract_url(text: str) -> list[str]:
    """Identify urls (url-like strings) from text.

    Args:
        text: Input text to search for URLs

    Returns:
        List of URLs found in the text

    Examples:
        >>> extract_url("Visit https://example.com for more info")
        ['https://example.com']
    """
    if not text:
        return []

    matches = URL_PATTERN.findall(text)
    return matches


def validate_url(url: str) -> str:
    """Validate a URL.

    Args:
        url: URL string to validate

    Returns:
        Validated URL string

    Raises:
        ValidationError: If URL is invalid
        ValueError: If URL is empty or None
    """
    if not url or not url.strip():
        raise ValueError("URL cannot be empty or None")

    try:
        _url = HttpUrl(url.strip())
    except ValidationError:
        logger.exception("Invalid URL: %s", url)
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
    """Clean url after identification.

    Removes markdown-like artifacts from URLs that may have been introduced during parsing.

    Args:
        url: URL that may contain markdown artifacts

    Returns:
        Cleaned URL with artifacts removed

    Examples:
        >>> clean_md_artifacts("https://example.com)[–](other_url")
        "https://example.com"
    """
    if not url:
        return url

    # Sometimes urls have weird markdown-like artifacts
    # "...)[–](...,    ...)[—](...,    ...)['](...,    ...)['](...,    ...)[\\](...,    ...)[�](..."
    split_parts = MD_ARTIFACTS_PATTERN.split(url)
    return split_parts[0] if split_parts else url


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
    """Convert safelinks to original url.

    Args:
        url: URL that may be a safelink

    Returns:
        Decoded original URL or original URL if not a safelink

    Raises:
        ValueError: If safelink pattern is detected but URL cannot be extracted
    """
    safelinks_str = "safelinks.protection.outlook.com"  # typos:disable
    if safelinks_str not in url:
        return url

    # Try unquote first (for general URL decoding)
    try:
        decoded = unquote(url)
    except ValueError:
        # If unquote fails, try unquote_plus (for '+' encoding)
        try:
            decoded = unquote_plus(url)
        except ValueError:
            logger.warning(f"Failed to decode safelink URL: {url}")
            return url

    matches = SAFELINKS_PATTERN.findall(decoded)
    if matches:
        return matches[0]
    else:
        raise ValueError(f"Could not find safelinks url in {decoded}")


def follow_redirects(url: str, timeout: int = 5, max_redirects: int = 10) -> str:
    """Get the original URL after following redirections.

    Args:
        url: URL to follow redirects for
        timeout: Request timeout in seconds
        max_redirects: Maximum number of redirects to follow

    Returns:
        Final URL after following redirects, or original URL if failed

    Note:
        This function should be used carefully to avoid SSRF attacks.
        Consider validating the final URL domain before use.
    """
    try:
        # Validate URL scheme to prevent SSRF
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            logger.warning(f"Potentially unsafe URL scheme: {parsed.scheme}")
            return url

        # Use a session with custom adapter for better control
        session = requests.Session()
        session.max_redirects = max_redirects

        with session.get(
            url, timeout=timeout, allow_redirects=True, headers={"User-Agent": "AIZK URL Helper/1.0"}
        ) as response:
            response.raise_for_status()
            return response.url

    except requests.exceptions.RequestException as e:
        logger.debug(f"Failed to follow redirects from original URL {url}: {e}")
        return url


def emergentmind_to_arxiv(url: str) -> str:
    """Convert emergentmind links to arxiv.org.

    Args:
        url: URL to potentially convert

    Returns:
        Converted arXiv URL or original URL if no conversion needed
    """
    if matches := EMERGENTMIND_PATTERN.findall(url):
        return f"https://arxiv.org/abs/{matches[0]}"
    return url


def huggingface_to_arxiv(url: str) -> str:
    """Convert huggingface papers links to arxiv.org.

    Args:
        url: URL to potentially convert

    Returns:
        Converted arXiv URL or original URL if no conversion needed
    """
    if matches := HUGGINGFACE_PATTERN.findall(url):
        return f"https://arxiv.org/abs/{matches[0]}"
    return url


def standardize_arxiv(url: str) -> str:
    """Point to standard arxiv abstract pages.

    Args:
        url: URL to potentially standardize

    Returns:
        Standardized arXiv URL or original URL if no conversion needed
    """
    if matches := ARXIV_PATTERN.findall(url):
        return f"https://arxiv.org/abs/{matches[0]}"
    return url


def standardize_github(url: str) -> str:
    """Point to repository root if possible.

    This attempts to retain any branch and/or file specification that exists,
    but may fail if commit links are given.

    Args:
        url: GitHub URL to standardize

    Returns:
        Standardized GitHub URL or original URL if no conversion needed
    """
    if not any(domain in url for domain in ["githubusercontent.com", "github.com"]):
        return url

    parsed = urlparse(url)
    match = GITHUB_PATTERN.match(parsed.path)

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
    else:
        # Return original URL if no specific handling is needed
        return url


def _process_url(url: str, processors: Optional[List[Callable[[str], str]]] = None) -> str:
    """Apply a list of processors to a URL.

    Args:
        url: The URL to process
        processors: List of processor functions to apply. Defaults to standard processors.

    Returns:
        The processed URL
    """
    if processors is None:
        processors = [
            fix_url_from_markdown,
            safelink_to_url,
            strip_utm_params,
            emergentmind_to_arxiv,
            huggingface_to_arxiv,
            standardize_arxiv,
            standardize_github,
        ]

    for processor in processors:
        url = processor(url)
    return url


def find_all_urls(urls_str: str) -> t.Generator[str, None, None]:
    """Find all urls in text blob.

    Args:
        urls_str: Text string to search for URLs

    Yields:
        Processed URLs found in the text

    Examples:
        >>> list(find_all_urls("Check out https://example.com and http://test.org"))
        ['https://example.com', 'http://test.org']
    """
    if not urls_str:
        return

    for url in extract_url(urls_str):
        try:
            processed_url = _process_url(url)
            if processed_url:  # Only yield non-empty URLs
                yield processed_url
        except Exception as e:
            logger.warning("Failed to process URL '%s': %s", url, e)
            # Yield original URL if processing fails
            yield url


def is_social_url(url: str) -> bool:
    """Determine whether the url is social media.

    Most social media requires login; it is insecure to have AIZK use personal logins.

    Args:
        url: URL to check

    Returns:
        True if URL is from a social media domain, False otherwise
    """
    if not url:
        return False

    try:
        parsed = urlparse(url)
    except Exception:
        return False
    else:
        return parsed.netloc in SOCIAL_MEDIA_DOMAINS


def is_github_url(url: str) -> bool:
    """Determine whether url is a github property.

    This is for use with the gitingest parser; github.io links are treated as normal webpages.

    Args:
        url: URL to check

    Returns:
        True if URL is from a GitHub domain, False otherwise
    """
    if not url:
        return False

    try:
        parsed = urlparse(url)
    except Exception:
        return False
    else:
        return parsed.netloc in GITHUB_DOMAINS
