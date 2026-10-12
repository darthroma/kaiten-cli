"""Scoped single-PATCH edits against a synthetic HTTP API and installed command."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import subprocess
import threading
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

import pytest

from kaiten_cli.auth import CredentialStore
from kaiten_cli.client import KaitenClient, NoRedirect
from kaiten_cli.editing import KaitenEditing
from kaiten_cli.policy import KaitenError, LocalConfigStore
from test_full_e2e import installed_surface


@contextmanager
def editing_api():
    state = dict(calls=[], card=dict(id=33, title='Задача', board_id=12, column_id=4, lane_id=5,
        state=1, description='Описание', due_date='2026-10-15T00:00:00.000Z', due_date_time_present=False),
        status=200, apply=True, readback_status=200, readback_override={}, detail_count=0, change_at=None, readback_from_patch=1)

    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, data):
            self.send_response(status); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

        def do_GET(self):
            path = urlsplit(self.path).path.removeprefix('/api/latest')
            state['calls'].append(('GET', path, None))
            if path == '/cards': return self.respond(200, [state['card']])
            if path == '/cards/33':
                state['detail_count'] += 1
                if state['detail_count'] == state['change_at']:
                    state['card']['description'] = 'Concurrent edit'
                after_write = sum(c[0]=='PATCH' for c in state['calls']) >= state['readback_from_patch']
                data = {**state['card'], 'owner': {'email':'PRIVATE'}}
                if after_write: data.update(state['readback_override'])
                return self.respond(state['readback_status'] if after_write else 200, data)
            return self.respond(404, {})

        def do_PATCH(self):
            path = urlsplit(self.path).path.removeprefix('/api/latest')
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['calls'].append(('PATCH', path, body))
            assert path == '/cards/33' and self.headers['Authorization'] == 'Bearer synthetic-edit'
            assert body and set(body) <= {'title','description','due_date','due_date_time_present'}
            if state['status'] < 400 and state['apply']:
                state['card'].update(body)
                if state['card']['description'] == '': state['card']['description'] = None
                if 'due_date' in body and body['due_date'] is not None:
                    state['card']['due_date'] += 'T00:00:00.000Z'
            self.respond(state['status'], {'id':33, 'private':'PRIVATE'})

        def log_message(self, *args): pass

    server=ThreadingHTTPServer(('127.0.0.1',0), Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try: yield f'http://127.0.0.1:{server.server_port}', state
    finally: server.shutdown();thread.join(timeout=3);server.server_close()


@pytest.fixture
def editing(tmp_path, monkeypatch):
    monkeypatch.delenv('KAITEN_TOKEN', raising=False)
    store=LocalConfigStore(tmp_path/'personal/policy.json')
    store.save(profile='work',tenant='example.kaiten.ru',board_allowlist=[12])
    credentials=CredentialStore(tmp_path/'personal/profiles.json')
    credentials.save_credentials('work','example.kaiten.ru','synthetic-edit')
    with editing_api() as (origin,state):
        opener=build_opener(NoRedirect())
        def transport(request,**kwargs):
            p=urlsplit(request.full_url)
            return opener.open(Request(origin+p.path+('?' +p.query if p.query else ''),
                data=request.data,method=request.method,headers=dict(request.headers)),**kwargs)
        yield KaitenEditing(store,credentials=credentials,
            client_factory=lambda selected:KaitenClient(selected,open_request=transport)),state


def patches(state): return [c for c in state['calls'] if c[0]=='PATCH']


def test_edit_selected_fields_and_keep_everything_else(editing):
    adapter,state=editing
    result=adapter.update_card('https://example.kaiten.ru/boards/card/33',board_id=12,
        title='Новое название',description='Текст\nсо второй строкой',authorization='direct')
    assert result['status']=='applied' and result['readback']
    assert patches(state)==[('PATCH','/cards/33',{'title':'Новое название','description':'Текст\nсо второй строкой'})]
    assert result['url']=='https://example.kaiten.ru/boards/card/33'
    assert state['card']['column_id']==4 and state['card']['due_date'].startswith('2026-10-15')
    assert 'PRIVATE' not in json.dumps(result)


@pytest.mark.parametrize('options,expected', [
    ({'description':''},{'description':''}),
    ({'clear_due_date':True},{'due_date':None,'due_date_time_present':False}),
    ({'due_date':'2026-10-20'},{'due_date':'2026-10-20','due_date_time_present':False})])
def test_clear_or_replace_fields_explicitly(editing,options,expected):
    adapter,state=editing
    result=adapter.update_card('33',authorization='direct',**options)
    assert result['status']=='applied' and patches(state)[0][2]==expected
    assert state['card']['title']=='Задача'


@pytest.mark.parametrize('options,code', [({},'update_empty'),({'title':''},'title_invalid'),
    ({'title':'x'*1025},'title_invalid'),({'description':'x'*32769},'description_invalid'),
    ({'description':'\x00'},'description_invalid'),({'due_date':'2026-02-30'},'due_date_invalid'),
    ({'due_date':'20261012'},'due_date_invalid'),({'due_date':'2026-10-20','clear_due_date':True},'due_date_invalid'),
    ({'title':'New','board_id':99},'forbidden')])
def test_invalid_updates_do_not_write(editing,options,code):
    adapter,state=editing
    with pytest.raises(KaitenError) as exc: adapter.prepare_update('33',**options)
    assert exc.value.code==code and not patches(state)
    if code in {'forbidden','update_empty'}: assert not state['calls']


@pytest.mark.parametrize('reference',['Task','https://other.kaiten.ru/boards/card/33'])
def test_non_id_or_foreign_reference_is_denied_without_http(editing,reference):
    adapter,state=editing
    with pytest.raises(KaitenError): adapter.prepare_update(reference,title='New')
    assert not state['calls']


def test_approved_preview_bound_to_source_inputs_and_credentials(editing):
    adapter,state=editing
    preview=adapter.prepare_update('33',description='New')
    assert preview['before']=={'description':'Описание'} and preview['changes']=={'description':'New'}
    with pytest.raises(KaitenError): adapter.update_card('33',description='New')
    with pytest.raises(KaitenError):
        adapter.update_card('33',description='Other',authorization='approved',approval=preview['approval'])
    state['card']['description']='Another editor'
    with pytest.raises(KaitenError) as exc:
        adapter.update_card('33',description='New',authorization='approved',approval=preview['approval'])
    assert exc.value.code=='preview_changed'
    state['card']['description']='Описание'
    adapter.credentials.save_credentials('work','example.kaiten.ru','changed-token')
    with pytest.raises(KaitenError) as exc:
        adapter.update_card('33',description='New',authorization='approved',approval=preview['approval'])
    assert exc.value.code=='preview_changed' and not patches(state)


def test_change_between_inspection_and_write_blocks_even_direct_update(editing):
    adapter,state=editing;state['change_at']=2
    with pytest.raises(KaitenError) as exc:
        adapter.update_card('33',description='New',authorization='direct')
    assert exc.value.code=='preview_changed' and not patches(state)


@pytest.mark.parametrize('options',[{'title':'Задача'},{'due_date':'2026-10-15'}])
def test_noop_performs_no_patch(editing,options):
    adapter,state=editing
    result=adapter.update_card('33',authorization='direct',**options)
    assert result['status']=='already-applied' and result['attempts']==0 and not patches(state)


@pytest.mark.parametrize('applied',[False,True])
def test_timeout_verifies_state_without_retry(editing,applied):
    adapter,state=editing;state['apply']=applied;original=adapter._write_once
    def timeout(*args):
        original(*args)
        return None,KaitenError('network','Synthetic timeout')
    adapter._write_once=timeout
    result=adapter.update_card('33',title='New',authorization='direct')
    assert result['status']==('applied' if applied else 'ambiguous')
    assert len(patches(state))==1 and not result['retry_safe']


@pytest.mark.parametrize('failure',['403','503','missing_effect','readback_failure','wrong_board','wrong_id','malformed_date'])
def test_write_failures_and_unverified_readback_are_not_success(editing,failure):
    adapter,state=editing
    if failure.isdecimal(): state['status']=int(failure)
    if failure=='missing_effect': state['apply']=False
    if failure=='readback_failure': state['readback_status']=503
    if failure=='wrong_board': state['readback_override']={'board_id':99,'description':'PRIVATE'}
    if failure=='wrong_id': state['readback_override']={'id':999}
    if failure=='malformed_date': state['readback_override']={'due_date':'2026-10-20-invalid'}
    result=adapter.update_card('33',due_date='2026-10-20',authorization='direct')
    assert result['status']==('not-applied' if failure=='403' else 'ambiguous')
    assert len(patches(state))==1 and 'PRIVATE' not in json.dumps(result)


def test_policy_change_after_prepare_does_not_patch(editing):
    adapter,state=editing;original=adapter.prepare_update
    def changed(*args,**kwargs):
        preview=original(*args,**kwargs)
        adapter.store.save(profile='work',tenant='example.kaiten.ru',board_allowlist=[99])
        return preview
    adapter.prepare_update=changed
    with pytest.raises(KaitenError): adapter.update_card('33',title='New',authorization='direct')
    assert not patches(state)


def test_installed_update_preview_approval_and_nonzero_readback_failure(installed_surface,tmp_path):
    driver=tmp_path/'driver.py'
    driver.write_text('''import runpy,sys
from urllib.parse import urlsplit
from urllib.request import Request,build_opener
from kaiten_cli.client import KaitenClient,NoRedirect
origin,binary=sys.argv[1:3]
original=KaitenClient.__init__
opener=build_opener(NoRedirect())
def transport(request,**kwargs):
 p=urlsplit(request.full_url)
 return opener.open(Request(origin+p.path+('?' +p.query if p.query else ''),data=request.data,method=request.method,headers=dict(request.headers)),**kwargs)
def init(self,credentials,**kwargs): original(self,credentials,open_request=transport)
KaitenClient.__init__=init
sys.argv=[binary,*sys.argv[3:]]
runpy.run_path(binary,run_name='__main__')
''')
    env={k:v for k,v in os.environ.items() if k not in {'PYTHONPATH','PYTHONHOME','CLI_ALL_ROOT'} and not k.startswith('KAITEN_')}
    env.update(XDG_CONFIG_HOME=str(tmp_path/'personal'),PATH=str(tmp_path))
    with editing_api() as (origin,state):
        def invoke(*args,code=0,input=None):
            reply=subprocess.run([str(installed_surface/'python'),str(driver),origin,str(installed_surface/'kaiten'),'--json',*args],
                env=env,input=input,cwd=tmp_path,capture_output=True,text=True,timeout=10)
            assert reply.returncode==code,reply.stdout+reply.stderr
            assert 'PRIVATE' not in reply.stdout and 'synthetic-edit' not in reply.stdout
            return json.loads(reply.stdout)
        invoke('auth','login','--profile','work','--tenant','example.kaiten.ru','--token-stdin',input='synthetic-edit\n')
        invoke('setup','--profile','work','--tenant','example.kaiten.ru','--board','12','--confirm')
        args=('cards','update','33','--board','12','--description','New description')
        preview=invoke(*args,'--dry-run')['data']
        assert preview['before']=={'description':'Описание'} and not patches(state)
        invoke(*args,code=1)
        result=invoke(*args,'--authorization','approved','--approval',preview['approval'])
        assert result['data']['status']=='applied' and len(patches(state))==1
        assert invoke(*args,'--authorization','direct')['data']['status']=='already-applied'
        state['readback_status']=503
        state['readback_from_patch']=2
        result=invoke('cards','update','33','--clear-due-date','--authorization','direct',code=1)
        assert result['error']['code']=='mutation_ambiguous' and len(patches(state))==2
