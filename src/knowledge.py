"""Граф, источники и осторожное извлечение. Все предложения зрителей — не канон."""
from __future__ import annotations
import re, time, json
from pathlib import Path
from typing import Any

SCHEMA='''
CREATE TABLE IF NOT EXISTS memory_candidates(
 id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
 viewer_id TEXT NOT NULL, quote TEXT NOT NULL, relation TEXT NOT NULL, object TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
 created REAL NOT NULL, UNIQUE(message_id,relation,object));
CREATE TABLE IF NOT EXISTS knowledge_edges(
 id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL, relation TEXT NOT NULL, object TEXT NOT NULL,
 evidence TEXT NOT NULL, source TEXT NOT NULL, viewer_id TEXT NOT NULL DEFAULT '',
 candidate_id INTEGER REFERENCES memory_candidates(id) ON DELETE CASCADE,
 UNIQUE(subject,relation,object,source,viewer_id));
CREATE TABLE IF NOT EXISTS note_versions(
 id INTEGER PRIMARY KEY AUTOINCREMENT, note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL, body TEXT NOT NULL, viewer_id TEXT, fact_key TEXT, created REAL NOT NULL,
 UNIQUE(note_id,revision));
'''
# Only bounded, explicit self-reports. Everything is reviewable, never canonical.
PATTERNS=[
    ('любит',r'\bя\s+люблю\s+([^.!?\n]{2,100})'),
    ('нравится',r'\bмне\s+нравится\s+([^.!?\n]{2,100})'),
    ('предпочитает',r'\bя\s+предпочитаю\s+([^.!?\n]{2,100})'),
    ('занимается',r'\bя\s+(?:занимаюсь|увлекаюсь)\s+([^.!?\n]{2,100})'),
    ('собирает',r'\bя\s+собираю\s+([^.!?\n]{2,100})'),
    ('изучает',r'\bя\s+изучаю\s+([^.!?\n]{2,100})'),
    ('имеет',r'\bу\s+меня\s+есть\s+([^.!?\n]{2,100})'),
    ('работает',r'\bя\s+работаю\s+([^.!?\n]{2,100})'),
    ('живёт',r'\bя\s+живу\s+([^.!?\n]{2,100})'),
    ('имя',r'\bменя\s+зовут\s+([^.!?\n]{2,80})'),
]
SENSITIVE=re.compile(
    r'адрес|телефон|парол|токен|почт[аыуе]|болезн|диагноз|паспорт|банков|'
    r'карт[аыуе]|улиц|здоров|лечени|депресси|секрет|игнорируй|инструкци|'
    r'забудь|системн|https?://|www\.|[\w.+-]+@[\w.-]+\.[a-z]{2,}',
    re.I,
)
NUMBER_SEQUENCE=re.compile(r'(?<!\w)\d[\d\s().-]{5,}\d(?!\w)')

def contains_sensitive(text: str) -> bool:
    """Reject likely secrets/PII without rejecting ordinary ages and counts."""
    return bool(SENSITIVE.search(text) or NUMBER_SEQUENCE.search(text))

def proposals(body: str) -> list[dict[str,str]]:
    if '?' in body or contains_sensitive(body): return []
    result=[]
    for relation, pattern in PATTERNS:
        for hit in re.finditer(pattern,body,re.I):
            obj=hit.group(1).strip(' ,;:—-')
            if len(obj)<2 or obj.isdigit() or contains_sensitive(obj) or len(obj.split())>10: continue
            result.append({'relation':relation,'object':obj,'quote':hit.group(0).strip()})
    return result[:5]

def capture(db: Any, message_id: int, viewer: str, body: str) -> None:
    for p in proposals(body):
        db.execute('INSERT OR IGNORE INTO memory_candidates(message_id,viewer_id,quote,relation,object,created) VALUES(?,?,?,?,?,?)',
          (message_id,viewer,p['quote'],p['relation'],p['object'],time.time()))

def records(db: Any) -> list[dict[str,Any]]:
    rows=db.execute("SELECT * FROM memory_candidates WHERE status='approved'").fetchall()
    return [{'id':f'fact:{r["id"]}','kind':'viewer_report','text':f'Зритель {r["viewer_id"]} сообщил: «{r["quote"]}».',
             'source':f'Сообщение #{r["message_id"]}; подтверждено владельцем', 'viewer_id':r['viewer_id']} for r in rows]

def decide(db: Any, candidate_id: int, status: str) -> bool:
    if status not in ('approved','rejected','pending'): raise ValueError('Недопустимый статус')
    r=db.execute('SELECT * FROM memory_candidates WHERE id=?',(candidate_id,)).fetchone()
    if not r:return False
    db.execute('UPDATE memory_candidates SET status=? WHERE id=?',(status,candidate_id))
    db.execute('DELETE FROM knowledge_edges WHERE candidate_id=?',(candidate_id,))
    if status=='approved':
        db.execute('INSERT INTO knowledge_edges(subject,relation,object,evidence,source,viewer_id,candidate_id) VALUES(?,?,?,?,?,?,?)',
           (r['viewer_id'],r['relation'],r['object'],r['quote'],f'Сообщение #{r["message_id"]}',r['viewer_id'],r['id']))
    return True

def sync_graph(db: Any, lore: Path) -> int:
    """Import only graph edges whose evidence is still literally in allowed lore."""
    seed=lore.parent.parent/'graph.json'
    db.execute("DELETE FROM knowledge_edges WHERE source LIKE 'config/lore/character/%'")
    if not seed.is_file() or seed.is_symlink(): return 0
    data=json.loads(seed.read_text(encoding='utf-8'))
    count=0
    for e in data:
        name=e.get('file',''); evidence=e.get('evidence','')
        if Path(name).name!=name or not name.endswith('.md') or not evidence: continue
        f=lore/name
        if not f.is_file() or f.is_symlink() or evidence not in f.read_text(encoding='utf-8'): continue
        vals=[str(e.get(k,''))[:200].strip() for k in ('subject','relation','object')]
        if not all(vals): continue
        db.execute('INSERT OR IGNORE INTO knowledge_edges(subject,relation,object,evidence,source) VALUES(?,?,?,?,?)',(*vals,evidence,'config/lore/character/'+name));count+=1
    return count

def graph_matches(db: Any, query: str, viewer: str, limit: int=3) -> list[dict[str,Any]]:
    """Ограниченный двухшаговый обход от упомянутых сущностей с источниками."""
    q=query.casefold().replace('ё','е')
    query_tokens=re.findall(r'[а-яa-z0-9]+',q)
    rows=db.execute("SELECT * FROM knowledge_edges WHERE viewer_id='' OR viewer_id=? ORDER BY id",(viewer,)).fetchall()
    def matches(name: str) -> bool:
        name=name.casefold().replace('ё','е')
        if name in ('мира',viewer.casefold().replace('ё','е')): return False
        parts=[p for p in re.findall(r'[а-яa-z0-9]+',name) if len(p)>=3]
        for part in parts:
            # Short names require a close token match; longer entities allow
            # common Russian case endings without matching unrelated words.
            if len(part) <= 4:
                if any(token == part or (len(token) >= len(part) and token[:3] == part[:3]) for token in query_tokens):
                    return True
            else:
                stem=part[:4]
                if any(token == part or token.startswith(stem) for token in query_tokens):
                    return True
        return False
    seeds={x for r in rows for x in (r['subject'],r['object']) if matches(x)}
    direct=[r for r in rows if r['subject'] in seeds or r['object'] in seeds]
    # Second hop is only traversed through specific nodes, not generic persona.
    neighbours={x for r in direct for x in (r['subject'],r['object']) if x not in seeds and x.casefold()!='мира'}
    second=[r for r in rows if r not in direct and (r['subject'] in neighbours or r['object'] in neighbours)]
    out=[];seen=set()
    for r in direct+second:
        if r['evidence'] in seen: continue
        seen.add(r['evidence']);out.append({'id':f'graph:{r["id"]}','kind':'graph','text':r['evidence'],'source':r['source'], 'viewer_id':r['viewer_id'],'поиск':'граф'})
        if len(out)>=limit: break
    return out
