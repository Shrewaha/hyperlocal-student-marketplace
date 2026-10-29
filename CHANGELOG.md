# Changelog

Notable changes to this project. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-10-29

First release. The marketplace works end to end: a student posts what they
need, shops in the same area are emailed, their offers come back sorted
cheapest first, and the student is emailed as each one lands.

### Added

- **Downloadable client.** Self-contained builds for Windows, macOS (Apple
  Silicon and Intel) and Linux with a Java runtime bundled in, plus a
  cross-platform `MarketplaceApp.jar` for anyone who already has Java 17+.
- **Configurable backend address.** The client reads `MARKETPLACE_API` or
  `-Dmarketplace.api`, falling back to `http://localhost:8000`. A downloaded
  build can be pointed at a hosted API without recompiling.
- **Maven build** for the client (`frontend/pom.xml`), bundling JavaFX for
  every platform into one jar. The original single-file IntelliJ workflow still
  works untouched.
- **Release workflow** that builds, checksums and publishes the downloads from
  a `v*` tag.
- Student and shop accounts with PBKDF2 password hashing and bearer tokens.
- Requests, offers and a public product catalog, over a pure JSON REST API.
- Server-side price comparison: offers sorted by price, cheapest flagged with
  `is_best`, and every other offer carrying its difference against it.
- Email notifications on registration, on a new request to shops in the
  student's area, and on every new offer to the student, queued as background
  tasks so a slow SMTP server never delays a response.
- Per-user email opt-out, a test-email endpoint and a delivery log.

### Known limitations

- Sessions live until logout; there is no token expiry yet.
- The macOS and Windows bundles are unsigned, so the first launch needs to be
  approved by hand.
- The backend is not hosted anywhere, so each user runs it locally for now.

[Unreleased]: https://github.com/shrewaha/hyperlocal-student-marketplace/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/shrewaha/hyperlocal-student-marketplace/releases/tag/v1.0.0
