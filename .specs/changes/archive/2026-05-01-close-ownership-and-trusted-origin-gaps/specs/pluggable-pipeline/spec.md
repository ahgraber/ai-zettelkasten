# Delta for pluggable-pipeline — close-ownership-and-trusted-origin-gaps

## MODIFIED Requirements

### Requirement: Apply network egress policy to all external-content dereferences

The operator-trusted KaraKeep carve-out SHALL be matched by parsed origin, not by
string prefix.
An external URL qualifies for the KaraKeep trusted-infrastructure path only when its
effective origin exactly matches the configured `karakeep_base_url` origin
(`scheme`, normalized hostname, and effective port) **and** its path begins with
`/api/v1/assets/`.

URLs that merely share a textual prefix with `karakeep_base_url` SHALL NOT qualify.
Same-origin URLs outside the asset path prefix SHALL NOT qualify.
Non-qualifying URLs SHALL be treated as ordinary outbound URLs and SHALL go through
the normal egress-validation path.

(Previously: the KaraKeep carve-out was described as operator-trusted but did not
forbid prefix-based matching.)

#### Scenario: Exact-origin KaraKeep asset URL is trusted

- **GIVEN** `karakeep_base_url = "https://karakeep.example.internal"`
- **AND** a candidate URL of
  `https://karakeep.example.internal/api/v1/assets/abc123`
- **WHEN** the fetcher decides whether to use the KaraKeep trusted-infrastructure
  path
- **THEN** the URL qualifies for the carve-out because the origin matches exactly and
  the path is under `/api/v1/assets/`

#### Scenario: Lookalike host does not qualify for KaraKeep trust

- **GIVEN** `karakeep_base_url = "https://karakeep.example.internal"`
- **AND** a candidate URL of
  `https://karakeep.example.internal.evil.test/api/v1/assets/abc123`
- **WHEN** the fetcher decides whether to use the KaraKeep trusted-infrastructure
  path
- **THEN** the URL does not qualify for the carve-out and is processed through the
  normal egress-validation path

#### Scenario: Same-origin non-asset URL does not qualify for KaraKeep trust

- **GIVEN** `karakeep_base_url = "https://karakeep.example.internal"`
- **AND** a candidate URL of
  `https://karakeep.example.internal/api/v1/bookmarks/abc123`
- **WHEN** the fetcher decides whether to use the KaraKeep trusted-infrastructure
  path
- **THEN** the URL does not qualify for the carve-out because the path is outside
  `/api/v1/assets/`, and it is processed through the normal egress-validation path
