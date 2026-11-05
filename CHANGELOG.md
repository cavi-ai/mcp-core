# Changelog

Versions come from git tags; a pull request adds its lines under Unreleased and never edits a version.

## Unreleased

### Changed

- The package version is read from the release tag.

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
