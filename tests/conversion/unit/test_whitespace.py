"""Unit tests for whitespace normalization."""

import pytest

from aizk.conversion.utilities.whitespace import normalize_whitespace


class TestBasicNormalization:
    """Tests for basic space and newline collapsing."""

    def test_collapse_multiple_spaces(self) -> None:
        """Collapse 2+ consecutive spaces to single space."""
        assert normalize_whitespace("hello    world") == "hello world"
        assert normalize_whitespace("a  b  c") == "a b c"
        assert normalize_whitespace("word          word") == "word word"

    def test_collapse_newlines_three_plus(self) -> None:
        """Collapse 3+ consecutive newlines to exactly 2."""
        assert normalize_whitespace("a\n\n\nb") == "a\n\nb"
        assert normalize_whitespace("line1\n\n\n\nline2") == "line1\n\nline2"
        assert normalize_whitespace("x\n\n\n\n\n\ny") == "x\n\ny"

    def test_preserve_single_spaces(self) -> None:
        """Single spaces are preserved unchanged."""
        assert normalize_whitespace("hello world") == "hello world"
        assert normalize_whitespace("a b c d e") == "a b c d e"

    def test_preserve_single_newlines(self) -> None:
        """Single and double newlines are preserved."""
        assert normalize_whitespace("line1\nline2") == "line1\nline2"
        assert normalize_whitespace("line1\n\nline2") == "line1\n\nline2"

    def test_empty_string(self) -> None:
        """Empty string returns empty."""
        assert normalize_whitespace("") == ""

    def test_single_line_content(self) -> None:
        """Single line with no newlines handled correctly."""
        assert normalize_whitespace("no newlines here") == "no newlines here"

    def test_document_final_newline(self) -> None:
        """Trailing newline is preserved (one)."""
        text = "line1\nline2\n"
        assert normalize_whitespace(text) == text

    def test_leading_trailing_whitespace(self) -> None:
        """Document boundaries handled correctly."""
        assert normalize_whitespace("\nline\n") == "\nline\n"
        assert normalize_whitespace("  line  ") == "  line  "


class TestCodeBlockPreservation:
    """Tests for preserving content inside code blocks."""

    def test_code_block_indentation_preserved(self) -> None:
        """Indentation inside fenced code blocks is preserved."""
        code = "```\ndef foo():\n    x = 1  # extra spaces preserved\n    return x\n```"
        assert normalize_whitespace(code) == code

    def test_multiline_code_block(self) -> None:
        """Indentation across multiple lines in code block is preserved."""
        code = """```python
def process(data):
    if data:
        result = {
            "key":  "value",
            "nested": {
                "field": 123
            }
        }
        return result
```"""
        assert normalize_whitespace(code) == code

    def test_inline_code_spaces_preserved(self) -> None:
        """Spaces around and inside inline code are not collapsed."""
        assert normalize_whitespace("Use `x = 1` in code") == "Use `x = 1` in code"
        assert normalize_whitespace("`a  b` here") == "`a  b` here"

    def test_code_block_with_surrounding_text(self) -> None:
        """Text around code blocks is normalized, content inside is preserved."""
        text = "Some text with    spaces\n\n\n```\ndef foo():\n    pass\n```\n\n\nMore text    here"
        result = normalize_whitespace(text)
        assert "Some text with spaces" in result
        assert "def foo():\n    pass" in result
        assert "More text here" in result

    def test_multiple_code_blocks(self) -> None:
        """Multiple code blocks all preserve their internal spacing."""
        text = """First block:
```
x  =  1
```

Second block:
```
y   =   2
```"""
        result = normalize_whitespace(text)
        assert "x  =  1" in result
        assert "y   =   2" in result

    def test_code_block_with_inline_backticks_preserved(self) -> None:
        """Single backticks inside a fenced code block are preserved unchanged."""
        text = "```\nresult = `x  y`\n```"
        assert normalize_whitespace(text) == text


class TestStructuredContent:
    """Tests for preserving formatting in structured Markdown."""

    def test_markdown_table_cells_with_extra_spaces_normalized(self) -> None:
        """Extra spaces inside table cells are collapsed."""
        table = "| Header  1 | Header  2 |\n| --------- | --------- |\n| Cell  1   | Cell  2   |"
        result = normalize_whitespace(table)
        assert "| Header 1 | Header 2 |" in result
        assert "| Cell 1 | Cell 2 |" in result

    def test_markdown_table_already_clean_unchanged(self) -> None:
        """A table with single spaces throughout is left unchanged."""
        table = "| H1 | H2 |\n|----|----|\n| a | b |"
        assert normalize_whitespace(table) == table

    def test_bullet_list_indentation(self) -> None:
        """Bullet list indentation is preserved."""
        text = "- Item 1\n  - Nested item\n    - Deeper item"
        assert normalize_whitespace(text) == text

    def test_numbered_list_indentation(self) -> None:
        """Numbered list indentation is preserved."""
        text = "1. Item 1\n   1. Nested\n      1. Deeper"
        assert normalize_whitespace(text) == text

    def test_blockquote_content_preserved(self) -> None:
        """Blockquote structure and markers are preserved exactly."""
        text = "> This is a quote\n> Still part of quote\n>> Nested quote"
        assert normalize_whitespace(text) == text

    def test_yaml_frontmatter_double_spaces_collapsed(self) -> None:
        """Extra spaces in YAML frontmatter values are collapsed."""
        text = "---\ntitle: Test Document\nauthor:  John Doe\ntags:  [a, b, c]\n---\n\n# Content"
        result = normalize_whitespace(text)
        assert "author: John Doe" in result
        assert "tags: [a, b, c]" in result
        assert result.startswith("---\ntitle: Test Document\n")
        assert result.endswith("# Content")

    def test_heading_spacing_normalized(self) -> None:
        """Multiple spaces in heading text are collapsed."""
        text = "# Title    with    spaces\n\n\n## Subtitle"
        assert normalize_whitespace(text) == "# Title with spaces\n\n## Subtitle"


class TestTrailingSpaceStripping:
    """All trailing spaces before newlines are stripped.

    Docling never produces intentional two-space Markdown hard breaks;
    all trailing spaces in its output are conversion artifacts.
    """

    @pytest.mark.parametrize(
        "input_text, expected",
        [
            pytest.param("hello \n", "hello\n", id="single_trailing_space"),
            pytest.param("hello  \n", "hello\n", id="two_trailing_spaces"),
            pytest.param("hello     \n", "hello\n", id="many_trailing_spaces"),
            pytest.param("a \nb \nc \n", "a\nb\nc\n", id="trailing_space_each_line"),
            pytest.param(" \n", "\n", id="blank_line_with_space"),
            pytest.param("   \n", "\n", id="blank_line_with_multiple_spaces"),
            pytest.param("a  \n  \nb", "a\n\nb", id="trailing_and_leading_spaces"),
            pytest.param(
                "line1  \n\n\nline2  \n",
                "line1\n\nline2\n",
                id="trailing_spaces_with_excess_newlines",
            ),
        ],
    )
    def test_trailing_spaces_before_newlines_stripped(self, input_text: str, expected: str) -> None:
        assert normalize_whitespace(input_text) == expected

    def test_trailing_spaces_at_document_end_without_newline(self) -> None:
        """Trailing spaces at end-of-document (no trailing newline) are subject
        to space collapsing but not stripped by the newline-targeted regex."""
        # _strip_trailing_spaces only targets ` +\n`; document-final spaces
        # survive that step but _collapse_spaces reduces them.
        # "hello  " → collapse_spaces preserves trailing → strip_trailing no-op
        # (no \n after) → result keeps trailing but collapsed.
        # Actually: _collapse_spaces preserves trailing ws, _strip_trailing
        # only matches ` +\n`. So "hello  " stays "hello  " after strip_trailing,
        # but "  " is the trailing group, which _collapse_spaces preserves.
        # Let's just verify the observable behavior:
        result = normalize_whitespace("hello  ")
        # Two trailing spaces at doc end — _collapse_spaces preserves trailing
        assert result == "hello  "

    def test_trailing_spaces_inside_code_block_preserved(self) -> None:
        """Trailing spaces inside a code block are NOT stripped."""
        code = "```\nline with trailing  \nanother  \n```"
        result = normalize_whitespace(code)
        assert result == code


class TestTabNormalization:
    """Tabs outside code blocks are expanded to 4 spaces.

    Code blocks are exempt — tab indentation inside fences is preserved.
    """

    @pytest.mark.parametrize(
        "input_text, expected",
        [
            pytest.param("a\tb", "a b", id="single_tab_mid_line_collapses"),
            pytest.param("a\t\tb", "a b", id="two_tabs_mid_line_collapses"),
            pytest.param("\ta", "    a", id="leading_tab_becomes_4_spaces"),
            pytest.param("\t\ta", "        a", id="two_leading_tabs_become_8_spaces"),
            pytest.param("a\t\n", "a\n", id="trailing_tab_stripped_after_expand"),
            pytest.param("col1\tcol2\tcol3", "col1 col2 col3", id="tsv_like_prose"),
        ],
    )
    def test_tab_normalization(self, input_text: str, expected: str) -> None:
        assert normalize_whitespace(input_text) == expected

    def test_tab_inside_code_block_preserved(self) -> None:
        """Tabs inside a fenced code block are never expanded."""
        code = "```\ndef foo():\n\treturn 1\n```"
        assert normalize_whitespace(code) == code

    def test_tab_inside_inline_code_expanded(self) -> None:
        """Tabs inside inline code are expanded to 4 spaces.

        _normalize_tabs runs before the inline-code-aware space-collapsing pass,
        so it expands tabs everywhere in non-fence text, including inside backtick spans.
        Docling never produces tabs in inline code, so this is not a practical concern.
        """
        assert normalize_whitespace("run `cmd\targ`") == "run `cmd    arg`"


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_only_spaces(self) -> None:
        """String of only spaces normalized."""
        assert normalize_whitespace("    ") == "    "

    def test_only_newlines(self) -> None:
        """Multiple newlines normalized to two."""
        assert normalize_whitespace("\n\n\n\n") == "\n\n"

    def test_mixed_whitespace(self) -> None:
        """Mixed spaces and newlines handled correctly."""
        assert normalize_whitespace("a  \n  \n  \nb") == "a\n\nb"

    def test_code_fence_without_closing(self) -> None:
        """Unclosed code fence - trailing content still normalized."""
        text = "text with    spaces\n```\ncode"
        result = normalize_whitespace(text)
        # Since fence is unclosed, regex won't match, content after will be normalized
        assert "text with spaces" in result

    def test_nested_code_attempts(self) -> None:
        """Multiple backticks in content."""
        text = "Use `` for code or `single` or triple ```"
        result = normalize_whitespace(text)
        assert "`single`" in result

    def test_tab_in_prose_normalized_to_spaces(self) -> None:
        """Tabs in prose are expanded to 4 spaces, then space-collapsing reduces them."""
        # \t → 4 spaces → collapse 4 spaces to 1
        assert normalize_whitespace("col1\tcol2\tcol3") == "col1 col2 col3"

    def test_trailing_tab_before_newline_stripped(self) -> None:
        """A trailing tab is expanded to 4 spaces, then stripped before the newline."""
        assert normalize_whitespace("line\t\n") == "line\n"

    def test_crlf_line_endings_trailing_space_not_stripped(self) -> None:
        """Trailing spaces before \\r\\n are NOT stripped.

        _strip_trailing_spaces targets the pattern ' +\\n' (LF only).
        A \\r between the spaces and the \\n breaks the match, so CRLF
        trailing spaces survive.  Document this as a known limitation:
        Docling outputs LF line endings, so CRLF is not an in-scope case.
        """
        text = "hello  \r\n"
        assert normalize_whitespace(text) == text

    def test_empty_code_block_unchanged(self) -> None:
        """An empty fenced code block is preserved exactly."""
        text = "before\n```\n```\nafter"
        assert normalize_whitespace(text) == text

    def test_whitespace_only_code_block_unchanged(self) -> None:
        """A code block containing only spaces/newlines is not modified."""
        text = "```\n   \n   \n```"
        assert normalize_whitespace(text) == text

    def test_adjacent_code_blocks_both_preserved(self) -> None:
        """Two consecutive code blocks both preserve their content."""
        text = "```\na  b\n```\n\n```\nc  d\n```"
        result = normalize_whitespace(text)
        assert "a  b" in result
        assert "c  d" in result
        # Verify both fences are still present
        assert result.count("```") == 4

    def test_language_tagged_code_fence_preserved(self) -> None:
        """Language-tagged fences (```python, ```js) preserve internal content."""
        text = "```python\nx  =  1\n```"
        assert normalize_whitespace(text) == text

    def test_code_fence_at_document_start(self) -> None:
        """Code block at the very start of the document is preserved."""
        text = "```\nx  =  1\n```\n\nsome text"
        result = normalize_whitespace(text)
        assert result.startswith("```\nx  =  1\n```")

    def test_code_fence_at_document_end(self) -> None:
        """Code block at the very end of the document (no trailing newline) is preserved."""
        text = "some text\n\n```\nx  =  1\n```"
        result = normalize_whitespace(text)
        assert result.endswith("```\nx  =  1\n```")


class TestNormalizationStepInteractions:
    """Tests that verify correct ordering and interaction between the four
    normalization passes: tab expand → space collapse → trailing-space strip → newline collapse."""

    def test_tab_expanded_then_space_collapsed(self) -> None:
        """Tab is expanded to 4 spaces, which are then collapsed to 1."""
        assert normalize_whitespace("a\tb") == "a b"

    def test_trailing_tab_expanded_then_stripped(self) -> None:
        """Tab at line end: expand to spaces, then strip before newline."""
        assert normalize_whitespace("end\t\n") == "end\n"

    def test_trailing_spaces_stripped_after_space_collapse(self) -> None:
        """Space-collapsing runs before trailing-space stripping.
        'hello    \\n' → collapse → 'hello    \\n' (trailing preserved by collapse)
        → strip trailing → 'hello\\n'."""
        assert normalize_whitespace("hello    \n") == "hello\n"

    def test_blank_line_spaces_collapsed_then_stripped(self) -> None:
        """A whitespace-only line has its spaces stripped, leaving a bare newline."""
        assert normalize_whitespace("a\n   \nb") == "a\n\nb"

    def test_multiple_blank_lines_with_spaces_collapse_to_two_newlines(self) -> None:
        """Whitespace-only lines between paragraphs count as blank lines;
        three or more consecutive newlines (after stripping) collapse to two."""
        # "a\n   \n   \nb" → strip trailing → "a\n\n\nb" → collapse → "a\n\nb"
        assert normalize_whitespace("a\n   \n   \nb") == "a\n\nb"

    def test_inline_code_surrounded_by_excess_spaces(self) -> None:
        """Spaces outside inline code are collapsed; spaces inside are preserved."""
        assert normalize_whitespace("text  `a  b`  more") == "text `a  b` more"

    def test_trailing_space_plus_excess_newlines(self) -> None:
        """Trailing spaces and excess newlines are each normalized independently."""
        text = "para one  \n\n\n\npara two  \n"
        assert normalize_whitespace(text) == "para one\n\npara two\n"

    def test_all_four_steps_in_one_input(self) -> None:
        """Exercise all four normalization passes on a single input."""
        # tab → space(s) → collapse → strip trailing → collapse newlines
        text = "word\tword  \n\n\ncode  `x  y`  end  \n"
        result = normalize_whitespace(text)
        assert result == "word word\n\ncode `x  y` end\n"


class TestIdempotence:
    """Tests to verify normalization is idempotent."""

    def test_normalization_is_idempotent(self) -> None:
        """Applying normalization twice gives same result."""
        text = "hello    world\n\n\nfoo  bar"
        once = normalize_whitespace(text)
        twice = normalize_whitespace(once)
        assert once == twice

    def test_idempotent_with_code_blocks(self) -> None:
        """Idempotence holds with code blocks."""
        text = """Normal    text

```
code  with  spaces
```

More    text"""
        once = normalize_whitespace(text)
        twice = normalize_whitespace(once)
        assert once == twice

    def test_idempotent_with_multiple_sections(self) -> None:
        """Idempotence across complex document."""
        text = "A\n\n\nB\n\n\nC\n\n\nD"
        once = normalize_whitespace(text)
        twice = normalize_whitespace(once)
        assert once == twice
        assert once.count("\n\n") == 3
