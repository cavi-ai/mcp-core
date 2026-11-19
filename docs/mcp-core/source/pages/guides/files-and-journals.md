# Files and journals

## Bounded reads

`files.inside(workspace, path)` resolves a path inside an existing workspace and refuses `..`, escapes
through symbolic links and the workspace root.

`files.read_bytes(path, limit)`, `files.load_json` and `files.digest` read bounded regular files and refuse
a file that changes while it is read. `files.parse_json` refuses NaN and Infinity.

`files.digest(path, limit=64 MiB)` streams the file into SHA-256 without holding it in memory; `limit=None`
hashes a file of any size. It refuses a file whose device, inode, size, modification or status-change time
moves while it is hashed.

## The operation journal

`journal.write_json` replaces a JSON file atomically, durably and readable only by its owner.
`journal.completion` reads the completion receipt of one operation, `journal.archive` records how an
interrupted operation was resolved, and `journal.fingerprint` hashes canonical JSON.

`journal.create_json` creates an exclusive private record, syncs its file and directory, and returns its
exact-byte SHA-256. Existing records are preserved. `journal.create_directory` durably links new private
directories and any missing parents.
