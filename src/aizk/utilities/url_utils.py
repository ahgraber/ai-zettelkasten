# ruff: NOQA: E731
import asyncio
from collections.abc import Callable
import logging
import os
import random
import re
from typing import Generator, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, unquote_plus, urlencode, urljoin, urlparse, urlunparse

import httpx
from pydantic import HttpUrl, ValidationError as PydanticValidationError
import validators
from validators import ValidationError as URLValidatorValidationError

import requests

from aizk.utilities.parse import check_balanced_brackets
from aizk.utilities.process import temp_env_var

logger = logging.getLogger(__name__)

# https://mathiasbynens.be/demo/url-regex
# https://gist.github.com/dperini/729294
# Validated URL regex - DO NOT CHANGE
URL_REGEX = (
    r"(?:http|ftp)s?://"  # http:// or https://
    r"(?:\S+(?::\S*)?@)?"  # optional user:pass@
    r"(?![-_])(?:[-\w\u00a1-\uffff]{0,63}[^-_]\.)+"  # domain...
    r"(?:[a-z\u00a1-\uffff]{2,}\.?)"  # tld
    r"(?:[/?#]\S*)?"  # path
)

# Domain constants for URL detection
SOCIAL_MEDIA_DOMAINS = frozenset(
    {"linkedin.com", "twitter.com", "x.com", "bsky.app", "facebook.com", "instagram.com", "threads.net"}
)

GITHUB_DOMAINS = frozenset({"github.com", "gist.github.com", "raw.githubusercontent.com"})

ARXIV_DOMAINS = frozenset({"arxiv.org", "export.arxiv.org"})


# --- Core URL Extraction ----------------------------------------------------
def extract_domain(url: str) -> str:
    """Extract the domain from a URL.

    Args:
        url: The URL to extract domain from

    Returns:
        The domain portion of the URL (e.g., "example.com")

    Raises:
        ValueError: If the URL is invalid or has no domain
    """
    try:
        parsed = urlparse(url)
    except Exception as e:
        raise ValueError(f"Invalid URL: {url}") from e
    else:
        if not parsed.netloc:
            raise ValueError(f"No domain found in URL: {url}")
        return parsed.netloc.lower()


def fix_url_from_markdown(url: str) -> str:
    """Clean up URLs that may have dangling parens from markdown parsing."""
    # Remove trailing characters until brackets are balanced
    fixed = url.strip()
    while not check_balanced_brackets(fixed):
        fixed = fixed[:-1]

    fixed = fixed.rstrip(".,;:!'`*")

    # Verify the trimmed URL is still valid
    try:
        validate_url(fixed)
    except (PydanticValidationError, URLValidatorValidationError, ValueError):
        return url
    else:
        return fixed


def clean_markdown_title(title: str) -> str:
    """Clean up a markdown title by removing unwanted characters."""
    if not title:
        raise ValueError("Title cannot be empty")

    # leading/trailing whitespace
    title = title.strip()

    # replace multiple spaces with a single space
    title = re.sub(r"\s+", " ", title).strip()

    # # Split on possible markdown-url divider ']('
    # match = re.search(r"^(?:\[)?(.*?)\]\(", title)
    # if match:
    #     title = match.group(1)

    # Replace extra escapes
    title = title.replace("\\", "")

    # any surrounding brackets
    if title.startswith("[") and title.endswith("]"):
        title = title[1:-1]

    return title


def extract_urls(text: str) -> List[str]:
    """Extract all URLs from text using the validated URL_REGEX."""
    if not text:
        raise ValueError("Text cannot be empty")

    pattern = re.compile(URL_REGEX, re.IGNORECASE | re.UNICODE)
    urls = pattern.findall(text)
    urls = [fix_url_from_markdown(url) for url in urls if url.strip()]

    return urls


def validate_url(url: str) -> str:
    """Validate a URL.

    Args:
        url: The URL string to validate

    Returns:
    -------
        str: Validated and normalized URL
    """
    if not url or url.strip() == "":
        raise ValueError("URL cannot be empty")

    # First check if URL matches our regex pattern
    pattern = re.compile(URL_REGEX, re.IGNORECASE | re.UNICODE)
    if not pattern.match(url.strip()):
        raise ValueError(f"URL {url} does not match expected pattern")

    validated = HttpUrl(url)
    url = str(validated)

    # ref: https://github.com/python-validators/validators/issues/139
    with temp_env_var("RAISE_VALIDATION_ERROR", "True"):
        _ = validators.url(url)

    return url


def extract_markdown_urls(text: str) -> List[Tuple[Optional[str], str]]:
    """Extract all URLs from markdown text, returning (title, url) pairs.

    Returns:
    -------
        List of (title, url) tuples where:
        - title is the link text for markdown links [title](url)
        - title is empty string for plain URLs
    """
    pattern = re.compile(URL_REGEX, re.IGNORECASE | re.UNICODE).pattern

    if not text:
        raise ValueError("Text cannot be empty")

    results = []

    # Inline markdown links: [title](url)
    inline_link_pattern = re.compile(
        rf'\[(?P<title>(?:\\.|[^\[\]])+?)\]\((?P<url>{pattern})(?:\s+"[^"]*")?\)', re.UNICODE
    )
    results += [(m.group("title"), m.group("url")) for m in inline_link_pattern.finditer(text)]

    # Reference link definitions: [ref]: url
    ref_def_pattern = re.compile(rf"^\s{0, 3}\[(?P<ref>[^\]]+)\]:\s*(?P<url>{pattern})", re.MULTILINE)
    ref_map = {m.group("ref"): m.group("url") for m in ref_def_pattern.finditer(text)}

    # Reference links: [title][ref]
    reference_link_pattern = re.compile(r"\[(?P<title>(?:\\.|[^\[\]])+?)\]\[(?P<ref>[^\[\]]+)\]")
    for m in reference_link_pattern.finditer(text):
        ref = m.group("ref")
        url = ref_map.get(ref)
        if url:
            results.append((m.group("title"), url))

    # Raw URLs in angle brackets: <https://example.com>
    angle_bracket_pattern = re.compile(rf"<(?P<url>{pattern})>")
    results += [(None, m.group("url")) for m in angle_bracket_pattern.finditer(text)]

    # HTML <a href=""> tags
    html_link_pattern = re.compile(
        rf'<a\s+[^>]*href=[\'"](?P<url>{pattern})[\'"][^>]*>(?P<title>.*?)</a>', re.IGNORECASE
    )
    results += [(m.group("title"), m.group("url")) for m in html_link_pattern.finditer(text)]

    results = [(clean_markdown_title(title) if title else None, fix_url_from_markdown(url)) for title, url in results]
    return results


# --- URL Detection/Classification -------------------------------------------
def is_social_url(url: str) -> bool:
    """Check if URL is from a social media domain."""
    validated = validate_url(url)

    try:
        parsed = urlparse(str(validated))
    except Exception:
        return False
    else:
        return parsed.netloc in SOCIAL_MEDIA_DOMAINS


def is_github_url(url: str) -> bool:
    """Check if URL is from a GitHub domain."""
    validated = validate_url(url)

    try:
        parsed = urlparse(str(validated))
    except Exception:
        return False
    else:
        return parsed.netloc in GITHUB_DOMAINS


def is_arxiv_url(url: str) -> bool:
    """Check if URL is from arXiv.org."""
    validated = validate_url(url)

    try:
        parsed = urlparse(str(validated))
    except Exception:
        return False
    else:
        return parsed.netloc in ARXIV_DOMAINS


# --- arXiv Utilities --------------------------------------------------------
def validate_arxiv_url(url: str) -> str:
    """Validate arXiv URL."""
    if "arxiv.org" not in url:
        raise ValueError("URL must be from arXiv.org")

    validated = validate_url(url)
    parsed = urlparse(validated)

    if parsed.path and not (
        parsed.path.startswith("/pdf") or parsed.path.startswith("/abs") or parsed.path.startswith("/html")
    ):
        raise ValueError("URL must be to PDF, abstract, or HTML page")

    return validated


def get_arxiv_id(url: str) -> str:
    """Extract arXiv ID from URL."""
    url = validate_arxiv_url(url)
    path = urlparse(url).path

    # arXiv ID pattern
    arxiv_id_regex = re.compile(r"([0-2])([0-9])(0|1)([0-9])\.[0-9]{4,5}(v[0-9]{1,2})?", re.IGNORECASE)
    match = re.search(arxiv_id_regex, path)
    if match:
        return match[0]
    else:
        raise ValueError(f"Could not find arXiv ID in {url}.")


def arxiv_abs_url(arxiv_id: str, use_export_url: bool = True) -> str:
    """Convert arXiv ID to abstract URL."""
    base_url = "http://export.arxiv.org/" if use_export_url else "https://arxiv.org/"
    return urljoin(base_url, f"abs/{arxiv_id}")


def arxiv_pdf_url(arxiv_id: str, use_export_url: bool = True) -> str:
    """Convert arXiv ID to PDF URL."""
    base_url = "http://export.arxiv.org/" if use_export_url else "https://arxiv.org/"
    return urljoin(base_url, f"pdf/{arxiv_id}")


def arxiv_html_url(arxiv_id: str, use_export_url: bool = True) -> str:
    """Convert arXiv ID to HTML URL."""
    base_url = "http://export.arxiv.org/" if use_export_url else "https://arxiv.org/"
    return urljoin(base_url, f"html/{arxiv_id}")


def to_arxiv_export_url(url: str) -> str:
    """Convert arXiv URL to export URL."""
    if not is_arxiv_url(url):
        raise ValueError("URL must be from arXiv.org")

    return re.sub(r"https?://arxiv\.org", "http://export.arxiv.org", url)


async def arxiv_title(arxiv_id: str, timeout: int = 30) -> str:
    """Extract the title from an arXiv abstract page.

    Args:
        arxiv_id: The arXiv paper ID (e.g., "2301.07041")
        timeout: Request timeout in seconds

    Returns:
        The cleaned paper title as a string

    Raises:
        ValueError: If arxiv_id is empty or invalid
        httpx.RequestError: If the HTTP request fails
        RuntimeError: If the title cannot be extracted from the page
    """
    from bs4 import BeautifulSoup

    if not arxiv_id or not arxiv_id.strip():
        raise ValueError("arXiv ID cannot be empty")

    url = arxiv_abs_url(arxiv_id)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            await asyncio.sleep(random.uniform(1, 3))  # Random delay to avoid appearing as a crawler
            response = await client.get(url)
            response.raise_for_status()
    except httpx.RequestError:
        logger.exception("Failed to fetch arXiv page for ID %s", arxiv_id)
        raise

    soup = BeautifulSoup(response.text, "html.parser")

    # Find the title element
    title_element = soup.find("h1", class_="title mathjax")
    if title_element is None:
        raise RuntimeError(f"Could not find title element on arXiv page for ID {arxiv_id}")

    # Extract text content from the element
    title_text = title_element.get_text(strip=True)
    if not title_text:
        raise RuntimeError(f"Could not extract title text from arXiv page for ID {arxiv_id}")

    # Remove common prefixes like "Title:" if present
    title = title_text[6:].strip() if title_text.lower().startswith("title:") else title_text

    if not title:
        raise RuntimeError(f"Extracted title is empty for arXiv ID {arxiv_id}")

    return title


# --- URL Processing/Standardization -----------------------------------------
def strip_utm_params(url: str) -> str:
    """Remove UTM tracking parameters from URL."""
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
        logger.debug(f"Stripped UTM params: {url} -> {cleaned}")

    return cleaned


def safelink_to_url(url: str) -> str:
    """Decode Microsoft SafeLinks URLs to original URLs."""
    safelinks_str = "safelinks.protection.outlook.com"
    if safelinks_str not in url:
        return url

    try:
        decoded = unquote(url)
    except ValueError:
        decoded = unquote_plus(url)

    pattern = re.compile(f"{safelinks_str}\\/\\?url=(.*?)&data=")
    matches = pattern.findall(decoded)

    if matches:
        return matches[0]
    else:
        raise ValueError(f"Could not extract URL from SafeLink: {decoded}")


def _emergentmind_to_arxiv(url: str, use_export_url: bool = True) -> str:
    """Convert emergentmind links to arxiv.org."""
    pattern = re.compile(r"(?:emergentmind.com/papers/)(\d+\.\d+)", re.IGNORECASE)
    if matches := re.findall(pattern, url):
        url = arxiv_abs_url(matches[0], use_export_url=use_export_url)

    return url


def _huggingface_to_arxiv(url: str, use_export_url: bool = True) -> str:
    """Convert huggingface papers links to arxiv.org."""
    pattern = re.compile(r"(?:huggingface.co/papers/)(\d+\.\d+)", re.IGNORECASE)
    if matches := re.findall(pattern, url):
        return arxiv_abs_url(matches[0], use_export_url=use_export_url)
    else:
        return url


def convert_paper_urls_to_arxiv(url: str, use_export_url: bool = True) -> str:
    """Convert paper URLs from various platforms to arXiv."""
    # Hugging Face papers
    url = _emergentmind_to_arxiv(url, use_export_url)
    url = _huggingface_to_arxiv(url, use_export_url)
    return url


def standardize_arxiv(url: str, use_export_url: bool = True) -> str:
    """Standardize arXiv URLs to abstract pages."""
    pattern = re.compile(r"(?:arxiv.org/[a-z]+?/)(\d+\.\d+)", re.IGNORECASE)
    matches = pattern.findall(url)
    if matches:
        return arxiv_abs_url(matches[0], use_export_url=use_export_url)
    return url


def standardize_github(url: str) -> str:
    """Standardize GitHub URLs to repository root when possible."""
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

    owner = match.group("owner")
    repo = match.group("repo")
    branch = match.group("branch")

    if parsed.netloc == "gist.github.com":
        return urlunparse((parsed.scheme, parsed.netloc, f"{owner}/{repo}", None, None, None))
    elif parsed.netloc == "raw.githubusercontent.com":
        path = f"{owner}/{repo}"
        if branch:
            path += f"/tree/{branch}"
        return urlunparse((parsed.scheme, "github.com", path, None, None, None))
    elif parsed.netloc == "github.com":
        path = f"{owner}/{repo}"
        if branch:
            path += f"/tree/{branch}"
        return urlunparse((parsed.scheme, parsed.netloc, path, None, None, None))

    return url


# --- Main Processing Pipeline -----------------------------------------------
DEFAULT_PROCESSORS = [
    fix_url_from_markdown,
    safelink_to_url,
    strip_utm_params,
    convert_paper_urls_to_arxiv,
    standardize_arxiv,
    standardize_github,
    validate_url,
]


def process_url(url: str, processors: Optional[List[Callable[[str], str]]] = None) -> str:
    """Apply a chain of processors to clean and standardize a URL."""
    if processors is None:
        processors = DEFAULT_PROCESSORS

    for processor in processors:
        url = processor(url)

    return url


def extract_urls_from_text(text: str) -> Generator[str, None, None]:
    """Find all URLs in text and process them through the standardization pipeline.

    Args:
        text: Text to search for URLs

    Yields:
    ------
        Processed URLs found in the text
    """
    if not text:
        raise ValueError("Text cannot be empty")

    found_urls = set()

    # Extract plain URLs
    for url in extract_urls(text):
        try:
            processed_url = process_url(url)
            if processed_url and processed_url not in found_urls:
                found_urls.add(processed_url)
                yield processed_url
        except Exception as e:
            logger.warning("Failed to process URL '%s': %s", url, e)


# ============================================================================
# Backwards Compatibility (simplified versions of original functions)
# ============================================================================
def clean_md_link_title(title: str) -> str:
    """Clean markdown link titles by removing escapes and artifacts."""
    if not title:
        return title

    # Replace extra escapes
    title = title.replace("\\", "")
    # Split on possible markdown-url divider ']('
    title = title.split("](")[0]

    return title


def clean_md_artifacts(url: str) -> str:
    """Clean URL artifacts from markdown parsing."""
    if not url:
        return url

    # Remove markdown-like artifacts: ")[stuff](" patterns
    pattern = re.compile(r"\)\[[^\]\(]+?\]\(", re.IGNORECASE)
    split_parts = pattern.split(url)
    if split_parts:
        return split_parts[0]

    return url


def follow_redirects(url: str, timeout: int = 5) -> str:
    """Follow HTTP redirects and return the final URL."""
    try:
        with requests.get(url, timeout=timeout, allow_redirects=True) as response:
            response.raise_for_status()
            return response.url
    except requests.exceptions.RequestException:
        logger.debug(f"Failed to follow redirects from original URL {url}")
        return url


async def follow_redirects_async(url: str, timeout: int = 5) -> str:
    """Async version: Follow HTTP redirects and return the final URL."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            return str(response.url)
    except (httpx.HTTPError, asyncio.TimeoutError) as e:
        logger.debug(f"Failed to follow redirects from original URL {url}: {e}")
        return url
