"""Unit tests for whitespace normalization.

Excerpts are drawn from actual conversion outputs (S3 bucket aizk):
- EU AI Ethics PDF  (uuid 0000661e): classic PDF word-level double-spacing
- Tulu 3 PDF        (uuid 019618ce): template syntax inside code block (preserve)
- SWE-agent PDF     (uuid 026a32bf): trailing whitespace on lines
- Photorealistic PDF(uuid 02a91012): 3-4 excess newlines between sections
- HuggingFace HTML  (uuid 00b2e9a4): spaces inside code block must be unchanged

The excerpts are embedded as string literals so markdown linters cannot
accidentally normalize them before the tests run.
"""

import hashlib
import re

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


# ── Real-world fixtures (raw, pre-normalization content from real S3 outputs) ──
#
# Embedded as string literals so markdown linters cannot accidentally normalize
# them before the tests run. UUIDs reference S3 bucket `aizk`.

# EU AI Ethics PDF (uuid 0000661e) — every word double-spaced by Docling PDF extraction.
EU_AI_ETHICS_RAW = (
    "## High-Level Expert Group on Artificial Intelligence\n"
    "\n"
    "This  document  was  written  by  the  High-Level  Expert  Group  on  AI"
    "  (AI  HLEG).  The  members  of  the  AI  HLEG named in this document"
    " support the overall framework for  Trustworthy AI put forward in these"
    " Guidelines, although they do not necessarily agree with every single"
    " statement in the document.\n"
    "\n"
    "A  revised  version  of  the  assessment  list,  taking  into  account"
    "  the feedback gathered through the piloting phase, will be presented to"
    " the European Commission in early 2020.\n"
    "\n"
    "Contact   Nathalie Smuha - AI HLEG Coordinator\n"
    "\n"
    "European Commission B-1049 Brussels\n"
)

# Tulu 3 PDF (uuid 019618ce) — double spaces inside a code block (Jinja template syntax).
# The `{{  '<|user|>` pattern uses intentional double-space; must be preserved.
TULU3_TEMPLATE_CODE_RAW = (
    "The chat template controls token formatting.\n"
    "\n"
    "```\n"
    "Exact implementation of our TULU 3 chat template.\n"
    "\n"
    "\"{{  '<|user|>\\n' + message['content'] + '\\n' }}\"\n"
    "\"{{  '<|assistant|>\\n'  + message['content'] + eos_token + '\\n' }}\"\n"
    "\"{{  '<|assistant|>\\n' }}\"\n"
    "```\n"
    "\n"
    "This template is used during fine-tuning.\n"
)

# SWE-agent PDF (uuid 026a32bf) — lines with trailing whitespace from figure-description
# extraction. Trailing spaces appear after quotation marks on the bullet lines.
SWE_AGENT_TRAILING_WS_RAW = (
    'Panel 3 (right, green header labeled "edit w/ Linting")\n'
    '- A prominent callout box reads: "Your proposed edit has introduced new'
    ' syntax errors" in bold with two bullets: \n'
    '  - "How your edit would have looked..." \n'
    '  - "The original before edit had: [File Viewer]" \n'
    "- Below, a blue-tinted box presents the intended improvement.\n"
)

# Photorealistic diffusion PDF (uuid 02a91012) — 4 consecutive newlines between a code
# block and the following bullet list (Docling adds blank lines around figures).
PHOTOREALISTIC_EXCESS_NEWLINES_RAW = (
    "```\n"
    "def sample(p: float):\n"
    "    for t in reversed(range(T)):\n"
    "        z_t = z_t1\n"
    "        return x_hr_t\n"
    "```\n"
    "\n"
    "\n"
    "\n"
    "- (a) Training using conditioning augmentation.\n"
    "\n"
    "(b) Sampling using conditioning augmentation.\n"
    "\n"
    "Figure A.32: Pseudo-code implementation.\n"
)

# HuggingFace static-embeddings HTML (uuid 00b2e9a4) — code block containing matrix
# output where column alignment uses double spaces (e.g., `1.0000,  0.8388`).
# Every space inside the fence must survive unchanged.
HUGGINGFACE_TENSOR_CODE_RAW = (
    "Use the `similarity` method to compare embeddings:\n"
    "\n"
    "```python\n"
    "similarities = model.similarity(embeddings, embeddings)\n"
    "print(similarities)\n"
    "# tensor([[ 1.0000,  0.8388, -0.0012],\n"
    "#         [ 0.8388,  1.0000,  0.0445],\n"
    "#         [-0.0012,  0.0445,  1.0000]])\n"
    "```\n"
    "\n"
    "Values close to 1.0 indicate high similarity.\n"
)

# GitHub HTML pipeline (uuid 9858bf92) — github/spec-kit README. Docling HTML pipeline
# right-pads each cell with spaces to align pipes; blank lines separate rows.
GITHUB_SPEC_KIT_RAW = (
    "| `--ai`                 | Option   | AI assistant to use: `claude`, `gemini`, `copilot`,"
    " `cursor-agent`, `qwen`, `opencode`, `codex`, `windsurf`, `kilocode`, `auggie`,"
    " `roo`, `codebuddy`, `amp`, `shai`, `q`, `bob`, or `qoder`"
    "                                                                                      |\n"
    "\n"
    "| `--script`             | Option   | Script variant to use: `sh` (bash/zsh) or `ps` (PowerShell)"
    "                                                                                                 |\n"
    "\n"
    "| `--ignore-agent-tools` | Flag     | Skip checks for AI agent tools like Claude Code"
    "                                                                                              |\n"
    "\n"
    "| `--no-git`             | Flag     | Skip git repository initialization"
    "                                                                                           |\n"
    "\n"
    "| `--here`               | Flag     | Initialize project in the current directory instead of creating a new one"
    "                                                                                                    |\n"
)

# General HTML pipeline (uuid 6e17bc1e) — alignmentforum.org figure-description list.
# Pattern: Docling emits bullet lines ending '- Header: \n' (trailing space before newline).
ALIGNMENT_FORUM_TRAILING_WS_RAW = (
    "- Activations row (colored emphasis): \n"
    '  - Under Description: a note that the activation involves "blackboard: shared black"'
    " with the word black highlighted.\n"
    '  - Under Local context: a cue "3. Which pass option" with "Which" highlighted.\n'
    '  - Under Succession: the phrase "suspects, aged 16 to" with "16 to" highlighted.\n'
    "- DFA (blue-highlighted row): \n"
    '  - Under Description: "blackboard: shared black"\n'
    '  - Under Local context: "3. Which pass option"\n'
    '  - Under Succession: "suspects, aged 16 to"\n'
    "- Top Logit row (textual): \n"
    '  - Under Description: "board"\n'
    '  - Under Local context: "?" (a placeholder)\n'
    '  - Under Succession: "17"\n'
)

_REAL_WORLD_FIXTURES = [
    pytest.param(EU_AI_ETHICS_RAW, id="eu_ai_ethics_pdf"),
    pytest.param(TULU3_TEMPLATE_CODE_RAW, id="tulu3_template_code"),
    pytest.param(SWE_AGENT_TRAILING_WS_RAW, id="swe_agent_trailing_ws"),
    pytest.param(PHOTOREALISTIC_EXCESS_NEWLINES_RAW, id="photorealistic_excess_newlines"),
    pytest.param(HUGGINGFACE_TENSOR_CODE_RAW, id="huggingface_tensor_code"),
    pytest.param(GITHUB_SPEC_KIT_RAW, id="github_spec_kit_html"),
    pytest.param(ALIGNMENT_FORUM_TRAILING_WS_RAW, id="alignment_forum_html"),
]


class TestIdempotence:
    """Output stability — normalizing an already-normalized string is a no-op."""

    def test_repeated_normalization_produces_identical_output(self) -> None:
        """Three successive normalizations produce identical strings."""
        raw_markdown = """# Research Paper

Abstract: This  paper  discusses  important findings...


## Introduction

The introduction section provides context  for  the  research.


### Background


The background discusses prior work in detail.

```python
def process_data(input_data):
    # Process with extra spacing
    result = process(input_data)  # comment  with  spaces
    return result
```



## Methods

We used the following methodology:

1. Data collection
2. Analysis  and  processing
3. Results  validation


- Bullet point with    extra spaces
- Another point


## Results

| Metric | Value |
|--------|-------|
| Accuracy  | 95%  |
| Precision | 92%  |



## Conclusion

The findings show significant    improvement    in performance.


---

Generated on 2026-03-21
"""
        once = normalize_whitespace(raw_markdown)
        twice = normalize_whitespace(once)
        thrice = normalize_whitespace(twice)
        assert once == twice == thrice

    @pytest.mark.parametrize(
        "original",
        [
            pytest.param("Simple text with    extra    spaces", id="extra_spaces"),
            pytest.param("Multiple\n\n\nnewlines\n\n\nhere", id="excess_newlines"),
            pytest.param(
                "Mixed:\n  - list  items\n  - with    spaces\n\n\n\nand many newlines",
                id="mixed_list_and_newlines",
            ),
            pytest.param("Code `example  with  spaces` inline", id="inline_code"),
            pytest.param("```\ncode block  with  spacing preserved\n```", id="fenced_code"),
        ],
    )
    def test_byte_identity_on_repeated_processing(self, original: str) -> None:
        """Normalized UTF-8 bytes are identical after a second pass."""
        normalized = normalize_whitespace(original)
        renormalized = normalize_whitespace(normalized)
        assert normalized.encode("utf-8") == renormalized.encode("utf-8")

    def test_hash_stability_with_normalization(self) -> None:
        """Normalizing twice produces byte-identical output and therefore the same hash."""
        raw = """# Title    Here

Content    with    varied    spacing.

```
code  block  spacing
```

More content."""
        normalized1 = normalize_whitespace(raw)
        normalized2 = normalize_whitespace(normalized1)
        assert hashlib.sha256(normalized1.encode()).hexdigest() == hashlib.sha256(normalized2.encode()).hexdigest()

    def test_realistic_conversion_output_stability(self) -> None:
        """Stability across a realistic Docling-shaped output with code, headings, and paragraphs."""
        realistic_html_output = """# DeepSeek-R1: Incentivizing Reasoning Thinking for LLMs

## Summary

This  paper  introduces DeepSeek-R1, a large language model that emphasizes reasoning and thinking processes. The model demonstrates strong performance across multiple benchmarks.


### Key Contributions

1.  A  new  training  approach  that  emphasizes reasoning
2.  Strong  empirical  results  on  multiple  benchmarks
3.  Open  source  release  of  the  model


## Introduction

Large language models have achieved impressive results...


## Method

```python
class ReasoningModel(BaseModel):
    def __init__(self, config):
        super().__init__(config)
        self.reasoning_head = ReasoningHead(config)

    def forward(self, input_ids):
        hidden = self.backbone(input_ids)
        reasoning = self.reasoning_head(hidden)
        return output, reasoning
```


## Experiments



The experiments show strong results...


## Conclusion

This work demonstrates the importance of reasoning...
"""
        normalized_v1 = normalize_whitespace(realistic_html_output)
        normalized_v2 = normalize_whitespace(normalized_v1)
        normalized_v3 = normalize_whitespace(normalized_v2)
        assert normalized_v1 == normalized_v2 == normalized_v3
        # Structure spot-checks
        assert "# DeepSeek-R1" in normalized_v1
        assert "class ReasoningModel(BaseModel):" in normalized_v1
        assert "def forward" in normalized_v1
        assert "return output, reasoning" in normalized_v1
        assert "\n\n" in normalized_v1

    @pytest.mark.parametrize("raw", _REAL_WORLD_FIXTURES)
    def test_real_world_fixture_is_idempotent(self, raw: str) -> None:
        """Every real-world fixture normalizes to a fixed point in one pass."""
        once = normalize_whitespace(raw)
        twice = normalize_whitespace(once)
        assert once == twice


class TestRealWorldPdfDoubleSpacing:
    """PDF extraction produces double-spaced prose; verify it is collapsed."""

    def test_word_level_double_spaces_collapsed(self) -> None:
        result = normalize_whitespace(EU_AI_ETHICS_RAW)
        assert "This document was written by" in result
        assert "High-Level Expert Group on AI (AI HLEG)" in result
        assert "A revised version of the assessment list, taking into account" in result

    def test_triple_space_after_label_collapsed(self) -> None:
        # "Contact   Nathalie" (3 spaces) -> "Contact Nathalie"
        result = normalize_whitespace(EU_AI_ETHICS_RAW)
        assert "Contact Nathalie Smuha" in result

    def test_original_double_spaces_absent(self) -> None:
        result = normalize_whitespace(EU_AI_ETHICS_RAW)
        # Strip code blocks before checking for residual double spaces
        stripped = re.sub(r"```[\s\S]*?```", "", result)
        assert "  " not in stripped, "Double spaces remain after normalization"

    def test_structure_preserved(self) -> None:
        result = normalize_whitespace(EU_AI_ETHICS_RAW)
        assert result.startswith("## High-Level Expert Group")
        assert "European Commission B-1049 Brussels" in result
        assert "\n\n" in result


class TestRealWorldCodeBlockPreservation:
    """Code blocks from real outputs must survive normalization unchanged."""

    def test_tulu3_template_double_spaces_preserved(self) -> None:
        result = normalize_whitespace(TULU3_TEMPLATE_CODE_RAW)
        assert "{{  '<|user|>" in result
        assert "{{  '<|assistant|>" in result
        assert "eos_token + '\\n' }}" in result

    def test_tulu3_surrounding_prose_normalized(self) -> None:
        result = normalize_whitespace(TULU3_TEMPLATE_CODE_RAW)
        assert "The chat template controls token formatting." in result
        assert "This template is used during fine-tuning." in result

    def test_huggingface_matrix_alignment_preserved(self) -> None:
        result = normalize_whitespace(HUGGINGFACE_TENSOR_CODE_RAW)
        assert "1.0000,  0.8388, -0.0012" in result
        assert " 0.8388,  1.0000,  0.0445" in result
        assert "-0.0012,  0.0445,  1.0000" in result

    def test_huggingface_prose_normalized(self) -> None:
        result = normalize_whitespace(HUGGINGFACE_TENSOR_CODE_RAW)
        assert "Use the `similarity` method to compare embeddings:" in result
        assert "Values close to 1.0 indicate high similarity." in result

    def test_code_block_content_byte_identical(self) -> None:
        # Extract fence content before and after normalization; must match
        def extract_fences(text: str) -> list[str]:
            return re.findall(r"```[\s\S]*?```", text)

        for raw in (TULU3_TEMPLATE_CODE_RAW, HUGGINGFACE_TENSOR_CODE_RAW):
            original_blocks = extract_fences(raw)
            normalized_blocks = extract_fences(normalize_whitespace(raw))
            assert original_blocks == normalized_blocks, (
                f"Code block changed after normalization in fixture starting: {raw[:60]!r}"
            )


class TestRealWorldTrailingWhitespace:
    """Trailing whitespace observed in real PDF figure-description outputs."""

    def test_trailing_spaces_stripped(self) -> None:
        result = normalize_whitespace(SWE_AGENT_TRAILING_WS_RAW)
        for line in result.splitlines():
            assert line == line.rstrip(), f"Trailing whitespace on line: {line!r}"

    def test_list_indentation_preserved(self) -> None:
        result = normalize_whitespace(SWE_AGENT_TRAILING_WS_RAW)
        assert '  - "How your edit would have looked..."' in result
        assert '  - "The original before edit had: [File Viewer]"' in result

    def test_content_unchanged_except_trailing_ws(self) -> None:
        result = normalize_whitespace(SWE_AGENT_TRAILING_WS_RAW)
        assert "Panel 3 (right, green header labeled" in result
        assert "Your proposed edit has introduced new syntax errors" in result


class TestRealWorldExcessNewlines:
    """3-4 blank lines between sections in real PDF outputs collapse to 2."""

    def test_four_newlines_collapse_to_two(self) -> None:
        result = normalize_whitespace(PHOTOREALISTIC_EXCESS_NEWLINES_RAW)
        assert "\n\n\n" not in result

    def test_content_order_preserved(self) -> None:
        result = normalize_whitespace(PHOTOREALISTIC_EXCESS_NEWLINES_RAW)
        code_pos = result.find("def sample")
        bullet_pos = result.find("(a) Training using conditioning")
        assert code_pos != -1
        assert bullet_pos != -1
        assert code_pos < bullet_pos

    def test_code_block_inside_newline_context_preserved(self) -> None:
        result = normalize_whitespace(PHOTOREALISTIC_EXCESS_NEWLINES_RAW)
        assert "for t in reversed(range(T)):" in result
        assert "z_t = z_t1" in result


class TestRealWorldGithubHtmlOutput:
    """GitHub HTML pipeline: table cell padding spaces are collapsed; inline code preserved."""

    def test_real_github_cell_padding_spaces_collapsed(self) -> None:
        result = normalize_whitespace(GITHUB_SPEC_KIT_RAW)
        assert "| Option |" in result
        assert "| Flag |" in result

    def test_real_github_inline_code_option_names_preserved(self) -> None:
        result = normalize_whitespace(GITHUB_SPEC_KIT_RAW)
        assert "`--ai`" in result
        assert "`--script`" in result
        assert "`claude`" in result

    def test_real_github_no_double_spaces_outside_code(self) -> None:
        result = normalize_whitespace(GITHUB_SPEC_KIT_RAW)
        original_inline_spans = re.findall(r"`[^`]*`", GITHUB_SPEC_KIT_RAW)
        normalized_inline_spans = re.findall(r"`[^`]*`", result)
        assert normalized_inline_spans == original_inline_spans
        # Replace inline code with a token so removal does not create artificial
        # double spaces (e.g., table cell "| `--ai` |" -> "|  |" if deleted).
        stripped = re.sub(r"`[^`]*`", "CODE", result)
        assert "  " not in stripped, "Double spaces remain outside inline code after normalization"


class TestRealWorldGeneralHtmlOutput:
    """General web HTML pipeline: trailing spaces on list-header bullets are stripped.

    Pattern: Docling emits bullet lines ending '- Header: \\n' (trailing space before newline).
    """

    def test_real_html_trailing_spaces_stripped(self) -> None:
        result = normalize_whitespace(ALIGNMENT_FORUM_TRAILING_WS_RAW)
        for line in result.splitlines():
            assert line == line.rstrip(), f"Trailing whitespace on line: {line!r}"

    def test_real_html_section_headers_normalized(self) -> None:
        result = normalize_whitespace(ALIGNMENT_FORUM_TRAILING_WS_RAW)
        assert "- Activations row (colored emphasis):\n" in result
        assert "- DFA (blue-highlighted row):\n" in result
        assert "- Top Logit row (textual):\n" in result

    def test_real_html_list_indentation_preserved(self) -> None:
        result = normalize_whitespace(ALIGNMENT_FORUM_TRAILING_WS_RAW)
        assert '  - Under Description: "blackboard: shared black"' in result
        assert '  - Under Local context: "3. Which pass option"' in result
