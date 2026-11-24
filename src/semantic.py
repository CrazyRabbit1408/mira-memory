"""Восстанавливаемый смысловой индекс; SQLite и исходные файлы остаются источником данных."""
from __future__ import annotations
import hashlib
import json
import math
import os
import re
import threading
import uuid
from typing import Any
import httpx
from .memory import MemoryManager
from .embeddings import tokens

MODEL='qwen3-embedding:0.6b'
URL='http://127.0.0.1:11434/api/embed'
QUERY_PREFIX='Instruct: Retrieve relevant passages for this question.\nQuery: '

class SemanticError(Exception):
    """Ошибка смыслового поиска, не изменяющая основную память."""

def digest(text: str) -> str:
    """Проверить актуальность вектора по содержимому источника."""
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def embed(texts: list[str]) -> list[list[float]]:
    """Вызвать локальный Ollama embeddings API; не обращаться к сетевым прокси."""
    try:
        with httpx.Client(timeout=180,trust_env=False) as client:
            response=client.post(URL,json={'model':MODEL,'input':texts,'truncate':False})
            if response.status_code==404:
                raise SemanticError('Модель поиска не найдена. Установите её командой ollama pull qwen3-embedding:0.6b.')
            response.raise_for_status()
            body=response.json()
    except httpx.TimeoutException as exc:
        raise SemanticError('Модель поиска не ответила за 180 секунд.') from exc
    except httpx.ConnectError as exc:
        raise SemanticError('Нет связи с локальной Ollama. Проверьте, что сервер запущен.') from exc
    except (httpx.HTTPError,ValueError) as exc:
        raise SemanticError('Ошибка Ollama при создании векторов. Проверьте сервер и модель поиска.') from exc
    vectors=body.get('embeddings') if isinstance(body,dict) else None
    if not isinstance(vectors,list) or len(vectors)!=len(texts) or not vectors:
        raise SemanticError('Ollama вернула неполный набор векторов.')
    size=None
    for v in vectors:
        if not isinstance(v,list) or not v or any(not isinstance(n,(int,float)) or not math.isfinite(n) for n in v):
            raise SemanticError('Ollama вернула некорректный вектор.')
        if size is None: size=len(v)
        if len(v)!=size: raise SemanticError('Размеры векторов не совпадают.')
    return vectors

# Исключаем частые служебные слова только для словесной части;
# смысловая модель по-прежнему получает оригинальный запрос полностью.
STOPWORDS = frozenset("""а без был была было были бы быть в вам вас весь во вот все всею вы где да для до его ее ей еею если есть еще же за зачем и из или им ими их к как какая какие какой какую какого каком каким какими каких кому кто ли меня мне мной мы на над наш не него нее ней нет ни но о об он она они оно от по под при про раз с себя себе со так такой там те тебя тебе тем то тот ты у уже хотя чего чей чему чем чья чьи что чтобы я это эта эти этою этот этой этом""".split()) - {"не", "ни", "нет", "без"}

def lexical_rank(query: str, records: list[dict[str,Any]], limit: int=20) -> list[dict[str,Any]]:
    """Ранжировать по значимым точным словам и их редкости в текущей выборке."""
    def terms(text: str) -> set[str]:
        def stem(word: str) -> str:
            word=word.replace('ё','е')
            # A small conservative suffix normalizer, not a full morphological analyzer.
            if re.fullmatch(r'[а-я]+',word):
                for suffix in ('ешь','ишь','ете','ите','ать','ять','ить','ала','али','ели','ого','ему','ами','ями','ами','ами','ой','ый','ая','ое','ые','ов','ам','ах','ом','ем','ет','ит','ут','ют','ую','юю','а','я','ы','и','е','у','ю'):
                    if word.endswith(suffix) and len(word)-len(suffix)>=4:
                        return word[:-len(suffix)]
            return word
        return {stem(t) for t in tokens(text) if t.replace('ё','е') not in STOPWORDS}
    query_terms=terms(query)
    if not query_terms: return []
    indexed=[(r,terms(r['text']+' '+str(r.get('fact_key','')))) for r in records]
    counts={word:sum(word in terms_ for _,terms_ in indexed) for word in query_terms}
    result=[]
    for r,doc in indexed:
        matched=query_terms & doc
        if not matched: continue
        score=sum(math.log((1+len(indexed))/(1+counts[w]))+1 for w in matched)
        result.append({**r, 'word_score':round(score,4)})
    return sorted(result,key=lambda r:(-r['word_score'],0 if r['kind']=='note' else 1,r['id']))[:limit]

class SemanticIndex:
    """Строить отдельный индекс и безопасно переключать его после успеха."""
    def __init__(self, memory: MemoryManager, enabled: bool | None = None) -> None:
        self.memory=memory
        self.enabled=(os.getenv('KUMAR_SEMANTIC_SEARCH','0').strip().lower() in {'1','true','yes'}
                      if enabled is None else enabled)
        self.build_lock=threading.Lock()
        self.event=threading.Event()
        self.stopping=False
        self.last_error=''
        self.worker: threading.Thread | None=None

    def start(self) -> None:
        """После перезапуска проверить неиндексированные изменения."""
        if not self.enabled: return
        if self.worker is None:
            self.worker=threading.Thread(target=self._run,daemon=True,name='memory-indexer')
            self.worker.start()
            if self._name(): self.schedule()

    def schedule(self) -> None:
        """Поставить обновление в очередь, если индекс уже создан."""
        if not self.enabled: return
        if self.worker is not None and self._name(): self.event.set()

    def stop(self) -> None:
        """Дождаться завершения фоновой записи до закрытия базы."""
        self.stopping=True
        self.event.set()
        if self.worker is not None: self.worker.join()

    def _run(self) -> None:
        while True:
            self.event.wait()
            if self.stopping: return
            self.event.clear()
            try:
                self.sync()
                self.last_error=''
            except Exception as exc:
                self.last_error=str(exc) or 'Ошибка обновления индекса'

    def sync(self) -> None:
        """Обновлять только изменённые записи; источник истины — SQLite и файлы."""
        with self.build_lock:
            coll=self._collection()
            if coll is None: return
            records=self._records()
            stored=coll.get(include=['metadatas'])
            saved={i:(m or {}).get('hash') for i,m in zip(stored['ids'],stored['metadatas'])}
            changed=[r for r in records if saved.get(r['id'])!=digest(r['text'])]
            for start in range(0,len(changed),16):
                batch=changed[start:start+16]
                vectors=embed([r['text'] for r in batch])
                coll.upsert(ids=[r['id'] for r in batch],embeddings=vectors,metadatas=[{'hash':digest(r['text'])} for r in batch])
            live={r['id'] for r in self._records()}
            stale=[i for i in saved if i not in live]
            if stale: coll.delete(ids=stale)


    def _name(self) -> str | None:
        with self.memory.lock:
            row=self.memory.db.execute("SELECT value FROM state WHERE key='semantic_index'").fetchone()
        if not row: return None
        try:
            value=json.loads(row[0])
            return value['name'] if value.get('model')==MODEL else None
        except (ValueError,TypeError,KeyError): return None

    def _collection(self) -> Any | None:
        name=self._name()
        if not name: return None
        try: return self.memory.client.get_collection(name)
        except Exception: return None

    def _records(self) -> list[dict[str,Any]]:
        with self.memory.lock: return self.memory._records()

    def status(self) -> dict[str,Any]:
        """Показать, сколько актуальных фрагментов находится в смысловом индексе."""
        records=self._records()
        if not self.enabled:
            return {'ready':False,'indexed':0,'total':len(records),'building':False,
                    'model':'disabled','last_error':'Смысловой поиск отключён: Ollama не используется.'}
        coll=self._collection()
        if coll is None: return {'ready':False,'indexed':0,'total':len(records),'building':self.build_lock.locked(),'model':MODEL,'last_error':self.last_error}
        items=coll.get(include=['metadatas'])
        saved={i:m.get('hash') for i,m in zip(items['ids'],items['metadatas']) if m}
        valid=sum(saved.get(r['id'])==digest(r['text']) for r in records)
        return {'ready':True,'indexed':valid,'total':len(records),'building':self.build_lock.locked(),'model':MODEL,'last_error':self.last_error}

    def build(self) -> dict[str,Any]:
        """Создать индекс в отдельной коллекции; прежняя остаётся при сбое."""
        if not self.enabled:
            raise SemanticError('Смысловой поиск отключён в этой сборке; используется поиск по словам.')
        if not self.build_lock.acquire(blocking=False): raise SemanticError('Индексация уже выполняется.')
        coll=None
        try:
            records=self._records()
            if not records: raise SemanticError('Нет записей для индексации.')
            name='kumar_sem_'+uuid.uuid4().hex
            coll=self.memory.client.create_collection(name,metadata={'hnsw:space':'cosine','model':MODEL})
            for start in range(0,len(records),16):
                batch=records[start:start+16]
                vectors=embed([r['text'] for r in batch])
                coll.upsert(ids=[r['id'] for r in batch],embeddings=vectors,
                    metadatas=[{'hash':digest(r['text'])} for r in batch])
            old=self._name()
            with self.memory.lock, self.memory.db:
                self.memory.db.execute("INSERT INTO state(key,value) VALUES('semantic_index',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps({'name':name,'model':MODEL}),))
            # Удаление старого индекса после переключения: это только производные данные.
            if old and old!=name:
                try: self.memory.client.delete_collection(old)
                except Exception: pass
            coll=None
            return self.status()
        except SemanticError: raise
        except Exception as exc: raise SemanticError('Не удалось создать индекс. Старая память сохранена.') from exc
        finally:
            if coll is not None:
                try: self.memory.client.delete_collection(coll.name)
                except Exception: pass
            self.build_lock.release()

    def search(self, query: str, viewer_id: str | None=None, limit: int=5) -> dict[str,Any]:
        """Объединить ранги словесного и смыслового поиска, не выдавая чужие сообщения."""
        if not query.strip(): return {'matches':[],'mode':'words','warning':''}
        records=self._records()
        def eligible_record(r: dict[str,Any]) -> bool:
            if viewer_id is not None and r.get('viewer_id') and r['viewer_id']!=viewer_id: return False
            if r['kind']=='message':
                text=r['text'].strip().casefold()
                if '?' in text or text in {'привет','здравствуй','здравствуйте','хай','ку'}: return False
                if re.match(r'^(кто|что|как|где|почему|зачем|какой|какая|какое|какие|расскажи)\b',text): return False
            return True
        allowed={r['id']:r for r in records if eligible_record(r)}
        lexical=lexical_rank(query,list(allowed.values()))
        if not self.enabled:
            return {'matches':lexical[:limit], 'mode':'words',
                    'warning':'Ollama отключена; поиск работает только по словам.'}
        coll=self._collection()
        if coll is None:
            return {'matches':lexical[:limit], 'mode':'words', 'warning':'Смысловой индекс не создан: поиск только по словам.'}
        try:
            stored=coll.get(include=['metadatas'])
            eligible=[i for i,m in zip(stored['ids'],stored['metadatas']) if i in allowed and m and m.get('hash')==digest(allowed[i]['text'])]
            if not eligible:
                return {'matches':lexical[:limit], 'mode':'words', 'warning':'Актуальных смысловых записей нет: обновите индекс.'}
            vector=embed([QUERY_PREFIX+query])[0]
            found=coll.query(query_embeddings=[vector],ids=eligible,n_results=min(20,len(eligible)),include=['distances'])
            semantic=[]
            for rid, distance in zip(found['ids'][0],found['distances'][0]):
                if rid in allowed and float(distance)<=0.65: semantic.append((rid,float(distance)))
            scores: dict[str,float]={}
            details: dict[str,dict[str,Any]]={}
            for rank,r in enumerate(lexical,1):
                rid=r['id'];scores[rid]=scores.get(rid,0)+1/(60+rank);details[rid]={**r}
            for rank,(rid,distance) in enumerate(semantic,1):
                scores[rid]=scores.get(rid,0)+1/(60+rank);details.setdefault(rid,{**allowed[rid]});details[rid]['distance']=round(distance,3)
            # Если итоговый ранг совпал, сначала смысловое совпадение; это
            # лишь правило разрешения ничьей, не оценка достоверности.
            ranked=sorted(scores,key=lambda r:(-scores[r],0 if 'distance' in details[r] else 1,r))[:limit]
            result=[{**details[rid], 'поиск': ('слова + смысл' if 'distance' in details[rid] and any(x['id']==rid for x in lexical) else 'смысл' if 'distance' in details[rid] else 'слова')} for rid in ranked]
            seen=set(); unique=[]
            for item in result:
                normalized=' '.join(item['text'].casefold().split())
                if normalized not in seen: unique.append(item); seen.add(normalized)
            result=unique
            current={r['id']:digest(r['text']) for r in records}
            total=len(records);fresh=sum(bool(m) and current.get(i)==m.get('hash') for i,m in zip(stored['ids'],stored['metadatas']))
            warning='Не все записи проиндексированы: новые и изменённые пока ищутся только по словам.' if fresh<total else ''
            return {'matches':result,'mode':'hybrid','warning':warning}
        except SemanticError as exc:
            return {'matches':lexical[:limit], 'mode':'words', 'warning':str(exc)+' Поиск работает только по словам.'}
        except Exception:
            return {'matches':lexical[:limit], 'mode':'words', 'warning':'Смысловой поиск недоступен. Используется поиск по словам; попробуйте обновить индекс.'}
