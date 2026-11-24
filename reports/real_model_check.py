"""Small-model smoke check: never touches user data; records raw results."""
from pathlib import Path
import json,tempfile,time,sys
from fastapi.testclient import TestClient
import src.engines as eng
import src.app as appmod
from src.app import create_app
from src.config import ROOT
eng.MODELS=(*eng.MODELS,'qwen3:0.6b');appmod.MODELS=(*appmod.MODELS,'qwen3:0.6b')
results=[]
with tempfile.TemporaryDirectory() as d, TestClient(create_app(Path(d)/'data',ROOT/'config/lore/character',testing=True)) as c:
 t=time.monotonic(); idx=c.post('/api/semantic-reindex'); print('index',idx.status_code,idx.text[:200],round(time.monotonic()-t,1),flush=True)
 if idx.status_code!=200:sys.exit(1)
 c.post('/api/engine-settings',json={'engine':'ollama','model':'qwen3:0.6b','thinking':False})
 qs=[('Как называется маяк?', 'Ласточк'),('Что Мира пьёт?', 'ча'),('Кто чинит радиоприёмники?', 'Арт'),('Как зовут рыжего кота?', 'Фонарик'),('Как зовут маму Миры?', None)]
 for run in range(2):
  for i,(q,want) in enumerate(qs):
   t=time.monotonic();r=c.post('/api/chat',json={'viewer':f'bench-{run}-{i}','text':q}); data=r.json()
   answer=data.get('response',''); matches=data.get('matches',[])
   rec={'run':run+1,'question':q,'expected_fragment':want,'response':answer,'http':r.status_code,'seconds':round(time.monotonic()-t,1),'context':[m['text'] for m in matches], 'literal_expected_in_answer':want.casefold() in answer.casefold() if want else None}
   results.append(rec);print(json.dumps({k:v for k,v in rec.items() if k!='context'},ensure_ascii=False),flush=True)
 report={'model':'qwen3:0.6b on CPU; not the user 8B model','embedding':'qwen3-embedding:0.6b on CPU','cases':results}
 (ROOT/'reports/real-model-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
