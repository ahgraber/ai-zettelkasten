import logging
import re
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

from pydantic import HttpUrl, ValidationError as PydanticValidationError
import validators
from validators import ValidationError as URLValidatorValidationError

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

_URL_PATTERN = re.compile(URL_REGEX, re.IGNORECASE | re.UNICODE)

# Compiled patterns for markdown extraction
_WHITESPACE_PATTERN = re.compile(r"\s+")
_MD_LINK_EXTRACT_PATTERN = re.compile(
    r"\[(?:[^\[\]]|\[(?:[^\[\]])*\])*\]"
    r"\("
    r"((?:[^()\s]|\([^()\s]*\))+)"
    r"\)"
)
_INLINE_LINK_PATTERN = re.compile(
    rf'\[(?P<title>(?:\\.|[^\[\]])+?)\]\((?P<url>{URL_REGEX})(?:\s+"[^"]*")?\)', re.UNICODE
)
_REF_DEF_PATTERN = re.compile(rf"^\s{{0,3}}\[(?P<ref>[^\]]+)\]:\s*(?P<url>{URL_REGEX})", re.MULTILINE)
_REFERENCE_LINK_PATTERN = re.compile(r"\[(?P<title>(?:\\.|[^\[\]])+?)\]\[(?P<ref>[^\[\]]+)\]")
_ANGLE_BRACKET_PATTERN = re.compile(rf"<(?P<url>{URL_REGEX})>")
_HTML_LINK_PATTERN = re.compile(
    rf'<a\s+[^>]*href=[\'"](?P<url>{URL_REGEX})[\'"][^>]*>(?P<title>.*?)</a>', re.IGNORECASE
)
_SAFELINK_PATTERN = re.compile(r"safelinks\.protection\.outlook\.com/\?url=(.*?)&data=")

# Domain constants for URL detection
SOCIAL_MEDIA_DOMAINS = frozenset(
    {
        "linkedin.com",
        "twitter.com",
        "x.com",
        "t.co",
        "bsky.app",
        "facebook.com",
        "fbcdn.net",
        "instagram.com",
        "threads.net",
    }
)


# --- Core URL Extraction ----------------------------------------------------
def fix_url_from_markdown(url: str) -> str:
    """Clean up URLs that may have dangling parens from markdown parsing."""
    fixed = url.strip()
    while not check_balanced_brackets(fixed):
        fixed = fixed[:-1]

    fixed = fixed.rstrip(".,;:!'`*")

    try:
        validate_url(fixed)
    except (PydanticValidationError, URLValidatorValidationError, ValueError):
        return url
    else:
        return fixed


def extract_domain(url: str) -> str:
    """Extract the domain from a URL.

    Args:
        url: The URL to extract domain from

    Returns:
        The domain portion of the URL (e.g., "example.com")

    Raises:
        ValueError: If the URL is invalid or has no domain
    """
    if not url or url.strip() == "":
        raise ValueError(f"Invalid URL: {url}")

    validated = validate_url(url)
    parsed = urlparse(validated)
    if not parsed.netloc:
        raise ValueError(f"No domain found in URL: {url}")
    return parsed.netloc


def clean_markdown_title(title: str) -> str:
    """Clean up a markdown title by removing unwanted characters."""
    if not title:
        raise ValueError("Title cannot be empty")

    title = title.strip()
    title = _WHITESPACE_PATTERN.sub(" ", title).strip()
    title = title.replace("\\", "")

    if title.startswith("[") and title.endswith("]"):
        title = title[1:-1]

    return title


def extract_urls(text: str) -> list[str]:
    """Extract all URLs from text using two-phase approach.

    Phase 1: Extract URLs from markdown link syntax [text](url)
    Phase 2: Extract bare URLs from remaining text

    Args:
        text: Text to search for URLs

    Returns:
        List of extracted URLs

    Raises:
        ValueError: If text is empty
    """
    if not text:
        raise ValueError("Text cannot be empty")

    urls: list[str] = []
    seen_spans: list[tuple[int, int]] = []
    seen_urls: set[str] = set()

    # Phase 1: Extract URLs from markdown links (precise boundaries).
    # Regex matches: [text](url) where text can contain nested brackets (one level)
    # and url can contain balanced parens
    for match in _MD_LINK_EXTRACT_PATTERN.finditer(text):
        url = match.group(1).strip()
        if url and url not in seen_urls:
            urls.append(url)
            seen_spans.append(match.span())
            seen_urls.add(url)

    # Phase 2: Extract bare URLs from text outside markdown links.
    for match in _URL_PATTERN.finditer(text):
        start, end = match.span()
        # Skip if this URL was already captured inside a markdown link.
        if any(s <= start and end <= e for s, e in seen_spans):
            continue
        url = fix_url_from_markdown(match.group(0))
        if url.strip() and url not in seen_urls:
            urls.append(url)
            seen_urls.add(url)

    return urls


def validate_url(url: str) -> str:
    """Validate a URL.

    Args:
        url: The URL string to validate

    Returns:
        str: Validated and normalized URL
    """
    if not url or url.strip() == "":
        raise ValueError("URL cannot be empty")

    if not _URL_PATTERN.match(url.strip()):
        raise ValueError(f"URL does not match expected url regex: {url}")

    validated = HttpUrl(url)
    url = str(validated)

    # ref: https://github.com/python-validators/validators/issues/139
    with temp_env_var("RAISE_VALIDATION_ERROR", "True"):
        _ = validators.url(url)

    return url


def _strip_www(netloc: str) -> str:
    """Remove leading 'www.' from a network location string."""
    return netloc[4:] if netloc.startswith("www.") else netloc


def normalize_url(url: str) -> str:
    """Normalize a URL for deduplication.

    Args:
        url: Input URL.

    Returns:
        A normalized URL with lowercased scheme and domain, ``www.`` prefix
        removed, trailing path slashes stripped, sorted query params, and no
        fragment.
    """
    validated = validate_url(url)
    parsed = urlparse(strip_utm_params(validated))
    query_pairs = sorted(parse_qsl(parsed.query, keep_blank_values=True))
    normalized_query = urlencode(query_pairs, doseq=True)
    path = parsed.path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    normalized = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=_strip_www(parsed.netloc.lower()),
        path=path,
        query=normalized_query,
        fragment="",
    )
    return urlunparse(normalized)


def extract_markdown_urls(text: str) -> list[tuple[str | None, str]]:
    """Extract all URLs from markdown text, returning (title, url) pairs.

    Returns:
        List of (title, url) tuples where:
        - title is the link text for markdown links [title](url)
        - title is None for plain URLs
    """
    if not text:
        raise ValueError("Text cannot be empty")

    results = []

    # Inline markdown links: [title](url)
    results += [(m.group("title"), m.group("url")) for m in _INLINE_LINK_PATTERN.finditer(text)]

    # Reference link definitions: [ref]: url
    ref_map = {m.group("ref"): m.group("url") for m in _REF_DEF_PATTERN.finditer(text)}

    # Reference links: [title][ref]
    for m in _REFERENCE_LINK_PATTERN.finditer(text):
        url = ref_map.get(m.group("ref"))
        if url:
            results.append((m.group("title"), url))

    # Raw URLs in angle brackets: <https://example.com>
    results += [(None, m.group("url")) for m in _ANGLE_BRACKET_PATTERN.finditer(text)]

    # HTML <a href=""> tags
    results += [(m.group("title"), m.group("url")) for m in _HTML_LINK_PATTERN.finditer(text)]

    results = [(clean_markdown_title(title) if title else None, fix_url_from_markdown(url)) for title, url in results]
    return results


# --- URL Detection/Classification -------------------------------------------
def _netloc_in_domains(url: str, domains: frozenset[str]) -> bool:
    """Return True when the URL's netloc matches a domain or is a subdomain of one."""
    validated = validate_url(url)
    netloc = urlparse(validated).netloc.lower()
    return netloc in domains or any(netloc.endswith("." + d) for d in domains)


def is_social_url(url: str) -> bool:
    """Check if URL is from a social media domain."""
    return _netloc_in_domains(url, SOCIAL_MEDIA_DOMAINS)


# --- URL Processing/Standardization -----------------------------------------
def strip_utm_params(url: str) -> str:
    """Remove UTM tracking parameters from URL."""
    parsed = urlparse(url)
    pairs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if not k.startswith("utm_")]
    cleaned = urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(pairs),
            parsed.fragment,
        )
    )

    if cleaned != url:
        logger.debug("Stripped UTM params: %s -> %s", url, cleaned)

    return cleaned


def safelink_to_url(url: str) -> str:
    """Decode Microsoft SafeLinks URLs to original URLs."""
    if "safelinks.protection.outlook.com" not in url:
        return url

    decoded = unquote(url)
    matches = _SAFELINK_PATTERN.findall(decoded)

    if matches:
        return matches[0]
    else:
        raise ValueError(f"Could not extract URL from SafeLink: {decoded}")
