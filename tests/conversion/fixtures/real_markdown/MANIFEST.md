# Real-World Whitespace Fixtures

Excerpts sourced from actual Docling conversion outputs stored in S3 bucket `aizk`.

## How fixtures are used

Fixture content is embedded as string literals in `tests/conversion/unit/test_whitespace_real_world.py`.
They are NOT stored as separate `.md` files because markdown linters normalize whitespace, which would destroy the pre-normalization content that the tests depend on.

## Sources

| Fixture variable                     | UUID       | Pipeline | Pattern tested                                                           |
| ------------------------------------ | ---------- | -------- | ------------------------------------------------------------------------ |
| `EU_AI_ETHICS_RAW`                   | `0000661e` | pdf      | Word-level double-spacing (every word `word  word`)                      |
| `TULU3_TEMPLATE_CODE_RAW`            | `019618ce` | pdf      | Double spaces inside code block (Jinja template `{{  '`) — must preserve |
| `SWE_AGENT_TRAILING_WS_RAW`          | `026a32bf` | pdf      | Lines with trailing whitespace                                           |
| `PHOTOREALISTIC_EXCESS_NEWLINES_RAW` | `02a91012` | pdf      | 3–4 consecutive blank lines between sections                             |
| `HUGGINGFACE_TENSOR_CODE_RAW`        | `00b2e9a4` | html     | Matrix alignment spaces inside code block — must preserve                |

## How the sources were found

```sh
uv run scripts/mine-whitespace/sample_whitespace_patterns.py --min-score 5 --repr
```

Samples documents from the DB+S3, scores each by whitespace complexity (multi-spaces, excess newlines, trailing whitespace), and prints `repr()` strings for the most interesting excerpts.
See `scripts/mine-whitespace/README.md` for full details.
