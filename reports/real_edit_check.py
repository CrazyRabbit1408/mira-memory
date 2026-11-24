"""Check updated note against older assistant output using actual small model."""
from pathlib import Path
import json,tempfile,time
from fastapi.testclient import TestClient
import src.engines as eng
import src.app as appmod
from src.app import create_app
from src.config import ROOT
eng.MODELS=(*eng.MODELS,'qwen3:0.6b');appmod.MODELS=(*appmod.MODELS,'qwen3:0.6b')
result=[]
with tempfile.TemporaryDirectory() as d, TestClient(create_app(Path(d)/'data',ROOT/'config/lore/character',testing=True)) as c:
 c.post('/api/semantic-reindex');c.post('/api/engine-settings',json={'engine':'ollama','model':'qwen3:0.6b','thinking':False})
 for run in range(2):
  viewer=f'edit{run}'; key=f'коллекция{run}'
  note=c.post('/api/notes',json={'text':'Мира собирает деревянные кораблики.','fact_key':key}).json()['id']
  q='Что ты собираешь?'
  a=c.post('/api/chat',json={'viewer':viewer,'text':q}).json()
  c.put(f'/api/notes/{note}',json={'text':'Мира собирает стеклянные шарики, больше не собирает кораблики.','fact_key':key,'revision':1})
  b=c.post('/api/chat',json={'viewer':viewer,'text':q}).json()
  rec={'run':run+1,'before':a.get('response'),'after':b.get('response'),'after_context':[x['text'] for x in b.get('matches',[])],'new_fact_in_answer':'шарик' in b.get('response','').lower()}
  result.append(rec);print(json.dumps(rec,ensure_ascii=False),flush=True)
  c.delete(f'/api/notes/{note}')
 (ROOT/'reports/real-edit-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
