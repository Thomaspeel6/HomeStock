"""`python -m homestock.ui_main` — the pantry window's entry point.

Separate from __main__.py, which is the MCP server: the Mac app spawns this one
and reads the handshake line, while an MCP client spawns the other."""

from homestock.ui import main

if __name__ == "__main__":
    main()
