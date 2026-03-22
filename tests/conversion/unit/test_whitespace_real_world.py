"""Real-world fixture tests for whitespace normalization.

Excerpts are drawn from actual conversion outputs (S3 bucket aizk):
- EU AI Ethics PDF  (uuid 0000661e): classic PDF word-level double-spacing
- Tulu 3 PDF        (uuid 019618ce): template syntax inside code block (preserve)
- SWE-agent PDF     (uuid 026a32bf): trailing whitespace on lines
- Photorealistic PDF(uuid 02a91012): 3-4 excess newlines between sections
- HuggingFace HTML  (uuid 00b2e9a4): spaces inside code block must be unchanged

The excerpts are embedded as string literals so markdown linters cannot
accidentally normalize them before the tests run.
"""

import pytest

from aizk.conversion.utilities.whitespace import normalize_whitespace

# ── Fixtures (raw, pre-normalization content from real S3 outputs) ────────────

# EU AI Ethics PDF — every word double-spaced by Docling PDF extraction
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

# Tulu 3 PDF — double spaces inside a code block (Jinja template syntax).
# The {{  '<|user|> pattern uses intentional double-space; must be preserved.
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

# SWE-agent PDF — lines with trailing whitespace from figure-description extraction.
# Trailing spaces appear after quotation marks on the bullet lines.
SWE_AGENT_TRAILING_WS_RAW = (
    'Panel 3 (right, green header labeled "edit w/ Linting")\n'
    '- A prominent callout box reads: "Your proposed edit has introduced new'
    ' syntax errors" in bold with two bullets: \n'
    '  - "How your edit would have looked..." \n'
    '  - "The original before edit had: [File Viewer]" \n'
    "- Below, a blue-tinted box presents the intended improvement.\n"
)

# Photorealistic diffusion PDF — 4 consecutive newlines between a code block
# and the following bullet list (Docling adds blank lines around figures).
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

# HuggingFace static-embeddings HTML — code block containing matrix output
# where column alignment uses double spaces (e.g., `1.0000,  0.8388`).
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


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestRealWorldPdfDoubleSpacing:
    """PDF extraction produces double-spaced prose; verify it is collapsed."""

    def test_word_level_double_spaces_collapsed(self) -> None:
        result = normalize_whitespace(EU_AI_ETHICS_RAW)
        # Each double-spaced word pair should reduce to one space
        assert "This document was written by" in result
        assert "High-Level Expert Group on AI (AI HLEG)" in result
        assert "A revised version of the assessment list, taking into account" in result

    def test_triple_space_after_label_collapsed(self) -> None:
        # "Contact   Nathalie" (3 spaces) -> "Contact Nathalie"
        result = normalize_whitespace(EU_AI_ETHICS_RAW)
        assert "Contact Nathalie Smuha" in result

    def test_original_double_spaces_absent(self) -> None:
        result = normalize_whitespace(EU_AI_ETHICS_RAW)
        # No double spaces should remain outside code blocks
        import re

        # Strip code blocks before checking
        stripped = re.sub(r"```[\s\S]*?```", "", result)
        assert "  " not in stripped, "Double spaces remain after normalization"

    def test_structure_preserved(self) -> None:
        result = normalize_whitespace(EU_AI_ETHICS_RAW)
        assert result.startswith("## High-Level Expert Group")
        assert "European Commission B-1049 Brussels" in result
        # Paragraph breaks preserved
        assert "\n\n" in result

    def test_idempotent(self) -> None:
        once = normalize_whitespace(EU_AI_ETHICS_RAW)
        twice = normalize_whitespace(once)
        assert once == twice


class TestRealWorldCodeBlockPreservation:
    """Code blocks from real outputs must survive normalization unchanged."""

    def test_tulu3_template_double_spaces_preserved(self) -> None:
        result = normalize_whitespace(TULU3_TEMPLATE_CODE_RAW)
        # The intentional double spaces in Jinja template syntax must stay
        assert "{{  '<|user|>" in result
        assert "{{  '<|assistant|>" in result
        # Triple space in assistant template also preserved
        assert "eos_token + '\\n' }}" in result

    def test_tulu3_surrounding_prose_normalized(self) -> None:
        # Prose outside the code block is still subject to normalization
        result = normalize_whitespace(TULU3_TEMPLATE_CODE_RAW)
        assert "The chat template controls token formatting." in result
        assert "This template is used during fine-tuning." in result

    def test_huggingface_matrix_alignment_preserved(self) -> None:
        result = normalize_whitespace(HUGGINGFACE_TENSOR_CODE_RAW)
        # Column-aligned tensor output uses double spaces for alignment
        assert "1.0000,  0.8388, -0.0012" in result
        assert " 0.8388,  1.0000,  0.0445" in result
        assert "-0.0012,  0.0445,  1.0000" in result

    def test_huggingface_prose_normalized(self) -> None:
        result = normalize_whitespace(HUGGINGFACE_TENSOR_CODE_RAW)
        assert "Use the `similarity` method to compare embeddings:" in result
        assert "Values close to 1.0 indicate high similarity." in result

    def test_code_block_content_byte_identical(self) -> None:
        # Extract fence content before and after normalization; must match
        import re

        def extract_fences(text):
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
        # The 2-space indent before `- "How your edit..."` must be kept
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
        # Both the code block and bullet list must still appear, in order
        code_pos = result.find("def sample")
        bullet_pos = result.find("(a) Training using conditioning")
        assert code_pos != -1
        assert bullet_pos != -1
        assert code_pos < bullet_pos

    def test_code_block_inside_newline_context_preserved(self) -> None:
        # The code block itself must not be changed
        result = normalize_whitespace(PHOTOREALISTIC_EXCESS_NEWLINES_RAW)
        assert "for t in reversed(range(T)):" in result
        assert "z_t = z_t1" in result

    def test_idempotent(self) -> None:
        once = normalize_whitespace(PHOTOREALISTIC_EXCESS_NEWLINES_RAW)
        twice = normalize_whitespace(once)
        assert once == twice
