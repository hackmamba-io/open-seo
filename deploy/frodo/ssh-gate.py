#!/usr/bin/env python3
"""The deployment SSH key accepts one protocol, never a shell command."""
import os
import re
import sys

COMMAND = re.compile(
    r"deploy ([0-9a-f]{40}) ((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)) ([0-9a-f]{64}) ([1-9][0-9]{0,10})"
)


def main():
    match = COMMAND.fullmatch(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
    if not match:
        print("Only the Frodo deployment protocol is allowed", file=sys.stderr)
        return 2
    os.execve(
        "/usr/bin/sudo",
        ["sudo", "-n", "/usr/local/sbin/frodo-deploy", *match.groups()],
        {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
    )


if __name__ == "__main__":
    sys.exit(main())
