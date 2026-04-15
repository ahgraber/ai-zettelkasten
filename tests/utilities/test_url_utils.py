from pydantic import ValidationError as PydanticValidationError
import pytest
from validators import ValidationError as URLValidatorValidationError

from aizk.utilities.url_utils import (
    clean_markdown_title,
    extract_domain,
    extract_markdown_urls,
    extract_urls,
    fix_url_from_markdown,
    is_social_url,
    normalize_url,
    safelink_to_url,
    strip_utm_params,
    validate_url,
)


class TestExtractURLs:
    # https://mathiasbynens.be/demo/url-regex

    @pytest.mark.parametrize(
        "url",
        [
            "http://foo.com/blah_blah",
            "http://foo.com/blah_blah/",
            "http://foo.com/blah_blah_(wikipedia)",
            "http://foo.com/blah_blah_(wikipedia)_(again)",
            "http://www.example.com/wpstyle/?p=364",
            "https://www.example.com/foo/?bar=baz&inga=42&quux",
            "http://✪df.ws/123",
            # "http://userid:password@example.com:8080",
            # "http://userid:password@example.com:8080/",
            # "http://userid@example.com",
            # "http://userid@example.com/",
            # "http://userid@example.com:8080",
            # "http://userid@example.com:8080/",
            # "http://userid:password@example.com",
            # "http://userid:password@example.com/",
            # "http://142.42.1.1/",
            # "http://142.42.1.1:8080/",
            "http://➡.ws/䨹",
            "http://⌘.ws",
            "http://⌘.ws/",
            "http://foo.com/blah_(wikipedia)#cite-1",
            "http://foo.com/blah_(wikipedia)_blah#cite-1",
            "http://foo.com/unicode_(✪)_in_parens",
            "http://foo.com/(something)?after=parens",
            "http://☺.damowmow.com/",
            "http://code.google.com/events/#&product=browser",
            "http://j.mp",
            "ftp://foo.bar/baz",
            "http://foo.bar/?q=Test%20URL-encoded%20stuff",
            "http://مثال.إختبار",
            "http://例子.测试",
            "http://उदाहरण.परीक्षा",
            # "http://-.~_!$&'()*+,;=:%40:80%2f::::::@example.com",
            "http://1337.net",
            "http://a.b-c.de",
            # "http://a.b-c.de.", # This is technically valid but will cause more issues than it's worth
            # "http://223.255.255.254",
        ],
    )
    def test_valid_urls_extracted_correctly(self, url):
        """Test that valid URLs are extracted correctly by extract_urls function."""
        extract = extract_urls(url)
        assert extract is not None and len(extract) > 0, f"Failed to extract URL: {url}"
        result = extract[0]
        assert url == result, f"Failed to match {url=}: {result=}"

    @pytest.mark.parametrize(
        "invalid_url",
        [
            "http://",
            "http://.",
            "http://..",
            "http://../",
            "http://?",
            "http://??",
            "http://??/",
            "http://#",
            "http://##",
            "http://##/",
            "http://foo.bar?q=Spaces should be encoded",
            "//",
            "//a",
            "///a",
            "///",
            "http:///a",
            "foo.com",
            "rdar://1234",
            "h://test",
            "http:// shouldfail.com",
            ":// should fail",
            "http://foo.bar/foo(bar)baz quux",
            # "ftps://foo.bar/",
            "http://-error-.invalid/",
            # "http://a.b--c.de/",
            "http://-a.b.co",
            "http://a.b-.co",
            "http://0.0.0.0",
            "http://10.1.1.0",
            "http://10.1.1.255",
            "http://224.1.1.1",
            "http://1.1.1.1.1",
            "http://123.123.123",
            "http://3628126748",
            "http://.www.foo.bar/",
            # "http://www.foo.bar./",
            "http://.www.foo.bar./",
            "http://10.1.1.1",
        ],
    )
    def test_invalid_urls_not_extracted(self, invalid_url):
        """Test that invalid URLs are not extracted or don't match exactly."""
        extract = extract_urls(invalid_url)
        if extract is None or len(extract) == 0:
            # No URL extracted - this is expected for invalid URLs
            return

        result = extract[0]
        # If a URL was extracted, it should not match the original invalid URL exactly
        assert invalid_url != result, f"Invalid URL was incorrectly matched: {invalid_url=}: {result=}"

    @pytest.mark.parametrize(
        "markdown_text,expected_urls",
        [
            # Inline markdown links: [title](url)
            ("Check out [Google](https://google.com) for search", ["https://google.com"]),
            (
                "Multiple links: [GitHub](https://github.com) and [Stack Overflow](https://stackoverflow.com)",
                ["https://github.com", "https://stackoverflow.com"],
            ),
            # Raw URLs in text
            ("Visit https://example.com for more info", ["https://example.com"]),
            (
                "URLs like https://example.com and http://test.org work great",
                ["https://example.com", "http://test.org"],
            ),
            # Mixed markdown links and raw URLs
            (
                "See [documentation](https://docs.example.com) or visit https://example.com directly",
                ["https://docs.example.com", "https://example.com"],
            ),
            # URLs in angle brackets (common in markdown)
            ("Contact us at <https://contact.example.com>", ["https://contact.example.com"]),
            # Complex markdown with multiple formats
            (
                """
                # My Article

                Check out [[2101.00001] arXiv paper](https://arxiv.org/abs/2101.00001) and the
                GitHub repo at https://github.com/user/repo.

                Also see:
                - [Hugging Face](https://huggingface.co/models)
                - Direct link: https://pytorch.org
                - Contact: <https://support.example.com>
                """,
                [
                    "https://arxiv.org/abs/2101.00001",
                    "https://github.com/user/repo",
                    "https://huggingface.co/models",
                    "https://pytorch.org",
                    "https://support.example.com",
                ],
            ),
            # URLs with complex paths and parameters
            (
                "API docs at [OpenAI](https://api.openai.com/v1/models?limit=10) and https://example.com/path/to/resource?param=value&other=123#section",
                [
                    "https://api.openai.com/v1/models?limit=10",
                    "https://example.com/path/to/resource?param=value&other=123#section",
                ],
            ),
            # URLs with special characters and Unicode
            (
                "Unicode domains: [测试](http://例子.测试) and http://مثال.إختبار",
                ["http://例子.测试", "http://مثال.إختبار"],
            ),
            # URLs with parentheses (common in academic citations)
            ("See paper (https://arxiv.org/abs/2000.56789) for details", ["https://arxiv.org/abs/2000.56789"]),
            # URLs in code blocks (should still be extracted)
            ("```\nGET https://api.example.com/v1/users\n```", ["https://api.example.com/v1/users"]),
            # Multiple URLs in the same line
            (
                "Compare https://site1.com vs https://site2.com vs https://site3.com",
                ["https://site1.com", "https://site2.com", "https://site3.com"],
            ),
            # HTML-style links in markdown
            ('Visit <a href="https://example.com">our website</a>', ["https://example.com"]),
        ],
    )
    def test_extract_urls_from_markdown_text(self, markdown_text, expected_urls):
        """Test that extract_urls can successfully extract various URL formats from markdown-formatted text."""
        extracted_urls = extract_urls(markdown_text)

        # Check that we extracted the expected number of URLs
        assert len(extracted_urls) == len(expected_urls), (
            f"Expected {len(expected_urls)} URLs but extracted {len(extracted_urls)}. "
            f"Expected: {expected_urls}, Got: {extracted_urls}"
        )

        # Check that each expected URL is in the extracted list
        for expected_url in expected_urls:
            assert expected_url in extracted_urls, (
                f"Expected URL '{expected_url}' not found in extracted URLs: {extracted_urls}"
            )

    def test_extract_urls_empty_text_raises_error(self):
        """Test that extract_urls raises ValueError for empty text."""
        with pytest.raises(ValueError, match="Text cannot be empty"):
            extract_urls("")

    def test_extract_urls_no_urls_returns_empty_list(self):
        """Test that extract_urls returns empty list when no URLs are present."""
        text_without_urls = "This is just plain text with no URLs whatsoever."
        result = extract_urls(text_without_urls)
        assert result == []

    def test_extract_urls_preserves_url_order(self):
        """Test that extract_urls preserves the order of URLs as they appear in text."""
        text = "First https://first.com then https://second.com and finally https://third.com"
        expected_order = ["https://first.com", "https://second.com", "https://third.com"]

        result = extract_urls(text)
        assert result == expected_order, f"URL order not preserved. Expected: {expected_order}, Got: {result}"


class TestValidateURL:
    """Test URL validation function."""

    @pytest.mark.parametrize(
        "input_url,expected_output",
        [
            # Basic valid URLs
            ("https://example.com", "https://example.com/"),
            ("http://test.org", "http://test.org/"),
            ("https://subdomain.example.com", "https://subdomain.example.com/"),
            # URLs with paths
            ("https://example.com/path", "https://example.com/path"),
            ("https://example.com/path/to/resource", "https://example.com/path/to/resource"),
            ("https://example.com/path/", "https://example.com/path/"),
            # URLs with query parameters
            ("https://example.com?param=value", "https://example.com/?param=value"),
            ("https://example.com/search?q=test", "https://example.com/search?q=test"),
            ("http://foo.bar?q=Spaces should be encoded", "http://foo.bar/?q=Spaces%20should%20be%20encoded"),
            # URLs with fragments
            ("https://example.com#section", "https://example.com/#section"),
            ("https://example.com/page#anchor", "https://example.com/page#anchor"),
            # URLs with ports
            ("https://example.com:8080", "https://example.com:8080/"),
            # ("http://localhost:3000", "http://localhost:3000/"),
            ("https://api.example.com:443/v1", "https://api.example.com/v1"),  # Pydantic normalizes default ports
            # International domains - Pydantic converts them to punycode
            ("https://例え.テスト", "https://xn--r8jz45g.xn--zckzah/"),
            ("https://مثال.إختبار", "https://xn--mgbh0fb.xn--kgbechtv/"),
            # # URLs with authentication
            # ("https://user:pass@example.com", "https://user:pass@example.com/"),
            # Complex URLs
            (
                "https://example.com/path?param=value&other=123#section",
                "https://example.com/path?param=value&other=123#section",
            ),
            (
                "https://api.example.com/v1/users/123?include=profile&format=json",
                "https://api.example.com/v1/users/123?include=profile&format=json",
            ),
            # URLs with special characters in path
            ("https://example.com/path_(info)", "https://example.com/path_(info)"),
            ("https://example.com/path-with-dashes", "https://example.com/path-with-dashes"),
            ("https://example.com/path_with_underscores", "https://example.com/path_with_underscores"),
            # URLs with encoded characters
            ("https://example.com/search?q=hello%20world", "https://example.com/search?q=hello%20world"),
            # Edge cases with whitespace (should be trimmed) - Pydantic auto-trims
            ("  https://example.com  ", "https://example.com/"),
            ("\thttps://example.com\n", "https://example.com/"),
        ],
    )
    def test_validate_url_success_cases(self, input_url: str, expected_output: str):
        """Test that valid URLs are properly validated and normalized."""
        result = validate_url(input_url)
        # validate_url returns HttpUrl objects, so convert to string for comparison
        assert str(result) == expected_output

    @pytest.mark.parametrize(
        "invalid_url",
        [
            # Empty and None-like inputs - these should raise ValueError, not ValidationError
            # They are tested separately in test_validate_url_empty_input_value_error
            # Invalid schemes
            "htp://example.com",  # typo in scheme
            "file://local/path",  # file scheme not supported by HttpUrl
            "javascript:alert('xss')",  # javascript scheme
            "data:text/html,<script>alert('xss')</script>",  # data scheme
            # Malformed URLs
            "http://",
            "https://",
            "http://.com",
            "https://.",
            "http://..",
            "https:///path",
            # Invalid domains
            "http://example.",
            "https://.example.com",
            "http://example..com",
            "https://example-.com",
            "http://-example.com",
            # Invalid characters
            "https://exam ple.com",  # space in domain
            "https://example .com",  # space in domain
            "http://example.com:abc",  # non-numeric port
            "https://example.com:-80",  # negative port
            # Local file paths
            "/local/file/path",
            "./relative/path",
            "C:\\Windows\\file.txt",
            # Just domain without scheme
            "example.com",
            "www.example.com",
            "subdomain.example.com",
            # Invalid protocols for HttpUrl
            "mailto:user@example.com",
            "tel:+1234567890",
            "sms:+1234567890",
        ],
    )
    def test_validate_url_validation_error(self, invalid_url: str):
        """Test that invalid URLs raise ValidationError."""
        with pytest.raises((PydanticValidationError, URLValidatorValidationError, ValueError)):
            validate_url(invalid_url)

    @pytest.mark.parametrize(
        "empty_input",
        [
            None,
            "",
            "   ",
            "\t",
            "\n",
            "  \t\n  ",
        ],
    )
    def test_validate_url_empty_input_value_error(self, empty_input):
        """Test that empty or None inputs raise ValidationError."""
        with pytest.raises((PydanticValidationError, URLValidatorValidationError, ValueError)):
            validate_url(empty_input)

    def test_validate_url_strips_whitespace(self):
        """Test that leading/trailing whitespace is properly stripped."""
        url_with_whitespace = "  \t https://example.com/path \n "
        result = validate_url(url_with_whitespace)
        assert str(result) == "https://example.com/path"

    def test_validate_url_preserves_complex_query_params(self):
        """Test that complex query parameters are preserved correctly."""
        complex_url = "https://api.example.com/search?q=test+query&filters[category]=tech&sort=date&limit=10"
        result = validate_url(complex_url)
        # The exact encoding might vary, but should be a valid URL
        result_str = str(result)
        assert result_str.startswith("https://api.example.com/search?")
        assert "q=test" in result_str
        assert "category" in result_str

    def test_validate_url_handles_unicode_domains(self):
        """Test that international domain names are handled correctly."""
        unicode_urls = [
            "https://例え.テスト",
            "https://мой.сайт",
            "https://مثال.إختبار",
        ]
        for url in unicode_urls:
            result = validate_url(url)
            result_str = str(result)
            assert result_str.startswith("https://")
            # Should not raise an exception

    @pytest.mark.parametrize(
        "url_with_auth",
        [
            "https://user:password@example.com",
            "https://user:pass@api.example.com/v1",
        ],
    )
    def test_validate_url_handles_authentication(self, url_with_auth: str):
        """Test that URLs with authentication info are handled correctly."""
        result = validate_url(url_with_auth)
        result_str = str(result)
        assert "://" in result_str
        # Should not raise an exception


class TestExtractMarkdownURL:
    def test_md_urls_in_text(self):
        testcases = [
            """This is a test with a [link](https://example.com) in it.""",
            """[link 1](https://this.is/a/test) [link 2](https://example.com) [link 3](https://www.dubdubdub.com)""",
            """""",
        ]
        expected = [
            [("link", "https://example.com")],
            [
                ("link 1", "https://this.is/a/test"),
                ("link 2", "https://example.com"),
                ("link 3", "https://www.dubdubdub.com"),
            ],
        ]

        for t, e in zip(testcases, expected):
            assert extract_markdown_urls(t) == e

    def test_special_chars(self):
        text = "[Title!@#$%](https://special.com)"
        assert extract_markdown_urls(text) == [("Title!@#$%", "https://special.com")]

    def test_no_urls(self):
        assert extract_markdown_urls("This is a test with no urls.") == []

    def test_empty_text(self):
        with pytest.raises(ValueError, match="Text cannot be empty"):
            extract_markdown_urls("")

    def test_raw_urls(self):
        assert extract_markdown_urls("http://this.is/a/test") == []

    def test_html_urls(self):
        # HTML links are actually extracted by the implementation
        assert extract_markdown_urls("<a href='https://example.com'>link</a>") == [("link", "https://example.com")]


class TestCleanMarkdownTitle:
    def test_basic_cleaning(self):
        assert clean_markdown_title("Simple Title") == "Simple Title"

    def test_remove_escapes(self):
        assert clean_markdown_title("\\[Title\\]") == "Title"

    def test_empty_string(self):
        with pytest.raises(ValueError):
            clean_markdown_title("")

    @pytest.mark.parametrize(
        "input_text,expected_output",
        [
            ("Multi  Space\n\nTitle", "Multi Space Title"),
            ("Title\t\twith\ttabs", "Title with tabs"),
            ("Title   with   spaces", "Title with spaces"),
            ("Title\nwith\nnewlines", "Title with newlines"),
            ("Title\r\nwith\r\nCRLF", "Title with CRLF"),
            ("Mixed\t \n\r whitespace", "Mixed whitespace"),
            ("Single space", "Single space"),
            ("NoSpaces", "NoSpaces"),
        ],
    )
    def test_whitespace_handling(self, input_text: str, expected_output: str):
        """Test that various whitespace characters are normalized to single spaces."""
        assert clean_markdown_title(input_text) == expected_output

    @pytest.mark.parametrize(
        "input_text,expected_output",
        [
            ("[2000.56789] This is an arxiv title", "[2000.56789] This is an arxiv title"),
            ("\\[2000.56789\\] This is an arxiv title", "[2000.56789] This is an arxiv title"),
            ("[[2000.56789] This is an arxiv title]", "[2000.56789] This is an arxiv title"),
            ("[\\[2000.56789\\] This is an arxiv title]", "[2000.56789] This is an arxiv title"),
            ("\\[\\[2000.56789\\] This is an arxiv title\\]", "[2000.56789] This is an arxiv title"),
            (
                "[[2000.56789] This is an arxiv title](https://arxiv.org/abs/2000.56789)",
                "[[2000.56789] This is an arxiv title](https://arxiv.org/abs/2000.56789)",
            ),
            (
                "[\\[2000.56789\\] This is an arxiv title](https://arxiv.org/abs/2000.56789)",
                "[[2000.56789] This is an arxiv title](https://arxiv.org/abs/2000.56789)",
            ),
            (
                "\\[\\[2000.56789\\] This is an arxiv title\\](https://arxiv.org/abs/2000.56789)",
                "[[2000.56789] This is an arxiv title](https://arxiv.org/abs/2000.56789)",
            ),
        ],
    )
    def test_escaped_brackets(self, input_text: str, expected_output: str):
        """Test that escaped brackets are properly handled."""
        assert clean_markdown_title(input_text) == expected_output


class TestFixURLFromMarkdown:
    def test_balanced_url(self):
        url = "https://example.com/page(info)"
        assert fix_url_from_markdown(url) == url

    def test_unbalanced_trailing(self):
        url = "https://wikipedia.org/article_(topic).html)"
        assert fix_url_from_markdown(url) == "https://wikipedia.org/article_(topic).html"

    def test_multiple_trailing(self):
        url = "https://example.com/page_(info)))"
        assert fix_url_from_markdown(url) == "https://example.com/page_(info)"

    def test_nested_parens(self):
        url = "https://example.com/page_((nested))"
        assert fix_url_from_markdown(url) == url

    def test_no_parens(self):
        url = "https://example.com/plain"
        assert fix_url_from_markdown(url) == url

    def test_invalid_url(self):
        url = "not_a_url_(test)"
        assert fix_url_from_markdown(url) == url

    def test_complex_case(self):
        url = "https://wikipedia.org/en/article_(Topic1)_(Topic2).html?param=value)"
        assert fix_url_from_markdown(url) == "https://wikipedia.org/en/article_(Topic1)_(Topic2).html?param=value"

    def test_partial_md_link(self):
        """Case if url regex detects a markdown link in the format [url](url)"""
        url = "https://openai.com/index/hello-gpt-4o/](https://openai.com/index/hello-gpt-4o/)"
        assert fix_url_from_markdown(url) == "https://openai.com/index/hello-gpt-4o/"


class TestStripUTMParams:
    def test_basic_url(self):
        url = "https://example.com"
        assert strip_utm_params(url) == url

    @pytest.mark.parametrize(
        "test_input,expected",
        [
            ("https://example.com?utm_source=test", "https://example.com"),
            ("https://example.com?id=123&utm_medium=email", "https://example.com?id=123"),
            ("https://example.com?utm_source=test&utm_medium=email", "https://example.com"),
            ("https://example.com/path?param=value&utm_source=test", "https://example.com/path?param=value"),
            ("https://example.com?utm_source=test#fragment", "https://example.com#fragment"),
        ],
    )
    def test_strip_utm_params(self, test_input, expected):
        assert strip_utm_params(test_input) == expected

    def test_invalid_url(self):
        url = "not_a_url_(test)"
        assert strip_utm_params(url) == url


class TestSafeLinkToURL:
    def test_safe_link(self):
        testcases = [
            (
                r"https://nam11.safelinks.protection.outlook.com/?url=https%3A%2F%2Fgithub.com%2FY2Z%2Fmonolith&data=05%7C02%7Cfake.email%40domain.com%7C3c9102cd3e7a4f28d75e08dc4ff0da05%7Cefa022f42c0246d8b6141b43989d652f%7C0%7C0%7C638473146309812612%7CUnknown%7CTWFpbGZsb3d8eyJWIjoiMC4wLjAwMDAiLCJQIjoiV2luMzIiLCJBTiI6Ik1haWwiLCJXVCI6Mn0%3D%7C40000%7C%7C%7C&sdata=MTRi%2FZxc1GwuazO%2BhltFhMyYrKbNGxgVAB%2F7ayQulz8%3D&reserved=0",
                "https://github.com/Y2Z/monolith",
            ),
            (
                r"https://nam11.safelinks.protection.outlook.com/?url=https%3A%2F%2Fwww.wired.com%2Fstory%2Feight-google-employees-invented-modern-ai-transformers-paper%2F&data=05%7C02%7Cfake.email%40domain.com%7C3c9102cd3e7a4f28d75e08dc4ff0da05%7Cefa022f42c0246d8b6141b43989d652f%7C0%7C0%7C638473146309824261%7CUnknown%7CTWFpbGZsb3d8eyJWIjoiMC4wLjAwMDAiLCJQIjoiV2luMzIiLCJBTiI6Ik1haWwiLCJXVCI6Mn0%3D%7C40000%7C%7C%7C&sdata=Y9x%2BuHaOhwoRod7Lz1wbcn73uHYjgxPpNC%2B%2FvLtyW2Q%3D&reserved=0",
                "https://www.wired.com/story/eight-google-employees-invented-modern-ai-transformers-paper/",
            ),
        ]
        for case in testcases:
            assert safelink_to_url(case[0]) == case[1]


class TestIsSocialUrl:
    """Test the is_social_url function for detecting social media URLs."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://linkedin.com/in/someone",
            "https://twitter.com/user",
            "https://x.com/user",
            "https://bsky.app/profile/user",
            "https://facebook.com/user",
            "https://instagram.com/user",
            "https://threads.net/@user",
        ],
    )
    def test_social_urls_exact_domain(self, url):
        """Test that exact social media domain URLs are detected correctly."""
        assert is_social_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.linkedin.com/in/someone",
            "https://www.twitter.com/user",
            "https://www.x.com/user",
            "https://www.bsky.app/profile/user",
            "https://www.facebook.com/user",
            "https://www.instagram.com/user",
            "https://www.threads.net/@user",
            "https://mobile.twitter.com/user",
            "https://api.twitter.com/user",
            "https://subdomain.linkedin.com/page",
        ],
    )
    def test_social_urls_with_subdomains(self, url):
        """Test that social media URLs with subdomains (www, mobile, etc.) are detected correctly."""
        assert is_social_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/not-social",
            "https://www.example.com/not-social",
            "https://github.com/user/repo",
            "https://arxiv.org/abs/2000.56789",
            "https://google.com/search",
            "https://linkedin.co.uk/in/someone",  # Different TLD
            "https://twitter.com.br/user",  # Different TLD
        ],
    )
    def test_non_social_urls(self, url):
        """Test that non-social media URLs are not detected as social."""
        assert is_social_url(url) is False

    def test_invalid_url(self):
        """Test that invalid URLs raise appropriate errors."""
        with pytest.raises((PydanticValidationError, URLValidatorValidationError, ValueError)):
            is_social_url("not-a-url")


class TestNormalizeURL:
    """Dedupe-normalization tests; moved here from tests/conversion/unit/ so the
    url-utils spec's declared test locations match code location.
    """

    def test_sorts_query_and_drops_fragment(self):
        url = "https://Example.com/path?b=2&a=1#section"
        assert normalize_url(url) == "https://example.com/path?a=1&b=2"

    def test_strips_www(self):
        assert normalize_url("https://www.example.com/path") == normalize_url("https://example.com/path")

    def test_strips_www_preserves_path_query(self):
        result = normalize_url("https://www.example.com/path?b=2&a=1")
        assert "www" not in result
        assert "/path" in result
        assert "a=1" in result
        assert result.index("a=1") < result.index("b=2")

    def test_idempotent(self):
        url = "https://www.example.com/path?utm_source=test&b=2&a=1#section"
        once = normalize_url(url)
        assert normalize_url(once) == once

    def test_idempotent_github(self):
        url = "https://github.com/owner/repo?utm_source=test"
        once = normalize_url(url)
        assert normalize_url(once) == once


class TestNormalizeURLDeduplication:
    """URL deduplication across www/UTM/query-order/fragment variations."""

    def test_dedup_www_variants(self):
        urls = ["https://www.example.com/path", "https://example.com/path"]
        assert len({normalize_url(u) for u in urls}) == 1

    def test_dedup_utm_params(self):
        urls = [
            "https://example.com/path?utm_source=email&utm_medium=newsletter",
            "https://example.com/path",
        ]
        assert len({normalize_url(u) for u in urls}) == 1

    def test_dedup_query_param_order(self):
        urls = ["https://example.com/path?a=1&b=2", "https://example.com/path?b=2&a=1"]
        assert len({normalize_url(u) for u in urls}) == 1

    def test_dedup_fragment_ignored(self):
        urls = ["https://example.com/path#section1", "https://example.com/path"]
        assert len({normalize_url(u) for u in urls}) == 1

    def test_dedup_combined_variations(self):
        urls = [
            "https://www.example.com/path?a=1&utm_source=test&b=2#section",
            "https://example.com/path?b=2&a=1",
            "https://WWW.EXAMPLE.COM/path?b=2&a=1#other",
        ]
        assert len({normalize_url(u) for u in urls}) == 1

    def test_idempotent_deduplication(self):
        urls = [
            "https://www.example.com/path?utm_source=test&b=2&a=1#section",
            "https://example.com/path?b=2&a=1",
        ]
        once = {normalize_url(u) for u in urls}
        twice = {normalize_url(u) for u in once}
        assert once == twice
        assert len(once) == 1


class TestExtractDomain:
    def test_extracts_from_valid_url(self):
        assert extract_domain("https://github.com/owner/repo") == "github.com"
        assert extract_domain("https://www.example.com/path") == "www.example.com"
        assert extract_domain("https://example.com:8080/path") == "example.com:8080"

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError):
            extract_domain("not a url")
        with pytest.raises(ValueError):
            extract_domain("")

    def test_rejects_malformed_urls(self):
        with pytest.raises((ValueError, Exception)):
            extract_domain("https://exa mple.com/path")
        with pytest.raises((ValueError, Exception)):
            extract_domain("https://github.com:bad/path")


class TestExtractURLsDedup:
    """Dedup-specific extract_urls cases not covered by TestExtractURLs above."""

    def test_balanced_parens_in_markdown_link(self):
        urls = extract_urls("[link](https://en.wikipedia.org/wiki/Example_(term))")
        assert "https://en.wikipedia.org/wiki/Example_(term)" in urls

    def test_no_duplicates_on_overlap(self):
        """URL appearing in both markdown and bare form is extracted once."""
        urls = extract_urls("[link](https://example.com) https://example.com")
        assert urls.count("https://example.com") == 1
