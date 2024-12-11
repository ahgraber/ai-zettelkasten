# 001 - Content Collection & Archiving

## Status

10 December 2024 - Accepted

## Context

<!-- What is the problem or challenge we're addressing?
What are the existing constraints?
Why does this decision matter? -->

In order to run RAG, the system must have access to content. Most of the content I'm interested in is content from the
web.

Is the code to handle content collection and archiving part of this project, or should it leverage other existing
projects?

## Decision

### Selected Approach

<!-- What solution are we selecting?
Provide a clear, concise description of the chosen approach -->

Attempt easy scraping approaches. Implement handlers for headless Chrome/Chromium, and direct download (`curl` or
`wget` equivalent). Supplement with other cli tools including `@postlight/parser` and `single-file`.

### Rationale

<!-- Why was this specific approach selected?
What alternative options were considered?
What are the key benefits of this decision? -->

Given I'm building this as an educational experiment, I want to do my best to implement everything myself (cribbing off
of other, existing projects as needed). Should that prove too challenging or distract from effort on other portions of
the project, I will reconsider and try to integrate existing tools.

### Alternative Considered

#### Option 1: BeautifulSoup

Pros: Scraping with BeautifulSoup is easy to implement in Python Cons: Really only works for pure-HTML pages, which are
rare.

### Option 2: [ArchiveBox](https://github.com/ArchiveBox/ArchiveBox/tree/dev)

Pros:

- All-in-one tool provides pretty much every scraping technique I could possibly need.
- Runs as CLI tool, python package, and browser. Hostable with Docker.

Cons:

- Feels like way more than I need.
- Current version with `abx-plugin` ecosystem is still under development
- Do I really need the ArchiveBox package manager?

Reason for not selecting:

- I want to try it myself (ArchiveBox will be a good source for inspiration / comparison)
- Trying to minimize this project's dependencies

## Additional Notes

Revisit ArchiveBox in the future; it may make sense to use it and reduce the scope of this project
