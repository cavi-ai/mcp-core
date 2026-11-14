# Changelog

Versions come from git tags; a pull request adds its lines under Unreleased and never edits a version.

## Unreleased

### Added

- `workspace` resolves a server's workspace root from its environment variable and confines input files and new output directories to it.

### Changed

- Process sampling and `ps` inventories allow 10 seconds, so supervision keeps working on a busy host.

## 0.1.3 - 2026-11-09

### Added

- Register owned subprocess launches before execution and retain process identities for cleanup.
- Supervise resident workers with durable completion and helper-retirement receipts.
- Inspect process resource use, sample stacks, and clean up only matching observed processes.
- Create operation-journal records without overwriting an existing record.

## 0.1.2 - 2026-11-06

### Changed

- Tool arguments reject undeclared fields before execution.
- Tool descriptions omit Python source indentation.
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
