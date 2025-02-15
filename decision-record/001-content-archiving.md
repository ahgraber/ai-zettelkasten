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

### Option 1: [ArchiveBox](https://github.com/ArchiveBox/ArchiveBox/tree/dev)

Pros:

- All-in-one tool provides pretty much every scraping technique I could possibly need.
- Runs as CLI tool, python package, and browser. Hostable with Docker.

Cons:

- Feels like way more than I need.
- Current version with `abx-plugin` ecosystem is still under development (stable is v0.7, dev is v0.8, anticipated next
  stable release is v0.9)
- Do I really need the ArchiveBox package manager?

Reason for not selecting:

- I want to try it myself (ArchiveBox will be a good source for inspiration / comparison)
- Trying to minimize this project's dependencies

### Option 2: [Firecrawl](https://www.firecrawl.dev/)

Pros:

- Service and [self-hosted](https://github.com/mendableai/firecrawl/blob/main/SELF_HOST.md)
  - service supposedly handles captchas and bot detection
- All-in-one does scraping, crawling, and parsing
- Integrations with LLM/AI Frameworks

Cons:

- Service costs $ (though it seems reasonable), given the goal is to scrape (not crawl) a limited set of pages
- Self-hosted version does not handle bot detection or captchas

### Option 3: [Jina Reader](https://jina.ai/reader/)

Pros:

- Service and [self-hosted](https://github.com/jina-ai/reader)
  - service supposedly handles captchas and bot detection
- All-in-one does scraping and extracts text content as markdown
- Free and paid

Cons:

- Free version is rate-limited
- This is an extractor more than an archiver. If the idea is to archive the exact content, then parse (so as parsing
  improves I don't have to re-scrape), Jina Reader (and services like it) do not fit the archiving requirement.

### Option 4: [Exa Contents API](https://docs.exa.ai/reference/get-contents)

Pros:

- Service
  - service supposedly handles captchas and bot detection
- All-in-one does scraping and extracts text content
- Available summarizer integration
- Can crawl subpages

Cons:

- Personal version is rate-limited (5 RPS / 12 RPM)
- No free / selfhosted version; [not inexpensive](https://exa.ai/pricing)
- Like Jina, this is an extractor more than an archiver. If the idea is to archive the exact content, then parse (so as
  parsing improves I don't have to re-scrape), Jina Reader (and services like it) do not fit the archiving requirement.

## Additional Notes

Revisit ArchiveBox in the future (especially after their v0.9 release!); it may make sense to use it and reduce the
scope of this project

### References

- [List of Chromium Command Line Switches « Peter Beverloo](https://peter.sh/experiments/chromium-command-line-switches/)
- [postlight/parser: 📜 Extract meaningful content from the chaos of a web page](https://github.com/postlight/parser)
- [gildas-lormeau/SingleFile: Web Extension for saving a faithful copy of a complete web page in a single HTML file](https://github.com/gildas-lormeau/SingleFile)
- [Y2Z/monolith: ⬛️ CLI tool for saving complete web pages as a single HTML file](https://github.com/Y2Z/monolith)
- [ArchiveBox](https://github.com/ArchiveBox/ArchiveBox)
- [cyclotruc/gitingest: Replace 'hub' with 'ingest' in any github url to get a prompt-friendly extract of a codebase](https://github.com/cyclotruc/gitingest/tree/main)
- [databridge-org/databridge-core: Multi-modal modular data ingestion and retrieval](https://github.com/databridge-org/databridge-core)
- [hoarder-app/hoarder: A self-hostable bookmark-everything app (links, notes and images) with AI-based automatic tagging and full text search](https://github.com/hoarder-app/hoarder) -
  uses monolith for archiving
