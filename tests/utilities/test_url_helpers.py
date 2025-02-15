from unittest.mock import Mock, patch

from pydantic import ValidationError
import pytest

import requests
import responses  # For mocking HTTP requests

from aizk.utilities.url_helpers import (
    _process_url,
    clean_link_title,
    clean_md_artifacts,
    emergentmind_to_arxiv,
    extract_md_url,
    extract_url,
    fix_url_from_markdown,
    follow_redirects,
    huggingface_to_arxiv,
    safelink_to_url,
    standardize_arxiv,
    standardize_github,
    strip_utm_params,
    validate_url,
)


class TestExtractURL:
    # https://mathiasbynens.be/demo/url-regex

    @pytest.fixture
    def expected_pass(self):
        return [
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
            # "http://223.255.255.254",
        ]

    @pytest.fixture
    def expected_fail(self):
        return [
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
        ]

    def test_pass(self, expected_pass):
        for url in expected_pass:
            # print(f"testing {url}")
            extract = extract_url(url)
            if extract is None or len(extract) == 0:  # NOQA:SIM108
                # print(f"Failed to identify url in {url}")
                # continue
                result = None
            else:
                result = extract[0]

            assert url == result, f"Failed to match {url=}: {result=}"

    def test_fail(self, expected_fail):
        for url in expected_fail:
            # print(f"testing {url}")
            extract = extract_url(url)
            if extract is None or len(extract) == 0:  # NOQA:SIM108
                # print(f"Failed to identify url in {url}")
                # continue
                result = None
            else:
                result = extract[0]

            with pytest.raises(AssertionError):
                assert url == result

    def test_validate_pass(self, expected_pass):
        # # expected_pass aren't necessarily a direct passthrough for validation...
        # for url in expected_pass:
        #     assert url == validate_url(url)

        # check edge cases like encoding spaces
        assert (
            validate_url("http://foo.bar?q=Spaces should be encoded")
            == "http://foo.bar/?q=Spaces%20should%20be%20encoded"
        )

    def test_validate_fail(self, expected_fail):
        pass  # expected_fail aren't necessarily failure modes for validation...


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
            assert extract_md_url(t) == e

    def test_whitespace_handling(self):
        text = "[Multi  Space\n\nTitle](http://test.com)"
        assert extract_md_url(text) == [("Multi Space Title", "http://test.com")]

    def test_escaped_brackets(self):
        text = "\\[Title\\](http://escaped.com)"
        assert extract_md_url(text) == [("Title", "http://escaped.com")]

    def test_special_chars(self):
        text = "[Title!@#$%](https://special.com)"
        assert extract_md_url(text) == [("Title!@#$%", "https://special.com")]

    def test_no_urls(self):
        assert extract_md_url("This is a test with no urls.") == []

    def test_empty_text(self):
        assert extract_md_url("") == []

    def test_raw_urls(self):
        assert extract_md_url("http://this.is/a/test") == []

    def test_html_urls(self):
        assert extract_md_url("<a href='https://example.com'>link</a>") == []


class TestCleanLinkTitle:
    def test_basic_cleaning(self):
        assert clean_link_title("Simple Title") == "Simple Title"

    def test_remove_escapes(self):
        assert clean_link_title("\\[Title\\]") == "[Title]"

    def test_remove_url_part(self):
        assert clean_link_title("Title](https://example.com") == "Title"

    def test_combined_cleanup(self):
        title = "\\[Title\\]([https://example.com"
        assert clean_link_title(title) == "[Title"

    def test_empty_string(self):
        assert clean_link_title("") == ""

    def test_real_examples(self):
        title1 = "There's An AI: The Best AI Tools Directory\\]([https://theresanai.com/"
        title2 = "\\[2407.20516\\] Machine Unlearning in Generative AI: A Survey\\]([https://arxiv.org/abs/2407.20516"

        assert clean_link_title(title1) == "There's An AI: The Best AI Tools Directory"
        assert clean_link_title(title2) == "[2407.20516] Machine Unlearning in Generative AI: A Survey"


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


class TestCleanMDArtifacts:
    def test_basic_url(self):
        url = "https://example.com"
        assert clean_md_artifacts(url) == url

    def test_markdown_artifacts(self):
        urls = [
            "https://example.com)[–](end",
            "https://example.com)[—](https://other.com",
            "https://example.com)[\\](https://other.com",
            "https://example.com)[�](https://other.com",
        ]
        for url in urls:
            assert clean_md_artifacts(url) == "https://example.com"

    def test_empty_string(self):
        assert clean_md_artifacts("") == ""


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


class TestFollowRedirects:
    @responses.activate
    def test_successful_redirect(self):
        start_url = "http://t.co/"
        final_url = "http://example.com/"

        responses.get(
            start_url,
            status=301,
            headers={"Location": final_url},
            body="Redirecting...",
        )
        responses.add(
            responses.GET,
            final_url,
            status=200,
            body="Final destination",
        )
        assert follow_redirects(start_url) == final_url

    @responses.activate
    def test_no_redirect(self):
        url = "http://example.com/"
        responses.get(url, status=200)
        assert follow_redirects(url) == url


class TestEmergentMindToArxiv:
    def test_basic_conversion(self):
        assert (
            emergentmind_to_arxiv("https://emergentmind.com/papers/1706.03762") == "https://arxiv.org/abs/1706.03762"
        )

    def test_case_insensitive(self):
        assert (
            emergentmind_to_arxiv("https://EmergentMind.com/papers/1706.03762") == "https://arxiv.org/abs/1706.03762"
        )

    def test_non_emergentmind_url(self):
        url = "https://example.com/1706.03762"
        assert emergentmind_to_arxiv(url) == url

    def test_invalid_paper_id(self):
        url = "https://emergentmind.com/papers/invalid"
        assert emergentmind_to_arxiv(url) == url

    def test_empty_string(self):
        assert emergentmind_to_arxiv("") == ""

    def test_full_url_with_params(self):
        assert (
            emergentmind_to_arxiv("https://emergentmind.com/papers/1706.03762?param=value")
            == "https://arxiv.org/abs/1706.03762"
        )


class TestHuggingfaceToArxiv:
    def test_basic_conversion(self):
        assert huggingface_to_arxiv("https://huggingface.co/papers/1706.03762") == "https://arxiv.org/abs/1706.03762"

    def test_case_insensitive(self):
        assert huggingface_to_arxiv("https://HuggingFace.co/papers/1706.03762") == "https://arxiv.org/abs/1706.03762"

    def test_non_emergentmind_url(self):
        url = "https://example.com/1706.03762"
        assert huggingface_to_arxiv(url) == url

    def test_invalid_paper_id(self):
        url = "https://huggingface.co/papers/invalid"
        assert huggingface_to_arxiv(url) == url

    def test_empty_string(self):
        assert huggingface_to_arxiv("") == ""

    def test_full_url_with_params(self):
        assert (
            huggingface_to_arxiv("https://huggingface.co/papers/1706.03762?param=value")
            == "https://arxiv.org/abs/1706.03762"
        )


class TestStandardizeArxiv:
    def test_self_conversion(self):
        assert standardize_arxiv("https://arxiv.org/abs/1706.03762") == "https://arxiv.org/abs/1706.03762"

    def test_pdf_conversion(self):
        assert standardize_arxiv("https://arxiv.org/pdf/1706.03762") == "https://arxiv.org/abs/1706.03762"

    def test_html_conversion(self):
        assert standardize_arxiv("https://arxiv.org/html/1706.03762") == "https://arxiv.org/abs/1706.03762"

    def test_case_insensitive(self):
        assert standardize_arxiv("https://arxiv.org/abs/1706.03762") == "https://arxiv.org/abs/1706.03762"

    def test_invalid_paper_id(self):
        url = "https://arxiv.org/abs/invalid"
        assert standardize_arxiv(url) == url

    def test_non_arxiv_url(self):
        url = "https://example.com/1706.03762"
        assert standardize_arxiv(url) == url

    def test_empty_string(self):
        assert standardize_arxiv("") == ""


class TestStandardizeGithub:
    @pytest.mark.parametrize(
        "input_url,expected",
        [
            # Non-GitHub URLs should remain unchanged
            ("https://example.com/path", "https://example.com/path"),
            # GitHub main site URLs
            ("https://github.com/owner/repo", "https://github.com/owner/repo"),
            # Various branches
            (
                "https://github.com/owner/repo/tree/main",
                "https://github.com/owner/repo/tree/main",
            ),
            (
                "https://github.com/owner/repo/tree/feature-1234",
                "https://github.com/owner/repo/tree/feature-1234",
            ),
            (
                "https://github.com/owner/repo/tree/feature/item-1234",
                "https://github.com/owner/repo/tree/feature/item-1234",
            ),
            (
                "https://github.com/owner/repo/tree/v1.2.34",
                "https://github.com/owner/repo/tree/v1.2.34",
            ),
            # Specific files
            (
                "https://github.com/owner/repo/blob/main/file.py",
                "https://github.com/owner/repo/tree/main/file.py",
            ),
            (
                "https://github.com/owner/repo/blob/feature-1234/file.py",
                "https://github.com/owner/repo/tree/feature-1234/file.py",
            ),
            # Raw URLs should convert to github.com
            (
                "https://raw.githubusercontent.com/owner/repo/refs/heads/main/README.md",
                "https://github.com/owner/repo/tree/main/README.md",
            ),
            (
                "https://raw.githubusercontent.com/owner/repo/refs/heads/main/file.py",
                "https://github.com/owner/repo/tree/main/file.py",
            ),
            (
                "https://raw.githubusercontent.com/owner/repo/refs/heads/master/path/file.txt",
                "https://github.com/owner/repo/tree/master/path/file.txt",
            ),
            # Gist URLs
            ("https://gist.github.com/owner/12345", "https://gist.github.com/owner/12345"),
            # Edge cases
            ("", ""),  # Empty URL
            ("https://github.com", "https://github.com"),  # No path
            ("https://github.com/owner/repo/main", "https://github.com/owner/repo"),  # false branch
            ("https://github.com/invalid@user/repo", "https://github.com/invalid@user/repo"),  # Invalid characters
        ],
    )
    def test_standardize_github(self, input_url: str, expected: str):
        assert standardize_github(input_url) == expected

    def test_different_schemes(self):
        # Test with different URL schemes
        assert standardize_github("http://github.com/owner/repo") == "http://github.com/owner/repo"
        assert standardize_github("git://github.com/owner/repo") == "git://github.com/owner/repo"

    def test_with_query_params(self):
        # URLs with query parameters should have them removed
        input_url = "https://github.com/owner/repo?ref=main"
        expected = "https://github.com/owner/repo"
        assert standardize_github(input_url) == expected

    def test_with_fragments(self):
        # URLs with fragments should have them removed
        input_url = "https://github.com/owner/repo#readme"
        expected = "https://github.com/owner/repo"
        assert standardize_github(input_url) == expected

    def test_malformed_urls(self):
        # Test handling of malformed URLs
        malformed_urls = [
            "not_a_url",
            "github.com/no/scheme",
            "https://github.com/only-owner",
        ]
        for url in malformed_urls:
            assert standardize_github(url) == url  # Should return unchanged

    @pytest.mark.parametrize(
        "input_url",
        [
            "https://github.com/owner/repo/refs/heads/feature",
            "https://raw.githubusercontent.com/owner/repo/refs/heads/feature/file.txt",
            "https://gist.github.com/owner/repo/refs/heads/feature",
        ],
    )
    def test_refs_heads_urls(self, input_url):
        # Test URLs containing refs/heads pattern
        result = standardize_github(input_url)
        assert "refs/heads" not in result
        assert "/owner/repo" in result
