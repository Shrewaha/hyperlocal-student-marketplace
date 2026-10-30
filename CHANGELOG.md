# Changelog

Notable changes to this project. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.2.2] - 2026-10-30

### Fixed

- **There was no way to post a request.** The browse screen told people to
  post what they needed and showed no control for it; the only route was the
  Post tab, which exists for students alone. Signed out, or signed in as a
  shop, you were instructed to do something the interface gave you no way to
  do. Browse now carries a button that goes where it should for whoever is
  reading it, and a shop is told what it does instead of being told to post.
- Empty screens offer a button rather than instructions pointing at a tab.

## [1.2.1] - 2026-10-30

### Fixed

- **Updating the Android app left the old interface on screen.** The bundled
  pages are served from one fixed address that never changes between
  versions, so nothing told the WebView its CSS and JavaScript had been
  replaced, and it kept serving the previous release from cache. The app now
  clears that cache the first time a new build runs.

## [1.2.0] - 2026-10-30

### Changed

- **The client is now built phone-first**, in the shape quick-commerce apps
  have taught people to expect: a pinned search bar, scrolling category chips,
  a two-across card grid, and tabs along the bottom that become ordinary
  header navigation on a wide screen. New palette throughout.
- **Categories carry a colour and a glyph.** The API has no product images, so
  rather than leave a grey rectangle where a photo would be, each category
  owns a saturated block — a decision rather than a gap.
- **The comparison screen leads with the cheapest offer** as a card of its
  own, with the rest listed under it, and a sticky bar carries the best price
  above the tabs — where a grocery app would keep the cart total.
- Search filters as you type instead of only on submit.

## [1.1.0] - 2026-10-30

### Added

- **Browser client** (`web/`) against the same JSON API as the desktop app:
  public product search, accounts, requests, offers and the comparison table.
  No framework and no build step. The API address comes from `web/config.js`,
  or from a box in the page header that stores it in your own browser.
- **Android app** (`android/`): a WebView around the same `web/` client, with
  the page bundled in the APK rather than fetched, so the interface opens
  offline and only the API needs a connection. Gradle copies `web/` in at
  build time, so the phone app and the website cannot drift apart. Debug-signed
  and attached to releases as a `.apk`.
- **`pages.yml`** publishes the browser client to GitHub Pages.
- **`render.yaml` and `Dockerfile`** for deploying the backend.

### Fixed

- The release workflow asked for the retired `macos-13` runner, which queues
  forever instead of failing, so `v1.0.0` was published with no files attached.
  Publishing now needs only the jar and carries on when a platform fails.
- A manual run can attach builds to an existing release, so a release missing
  its files can be repaired without moving tags.

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

[Unreleased]: https://github.com/shrewaha/hyperlocal-student-marketplace/compare/v1.2.2...HEAD
[1.2.2]: https://github.com/shrewaha/hyperlocal-student-marketplace/releases/tag/v1.2.2
[1.2.1]: https://github.com/shrewaha/hyperlocal-student-marketplace/releases/tag/v1.2.1
[1.2.0]: https://github.com/shrewaha/hyperlocal-student-marketplace/releases/tag/v1.2.0
[1.1.0]: https://github.com/shrewaha/hyperlocal-student-marketplace/releases/tag/v1.1.0
[1.0.0]: https://github.com/shrewaha/hyperlocal-student-marketplace/releases/tag/v1.0.0
