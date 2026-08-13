"""Точка входа для `python -m yadirect_mcp`.

Именно эта форма запуска стоит во всех конфигах MCP-клиентов: у них есть поле
command (путь к python) и args, а не «строка для шелла», поэтому console-script
`yadirect-mcp` там неудобен — он ещё и лежит в разных местах на Windows и Linux.
"""

from __future__ import annotations

from . import main

if __name__ == "__main__":
    main()
