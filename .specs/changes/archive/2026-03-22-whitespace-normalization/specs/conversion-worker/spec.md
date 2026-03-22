# Conversion Worker — Whitespace Normalization Delta

## ADDED

### Requirement: Normalize whitespace in Markdown output

The system SHALL normalize whitespace in the Markdown output before writing to the output file and computing its content hash.
Normalization collapses multiple consecutive spaces to a single space and collapses 3 or more consecutive newlines to exactly 2 newlines.

#### Scenario: Multiple spaces collapsed on write

- **GIVEN** Docling conversion produces Markdown with multiple consecutive spaces
- **WHEN** the worker prepares to write `output.md`
- **THEN** each run of 2+ spaces is collapsed to a single space

#### Scenario: Multiple newlines collapsed on write

- **GIVEN** Docling conversion produces Markdown with 3 or more consecutive newlines
- **WHEN** the worker prepares to write `output.md`
- **THEN** each run of 3+ newlines is collapsed to exactly 2 newlines

#### Scenario: Hash computed on normalized Markdown

- **GIVEN** normalization modifies the Markdown text
- **WHEN** the content hash is computed
- **THEN** the hash is computed over the normalized Markdown, ensuring consistency across reruns with identical input

#### Scenario: Indentation in code blocks preserved

- **GIVEN** the Markdown contains code blocks with intentional indentation
- **WHEN** whitespace normalization is applied
- **THEN** the indentation within code blocks is preserved and not collapsed

#### Scenario: Leading indentation for list nesting preserved

<!-- markdownlint-disable MD038 -->

- **GIVEN** the Markdown contains a nested list where nesting level is encoded by leading spaces (e.g. `- nested item` or `  - deeper item`)
- **WHEN** whitespace normalization is applied
- **THEN** the leading spaces on each line are preserved exactly, so list nesting structure is not altered

<!-- markdownlint-enable -->

#### Scenario: Trailing spaces on lines stripped

- **GIVEN** the Markdown contains lines with one or more trailing spaces before the newline
- **WHEN** whitespace normalization is applied
- **THEN** all trailing spaces before newlines are removed
- **AND** no two-space hard line breaks are introduced, because Docling never emits them

#### Scenario: Tab characters expanded to spaces

- **GIVEN** the Markdown contains tab characters outside code blocks
- **WHEN** whitespace normalization is applied
- **THEN** each tab is replaced by four spaces, which are then subject to space collapsing
