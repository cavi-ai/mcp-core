# Process inspection

On macOS, `sampling` reads process census, checked birth identities, descendant RSS and
socket-descriptor counts. Each call accepts `timeout` in seconds, capped at two seconds. Census, RSS,
cleanup identities and descriptor reads use independent read-only workers; an interrupted or timed-out
worker refuses subsequent calls.

```python
from cavi_mcp_core import sampling

rows = sampling.inventory(timeout=1)  # PID -> (parent PID, kernel name)
family = sampling.tree(pid, timeout=1)  # PID -> (RSS KiB, opaque birth identity)
identities = sampling.identities(family, timeout=1)
sockets = sampling.socket_count(pid, timeout=1)
```

Pass `children` to `tree` to reuse census discovery; each descendant still needs its current parent and
birth identity checked. Other processes' RSS is not read. Invalid or denied native reads raise; exited
processes are absent. `sampling.birth` returns the same `darwin:` token as `processes.birth`, or `None`
after exit. Unsupported platforms refuse before starting a native worker.

Inspection does not launch, signal or adopt processes. A sampled tree cannot prove that an unregistered
detached helper exited. Keep admission, ownership, aggregate memory policy and restoration in the
consuming supervisor. `examples/inspect_process.py` in the repository inspects the current process.
