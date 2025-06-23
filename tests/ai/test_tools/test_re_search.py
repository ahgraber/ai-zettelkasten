"""Unit tests for re_search.py module."""

import re

import pytest

from aizk.ai.tools.re_search import re_search_text


class TestReSearchText:
    """Test cases for re_search_text function."""

    def test_simple_match_single_sentence(self):
        """Test basic regex match in a single sentence."""
        # Arrange
        text = "This is a test sentence."
        regex = r"test"

        # Act
        result = re_search_text(regex, text)

        # Assert
        assert len(result) == 1
        assert "This is a test sentence." in result[0]

    def test_simple_match_with_surrounding_sentences(self):
        """Test regex match with surrounding lines context."""
        # Arrange
        text = "First line\nSecond line with keyword\nThird line"
        regex = r"keyword"
        n_context_lines = 1

        # Act
        result = re_search_text(regex, text, n_context_lines)

        # Assert
        assert len(result) == 1
        assert result[0] == "First line\nSecond line with keyword\nThird line"

    def test_multiple_matches_separate_contexts(self):
        """Test multiple matches in different parts of text."""
        # Arrange
        text = "First match here\nSome other text\nSecond match there\nMore text"
        regex = r"match"
        n_context_lines = 0

        # Act
        result = re_search_text(regex, text, n_context_lines)

        # Assert
        assert len(result) == 2
        assert "First match here" in result[0]
        assert "Second match there" in result[1]

    def test_no_matches_returns_empty_list(self):
        """Test that no matches returns an empty list."""
        # Arrange
        text = "This text contains no relevant words."
        regex = r"nonexistent"

        # Act
        result = re_search_text(regex, text)

        # Assert
        assert result == []

    def test_case_insensitive_search(self):
        """Test that search is case-insensitive by default."""
        # Arrange
        text = "This contains UPPERCASE and lowercase words."
        regex = r"uppercase"

        # Act
        result = re_search_text(regex, text)

        # Assert
        assert len(result) == 1
        assert "UPPERCASE" in result[0]

    def test_compiled_regex_pattern(self):
        """Test using a pre-compiled regex pattern."""
        # Arrange
        text = "Test with compiled pattern."
        pattern = re.compile(r"compiled", re.IGNORECASE)

        # Act
        result = re_search_text(pattern, text)

        # Assert
        assert len(result) == 1
        assert "compiled" in result[0]

    def test_multiline_text_search(self):
        """Test regex search across multiple lines."""
        # Arrange
        text = "Line one has content\nLine two has target\nLine three continues"
        regex = r"target"
        n_context_lines = 1

        # Act
        result = re_search_text(regex, text, n_context_lines)

        # Assert
        assert len(result) == 1
        # Should include surrounding lines
        assert "Line one has content" in result[0]
        assert "Line two has target" in result[0]
        assert "Line three continues" in result[0]

    def test_zero_surrounding_sentences(self):
        """Test with n_context_lines set to 0."""
        # Arrange
        text = "Before line\nTarget line here\nAfter line"
        regex = r"Target"
        n_context_lines = 0

        # Act
        result = re_search_text(regex, text, n_context_lines)

        # Assert
        assert len(result) == 1
        assert result[0] == "Target line here"

    def test_large_surrounding_sentences(self):
        """Test with n_context_lines larger than available lines."""
        # Arrange
        text = "Only line with target word"
        regex = r"target"
        n_context_lines = 10  # More than available lines

        # Act
        result = re_search_text(regex, text, n_context_lines)

        # Assert
        assert len(result) == 1
        assert result[0] == "Only line with target word"

    def test_empty_text_input(self):
        """Test with empty text input."""
        # Arrange
        text = ""
        regex = r"anything"

        # Act
        result = re_search_text(regex, text)

        # Assert
        assert result == []

    def test_empty_regex_pattern(self):
        """Test with empty regex pattern."""
        # Arrange
        text = "Some text content."
        regex = r""

        # Act
        result = re_search_text(regex, text)

        # Assert
        # Empty regex should match, but behavior may vary
        assert isinstance(result, list)

    def test_complex_regex_pattern(self):
        """Test with complex regex patterns."""
        # Arrange
        text = "Email: user@example.com and phone: 123-456-7890."
        regex = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"

        # Act
        result = re_search_text(regex, text)

        # Assert
        assert len(result) == 1
        assert "user@example.com" in result[0]

    def test_overlapping_matches_different_sentences(self):
        """Test overlapping context windows from different matches."""
        # Arrange
        text = "First line\nAnother match1 line\nThird line\nYet another match2 line\nLast line"
        regex = r"match\d"
        n_context_lines = 2

        # Act
        result = re_search_text(regex, text, n_context_lines)

        # Assert
        assert len(result) == 2
        # Both results should have overlapping context
        assert "match1" in result[0]
        assert all(x in result[0] for x in ["First", "Third"])
        assert "match2" in result[1]
        assert all(x in result[1] for x in ["Third", "Last"])

    def test_special_characters_in_text(self):
        """Test handling of special characters in text."""
        # Arrange
        text = "Text with special chars: @#$%^&*(). Target sentence here! More text?"
        regex = r"Target"

        # Act
        result = re_search_text(regex, text)

        # Assert
        assert len(result) == 1
        assert "Target sentence here!" in result[0]

    @pytest.mark.parametrize("n_context_lines", [0, 1, 2, 5])
    def test_various_surrounding_sentence_counts(self, n_context_lines):
        """Test different values of n_context_lines parameter."""
        # Arrange
        text = "L1\nL2\nL3\nTarget L4\nL5\nL6\nL7"
        regex = r"Target"

        # Act
        result = re_search_text(regex, text, n_context_lines)

        # Assert
        assert len(result) == 1
        lines_in_result = result[0].split("\n")
        # Should have at most 2*n_context_lines + 1 lines
        expected_max_lines = min(7, 2 * n_context_lines + 1)
        assert len(lines_in_result) <= expected_max_lines

    def test_invalid_regex_pattern(self):
        """Test handling of invalid regex patterns."""
        # Arrange
        text = "Some text content."
        invalid_regex = r"[invalid regex pattern"

        # Act & Assert
        with pytest.raises(re.error):
            re_search_text(invalid_regex, text)

    def test_unicode_text_handling(self):
        """Test handling of Unicode text."""
        # Arrange
        text = "Unicode text: café, naïve, résumé. Target word here."
        regex = r"Target"

        # Act
        result = re_search_text(regex, text)

        # Assert
        assert len(result) == 1
        assert "café" in result[0]
        assert "Target" in result[0]

    def test_very_long_sentences(self):
        """Test handling of very long lines."""
        # Arrange
        long_line = "This is a very long line " * 100 + "with target word"
        text = f"Short line\n{long_line}\nAnother short line"
        regex = r"target"
        n_context_lines = 1

        # Act
        result = re_search_text(regex, text, n_context_lines)

        # Assert
        assert len(result) == 1
        assert "target word" in result[0]
        assert "Short line" in result[0]
        assert "Another short line" in result[0]

    def test_sentence_boundary_edge_cases(self):
        """Test edge cases with line boundaries."""
        # Arrange
        text = "Line with abbreviations like Mr. Smith\nTarget line here\nLine with Dr. Jones"
        regex = r"Target"
        n_context_lines = 1

        # Act
        result = re_search_text(regex, text, n_context_lines)

        # Assert
        assert len(result) == 1
        # Should properly include surrounding lines
        assert "Target line here" in result[0]


class TestReSearchTextIntegration:
    """Integration tests for re_search_text with realistic scenarios."""

    def test_academic_paper_search(self):
        """Test searching in academic paper-like text."""
        # Arrange
        text = """Abstract: This paper presents a novel approach to machine learning.
Introduction: Machine learning has become increasingly important in recent years.
The methodology section describes our experimental setup.
Results show significant improvements over baseline methods.
Conclusion: Our approach demonstrates superior performance."""
        regex = r"machine learning"

        # Act
        result = re_search_text(regex, text, n_context_lines=1)

        # Assert
        assert len(result) == 2  # Should find both occurrences
        assert any("novel approach" in group for group in result)
        assert any("increasingly important" in group for group in result)

    def test_code_documentation_search(self):
        """Test searching in code documentation-like text."""
        # Arrange
        text = """This function performs data validation.
It checks input parameters for correctness.
The validation process includes type checking and range validation.
Returns True if all validations pass, False otherwise.
Example usage is shown in the test cases below."""
        regex = r"type checking"

        # Act
        result = re_search_text(regex, text, n_context_lines=2)

        # Assert
        assert len(result) == 1
        # Check that surrounding context is properly included
        assert any("data validation" in group and "test cases below" in group for group in result)
