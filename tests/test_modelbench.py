import asyncio
import json
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bench import live_snapshot, parser, request, run, sse_events, summarize, usage_metrics
from server import Store, create_app, validate
from workloads import advance, body_for, initial_messages


def args_for(tmp_path, **values):
    args = parser().parse_args(['--model', 'mock', '--output', str(tmp_path)])
    for key, value in dict(sessions=4, concurrency=2, turns=2, context_chars=300,
                           tool_chars=100, history_turns=1, ramp_seconds=0, **values).items():
        setattr(args, key, value)
    return args


def test_prefix_modes_and_tool_history(tmp_path):
    args = args_for(tmp_path)
    args.projects = 2
    a = initial_messages('codex', 0, args)
    b = initial_messages('codex', 2, args)
    assert a[0] == b[0] and a[1] != b[1]
    args.prefix_mode = 'unique'
    assert initial_messages('codex', 0, args)[0] != initial_messages('codex', 2, args)[0]
    assert a[2]['tool_calls'][0]['id'] == a[3]['tool_call_id']
    prior = list(a)
    assistant = {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 'next', 'type': 'function', 'function': {'name': 'exec_command', 'arguments': '{}'}}]}
    advance(a, assistant, 0, 0, args)
    assert a[:len(prior)] == prior
    assert a[-1]['tool_call_id'] == 'next'


def test_usage_is_not_guessed():
    assert usage_metrics({})['cached_tokens'] is None
    assert usage_metrics({'prompt_tokens': 100, 'prompt_cache_hit_tokens': 60})['cache_hit_ratio'] == .6
    assert usage_metrics({'prompt_tokens': 100, 'prompt_tokens_details': {'cached_tokens': 0}})['cached_tokens'] == 0
    assert usage_metrics({'prompt_tokens': 10, 'prompt_tokens_details': {'cached_tokens': 20}})['cached_tokens'] is None


def test_harness_style_usage_buckets():
    usage = usage_metrics({'prompt_tokens':100, 'completion_tokens':20,
        'prompt_cache_hit_tokens':60, 'prompt_cache_miss_tokens':40,
        'completion_tokens_details':{'reasoning_tokens':12}})
    assert usage['uncached_tokens'] == 40
    assert usage['reasoning_tokens'] == 12
    assert usage['total_tokens'] == 120
    assert usage_metrics({})['uncached_tokens'] is None


async def test_first_token_is_published_before_request_completes(tmp_path):
    release = asyncio.Event()
    first_sent = asyncio.Event()
    async def handle(req):
        await req.json()
        response = web.StreamResponse(headers={'Content-Type':'text/event-stream'})
        await response.prepare(req)
        await response.write(b'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n\n')
        first_sent.set()
        await release.wait()
        await response.write(b'data: {"choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}],"usage":{"prompt_tokens":100,"completion_tokens":10,"prompt_cache_hit_tokens":60,"completion_tokens_details":{"reasoning_tokens":4}}}\n\n')
        await response.write(b'data: [DONE]\n\n')
        return response
    app=web.Application()
    app.router.add_post('/v1/chat/completions',handle)
    async with TestServer(app) as mock:
        args=args_for(tmp_path)
        args.concurrency=args.sessions=args.turns=1
        args.base_url=str(mock.make_url('/v1'))
        task=asyncio.create_task(run(args))
        try:
            await asyncio.wait_for(first_sent.wait(),3)
            for _ in range(40):
                paths=list(tmp_path.glob('*/progress.json'))
                if paths:
                    progress=json.loads(paths[0].read_text())
                    break
                await asyncio.sleep(.05)
            assert progress['requests'] == 0
            assert progress['streaming'] == 1
            assert progress['waiting_first_token'] == 0
            assert progress['observed_ttft_ms']['count'] == 1
            assert progress['observed_ttft_ms']['mean'] > 0
            assert progress['token_totals']['completion_tokens']['value'] is None
        finally:
            release.set()
            await asyncio.wait_for(task,5)
    summary=json.loads(next(tmp_path.glob('*/summary.json')).read_text())
    assert summary['first_reasoning_ms']['count'] == 1
    assert summary['generation_ms']['mean'] > 0
    assert summary['weighted_decode_tokens_per_s'] > 0
    assert summary['token_totals']['cached_tokens']['value'] == 60
    assert summary['token_totals']['uncached_tokens']['value'] == 40
    assert summary['token_totals']['reasoning_tokens']['value'] == 4
    assert summary['recent_requests'][0]['ttft_ms'] > 0


def test_live_failure_ttft_not_hidden():
    args = {'ok':False,'session_id':'s','backend':None,'e2e_ms':100,'ttft_ms':30,'error':'incomplete_stream'}
    snapshot=live_snapshot([args],{'active':{'ttft_ms':50}},1,1,[])
    assert snapshot['ttft_ms']['count'] == 0
    assert snapshot['observed_ttft_ms']['count'] == 2
    assert snapshot['observed_ttft_ms']['mean'] == 40


@pytest.mark.parametrize('overrides', [{'concurrency': 2001}, {'concurrency': True}, {'duration': float('nan')},
    {'profiles': []}, {'model': ''}, {'base_url': 'http://user:pass@localhost/v1'},
    {'session_header': 'Authorization'}, {'output': '/tmp'}, {'prefix_mode': 'invalid'}])
def test_invalid_api_configuration(overrides):
    with pytest.raises(ValueError):
        validate({'model': 'mock', **overrides})


async def test_multiline_sse():
    async def lines():
        for line in [b': heartbeat\r\n', b'data: {"x":\r\n', b'data: 1}\r\n', b'\r\n', b'data: [DONE]\n', b'\n']:
            yield line
    assert [event async for event in sse_events(lines())] == ['{"x":\n1}', '[DONE]']


def mock_app(mode='ok'):
    app = web.Application()
    app['bodies'] = []
    app['sessions'] = []
    app['authorization'] = []
    async def handle(req):
        body = await req.json()
        app['bodies'].append(body)
        app['sessions'].append(req.headers.get('X-Session'))
        app['authorization'].append(req.headers.get('Authorization'))
        if mode == 'http':
            return web.Response(status=429, text='sensitive prompt echo')
        response = web.StreamResponse(headers={'Content-Type':'text/event-stream', 'X-Backend':'node-a'})
        await response.prepare(req)
        async def emit(obj):
            raw = ('data: '+json.dumps(obj)+'\r\n\r\n').encode()
            # Exercise arbitrary TCP fragmentation, including JSON boundaries.
            for part in (raw[:9], raw[9:]):
                await response.write(part)
        await emit({'choices':[{'index':0,'delta':{'role':'assistant'}}]})
        await asyncio.sleep(.01)
        await emit({'choices':[{'index':0,'delta':{'tool_calls':[{'index':0,'id':'call_1','function':{'name':'read_file','arguments':'{"path":'}}]}}]})
        await asyncio.sleep(.01)
        await emit({'choices':[{'index':0,'delta':{'tool_calls':[{'index':0,'function':{'arguments':'"a.py"}'}}]}}]})
        if mode != 'truncated':
            await emit({'choices':[{'index':0,'delta':{},'finish_reason':'tool_calls'}]})
            await emit({'choices':[], 'usage':{'prompt_tokens':100,'completion_tokens':5,'prompt_tokens_details':{'cached_tokens':50}}})
            await response.write(b'data: [DONE]\n\n')
        await response.write_eof()
        return response
    app.router.add_post('/v1/chat/completions', handle)
    return app


@pytest.mark.parametrize('mode', ['ok', 'truncated', 'http'])
async def test_stream_metrics_and_failure(tmp_path, mode):
    args = args_for(tmp_path, backend_header='X-Backend')
    async with TestServer(mock_app(mode)) as mock:
        async with aiohttp.ClientSession() as client:
            result, assistant = await request(client, str(mock.make_url('/v1/chat/completions')),
                body_for(args, initial_messages('codex',0,args),'codex'), {}, args)
    assert result['ok'] == (mode == 'ok')
    if mode == 'ok':
        assert result['output_chunks'] == 2
        assert result['ttft_ms'] > 0 and result['tpot_ms'] > 0
        assert result['cached_tokens'] == 50
        assert assistant['tool_calls'][0]['function']['arguments'] == '{"path":"a.py"}'
    else:
        assert result['error'] == ('http_429' if mode == 'http' else 'incomplete_stream')
        assert 'sensitive' not in json.dumps(result)


async def test_closed_loop_engine(tmp_path):
    app = mock_app()
    async with TestServer(app) as mock:
        args = args_for(tmp_path, session_header='X-Session', backend_header='X-Backend')
        args.base_url = str(mock.make_url('/v1'))
        assert await run(args) == 0
    directory = next(tmp_path.iterdir())
    summary = json.loads((directory/'summary.json').read_text())
    assert summary['requests'] == 8
    assert summary['peak_in_flight'] == 2
    assert summary['weighted_cache_hit_ratio'] == .5
    assert summary['backend_same_ratio'] == 1
    assert len(set(app['sessions'])) == 4
    for sid in set(app['sessions']):
        bodies = [b for b,s in zip(app['bodies'],app['sessions']) if s == sid]
        assert bodies[1]['messages'][:len(bodies[0]['messages'])] == bodies[0]['messages']


async def test_local_api_preview_validation_history(tmp_path):
    async with TestClient(TestServer(create_app(tmp_path))) as client:
        assert (await client.get('/')).status == 200
        assert (await client.get('/assets/app.js')).status == 200
        response = await client.post('/api/preview', json={'model':'mock','context_chars':200})
        assert response.status == 200
        assert (await response.json())['stream'] is True
        assert await (await client.get('/api/runs')).json() == []
        assert (await client.post('/api/runs',json={'model':'x','concurrency':2001})).status == 400
        assert (await client.post('/api/preview',json={'model':'x'},headers={'Origin':'https://example.com'})).status == 403
        assert (await client.get('/api/runs/missing')).status == 404


async def test_api_subprocess_end_to_end(tmp_path):
    upstream = mock_app()
    secret = 'modelbench-test-key-do-not-persist'
    async with TestServer(upstream) as mock:
        async with TestClient(TestServer(create_app(tmp_path))) as client:
            response = await client.post('/api/runs',json={'model':'mock','base_url':str(mock.make_url('/v1')),
                'sessions':2,'concurrency':2,'turns':2,'context_chars':200,'tool_chars':100,'ramp_seconds':0,
                'api_key':secret})
            assert response.status == 201
            rid = (await response.json())['id']
            for _ in range(100):
                row = await (await client.get('/api/runs/'+rid)).json()
                if row['status'] not in ('running','stopping'):
                    break
                await asyncio.sleep(.1)
            assert row['status'] == 'completed', row
            assert row['summary']['success'] == 4
            assert upstream['authorization'] == ['Bearer ' + secret] * 4
            assert secret not in json.dumps(row)
            assert (await client.get(f'/api/runs/{rid}/artifacts/summary.json')).status == 200
    store = Store(tmp_path/'modelbench.sqlite3')
    assert store.get(rid)['summary']['success'] == 4
    store.db.close()
    for path in tmp_path.rglob('*'):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path


async def test_stop_and_conflict(tmp_path):
    async with TestClient(TestServer(create_app(tmp_path))) as client:
        # Long ramp; all network targets are loopback. Stop before later workers run.
        payload={'model':'mock','base_url':'http://127.0.0.1:1/v1','sessions':10,'concurrency':2,'ramp_seconds':60}
        response=await client.post('/api/runs',json=payload)
        rid=(await response.json())['id']
        assert (await client.post('/api/runs',json=payload)).status == 409
        assert (await client.post(f'/api/runs/{rid}/stop',json={})).status == 200
        for _ in range(60):
            row=await (await client.get('/api/runs/'+rid)).json()
            if row['status']=='cancelled':
                break
            await asyncio.sleep(.1)
        assert row['status']=='cancelled'
        assert row['summary']['interrupted'] is True


def test_restart_marks_interrupted(tmp_path):
    store=Store(tmp_path/'test.sqlite3')
    store.insert('test', {'model':'x'})
    store.db.close()
    store=Store(tmp_path/'test.sqlite3')
    assert store.get('test')['status']=='interrupted'
    store.db.close()
