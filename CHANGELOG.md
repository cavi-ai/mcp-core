# Changelog

Versions come from git tags; a pull request adds its lines under Unreleased and never edits a version.

## Unreleased

### Changed

- Tool arguments reject undeclared fields before execution.
- Schemas share repeated validation rules and shorten equivalent unions, fixed
  tuples, and single-use references while retaining parameter descriptions and bounds.
- The package version is read from the release tag.
- Requires Python 3.15 or newer; CI lints and tests once, on Linux with Python 3.15; a green `main` dispatches `server-merged` to game-art-pipeline, whose pin bot moves it to that commit.

## 0.1.1 - 2026-11-04

### Added

- `apps`: application lookup by override, default location and `PATH`; macOS bundle lookup by identifier with `AppUnavailable` and `AppMismatch`; version and running-process probes.

## 0.1.0 - 2026-11-04

### Added

- `Server` with compact tool schemas, anticipated-error reporting and one structured failure for every tool error, including unknown tools.
- `Result`, `VariantResult`, `OperationRejected`, `OperationUnknown` and `failure` result contracts.
- Schema compaction and variant validation helpers.
- Bounded subprocess runner with process-group termination.
- Workspace-confined bounded file reads and an atomic JSON operation journal.
