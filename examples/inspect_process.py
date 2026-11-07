"""Inspect the current process on macOS without launching an application."""

import json
import os

from cavi_mcp_core import sampling


def main():
    pid = os.getpid()
    rss, _ = sampling.tree(pid)[pid]
    print(
        json.dumps(
            {
                "pid": pid,
                "rss_kib": rss,
                "birth": sampling.birth(pid),
                "sockets": sampling.socket_count(pid),
            }
        )
    )


if __name__ == "__main__":
    main()
