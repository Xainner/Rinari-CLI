"""``python -m rinari.engine_protocol``: the Engine on stdio, without the CLI (#91)."""

from __future__ import annotations

import sys


def main() -> int:
    from rinari.engine_protocol.entry import serve
    from rinari.shared.errors import RinariError

    try:
        return serve()
    except RinariError as err:
        # Same lines as `rinari engine --stdio`: the desktop shows them on a
        # failed start (another Engine on this home, a broken config).
        print(f"error: {err.message}", file=sys.stderr)
        if err.hint:
            print(f"hint: {err.hint}", file=sys.stderr)
        return int(err.code)


if __name__ == "__main__":
    sys.exit(main())
