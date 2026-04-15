"""Unit tests for whitespace normalization stability.

Verifies normalization idempotency and output stability for realistic
Markdown-shaped inputs.
"""

import pytest

from aizk.conversion.utilities.whitespace import normalize_whitespace


class TestWhitespaceStability:
    """Verify whitespace normalization ensures output stability."""

    def test_repeated_normalization_produces_identical_output(self) -> None:
        """Test that applying normalization multiple times is idempotent."""
        # Simulate realistic Markdown output from Docling with whitespace artifacts
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

        # First run: normalize the raw output
        normalized_once = normalize_whitespace(raw_markdown)

        # Second run: normalize the already normalized output
        normalized_twice = normalize_whitespace(normalized_once)

        # Third run: ensure stability
        normalized_thrice = normalize_whitespace(normalized_twice)

        # All runs should produce identical output
        assert normalized_once == normalized_twice, "Output differs on second normalization"
        assert normalized_twice == normalized_thrice, "Output differs on third normalization"

    def test_byte_identity_on_repeated_processing(self) -> None:
        """Test that normalized output is byte-identical on repeated normalizations."""
        test_cases = [
            "Simple text with    extra    spaces",
            "Multiple\n\n\nnewlines\n\n\nhere",
            "Mixed:\n  - list  items\n  - with    spaces\n\n\n\nand many newlines",
            "Code `example  with  spaces` inline",
            "```\ncode block  with  spacing preserved\n```",
        ]

        for original in test_cases:
            # Get normalized version
            normalized = normalize_whitespace(original)

            # Convert to bytes (as it would be written to disk)
            bytes_first = normalized.encode("utf-8")

            # Normalize again
            renormalized = normalize_whitespace(normalized)
            bytes_second = renormalized.encode("utf-8")

            # Bytes must be identical
            assert bytes_first == bytes_second, f"Byte identity lost for: {original!r}"

    def test_hash_stability_with_normalization(self) -> None:
        """Normalizing twice produces byte-identical output and therefore the same hash."""
        import hashlib

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
        """Test stability with realistic Docling conversion output."""
        # This simulates actual output patterns observed in the conversion database
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

        # Process the markdown through normalization
        normalized_v1 = normalize_whitespace(realistic_html_output)
        normalized_v2 = normalize_whitespace(normalized_v1)
        normalized_v3 = normalize_whitespace(normalized_v2)

        # Verify idempotence
        assert normalized_v1 == normalized_v2 == normalized_v3

        # Verify structure is preserved
        assert "# DeepSeek-R1" in normalized_v1
        assert "class ReasoningModel(BaseModel):" in normalized_v1
        assert "def forward" in normalized_v1
        assert "return output, reasoning" in normalized_v1

        # Verify key structural elements
        assert "\n\n" in normalized_v1, "Paragraph structure preserved"
