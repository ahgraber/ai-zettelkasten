"""A tool that performs a regex search over text."""

import re
from typing import Any, List, Pattern, TypeVar, Union

from pydantic_ai import Agent, RunContext

T = TypeVar("T")


def re_search_text(regex: Union[str, Pattern[str]], text: str, n_context_lines: int = 3) -> list[str]:
    """Perform a regex search over provided text.

    Args:
        regex: The regular expression pattern to search for (str or compiled Pattern)
        text: The text to search within
        n_context_lines: Number of surrounding lines to include in the result.

    Returns:
        A list of line groups containing matches with surrounding context
    """
    # Compile regex pattern if it's a string
    pattern = re.compile(regex, re.IGNORECASE | re.MULTILINE) if isinstance(regex, str) else regex

    # Split text into lines
    lines = text.splitlines()

    # Find lines containing matches
    matching_line_indices = []
    for i, line in enumerate(lines):
        if pattern.search(line):
            matching_line_indices.append(i)

    # If no matches found, return empty list
    if not matching_line_indices:
        return []

    # Collect surrounding lines for each match
    result_groups = []
    for match_idx in matching_line_indices:
        start_idx = max(0, match_idx - n_context_lines)
        end_idx = min(len(lines), match_idx + n_context_lines + 1)

        # Join the lines in this group
        line_group = "\n".join(lines[start_idx:end_idx])
        result_groups.append(line_group)

    return result_groups


def re_search_context(
    ctx: RunContext[T], regex: Union[str, Pattern[str]], key: str = "text", n_context_lines: int = 3
) -> list[str]:
    """Perform a regex search using text from the Agent context.

    Args:
        ctx: The RunContext containing the agent's context
        regex: The regular expression pattern to search for (str or compiled Pattern)
        key: The ctx key representing the text to search within.
        n_context_lines: Number of surrounding lines to include in the result.

    Returns:
        A list of line groups containing matches with surrounding context
    """
    # Try to get text from various context sources
    text_content = None

    # Check if there's text in the deps
    if hasattr(ctx, "deps") and ctx.deps and hasattr(ctx.deps, key):
        text_content = str(getattr(ctx.deps, key))

    if not text_content:
        raise ValueError(
            f"No text content found in agent context for key {key}. Context should contain text data to search within."
        )

    return re_search_text(regex, text_content, n_context_lines)
