"""Public MCP boundary, exercised by the official SDK rather than a fake wire."""
import asyncio
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import uuid

import pytest

from atom_search.agents.read_api import AgentReadAPI
from atom_search.service.contracts import (
    QueryInterpretation, ResultMetadata, SearchResponse, ServiceError,
)


RESULT_ID = '10000000-0000-4000-8000-000000000001'
SOURCE_ID = '20000000-0000-4000-8000-000000000002'
SECRET = 'PRIVATE-token-password-reference'


def load_adapter():
    name = 'atom_search.agents.mcp_adapter'
    assert importlib.util.find_spec(name) is not None, 'P7 public MCP transport API is missing'
    return importlib.import_module(name)


class MetadataClient:
    """Controlled service DTOs; real AgentReadAPI and SDK validation stay active."""
    def __init__(self):
        self.calls = []
        self.failure = None

    def search(self, request):
        self.calls.append(request)
        if self.failure:
            raise self.failure
        item = ResultMetadata(RESULT_ID, SOURCE_ID, 'чертёж.pdf', 'чертёж.pdf',
            'C:\\Documents\\чертёж.pdf', 12, 123, False, None, ('name',), 1.0, 'lexical')
        return SearchResponse(() if request.source_ids == () else (item,),
            QueryInterpretation(request.query, request.query, None, ()), (), (), 7, ())

    def fetch(self, result_id, *, source_ids=None):
        raise ServiceError('result_not_found', SECRET, details={'token': SECRET})


def envelope(result, *, error=None):
    value = result.structured_content
    assert type(value) is dict
    assert set(value) == {'schema_version', 'request_id', 'data', 'error'}
    assert type(value['schema_version']) is int and value['schema_version'] == 1
    assert str(uuid.UUID(value['request_id'])) == value['request_id']
    if error is None:
        assert value['error'] is None and not result.is_error
    else:
        assert value['data'] is None
        assert value['error']['code'] == error
        assert value['error']['details'] == {}
    assert SECRET not in json.dumps(result.model_dump(), ensure_ascii=False)
    return value


def test_tools_list_is_read_only():
    adapter = load_adapter()
    from mcp import Client

    async def scenario():
        async with Client(adapter.create_mcp_server(AgentReadAPI(MetadataClient()))) as client:
            tools = (await client.list_tools()).tools
            assert {tool.name for tool in tools} == {'search', 'fetch', 'health', 'settings_get'}
            for tool in tools:
                assert tool.annotations is not None
                assert tool.annotations.read_only_hint is True
                assert tool.annotations.open_world_hint is False
    asyncio.run(scenario())


@pytest.mark.parametrize('arguments', [
    {'query': ''}, {'query': 'я' * 513}, {'query': None}, {'query': 5},
    {'query': 'drawing', 'limit': 0}, {'query': 'drawing', 'limit': 101},
    {'query': 'drawing', 'limit': True}, {'query': 'drawing', 'limit': 1.5},
    {'query': 'drawing', 'limit': '20'}, {'query': 'drawing', 'source_ids': 'all'},
    {'query': 'drawing', 'source_ids': [False]},
    {'query': 'drawing', 'source_ids': [SOURCE_ID] * 65},
])
def test_sdk_invalid_arguments_do_not_reach_service(arguments):
    adapter = load_adapter()
    from mcp import Client
    service = MetadataClient()

    async def scenario():
        async with Client(adapter.create_mcp_server(AgentReadAPI(service))) as client:
            result = await client.call_tool('search', arguments)
            # Type errors may be rejected by the SDK; semantic failures retain
            # the facade's public envelope. Neither may become a successful read.
            if not result.is_error:
                envelope(result, error='invalid_argument')
            assert service.calls == []
    asyncio.run(scenario())


@pytest.mark.parametrize(('scope', 'expected', 'count'), [(None, None, 1), ([], (), 0)])
def test_sdk_preserves_null_empty_scope_and_trimmed_unicode(scope, expected, count):
    adapter = load_adapter()
    from mcp import Client
    service = MetadataClient()

    async def scenario():
        async with Client(adapter.create_mcp_server(AgentReadAPI(service))) as client:
            result = await client.call_tool('search', {
                'query': '\u2003' + 'я' * 512 + '\t', 'limit': 100, 'source_ids': scope})
            value = envelope(result)
            assert len(value['data']['results']) == count
            request, = service.calls
            assert request.query == 'я' * 512
            assert request.limit == 100 and request.source_ids == expected
            if count:
                assert value['data']['results'][0]['name'] == 'чертёж.pdf'
                assert 'content' not in value['data']['results'][0]
    asyncio.run(scenario())


@pytest.mark.parametrize('code', ['service_unavailable', 'stale_result', 'authentication_required'])
def test_service_errors_remain_safe_public_envelopes(code):
    adapter = load_adapter()
    from mcp import Client
    service = MetadataClient()
    service.failure = ServiceError(code, SECRET, True, {'token': SECRET})

    async def scenario():
        async with Client(adapter.create_mcp_server(AgentReadAPI(service))) as client:
            value = envelope(await client.call_tool('search', {'query': 'drawing'}), error=code)
            assert value['error']['retryable'] is True
            envelope(await client.call_tool('fetch', {'result_id': RESULT_ID}), error='result_not_found')
    asyncio.run(scenario())


def test_stdio_stdout_is_protocol_only(tmp_path):
    adapter = load_adapter()
    from mcp import Client, StdioServerParameters
    root = Path(__file__).resolve().parents[2]
    wire = tmp_path / 'stdout.jsonl'
    # This is an SDK transport harness, not a product CLI or alternate MCP
    # implementation. The tee records every byte, including import/exit output.
    child = '''
from atom_search.agents.mcp_adapter import create_mcp_server
from atom_search.agents.read_api import AgentReadAPI
from test_mcp_contract import MetadataClient
create_mcp_server(AgentReadAPI(MetadataClient())).run(transport="stdio")
'''
    tee = tmp_path / 'tee.py'
    tee.write_text('''import subprocess, sys, threading
process = subprocess.Popen([sys.executable, "-c", sys.argv[2]], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
def forward():
    try:
        while data := sys.stdin.buffer.read1(65536):
            process.stdin.write(data)
            process.stdin.flush()
    finally:
        process.stdin.close()
threading.Thread(target=forward, daemon=True).start()
with open(sys.argv[1], "wb") as capture:
    for line in process.stdout:
        capture.write(line)
        capture.flush()
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()
raise SystemExit(process.wait())
''', encoding='utf-8')
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() in {'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'}}
    environment['PYTHONPATH'] = os.pathsep.join(map(str, (
        root / 'src', root / 'packages/atom-search-core/src', root / 'tests/agents')))
    environment['PYTHONIOENCODING'] = 'utf-8'

    async def scenario():
        parameters = StdioServerParameters(command=sys.executable,
            args=[str(tee), str(wire), child], env=environment)
        async with Client(parameters) as client:
            assert {t.name for t in (await client.list_tools()).tools} == {
                'search', 'fetch', 'health', 'settings_get'}
            envelope(await client.call_tool('search', {'query': 'drawing'}))
    asyncio.run(scenario())
    lines = wire.read_bytes().splitlines()
    assert lines
    for line in lines:
        message = json.loads(line)
        assert message['jsonrpc'] == '2.0'
        assert 'method' in message or 'result' in message or 'error' in message
