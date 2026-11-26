"""Локальная панель управления тестовым прототипом."""
from __future__ import annotations
import hmac
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .config import APP_VERSION, ROOT, data_dir, load_config
from .engines import (
    StubLLM, OllamaEngine, OrcaRouterEngine, EngineError, MODELS,
    ORCA_MODELS, DEFAULT_ORCA_MODEL, TextStubTTS,
)
from .memory import MemoryManager, MEMORY_TYPES
from .semantic import SemanticIndex, SemanticError
from . import knowledge
from .context import resolve_context
from .scoring import safe_float

EMOTIONS=['neutral','joy','sadness','fear','anger','surprise','contempt','disgust','love']

def admin_guard(x_admin_token: str = Header(default='')) -> None:
    """C4: минимальная защита изменяющих эндпоинтов admin-токеном.

    Если KUMAR_ADMIN_TOKEN не задан (локальный прототип на 127.0.0.1),
    проверка отключена. В LAN-режиме токен обязателен: см. предупреждение
    в src/main.py. Токен нигде не логируется и не попадает в backup.
    """
    expected=os.getenv('KUMAR_ADMIN_TOKEN','').strip()
    if not expected:
        return
    if not hmac.compare_digest(x_admin_token.strip().encode('utf-8'), expected.encode('utf-8')):
        raise HTTPException(401,'Требуется корректный заголовок X-Admin-Token (KUMAR_ADMIN_TOKEN)')

class ChatInput(BaseModel):
    """Тестовое сообщение."""
    viewer: str = Field(default='test-viewer', min_length=1, max_length=80)
    text: str = Field(min_length=1,max_length=2000)
class TextInput(BaseModel):
    """Текст заметки."""
    text: str = Field(min_length=1,max_length=2000)
    viewer_id: str | None = Field(default=None,max_length=80)
    fact_key: str | None = Field(default=None,max_length=80)
    memory_type: str = Field(default='fact',max_length=30)
    importance: float = Field(default=0.5,ge=0.0,le=1.0)
    revision: int | None = None
class EngineSettingsInput(BaseModel):
    """Настройки локального тестового движка."""
    engine: str
    model: str
    thinking: bool = False

class EmotionInput(BaseModel):
    """Имя ручной эмоции."""
    emotion: str

class StreamMemoryInput(BaseModel):
    """Фрагмент, который оператор вводит прямо во время эфира."""
    text: str = Field(min_length=1, max_length=2000)
    memory_type: str = Field(default='fact', max_length=30)
    fragment_kind: str = Field(default='memory', max_length=20)
    importance: float = Field(default=0.8, ge=0.0, le=1.0)
    persistent: bool = False

class Pipeline:
    """Координировать локальный ответ и сохранение памяти."""
    def __init__(self, manager: MemoryManager, config: dict[str, Any] | None = None,
                 semantic_enabled: bool | None = None) -> None:
        self.memory=manager
        self.config=config or load_config()
        self.semantic=SemanticIndex(manager, enabled=semantic_enabled)
        self.tts=TextStubTTS(manager.data/'last_tts.txt')
        self.lock=threading.Lock()
        self.runtime_generation=0
        self.last_reload=''
        # C5: сессия OrcaRouter, которую панель создала после честного сброса.
        # None — используется ORCAROUTER_SESSION_ID из .env.
        self.orca_session_id: str | None = None

    def settings(self) -> dict[str, Any]:
        """Вернуть сохранённые настройки; по умолчанию безопасная заглушка."""
        m=self.memory
        with m.lock:
            row=m.db.execute("SELECT value FROM state WHERE key='engine_settings'").fetchone()
        if row:
            try:
                data=json.loads(row[0])
                if (data['engine']=='orcarouter' and data['model'] in ORCA_MODELS
                        or data['engine']=='stub' and data['model']=='stub') and isinstance(data['thinking'],bool):
                    return data
            except (ValueError, KeyError, TypeError): pass
        llm=self.config.get('llm', {})
        if llm.get('engine')=='stub':
            return {'engine':'stub','model':'stub','thinking':False}
        return {'engine':'orcarouter','model':llm.get('model',DEFAULT_ORCA_MODEL),'thinking':False}

    def chat(self, viewer: str, text: str) -> dict[str, Any]:
        """Собрать память, получить ответ и сохранить только успешный обмен."""
        viewer=viewer.strip(); text=text.strip()
        if not viewer or not text: raise ValueError('Введите имя зрителя и текст сообщения')
        with self.lock:
            cfg=self.settings()
            # Берём чуть более широкий набор кандидатов: слой приоритетов
            # должен увидеть актуальную заметку даже если лор оказался выше
            # неё в сыром смысловом ранге.
            retrieval=self.semantic.search(text, viewer_id=viewer, limit=12)
            matches=retrieval['matches']
            with self.memory.lock:
                graph=knowledge.graph_matches(self.memory.db,text,viewer)
            # Backstage-фрагменты текущего эфира всегда добавляются в контекст:
            # оператор может ввести их как сцену или сигнал, даже если вопрос
            # зрителя пока не содержит совпадающих слов.
            stream=self.memory.stream_memories()
            stream_records=[{
                'id':f'stream:{x["id"]}',
                'kind':'stream_memory',
                'text':x['body'],
                'source':f'Эфирный фрагмент #{x["id"]}',
                'memory_type':x.get('memory_type','fact'),
                'fragment_kind':x.get('fragment_kind','memory'),
                'importance':safe_float(x.get('importance'),0.8),
                'source_kind':'backstage',
                'confidence':1.0,
            } for x in stream]
            seen={x['text'] for x in matches}
            matches=(stream_records+[x for x in matches if x['text'] not in {s['text'] for s in stream_records}]
                     +[g for g in graph if g['text'] not in seen])[:20]
            matches,suppressed=resolve_context(text,matches)
            if suppressed:
                retrieval['warning']=(retrieval['warning']+f' {suppressed} устаревший фрагмент скрыт актуальной заметкой владельца.').strip()
            history=self.memory.recent(20, viewer_id=viewer)
            if any(x.get('kind')=='note' and x.get('revision',1)>1 for x in matches):
                history=[x for x in history if x['speaker']!='kumar']
                retrieval['warning']=(retrieval['warning']+' Ранее данные ответы модели исключены из запроса из-за найденной исправленной заметки.').strip()
            with self.memory.lock:
                row=self.memory.db.execute("SELECT value FROM state WHERE key='emotion'").fetchone()
            emotion=row[0] if row else 'neutral'
            if cfg['engine']=='orcarouter':
                engine=OrcaRouterEngine(cfg['model'], session_id=self.orca_session_id)
            elif cfg['engine']=='stub':
                engine=StubLLM()
            else:
                raise EngineError('Неизвестный движок диалога.')
            answer=engine.generate(text, matches, history, emotion, cfg['thinking'])
            self.memory.save_exchange(viewer,text,answer)
            self.semantic.schedule()
            self.tts.synthesize(answer)
            return {'response':answer,'matches':matches,'mode':cfg['engine'],
                    'search_mode':retrieval['mode'], 'search_warning':retrieval['warning'],
                    'prompt_messages':getattr(engine,'last_messages',[]),
                    'usage':getattr(engine,'last_usage',{}),
                    'prompt_source':(f'local: config/character.md ({getattr(engine,"persona_sha","")})'
                                     if cfg['engine']=='orcarouter' else 'stub')}

    def reload_llm(self) -> dict[str, Any]:
        """Честный сброс runtime-контекста (C5), без ложных обещаний.

        OrcaRouter: создаётся новый Session-Id, и следующий запрос уходит с
        заголовком X-OrcaRouter-Session-Id новой сессии. Локальная история
        сообщений в SQLite при этом сохраняется — сброс не удаляет память.
        Заглушка состояния не хранит — и об этом говорится прямо.
        """
        cfg=self.settings()
        self.runtime_generation+=1
        self.last_reload=time.strftime('%H:%M:%S')
        result: dict[str, Any]={'reloaded':True,'model_stopped':False,
                                'generation':self.runtime_generation}
        if cfg['engine']=='orcarouter':
            session_id=f'kumar-panel-{uuid.uuid4().hex[:12]}'
            self.orca_session_id=session_id
            result.update({'session_reset':True,'session_id':session_id,
                'message':f'Создана новая сессия OrcaRouter ({session_id}); '
                          'следующий запрос уйдёт с новым Session-Id. '
                          'Локальная история в базе сохранена.'})
        else:
            result.update({'session_reset':False,'session_id':None,
                'message':'Тестовая заглушка не хранит контекст: сбрасывать нечего. '
                          'Счётчик поколения увеличен.'})
        return result

def create_app(directory: Path | None=None, lore: Path | None=None, testing: bool=False) -> FastAPI:
    """Создать приложение с изолируемыми путями для тестов."""
    config=load_config()
    if config['llm']['engine'] not in ('orcarouter','stub') or config['tts']['engine'] != 'text_stub':
        raise ValueError('Поддерживаются llm.engine: orcarouter/stub и tts.engine: text_stub')
    runtime_config=json.loads(json.dumps(config))
    if testing:
        runtime_config['llm']={'engine':'stub','model':'stub'}
    path=directory or data_dir()
    lore_path=lore or ROOT/'config'/'lore'/'character'
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        manager=MemoryManager(path,lore_path,int(config['memory']['history_days']))
        app.state.memory=manager
        app.state.pipeline=Pipeline(manager, runtime_config, semantic_enabled=True if testing else None)
        app.state.pipeline.semantic.start()
        try: yield
        finally:
            app.state.pipeline.semantic.stop()
            manager.close()
    app=FastAPI(title='Лаборатория памяти',lifespan=lifespan)
    lan_mode=os.getenv('MIRA_LAN')=='1'
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=(['*'] if lan_mode else ['localhost','127.0.0.1','[::1]'])+(['testserver'] if testing else []))
    @app.middleware('http')
    async def local_origin(request: Request, call_next: Any) -> Response:
        """Не принимать запросы из сторонних веб-страниц на локальный API."""
        origin=request.headers.get('origin')
        request_host=request.headers.get('host','').split(':',1)[0]
        allowed={'http://localhost:8000','http://127.0.0.1:8000'}
        if lan_mode and request_host:
            allowed.update({f'http://{request_host}:8000',f'https://{request_host}:8000'})
        if testing: allowed.add('http://testserver')
        # Прототип намеренно ограничен фиксированным локальным портом.
        if origin and origin not in allowed: return JSONResponse({'detail':'Источник запроса запрещён'},status_code=403)
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        return response
    @app.get('/')
    def index() -> FileResponse:
        return FileResponse(ROOT/'src/static/index.html')
    app.mount('/static',StaticFiles(directory=ROOT/'src/static'),name='static')
    @app.get('/api/status')
    def status() -> dict[str, Any]:
        m: MemoryManager=app.state.memory
        with m.lock:
            row=m.db.execute("SELECT value FROM state WHERE key='emotion'").fetchone()
        settings=app.state.pipeline.settings()
        llm=('OrcaRouter / '+settings['model']+' — проверка при запросе'
             if settings['engine']=='orcarouter' else 'Тестовая заглушка — не модель')
        semantic=app.state.pipeline.semantic.status()
        return {'version':APP_VERSION, 'character_name':(ROOT/'config/name.txt').read_text(encoding='utf-8').strip()[:80] or 'Персонаж', 'mode':settings['engine'],'engine_settings':settings, 'emotion':row[0] if row else 'neutral','counts':m.counts(), 'semantic':semantic,
            'llm':llm, 'tts':'Текст в файл — без звука',
            'runtime':{'generation':app.state.pipeline.runtime_generation,
                       'last_reload':app.state.pipeline.last_reload,
                       'tts_stopped':app.state.pipeline.tts.stopped,
                       'session_id':(app.state.pipeline.orca_session_id or 'kumar-panel')
                                    if settings['engine']=='orcarouter' else None},
            'twitch':'Не подключён', 'index':('Слова + смысл (возможна частичная индексация)' if semantic['ready'] else 'Поиск по словам; Ollama отключена'),
            'llm_provider':settings['engine'],
            'api_key_configured':bool(os.getenv('ORCAROUTER_API_KEY')),
            'memory_types':sorted(MEMORY_TYPES)}
    @app.get('/api/metrics')
    def metrics() -> dict[str, Any]:
        """Лёгкие локальные метрики без дополнительных зависимостей."""
        cores=os.cpu_count() or 1
        try:
            load1,load5,load15=os.getloadavg()
        except (AttributeError,OSError):
            load1=load5=load15=0.0
        memory_total=memory_available=None
        try:
            values={}
            for line in Path('/proc/meminfo').read_text(encoding='utf-8').splitlines():
                key,value=line.split(':',1)
                values[key]=int(value.strip().split()[0])*1024
            memory_total=values.get('MemTotal')
            memory_available=values.get('MemAvailable')
        except (FileNotFoundError,ValueError):
            pass
        disk=shutil.disk_usage(ROOT)
        total=memory_total or 0
        used=(total-(memory_available or 0)) if total else 0
        gpu=None
        gpu_label='nvidia-smi не подключён'
        try:
            result=subprocess.run([
                'nvidia-smi',
                '--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw',
                '--format=csv,noheader,nounits'
            ],capture_output=True,text=True,timeout=2,check=False)
            line=next((x.strip() for x in result.stdout.splitlines() if x.strip()),'')
            parts=[x.strip() for x in line.split(',')]
            if len(parts)>=6:
                name,util,used_mib,total_mib,temp,power=parts[:6]
                gpu=float(util)
                gpu_label=f'{name} · {used_mib}/{total_mib} MiB · {temp}°C · {power} W'
        except (OSError,subprocess.TimeoutExpired,ValueError):
            pass
        return {'cpu':round(min(100.0,load1/cores*100),1),'load':round(load1,2),'cores':cores,
                'memory':round(used/total*100,1) if total else None,
                'memory_used':used,'memory_total':total,
                'disk':round((disk.total-disk.free)/disk.total*100,1) if disk.total else 0,
                'disk_used':disk.total-disk.free,'disk_total':disk.total,
                'gpu':gpu,'gpu_label':gpu_label,'ollama':'отключена проектом'}
    @app.post('/api/runtime/reload',dependencies=[Depends(admin_guard)])
    def runtime_reload() -> dict[str, Any]:
        return app.state.pipeline.reload_llm()
    @app.post('/api/runtime/tts/stop',dependencies=[Depends(admin_guard)])
    def runtime_tts_stop() -> dict[str, Any]:
        app.state.pipeline.tts.stop()
        return {'stopped':True}
    @app.post('/api/runtime/tts/resume',dependencies=[Depends(admin_guard)])
    def runtime_tts_resume() -> dict[str, Any]:
        app.state.pipeline.tts.resume()
        return {'stopped':False}
    @app.get('/api/runtime/stream-memories')
    def stream_memories() -> list[dict[str, Any]]:
        return app.state.memory.stream_memories()
    @app.post('/api/runtime/stream-memories',dependencies=[Depends(admin_guard)])
    def add_stream_memory(payload: StreamMemoryInput) -> dict[str, Any]:
        text=payload.text.strip()
        if not text: raise HTTPException(400,'Введите текст фрагмента')
        if payload.memory_type not in MEMORY_TYPES:
            raise HTTPException(400,'Неизвестный тип памяти')
        if payload.persistent:
            note_id=app.state.memory.add_note(text, fact_key=None,
                                               memory_type=payload.memory_type,
                                               importance=payload.importance,
                                               source_kind='backstage')
            app.state.pipeline.semantic.schedule()
            return {'id':note_id,'persistent':True,'message':'Фрагмент сохранён в постоянную память.'}
        fragment_id=app.state.memory.add_stream_memory(
            text,payload.memory_type,payload.fragment_kind,payload.importance
        )
        app.state.pipeline.semantic.schedule()
        return {'id':fragment_id,'persistent':False,'message':'Фрагмент добавлен в текущий эфир.'}
    @app.delete('/api/runtime/stream-memories',dependencies=[Depends(admin_guard)])
    def clear_stream_memories() -> dict[str, Any]:
        count=app.state.memory.clear_stream_memories()
        app.state.pipeline.semantic.schedule()
        return {'cleared':count}
    @app.post('/api/engine-settings',dependencies=[Depends(admin_guard)])
    def engine_settings(payload: EngineSettingsInput) -> dict[str, Any]:
        if payload.engine=='orcarouter' and payload.model not in ORCA_MODELS:
            raise HTTPException(400,'Недопустимая модель OrcaRouter')
        if payload.engine=='stub' and (not testing or payload.model!='stub'):
            raise HTTPException(400,'Заглушка доступна только во время тестов')
        if payload.engine not in ('stub','orcarouter'):
            raise HTTPException(400,'Недопустимый движок')
        data=payload.model_dump()
        m: MemoryManager=app.state.memory
        with m.lock,m.db:
            m.db.execute("INSERT INTO state(key,value) VALUES('engine_settings',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(data),))
        return data
    @app.get('/api/messages')
    def messages() -> list[dict[str, Any]]:
        return app.state.memory.recent()
    @app.delete('/api/messages/{message_id}',dependencies=[Depends(admin_guard)])
    def delete_message(message_id: int) -> dict[str, bool]:
        m: MemoryManager=app.state.memory
        with m.lock, m.db:
            row=m.db.execute('SELECT viewer_id,speaker FROM messages WHERE id=?',(message_id,)).fetchone()
            deleted=m.db.execute('DELETE FROM messages WHERE id=?',(message_id,)).rowcount>0
            if row and row['speaker']=='viewer':
                m.db.execute('UPDATE viewers SET count=MAX(count-1,0) WHERE viewer_id=?',(row['viewer_id'],))
                m.db.execute('DELETE FROM viewers WHERE viewer_id=? AND count=0',(row['viewer_id'],))
        if not deleted: raise HTTPException(404,'Сообщение не найдено')
        m.rebuild_index()
        app.state.pipeline.semantic.schedule()
        return {'deleted':True}
    @app.post('/api/chat')
    def chat(payload: ChatInput) -> dict[str, Any]:
        try: return app.state.pipeline.chat(payload.viewer,payload.text)
        except ValueError as exc: raise HTTPException(400,str(exc)) from exc
        except EngineError as exc: raise HTTPException(502,str(exc)) from exc
    @app.get('/api/search')
    def search(q: str='') -> list[dict[str, Any]]:
        return app.state.pipeline.semantic.search(q[:500])['matches']
    @app.get('/api/search-debug')
    def search_debug(q: str='') -> dict[str, Any]:
        return app.state.pipeline.semantic.search(q[:500])
    @app.get('/api/semantic-status')
    def semantic_status() -> dict[str, Any]:
        return app.state.pipeline.semantic.status()
    @app.post('/api/semantic-reindex',dependencies=[Depends(admin_guard)])
    def semantic_reindex() -> dict[str, Any]:
        try:
            result=app.state.pipeline.semantic.build()
            app.state.pipeline.semantic.schedule()
            return result
        except SemanticError as exc: raise HTTPException(502,str(exc)) from exc
    @app.get('/api/notes')
    def notes() -> list[dict[str, Any]]:
        return app.state.memory.notes()
    @app.post('/api/notes',dependencies=[Depends(admin_guard)])
    def add_note(payload: TextInput) -> dict[str, int]:
        text=payload.text.strip()
        if not text: raise HTTPException(400,'Введите текст заметки')
        if payload.memory_type not in MEMORY_TYPES:
            raise HTTPException(400,'Неизвестный тип памяти')
        try: note_id=app.state.memory.add_note(text,payload.viewer_id,payload.fact_key,payload.memory_type,payload.importance)
        except sqlite3.IntegrityError as exc: raise HTTPException(409,'Этот ключ уже занят. Измените существующую заметку.') from exc
        app.state.pipeline.semantic.schedule()
        return {'id':note_id}
    @app.put('/api/notes/{note_id}',dependencies=[Depends(admin_guard)])
    def edit_note(note_id: int, payload: TextInput) -> dict[str,bool]:
        text=payload.text.strip()
        if not text or payload.revision is None: raise HTTPException(400,'Введите текст и версию заметки')
        if payload.memory_type not in MEMORY_TYPES:
            raise HTTPException(400,'Неизвестный тип памяти')
        try:
            found=app.state.memory.update_note(note_id,text,payload.viewer_id,payload.fact_key,payload.revision,payload.memory_type,payload.importance)
        except sqlite3.IntegrityError as exc: raise HTTPException(409,'Этот ключ уже занят.') from exc
        except ValueError as exc: raise HTTPException(409,str(exc)) from exc
        if not found: raise HTTPException(404,'Заметка не найдена')
        app.state.pipeline.semantic.schedule()
        return {'updated':True}
    @app.delete('/api/notes/{note_id}',dependencies=[Depends(admin_guard)])
    def delete_note(note_id: int) -> dict[str,bool]:
        if not app.state.memory.delete_note(note_id): raise HTTPException(404,'Заметка не найдена')
        app.state.pipeline.semantic.schedule()
        return {'deleted':True}
    @app.get('/api/lore')
    def lore_list() -> list[dict[str, Any]]:
        return app.state.memory.lore_chunks()
    @app.post('/api/lore/reindex',dependencies=[Depends(admin_guard)])
    def reindex() -> dict[str,int]:
        m=app.state.memory
        with m.lock, m.db: knowledge.sync_graph(m.db,m.lore)
        indexed=m.rebuild_index()
        app.state.pipeline.semantic.schedule()
        return {'indexed':indexed}
    @app.get('/api/graph')
    def graph(limit: int=2000) -> list[dict[str,Any]]:
        m=app.state.memory
        limit=max(1,min(int(limit),5000))
        with m.lock: return [dict(r) for r in m.db.execute('SELECT * FROM knowledge_edges ORDER BY id DESC LIMIT ?',(limit,))]
    @app.get('/api/candidates')
    def candidates() -> list[dict[str,Any]]:
        m=app.state.memory
        with m.lock: return [dict(r) for r in m.db.execute('SELECT * FROM memory_candidates ORDER BY id DESC LIMIT 200')]
    @app.post('/api/candidates/{candidate_id}/{decision}',dependencies=[Depends(admin_guard)])
    def candidate_decision(candidate_id: int, decision: str) -> dict[str,bool]:
        if decision not in ('approved','rejected','pending'): raise HTTPException(400,'Неизвестное решение')
        m=app.state.memory
        with m.lock, m.db: found=knowledge.decide(m.db,candidate_id,decision)
        if not found: raise HTTPException(404,'Предложение не найдено')
        app.state.pipeline.semantic.schedule()
        return {'updated':True}
    @app.get('/api/notes/{note_id}/versions')
    def note_versions(note_id: int) -> list[dict[str,Any]]:
        m=app.state.memory
        with m.lock: return [dict(r) for r in m.db.execute('''SELECT revision,body,viewer_id,fact_key,
            memory_type,importance,source_kind,confidence,created FROM note_versions
            WHERE note_id=? ORDER BY revision DESC''',(note_id,))]
    @app.post('/api/demo',dependencies=[Depends(admin_guard)])
    def load_demo() -> dict[str,int]:
        m=app.state.memory
        with m.lock:
            if m.db.execute("SELECT 1 FROM state WHERE key='demo_loaded'").fetchone(): raise HTTPException(409,'Демонстрация уже загружена в эту базу.')
            # One transaction: source messages, derived candidates and marker.
            data=json.loads((ROOT/'fixtures/chat.json').read_text(encoding='utf-8'))
            import time
            now=time.time()
            with m.db:
                for item in data:
                    viewer,text,answer=item['viewer'],item['text'],item['response']
                    cur=m.db.execute('INSERT INTO messages(viewer_id,speaker,body,created) VALUES(?,?,?,?)',(viewer,'viewer',text,now))
                    knowledge.capture(m.db,int(cur.lastrowid),viewer,text)
                    m.db.execute('INSERT INTO messages(viewer_id,speaker,body,created) VALUES(?,?,?,?)',(viewer,'kumar',answer,now))
                    m.db.execute("INSERT INTO viewers(viewer_id,first_seen,last_seen,count) VALUES(?,?,?,1) ON CONFLICT(viewer_id) DO UPDATE SET last_seen=excluded.last_seen,count=count+1",(viewer,now,now))
                m.db.execute("INSERT INTO state(key,value) VALUES('demo_loaded','1')")
        app.state.pipeline.semantic.schedule()
        return {'exchanges':len(data)}
    @app.post('/api/emotion',dependencies=[Depends(admin_guard)])
    def set_emotion(payload: EmotionInput) -> dict[str,str]:
        if payload.emotion not in EMOTIONS: raise HTTPException(400,'Неизвестная эмоция')
        m: MemoryManager=app.state.memory
        with m.lock,m.db:
            m.db.execute("INSERT INTO state(key,value) VALUES('emotion',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(payload.emotion,))
        return {'emotion':payload.emotion}
    @app.get('/api/backup',dependencies=[Depends(admin_guard)])
    def backup() -> Response:
        """Скачать согласованную копию данных; без токенов и поискового индекса."""
        m: MemoryManager=app.state.memory
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'memory.sqlite3'; m.copy_database(db)
            buf=io.BytesIO()
            with zipfile.ZipFile(buf,'w',compression=zipfile.ZIP_DEFLATED) as z:
                z.write(db,'data/memory.sqlite3')
                z.writestr('backup-info.json',json.dumps({'format_version':1},ensure_ascii=False))
                # reference/* в обычный backup не попадает: там живёт приватный
                # авторский архив (author-private.md и т.п.), который персонажу
                # запрещено знать. Restore по-прежнему принимает старые архивы
                # с reference/*.md для обратной совместимости.
                for f in lore_path.glob('*.md'):
                    if f.is_file() and not f.is_symlink(): z.write(f,f'config/lore/character/{f.name}')
                z.write(ROOT/'config/config.yaml','config/config.yaml')
                z.write(ROOT/'config/character.md','config/character.md')
                z.write(ROOT/'config/name.txt','config/name.txt')
                z.write(ROOT/'config/graph.json','config/graph.json')
            return Response(buf.getvalue(),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="memory-lab-backup.zip"'})
    return app

app=create_app()
