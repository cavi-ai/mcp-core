# Applications

`apps.find_directory`, `apps.find_app` and `apps.find_executable` locate an installed application in one
order: an environment override, which is final, then the default location the caller passes, then
`PATH`.

`apps.bundle(name, identifier, pattern, env)` finds a macOS application by its bundle identifier: the
override in `env` when set, else the newest matching bundle under `/Applications`. It raises
`AppUnavailable` or `AppMismatch` with a stable `error_code` (`APP_UNAVAILABLE`,
`APP_IDENTITY_MISMATCH`).

`apps.version` reads a bundle's version without running it, `apps.running` asks whether an executable has
a process, and `apps.processes` lists the processes of one executable on macOS.

`apps.running` matches a binary only as the first argument of a process, and a `#!` script as any whole
argument of its interpreter. Helpers that only name the application, such as `--appPath=<executable>` or
`-launchedbyvulcan <executable>`, do not count. It returns None when the machine cannot say.
