import json, pathlib, sys
from fastapi.testclient import TestClient
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from backend.main import app

client=TestClient(app)

def run(cid):
    s=json.load(open(pathlib.Path(__file__).parents[1]/"conversations"/f"{cid}.json"))
    r=client.post('/agent/run',json={"conversation_id":s["id"],"today":s["today"],"turns":s["turns"]})
    assert r.status_code==200, r.text
    return r.json()

def test_booking():
    x=run('cv_0001'); assert x['terminal_state']=='booked'; assert [c['name'] for c in x['tool_calls']]==['lookup_patient','search_slots','book_appointment']

def test_mid_sentence_correction():
    x=run('cv_0002'); assert x['terminal_state']=='booked'; assert x['tool_calls'][1]['arguments']['date']=='2026-10-07'

def test_urgent_stops_booking():
    x=run('cv_0011'); assert x['terminal_state']=='escalated'; assert x['escalation_reason']=='clinical_urgent'; assert [c['name'] for c in x['tool_calls']]==['escalate_to_human']

def test_ambiguous():
    x=run('cv_0007'); assert x['terminal_state']=='escalated'; assert x['escalation_reason']=='ambiguous_patient'

def test_injection_refused():
    x=run('cv_0014'); assert x['terminal_state']=='refused'; assert x['tool_calls']==[]
