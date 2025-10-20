"""Use the arxiv.org API to get papers and metadata."""

# %%
import asyncio
import logging
import re
import sys
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urljoin
from xml.etree.ElementTree import Element

from defusedxml import ElementTree
import httpx

import requests

from aizk.utilities.limiters import LeakyBucketRateLimiter

logger = logging.getLogger(__name__)

# %%
ARXIV_API_URL = "http://export.arxiv.org/api/"


# %%
class ArxivParsingError(Exception):
    """Raised when there are errors parsing ArXiv XML response."""

    def __init__(self, entry_id: str, errors: List[str]) -> None:
        self.entry_id = entry_id
        self.errors = errors
        super().__init__(f"Failed to parse entry {entry_id}: {'; '.join(errors)}")


class ArxivAccessDeniedError(Exception):
    """Raised when access to the ArXiv API is denied.

    This can happen if the API key is invalid or if the request exceeds rate limits.
    Immediately stop; ArXiv will treat repeated requests following a 403 as a denial of service attack.
    """

    def __init__(self, message: str, exit_on_error: bool = False) -> None:
        super().__init__(f"Access denied to ArXiv API: {message}")
        if exit_on_error:
            logger.critical("Exiting due to ArXiv access denied error to prevent DoS attack classification.")
            sys.exit(1)


def _extract_xml_field(
    element: Element,
    xpath: str,
    namespaces: Dict[str, str],
    field_name: str,
    required: bool = True,
    attribute: Optional[str] = None,
) -> Optional[str]:
    """Extract a field value from XML element with validation.

    Args:
        element: The XML element to search in
        xpath: XPath expression to find the target element
        namespaces: XML namespace mappings
        field_name: Name of the field for error reporting
        required: Whether the field is required (raises error if missing)
        attribute: If specified, extract this attribute instead of text content

    Returns:
        The extracted value or None if not found and not required

    Raises:
        ValueError: If required field is missing or empty
    """
    found_element = element.find(xpath, namespaces)

    if found_element is None:
        if required:
            raise ValueError(f"Required field '{field_name}' not found")
        return None

    value = found_element.get(attribute) if attribute else found_element.text

    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError(f"Required field '{field_name}' is empty")
        return None

    return value.strip() if isinstance(value, str) else value


def _extract_required_fields(entry: Element, namespaces: Dict[str, str]) -> tuple[Dict[str, str], List[str]]:
    """Extract required fields from an ArXiv entry with error collection.

    Args:
        entry: XML element representing a single paper entry
        namespaces: XML namespace mappings

    Returns:
        Tuple of (extracted_fields_dict, errors_list)
    """
    errors = []
    fields = {}

    field_mappings = [
        ("id", "atom:id"),
        ("title", "atom:title"),
        ("summary", "atom:summary"),
        ("published", "atom:published"),
        ("updated", "atom:updated"),
    ]

    for field_name, xpath in field_mappings:
        try:
            value = _extract_xml_field(entry, xpath, namespaces, field_name, required=True)
            if value:
                fields[field_name] = value
        except ValueError as e:
            errors.append(str(e))

    return fields, errors


def _extract_authors(entry: Element, namespaces: Dict[str, str]) -> tuple[List[str], List[str]]:
    """Extract authors from an ArXiv entry with error collection.

    Args:
        entry: XML element representing a single paper entry
        namespaces: XML namespace mappings

    Returns:
        Tuple of (authors_list, errors_list)
    """
    errors = []
    authors = []

    for author in entry.findall("atom:author", namespaces):
        try:
            name = _extract_xml_field(author, "atom:name", namespaces, "author name")
            if name:
                authors.append(name)
        except ValueError:
            # Skip individual author errors but continue processing
            pass

    if not authors:
        errors.append("No valid authors found")

    return authors, errors


def _extract_optional_fields(entry: Element, namespaces: Dict[str, str]) -> Dict[str, Any]:
    """Extract optional fields from an ArXiv entry.

    Args:
        entry: XML element representing a single paper entry
        namespaces: XML namespace mappings

    Returns:
        Dictionary containing optional field values
    """
    # Extract categories
    categories = []
    for cat in entry.findall("atom:category", namespaces):
        term = cat.get("term")
        if term:
            categories.append(term)

    # Extract links
    pdf_url = None
    html_url = None
    for link in entry.findall("atom:link", namespaces):
        link_type = link.get("type")
        href = link.get("href")
        if link_type == "application/pdf" and href:
            pdf_url = href
        elif link_type == "text/html" and href:
            html_url = href

    # Extract primary category
    primary_category = None
    primary_cat = entry.find("arxiv:primary_category", namespaces)
    if primary_cat is not None:
        primary_category = primary_cat.get("term")

    return {
        "pdf_url": pdf_url,
        "html_url": html_url,
        "categories": categories,
        "primary_category": primary_category,
    }


def _parse_arxiv_entry(entry: Element, namespaces: Dict[str, str]) -> Dict[str, Any]:
    """Parse a single ArXiv entry from XML.

    Args:
        entry: XML element representing a single paper entry
        namespaces: XML namespace mappings

    Returns:
        Dictionary containing paper metadata

    Raises:
        ArxivParsingError: If there are validation errors during parsing
    """
    all_errors = []

    # Extract required fields
    required_fields, required_errors = _extract_required_fields(entry, namespaces)
    all_errors.extend(required_errors)

    # Extract authors
    authors, author_errors = _extract_authors(entry, namespaces)
    all_errors.extend(author_errors)

    # Extract optional fields
    optional_fields = _extract_optional_fields(entry, namespaces)

    # Raise collected errors if any
    entry_id = required_fields.get("id", "unknown")
    if all_errors:
        raise ArxivParsingError(entry_id, all_errors)

    return {
        **required_fields,
        "authors": authors,
        **optional_fields,
    }


# %%
"""AsyncArxivClient is an httpx client that must use the appropriate limiter to respect the arxiv API limits.  The clients should be able to be used as context managers and client objects."""
# IMPORTANT: "make no more than one request every three seconds, and limit requests to a single connection at a time.""
_arxiv_rate_limiter = LeakyBucketRateLimiter(
    max_requests=1,
    window_seconds=3.0,
    max_burst=1,
)


class AsyncArxivClient:
    """Asynchronous arXiv API client with rate limiting.

    This client respects arXiv API limits: no more than one request every
    three seconds, limited to a single connection at a time.

    Can be used as an async context manager to ensure proper resource cleanup.

    Example:
        >>> async with AsyncArxivClient() as client:
        ...     metadata = await client.get_paper_metadata(["1234.56789"])

        >>> # Or without context manager
        >>> client = AsyncArxivClient()
        >>> metadata = await client.get_paper_metadata(["1234.56789"])
        >>> await client.aclose()
    """

    def __init__(self, timeout: float = 30.0):
        """Initialize the AsyncArxivClient.

        Args:
            timeout: Request timeout in seconds (default: 30.0)
        """
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    def _ensure_client(self) -> httpx.AsyncClient:
        """Ensure the httpx async client is initialized."""
        if self._client is None:
            # Use limits to ensure single connection
            limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
            self._client = httpx.AsyncClient(timeout=self.timeout, limits=limits, follow_redirects=True)
        return self._client

    async def get_paper_metadata(self, ids: List[str]) -> List[Dict[str, Any]]:
        """Get arXiv papers by their IDs with rate limiting.

        Args:
            ids: List of arXiv paper IDs (e.g., ['2506.06395'])

        Returns:
            List of paper dictionaries containing metadata

        Raises:
            ValueError: If IDs list is empty or XML parsing fails
            httpx.HTTPError: If the API request fails
            ArxivAccessDeniedError: If the API returns HTTP 403 (access denied)
            ArxivParsingError: If there are errors parsing the XML response
        """
        if not ids:
            raise ValueError("IDs list cannot be empty")

        # Apply rate limiting - this will block until we can make the request
        await _arxiv_rate_limiter.acquire()

        client = self._ensure_client()
        id_list = ",".join(ids)
        query_params = {"id_list": id_list}
        url = urljoin(ARXIV_API_URL, "query?" + urlencode(query_params))

        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                error_msg = f"Access denied to ArXiv API (HTTP 403). This may indicate rate limiting or blocked access. URL: {url}"
                logger.critical(error_msg)
                logger.critical(
                    "CRITICAL: ArXiv treats repeated requests after 403 as DoS attacks. Stopping to prevent further issues."
                )
                raise ArxivAccessDeniedError(error_msg) from e
            else:
                raise httpx.HTTPError(f"Failed to fetch papers from ArXiv API: {e}") from e
        except httpx.HTTPError as e:
            raise httpx.HTTPError(f"Failed to fetch papers from ArXiv API: {e}") from e

        # Parse using existing function logic
        return self._parse_response(response.content)

    async def _get_paper_content(self, url: str) -> str:
        """Fetch the content of a paper from its URL.

        Args:
            url: The URL of the paper to fetch

        Returns:
            The content of the paper as a string

        Raises:
            httpx.HTTPError: If the request fails
            ArxivAccessDeniedError: If the API returns HTTP 403 (access denied)
        """
        client = self._ensure_client()
        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                error_msg = f"Access denied to ArXiv API (HTTP 403) when fetching paper content. URL: {url}"
                logger.critical(error_msg)
                logger.critical(
                    "CRITICAL: ArXiv treats repeated requests after 403 as DoS attacks. Stopping to prevent further issues."
                )
                raise ArxivAccessDeniedError(error_msg) from e
            else:
                raise httpx.HTTPError(f"Failed to fetch paper content from {url}: {e}") from e
        except httpx.HTTPError as e:
            raise httpx.HTTPError(f"Failed to fetch paper content from {url}: {e}") from e
        else:
            return response.text

    async def _get_paper_pdf(self, url: str, use_export_url: bool = True) -> bytes:
        """Fetch the PDF content of a paper from its URL.

        Args:
            url: The URL of the paper PDF to fetch
            use_export_url: If True, replace arxiv.org with export.arxiv.org in the URL

        Returns:
            The PDF content as bytes

        Raises:
            httpx.HTTPError: If the request fails
            ArxivAccessDeniedError: If the API returns HTTP 403 (access denied)
        """
        client = self._ensure_client()
        if use_export_url:
            # Replace arxiv.org with export.arxiv.org in the URL
            url = re.sub(r"https?://arxiv\.org", "http://export.arxiv.org", url)
        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                error_msg = f"Access denied to ArXiv API (HTTP 403) when fetching paper PDF. URL: {url}"
                logger.critical(error_msg)
                logger.critical(
                    "CRITICAL: ArXiv treats repeated requests after 403 as DoS attacks. Stopping to prevent further issues."
                )
                raise ArxivAccessDeniedError(error_msg) from e
            else:
                raise httpx.HTTPError(f"Failed to fetch paper PDF from {url}: {e}") from e
        except httpx.HTTPError as e:
            raise httpx.HTTPError(f"Failed to fetch paper PDF from {url}: {e}") from e
        else:
            return response.content

    def _parse_response(self, content: bytes) -> List[Dict[str, Any]]:
        """Parse arXiv API XML response."""
        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError as e:
            raise ValueError(f"Failed to parse XML response: {e}") from e

        # Define namespaces
        namespaces = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

        papers = []
        parsing_errors = []

        for entry in root.findall("atom:entry", namespaces):
            try:
                paper = _parse_arxiv_entry(entry, namespaces)
                papers.append(paper)
            except ArxivParsingError as e:
                parsing_errors.append(str(e))

        # Raise collected parsing errors if any occurred
        if parsing_errors:
            raise ValueError(f"Failed to parse {len(parsing_errors)} entries: {'; '.join(parsing_errors)}")

        return papers

    async def aclose(self) -> None:
        """Close the underlying httpx async client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "AsyncArxivClient":
        """Enter async context manager."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit async context manager and cleanup resources."""
        await self.aclose()


# %%
def get_arxiv_paper_metadata(ids: List[str]) -> List[Dict[str, Any]]:
    """Get arxiv papers by their IDs.

    Args:
        ids: List of arxiv paper IDs (e.g., ['2506.06395'])

    Returns:
        List of paper dictionaries containing metadata

    Raises:
        ValueError: If IDs list is empty
        requests.HTTPError: If the API request fails
        ArxivAccessDeniedError: If the API returns HTTP 403 (access denied)
        ArxivParsingError: If there are errors parsing the XML response
    """
    if not ids:
        raise ValueError("IDs list cannot be empty")

    id_list = ",".join(ids)
    query_params = {"id_list": id_list}
    url = urljoin(ARXIV_API_URL, "query?" + urlencode(query_params))

    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
    except requests.HTTPError as e:
        if hasattr(e, "response") and e.response.status_code == 403:
            error_msg = (
                f"Access denied to ArXiv API (HTTP 403). This may indicate rate limiting or blocked access. URL: {url}"
            )
            logger.critical(error_msg)
            logger.critical(
                "CRITICAL: ArXiv treats repeated requests after 403 as DoS attacks. Stopping to prevent further issues."
            )
            raise ArxivAccessDeniedError(error_msg) from e
        else:
            raise requests.HTTPError(f"Failed to fetch papers from ArXiv API: {e}") from e
    except requests.RequestException as e:
        raise requests.HTTPError(f"Failed to fetch papers from ArXiv API: {e}") from e

    # Parse the XML
    try:
        root = ElementTree.fromstring(response.content)
    except ElementTree.ParseError as e:
        raise ValueError(f"Failed to parse XML response: {e}") from e

    # Define namespaces
    namespaces = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

    papers = []
    parsing_errors = []

    for entry in root.findall("atom:entry", namespaces):
        try:
            paper = _parse_arxiv_entry(entry, namespaces)
            papers.append(paper)
        except ArxivParsingError as e:
            parsing_errors.append(str(e))

    # Raise collected parsing errors if any occurred
    if parsing_errors:
        raise ValueError(f"Failed to parse {len(parsing_errors)} entries: {'; '.join(parsing_errors)}")

    return papers


# %%
