import pytest

from aizk.utilities.parse import check_matched_pairs, extract_json, extract_md_url, extract_url, validate_url


# %%
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

    def test_no_urls(self):
        assert extract_md_url("This is a test with no urls.") == []

    def test_raw_urls(self):
        assert extract_md_url("http://this.is/a/test") == []

    def test_html_urls(self):
        assert extract_md_url("<a href='https://example.com'>link</a>") == []


class TestParensAreMatched:
    def test_balanced(self):
        testcases = ["abc", "(abc)" "a(b)c", "a(b)c(d)e", "()()()()", "(((())))"]

        for test in testcases:
            assert check_matched_pairs(test)

    def test_unbalanced(self):
        testcases = ["(abc", "a(b)c)", "a(b)c(d)e)", "(()", "())"]

        for test in testcases:
            assert not check_matched_pairs(test)

    def test_brackets(self):
        testcases = ["abc", "[abc]", "a[b]c", "a[b]c[d]e", "[][][]", "[[[]]]"]

        for test in testcases:
            assert check_matched_pairs(test, "[", "]")

    def test_braces(self):
        testcases = ["abc", "{abc}", "a{b}c", "a{b}c{d}e", "{}{}{}", "{{{}}}"]

        for test in testcases:
            assert check_matched_pairs(test, "{", "}")


class TestExtractJson:
    prefix = "Here's the generated abstract conceptual question in the requested JSON format: "
    suffix = "Would you like me to explain in more detail?"
    object = """{"key": "value"}"""
    array = """[1, 2, 3]"""
    nested = """{"outer": {"inner": [1, 2, 3]}}"""

    test_cases = [
        (object, object),
        (array, array),
        (nested, nested),
        (prefix + object, object),
        (object + suffix, object),
        (prefix + object + suffix, object),
        (prefix + array, array),
        (array + suffix, array),
        (prefix + array + suffix, array),
        (prefix + nested, nested),
        (nested + suffix, nested),
        (prefix + nested + suffix, nested),
        (object + array + nested, object),
        (nested + object + array, nested),
    ]

    @pytest.mark.parametrize("text, expected", test_cases)
    def test_extract_json(self, text, expected):
        assert extract_json(text) == expected

    def test_extract_empty_array(self):
        text = "Here is an empty array: [] and some text."
        expected = "[]"
        assert extract_json(text) == expected

    def test_extract_empty_object(self):
        text = "Here is an empty object: {} and more text."
        expected = "{}"
        assert extract_json(text) == expected

    def test_extract_incomplete_json(self):
        text = 'Not complete: {"key": "value", "array": [1, 2, 3'
        expected = 'Not complete: {"key": "value", "array": [1, 2, 3'
        assert extract_json(text) == expected

    def test_markdown_json(self):
        text = """
        ```python
        import json

        def modify_query(input_data):
            query = input_data["query"]
            style = input_data["style"]
            length = input_data["length"]

            if style == "Poor grammar":
                # Poor grammar modifications (simplified for brevity)
                query = query.replace("How", "how")
                query = query.replace("do", "does")
                query = query.replace("terms of", "in terms of")
                query = query.replace("and", "")

            if length == "long":
                # Long text modifications (simplified for brevity)
                query += "?"

            return {
                "text": query
            }

        input_data = {
            "query": "How can the provided commands be used to manage and troubleshoot namespaces in a Kubernetes environment?",
            "style": "Poor grammar",
            "length": "long"
        }

        output = modify_query(input_data)
        print(json.dumps(output, indent=4))
        ```

        Output:
        ```json
        {"text": "how does the provided commands be used to manage and troubleshoot namespaces in a Kubernetes environment?"}
        ```
        This Python function `modify_query` takes an input dictionary with query, style, and length as keys. It applies modifications based on the specified style (Poor grammar) and length (long). The modified query is then returned as a JSON object.

        Note: This implementation is simplified for brevity and may not cover all possible edge cases or nuances of natural language processing.
        """
        expected = """{"text": "how does the provided commands be used to manage and troubleshoot namespaces in a Kubernetes environment?"}"""
        assert extract_json(text) == expected
