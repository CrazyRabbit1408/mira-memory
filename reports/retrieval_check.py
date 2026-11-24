"""Check actual embedding model; no user data. Results are descriptive, not a guarantee."""
from pathlib import Path
import json,tempfile,time
from src.memory import MemoryManager
from src.semantic import SemanticIndex
from src.config import ROOT
cases=[('Как называется маяк?','Ласточка'),('Что пьёт смотрительница?','мят'),('Кто ремонтирует радио?','Артём'),('Как зовут кота?','Фонарик'),('В какой день не работает паром?','сред'),('Как зовут мать Миры?','матери'),('Где лежит синий компас?','компас'),('Что затапливает прилив?','аркой'),('Кто доставляет письма?','Лида')]
with tempfile.TemporaryDirectory() as d:
 m=MemoryManager(Path(d),ROOT/'config/lore/character');p=SemanticIndex(m);p.build(); rows=[]
 for rep in range(2):
  for question,expected in cases:
   r=p.search(question,viewer_id='fixture');found=any(expected.casefold() in x['text'].casefold() for x in r['matches']); rows.append({'run':rep+1,'query':question,'expected_text':expected,'found':found,'matches':[x['text'] for x in r['matches']]});print(rep+1,question,found,flush=True)
 (ROOT/'reports/real-retrieval-results.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf8');m.close()
