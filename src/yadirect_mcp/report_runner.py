"""Command-line client report collection, independent of an AI agent."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from . import config, report_pipeline
from .client import DirectClient


async def refresh_all(settings, api, *, as_of: str | None = None) -> dict:
    """Sequential queue, explicit local registration only; isolate client errors."""
    results = []
    last_finished = None
    for pointer in sorted(settings.out_dir.glob("*/.client-report/current.json")):
        login = pointer.parents[1].name
        if last_finished is not None:
            # Reports has a user-wide limit of 20 requests / 10 seconds.
            await asyncio.sleep(max(0, 0.55 - (time.monotonic() - last_finished)))
        try:
            item = await report_pipeline.refresh(settings, api, login, as_of=as_of)
            item.pop("brief", None)
        except Exception as exc:  # noqa: BLE001
            item = {"client_login": login, "status": "failed", "error": str(exc)}
        results.append(item)
        last_finished = time.monotonic()
    return {
        "status": "partial" if any(r["status"] == "failed" for r in results) else "ok",
        "clients": results,
        "llm_calls": 0,
    }


async def _run(args, settings) -> dict:
    if args.action == "initialize":
        configuration = json.loads(
            await asyncio.to_thread(
                Path(args.configuration).read_text,
                encoding="utf-8-sig",
            )
        )
        return await asyncio.to_thread(
            report_pipeline.initialize,
            settings,
            args.client,
            args.model,
            configuration,
        )
    if args.action == "brief":
        return await asyncio.to_thread(report_pipeline.read_brief, settings, args.client)
    async with DirectClient(settings) as api:
        if args.action == "enrich":
            return await report_pipeline.enrich(settings, api, args.client)
        if args.action == "refresh-all":
            return await refresh_all(settings, api, as_of=args.as_of)
        return await report_pipeline.refresh(
            settings,
            api,
            args.client,
            as_of=args.as_of,
            force=args.force,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("initialize")
    init.add_argument("--client", required=True)
    init.add_argument("--model", required=True)
    init.add_argument("--configuration", required=True)
    read = sub.add_parser("brief")
    read.add_argument("--client", required=True)
    enrich = sub.add_parser("enrich", help="Добавить детальные срезы к текущему периоду")
    enrich.add_argument("--client", required=True)
    for name in ("refresh", "refresh-all"):
        command = sub.add_parser(name)
        command.add_argument("--as-of", help="Дата запуска; собираются завершённые дни до неё")
        if name == "refresh":
            command.add_argument("--client", required=True)
            command.add_argument(
                "--force", action="store_true", help="Повторить чтение в тот же день"
            )
    args = parser.parse_args()
    if args.action == "refresh-all":
        args.force = False
    try:
        result = asyncio.run(_run(args, config.load()))
    except Exception as exc:  # noqa: BLE001
        parser.exit(1, f"Ошибка обновления отчёта: {exc}\n")
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] == "partial":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
