from pathlib import Path
import io, json, sqlite3, time, zipfile
import pytest
from fastapi.testclient import TestClient
from src.app import create_app
from src.memory import MemoryManager
from src.restore import restore
from src import semantic
from src.embeddings import stub_embedding

@pytest.fixture
def client(tmp_path,monkeypatch):
    lore=tmp_path/'lore';lore.mkdir();(lore/'world.md').write_text('Мира пьёт чай с мятой.\n\nМира дружит с Артёмом.',encoding='utf8')
    monkeypatch.setattr(semantic,'embed',lambda texts:[stub_embedding(x) for x in texts])
    app=create_app(tmp_path/'data',lore,testing=True)
    with TestClient(app) as c: yield c

def wait_fresh(c):
    for _ in range(150):
        s=c.get('/api/semantic-status').json()
        if s['ready'] and s['indexed']==s['total'] and not s['building']: return s
        time.sleep(.03)
    raise AssertionError(s)

def test_initial_stub(client):
    assert client.get('/api/status').json()['mode']=='stub'
    r=client.post('/api/chat',json={'viewer':'alice','text':'Привет'}); assert r.status_code==200
    assert 'заглушк' in r.json()['response'] or 'Тестовый' in r.json()['response']
    assert len(client.get('/api/messages').json())==2

def test_note_scope(client):
    for body in [{'text':'Алиса любит груши','viewer_id':'alice'},{'text':'Боб любит яблоки','viewer_id':'bob'},{'text':'Мира любит чай'}]: assert client.post('/api/notes',json=body).status_code==200
    p=client.app.state.pipeline.semantic
    assert not any('Боб' in r['text'] for r in p.search('Боб яблоки',viewer_id='alice')['matches'])
    assert not any('Алиса' in r['text'] for r in p.search('Алиса груши',viewer_id='bob')['matches'])
    assert any('Боб' in r['text'] for r in p.search('Боб яблоки')['matches']) # администратор

def test_slot_conflict_and_revision(client):
    data={'text':'Любит чай','fact_key':'НАПИТОК','viewer_id':'alice'}
    i=client.post('/api/notes',json=data).json()['id']
    assert client.post('/api/notes',json={**data,'text':'кофе','fact_key':'напиток'}).status_code==409
    assert client.post('/api/notes',json={**data,'viewer_id':'bob'}).status_code==200
    r=client.put(f'/api/notes/{i}',json={**data,'text':'Любит сок','revision':1}); assert r.status_code==200
    assert client.put(f'/api/notes/{i}',json={**data,'revision':1}).status_code==409
    assert client.get('/api/notes').json()[-1]['body']=='Любит сок'

def test_structured_memory_metadata_and_revision(client):
    r=client.post('/api/notes',json={
        'text':'Алиса обещала рассказать о старом маяке.',
        'viewer_id':'alice',
        'fact_key':'обещание',
        'memory_type':'promise',
        'importance':0.9,
    })
    assert r.status_code==200
    note=client.get('/api/notes').json()[0]
    assert note['memory_type']=='promise' and note['importance']==0.9
    assert client.put(f'/api/notes/{note["id"]}',json={
        'text':'Алиса рассказала о старом маяке.',
        'viewer_id':'alice',
        'fact_key':'обещание',
        'memory_type':'event',
        'importance':0.8,
        'revision':note['revision'],
    }).status_code==200
    current=client.get('/api/notes').json()[0]
    assert current['memory_type']=='event' and current['importance']==0.8
    versions=client.get(f'/api/notes/{note["id"]}/versions').json()
    assert versions[0]['memory_type']=='event' and versions[1]['memory_type']=='promise'

def test_invalid_structured_memory_type(client):
    r=client.post('/api/notes',json={'text':'Неверный тип','memory_type':'secret_memory'})
    assert r.status_code==400

def test_questions_are_not_memories(client):
    m=client.app.state.memory;m.save_exchange('alice','Кто ремонтирует радио?','Артём');m.save_exchange('alice','Привет','Здравствуйте')
    found=client.app.state.pipeline.semantic.search('ремонтирует радио привет',viewer_id='alice')['matches']
    assert all(r['kind']!='message' for r in found)
    assert len(m.recent(viewer_id='alice'))==4

def test_index_background_add_edit_delete(client):
    assert client.post('/api/semantic-reindex').status_code==200
    i=client.post('/api/notes',json={'text':'Мира нашла редкий фонарь','fact_key':'фонарь'}).json()['id'];wait_fresh(client)
    assert any(r['text']=='Мира нашла редкий фонарь' for r in client.get('/api/search?q=фонарь').json())
    assert client.put(f'/api/notes/{i}',json={'text':'Мира нашла старый фонарь','fact_key':'фонарь','revision':1}).status_code==200
    wait_fresh(client); assert client.delete(f'/api/notes/{i}').status_code==200;wait_fresh(client)
    assert not any(r['kind']=='note' for r in client.get('/api/search?q=фонарь').json())

def test_no_leak_on_stale_and_failed_index(client,monkeypatch):
    client.post('/api/semantic-reindex');i=client.post('/api/notes',json={'text':'Секретная груша'}).json()['id'];wait_fresh(client)
    monkeypatch.setattr(semantic,'embed',lambda texts: (_ for _ in ()).throw(semantic.SemanticError('Нет модели')))
    client.delete(f'/api/notes/{i}')
    assert all('Секретная груша' not in r['text'] for r in client.get('/api/search?q=груша').json())
    out=client.get('/api/search-debug?q=чай').json();assert out['mode']=='words' and 'Нет модели' in out['warning']

def test_index_build_failure_preserves_old(client,monkeypatch):
    client.post('/api/semantic-reindex');p=client.app.state.pipeline;name=p.semantic._name()
    monkeypatch.setattr(semantic,'embed',lambda texts: (_ for _ in ()).throw(semantic.SemanticError('отказ')))
    assert client.post('/api/semantic-reindex').status_code==502
    assert p.semantic._name()==name and p.semantic._collection() is not None

def test_backup_restore(client,tmp_path):
    client.post('/api/notes',json={'text':'Заметка для копии'});data=client.get('/api/backup').content
    archive=tmp_path/'copy.zip';archive.write_bytes(data)
    with zipfile.ZipFile(archive) as z:
        assert 'config/character.md' in z.namelist();assert 'config/lore/character/world.md' in z.namelist();assert all('.env' not in x and 'chroma' not in x for x in z.namelist())
    dest=tmp_path/'restored';restore(archive,dest)
    db=sqlite3.connect(dest/'data/memory.sqlite3');assert db.execute('select body from notes').fetchone()[0]=='Заметка для копии';db.close()
    with pytest.raises(ValueError):restore(archive,dest)

def test_backup_excludes_reference(client):
    """C3: обычный backup не содержит reference/* (в т.ч. author-private.md)."""
    with zipfile.ZipFile(io.BytesIO(client.get('/api/backup').content)) as z:
        names=z.namelist()
        assert all(not n.startswith('reference/') for n in names)
        assert 'config/character.md' in names

def test_backup_never_contains_private_author_file(client,tmp_path,monkeypatch):
    """Даже если в reference/ лежит приватный файл, в архив он не попадает."""
    fake=tmp_path/'fake-root';(fake/'reference').mkdir(parents=True);(fake/'config').mkdir()
    (fake/'reference'/'author-private.md').write_text('АВТОРСКИЙ СЕКРЕТ-МАРКЕР',encoding='utf8')
    for name in ('config.yaml','character.md','name.txt','graph.json'):
        (fake/'config'/name).write_text('заглушка',encoding='utf8')
    monkeypatch.setattr('src.app.ROOT',fake)
    data=client.get('/api/backup').content
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names=z.namelist()
        assert all(not n.startswith('reference/') for n in names)
        assert all('АВТОРСКИЙ СЕКРЕТ-МАРКЕР' not in z.read(n).decode('utf8','ignore') for n in names)
        assert 'config/lore/character/world.md' in names

def test_reject_malicious_archive(client,tmp_path):
    raw=client.get('/api/backup').content
    with zipfile.ZipFile(io.BytesIO(raw)) as source:
        archive=tmp_path/'bad.zip'
        with zipfile.ZipFile(archive,'w') as z:
            for item in source.infolist(): z.writestr(item.filename,source.read(item))
            z.writestr('../escape.txt','bad')
    dest=tmp_path/'restored'
    with pytest.raises(ValueError):restore(archive,dest)
    assert not dest.exists() and not (tmp_path/'escape.txt').exists()

def test_security_origin(client):
    assert client.post('/api/notes',headers={'origin':'https://evil.example'},json={'text':'bad'}).status_code==403
    assert client.get('/api/status',headers={'host':'evil.example'}).status_code==400

def test_blanks(client):
    assert client.post('/api/chat',json={'viewer':'  ','text':'hello'}).status_code==400
    assert client.post('/api/notes',json={'text':'  '}).status_code==400
    assert client.get('/api/search?q=').json()==[]

def test_reopen(tmp_path,monkeypatch):
    monkeypatch.setattr(semantic,'embed',lambda texts:[stub_embedding(x) for x in texts])
    lore=tmp_path/'lore';lore.mkdir();(lore/'x.md').write_text('Мира любит чай.',encoding='utf8');path=tmp_path/'data'
    with TestClient(create_app(path,lore,testing=True)) as c:
        c.post('/api/notes',json={'text':'Привет из прошлого'});assert c.post('/api/semantic-reindex').status_code==200
    with TestClient(create_app(path,lore,testing=True)) as c:
        assert c.get('/api/notes').json()[0]['body']=='Привет из прошлого'; wait_fresh(c)

def test_chat_failure_does_not_save(client,monkeypatch):
    from src.engines import StubLLM,EngineError
    monkeypatch.setattr(StubLLM,'generate',lambda *args: (_ for _ in ()).throw(EngineError('нет связи')))
    client.post('/api/engine-settings',json={'engine':'stub','model':'stub','thinking':False})
    assert client.post('/api/chat',json={'viewer':'a','text':'привет'}).status_code==502
    assert client.get('/api/messages').json()==[]

def test_private_reference_not_indexed(client,tmp_path):
    (tmp_path/'secret.md').write_text('суперсекрет',encoding='utf8')
    assert all('суперсекрет' not in x['text'] for x in client.get('/api/lore').json())
    assert not client.get('/api/search?q=суперсекрет').json()

def test_lore_stale_disallowed(client,tmp_path):
    client.post('/api/semantic-reindex');(tmp_path/'lore/world.md').write_text('Теперь Мира любит сок.',encoding='utf8')
    result=client.get('/api/search?q=чай').json()
    assert all('чай с мятой' not in r['text'] for r in result)
    client.post('/api/lore/reindex');wait_fresh(client)
    assert any('сок' in r['text'] for r in client.get('/api/search?q=сок').json())

@pytest.mark.parametrize('question', ['Привет','Здравствуйте','Кто такой Артём?','что ты пьёшь','Как называется маяк','Расскажи о себе','почему ветер?'])
def test_raw_questions_are_not_retrieved(client,question):
    m=client.app.state.memory;m.save_exchange('a',question,'Ответ')
    assert all(r['kind']!='message' for r in client.app.state.pipeline.semantic.search(question,viewer_id='a')['matches'])

def test_statement_attributed_to_viewer(client):
    m=client.app.state.memory;m.save_exchange('alice','Я коллекционирую кораблики','Понятно')
    results=client.app.state.pipeline.semantic.search('кораблики',viewer_id='alice')['matches']
    assert any(r['kind']=='message' and r['viewer_id']=='alice' for r in results)
    assert not any(r['kind']=='message' for r in client.app.state.pipeline.semantic.search('кораблики',viewer_id='bob')['matches'])

def test_old_message_not_retrieved_but_kept(client):
    m=client.app.state.memory;m.save_exchange('alice','Я коллекционирую кораблики','Понятно')
    with m.lock,m.db: m.db.execute('UPDATE messages SET created=?',(time.time()-9*86400,))
    assert len(m.recent())==2
    assert not any(r['kind']=='message' for r in client.app.state.pipeline.semantic.search('кораблики',viewer_id='alice')['matches'])

def test_deleted_note_does_not_appear_from_stale_vectors(client):
    note=client.post('/api/notes',json={'text':'Голубой необычный маяк'}).json()['id'];client.post('/api/semantic-reindex')
    p=client.app.state.pipeline; p.semantic.stop();p.semantic.worker=None
    client.delete(f'/api/notes/{note}')
    assert not any('Голубой необычный маяк' in x['text'] for x in p.semantic.search('маяк')['matches'])

def test_duplicate_content_suppressed(client):
    client.post('/api/notes',json={'text':'Мира любит чай'});client.post('/api/notes',json={'text':'Мира любит чай'});client.post('/api/semantic-reindex')
    results=client.get('/api/search?q=чай').json()
    assert sum(x['text']=='Мира любит чай' for x in results)==1

def test_storage_slot_concurrent(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    m=MemoryManager(tmp_path/'data',tmp_path/'lore')
    def add(i):
        try: m.add_note(str(i),'alice','favourite');return 'ok'
        except sqlite3.IntegrityError:return 'collision'
    with ThreadPoolExecutor(max_workers=8) as ex: results=list(ex.map(add,range(16)))
    assert results.count('ok')==1 and results.count('collision')==15;m.close()

def test_edit_missing(client):
    assert client.put('/api/notes/999',json={'text':'test','revision':1}).status_code==404
    assert client.delete('/api/notes/999').status_code==404

def test_secret_files_never_in_context(client,tmp_path):
    (tmp_path/'lore'/'hidden.md').symlink_to(tmp_path/'secret.md');(tmp_path/'secret.md').write_text('пароль PINEAPPLE',encoding='utf8')
    assert not any('PINEAPPLE' in x['text'] for x in client.get('/api/lore').json())

def test_rebuild_does_not_remove_notes(client,monkeypatch):
    i=client.post('/api/notes',json={'text':'дождь'}).json()['id'];client.post('/api/semantic-reindex')
    monkeypatch.setattr(semantic,'embed',lambda texts: (_ for _ in ()).throw(semantic.SemanticError('нет')))
    assert client.post('/api/semantic-reindex').status_code==502
    assert any(x['id']==i for x in client.get('/api/notes').json())

@pytest.mark.parametrize('badpath',['../bad','/absolute','config/lore/character/../../bad.md','data/.env','src/app.py'])
def test_archive_rejects_unsafe_paths(client,tmp_path,badpath):
    raw=client.get('/api/backup').content;archive=tmp_path/'bad.zip'
    with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(archive,'w') as dest:
        for item in source.infolist(): dest.writestr(item.filename,source.read(item))
        dest.writestr(badpath,'x')
    with pytest.raises(ValueError):restore(archive,tmp_path/'out')
    assert not (tmp_path/'out').exists()
