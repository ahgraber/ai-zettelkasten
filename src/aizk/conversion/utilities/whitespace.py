"""Whitespace normalization for Markdown output.

Collapses excessive whitespace in Markdown while preserving intentional formatting
in code blocks and structured content.
"""

import re


def normalize_whitespace(text: str) -> str:
    r"""Normalize whitespace in Markdown text.

    Collapses:
    - Tab characters to 4 spaces
    - Multiple consecutive spaces (2+) to single space
    - 3+ consecutive newlines to exactly 2 newlines
    - Trailing spaces before newlines

    Preserves:
    - Indentation and spacing inside code blocks (fenced with ```)
    - Indentation and spacing inside inline code (single backticks)
    - Single spaces and single/double newlines

    Args:
        text: Markdown text to normalize

    Returns:
        Normalized Markdown text

    Examples:
        >>> normalize_whitespace("hello    world")
        'hello world'
        >>> normalize_whitespace("a\\n\\n\\nb")
        'a\\n\\nb'
        >>> normalize_whitespace("```\\ndef foo():  # extra spaces preserved\\n    pass\\n```")
        '```\\ndef foo():  # extra spaces preserved\\n    pass\\n```'
    """
    if not text:
        return text

    # Split by code fences to preserve formatting inside code blocks
    parts = _split_by_code_fences(text)
    normalized_parts = []

    for part, is_code_fence in parts:
        if is_code_fence:
            # Don't modify content inside code fences
            normalized_parts.append(part)
        else:
            # Normalize outside code fences
            # First: expand tabs to spaces
            normalized = _normalize_tabs(part)
            # Second: collapse multiple spaces (but preserve at line starts for indentation)
            normalized = _collapse_spaces(normalized)
            # Third: strip trailing spaces before newlines
            normalized = _strip_trailing_spaces(normalized)
            # Fourth: collapse multiple newlines
            normalized = _collapse_newlines(normalized)
            normalized_parts.append(normalized)

    return "".join(normalized_parts)


def _split_by_code_fences(text: str) -> list[tuple[str, bool]]:
    """Split text by code fences (triple backticks).

    Returns:
        List of (text, is_inside_code_fence) tuples
    """
    parts = []
    pattern = r"(```[\s\S]*?```)"
    matches = list(re.finditer(pattern, text, re.DOTALL))

    if not matches:
        return [(text, False)]

    last_end = 0
    for match in matches:
        # Add text before code fence
        if match.start() > last_end:
            parts.append((text[last_end : match.start()], False))
        # Add code fence content
        parts.append((match.group(0), True))
        last_end = match.end()

    # Add remaining text after last code fence
    if last_end < len(text):
        parts.append((text[last_end:], False))

    return parts


def _normalize_tabs(text: str) -> str:
    """Replace each tab character with 4 spaces.

    _normalize_tabs expands tabs everywhere in non-fence text, including inside inline backtick spans.
    Docling never produces tabs in inline code, so this is not a practical concern.
    """
    return text.replace("\t", "    ")


def _collapse_spaces(text: str) -> str:
    """Collapse multiple consecutive spaces to single space.

    Preserves indentation at line starts (leading spaces that matter for list nesting
    and other structural indentation).
    Preserves spaces inside inline code (backticks).
    Trailing whitespace is left to _strip_trailing_spaces.
    """
    lines = text.split("\n")
    result = []

    for line in lines:
        # Preserve leading and trailing whitespace; only collapse spaces in content
        match = re.match(r"^(\s*)(.*?)(\s*)$", line)
        if match:
            leading = match.group(1)
            content = match.group(2)
            trailing = match.group(3)
            # Collapse multiple spaces but preserve inline code
            content = _collapse_spaces_preserve_inline_code(content)
            result.append(leading + content + trailing)
        else:
            result.append(line)

    return "\n".join(result)


def _strip_trailing_spaces(text: str) -> str:
    """Strip trailing spaces from each line (before newlines).

    Removes spaces immediately before newlines, normalizing whitespace-only
    lines and trailing spaces added by Markdown processors.
    Trailing spaces at end-of-document (not followed by a newline) are preserved.
    """
    return re.sub(r" +\n", "\n", text)


def _collapse_spaces_preserve_inline_code(content: str) -> str:
    """Collapse spaces in content while preserving inline code (backticks)."""
    parts = []
    pattern = r"(`[^`]*`)"
    matches = list(re.finditer(pattern, content))

    if not matches:
        # No inline code, just collapse spaces
        return re.sub(r" {2,}", " ", content)

    last_end = 0
    for match in matches:
        # Collapse spaces before inline code
        before = content[last_end : match.start()]
        before = re.sub(r" {2,}", " ", before)
        parts.append(before)
        # Add inline code unchanged
        parts.append(match.group(0))
        last_end = match.end()

    # Collapse spaces after last inline code
    after = content[last_end:]
    after = re.sub(r" {2,}", " ", after)
    parts.append(after)

    return "".join(parts)


def _collapse_newlines(text: str) -> str:
    """Collapse 3+ consecutive newlines to exactly 2 newlines."""
    # Replace 3+ newlines with exactly 2
    return re.sub(r"\n{3,}", "\n\n", text)
