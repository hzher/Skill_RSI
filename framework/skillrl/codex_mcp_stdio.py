"""Use the shared MCP dispatcher and preserve the original request and return."""
import json
import os
from pathlib import Path
import sys
import time

import mcp_stdio


def main():
    connection, journal = map(Path, sys.argv[1:])
    # Only the private temporary file in the owner's filesystem contains a
    # bearer URL. Neither the host's CLI arguments nor its history contains it.
    os.environ["BENCHMARK_RPC_URL"] = connection.read_text(encoding="utf-8")
    dispatch = mcp_stdio.dispatch

    def recorded(request, session):
        row = {"request": request, "session": session, "started_ns": time.time_ns()}
        if request.get("method") == "tools/call":
            row["operation_id"] = session + "-" + str(request["id"])
        try:
            row["result"] = dispatch(request, session)
            return row["result"]
        except Exception as error:
            row["error"] = {"type": type(error).__name__, "message": str(error)}
            raise
        finally:
            row["finished_ns"] = time.time_ns()
            with journal.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    mcp_stdio.dispatch = recorded
    mcp_stdio.main()


if __name__ == "__main__":
    main()
