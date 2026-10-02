"""Local stdio MCP client; never calls Direct HTTP APIs or alters approval settings."""
import asyncio
import json
import os
import sys
import tomllib
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    config = tomllib.loads((Path.home() / '.codex/config.toml').read_text(encoding='utf-8'))
    source = config['mcp_servers']['yandex-direct']
    env = dict(os.environ)
    env.update(source.get('env', {}))
    missing = [name for name in source.get('env_vars', []) if not env.get(name)]
    if missing:
        raise RuntimeError('Missing configured environment variable names: ' + ', '.join(missing))
    params = StdioServerParameters(command=source['command'], args=source.get('args', []),
                                  env=env, cwd='D:/mcp_direct')
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write, read_timeout_seconds=timedelta(minutes=10)) as session,
    ):
        await session.initialize()
        print(json.dumps({'status': 'MCP connected'}), flush=True)
        while line := await asyncio.to_thread(sys.stdin.readline):
            request = json.loads(line)
            if request.get('stop'):
                break
            arguments = request.get('arguments', {})
            if request.get('arguments_path'):
                content = await asyncio.to_thread(
                    Path(request['arguments_path']).read_text, encoding='utf-8',
                )
                arguments = json.loads(content)
            result = await session.call_tool(request['tool'], arguments)
            data = result.structuredContent
            if data is None:
                texts = [c.text for c in result.content if c.type == 'text']
                data = json.loads(''.join(texts))
            if request.get('output_path'):
                await asyncio.to_thread(
                    Path(request['output_path']).write_text,
                    json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8',
                )
            selected = {k: data[k] for k in request.get('fields', data.keys()) if k in data}
            print(json.dumps(selected, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
