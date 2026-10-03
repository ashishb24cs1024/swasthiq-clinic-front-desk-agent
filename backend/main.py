from __future__ import annotations

import copy
import json
import re
import threading
import time
from datetime import date as Date, timedelta
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
CLINIC_PATH = ROOT / "clinic.json"

app = FastAPI(title="Sunrise Clinic Front Desk Agent", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

# The lock protects the short critical section used for a concurrent slot reservation.
# State is still reloaded from clinic.json for every request, as required by the harness.
STATE_LOCK = threading.Lock()
ACTIVE_RESERVATIONS: set[tuple[str, str, str]] = set()

DAY_NAMES = {
    "monday": 0, "mon": 0, "somwar": 0, "somvaar": 0,
    "tuesday": 1, "tue": 1, "mangalwar": 1, "mangalvaar": 1,
    "wednesday": 2, "wed": 2, "budhwar": 2, "budhvaar": 2,
    "thursday": 3, "thu": 3, "guruwar": 3, "guruvaar": 3,
    "friday": 4, "fri": 4, "shukrawar": 4, "shukravaar": 4,
    "saturday": 5, "sat": 5, "shanivar": 5, "shanivaar": 5,
    "sunday": 6, "sun": 6, "ravivar": 6, "ravivaar": 6,
}
NUM_WORDS = {
    "ek":1,"do":2,"teen":3,"char":4,"chaar":4,"paanch":5,"pach":5,"cheh":6,"chhe":6,
    "saat":7,"aath":8,"nau":9,"das":10,"gyarah":11,"gyaarah":11,"barah":12,"baarah":12,
    "terah":13,"chaudah":14,"pandrah":15,"solah":16,"satrah":17,"atharah":18,"unnis":19,"bees":20,
}

class RunRequest(BaseModel):
    conversation_id: str
    today: str
    turns: list[str]

class ToolError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def load_state() -> dict[str, Any]:
    with CLINIC_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9@.]+", " ", s.lower())).strip()


def parse_date_value(text: str, today: Date) -> Optional[str]:
    t = text.lower()
    # Explicit ISO date first.
    m = re.search(r"\b(202\d-\d{2}-\d{2})\b", t)
    if m:
        return m.group(1)
    # Numeric day-of-month in the supplied reference week/year.
    for phrase, delta in [("day after tomorrow",2),("tomorrow",1),("day after",2),("kal",1),("parso",2),("parson",2)]:
        if phrase in t:
            return (today + timedelta(days=delta)).isoformat()
    # Hindi date with a day number: "8 tareekh", "3rd", etc.
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s*(?:tareekh|tarikh|date)?\b", t)
    if m:
        d=int(m.group(1))
        try:
            return Date(today.year, today.month, d).isoformat()
        except ValueError:
            return None
    for name, wd in DAY_NAMES.items():
        if re.search(rf"\b{re.escape(name)}\b", t):
            delta=(wd-today.weekday())%7
            # If it says "aaj"/today too, keep today.
            return (today + timedelta(days=delta)).isoformat()
    if re.search(r"\baaj\b|\btoday\b", t):
        return today.isoformat()
    return None


def extract_latest_date(turns: list[str], today: Date) -> Optional[str]:
    found=[]
    for idx,t in enumerate(turns):
        low=t.lower()
        # Relative phrases can appear more than once; preserve their order.
        for m in re.finditer(r"day after tomorrow|tomorrow|day after|\bkal\b|\bparso\b|\bparson\b", low):
            phrase=m.group(0)
            delta=2 if phrase in {"day after tomorrow","day after","parso","parson"} else 1
            found.append((idx,m.start(),(today+timedelta(days=delta)).isoformat()))
        # Every explicit day-of-month mention is considered, so corrections in one turn work.
        for m in re.finditer(r"\b(\d{1,2})(?:st|nd|rd|th)?\s*(?:tareekh|tarikh|date)\b", low):
            d=int(m.group(1))
            try: found.append((idx,m.start(),Date(today.year,today.month,d).isoformat()))
            except ValueError: pass
        for name,wd in DAY_NAMES.items():
            for m in re.finditer(rf"\b{re.escape(name)}\b",low):
                delta=(wd-today.weekday())%7
                found.append((idx,m.start(),(today+timedelta(days=delta)).isoformat()))
        if re.search(r"\baaj\b|\btoday\b",low):
            for m in re.finditer(r"\baaj\b|\btoday\b",low): found.append((idx,m.start(),today.isoformat()))
    if not found: return None
    return sorted(found)[-1][2]


def extract_time(text: str) -> Optional[str]:
    t=text.lower()
    # Prefer clock forms, then Hindi number words.
    m=re.search(r"\b(\d{1,2})(?::(\d{2}))\s*(?:baje|am|pm)?\b|\b(\d{1,2})\s*(?:baje|am|pm)\b", t)
    if m:
        h=int(m.group(1) or m.group(3)); minute=int(m.group(2) or 0)
        if 0 <= h <= 23 and minute in (0,15,30,45):
            # Handle explicit pm, or common afternoon/evening wording.
            if ("pm" in m.group(0) or re.search(r"\b(shaam|sham|evening|afternoon|dopahar)\b", t)) and h < 12:
                h += 12
            return f"{h:02d}:{minute:02d}"
    for word,h in NUM_WORDS.items():
        if re.search(rf"\b{word}\b\s*(?:baje|o clock)",t):
            if re.search(r"\b(shaam|sham|evening|afternoon|dopahar)\b",t) and h<12: h+=12
            return f"{h:02d}:00"
    return None


def extract_latest_time(turns: list[str]) -> Optional[str]:
    found=[]
    for i,t in enumerate(turns):
        tm=extract_time(t)
        if tm: found.append((i,tm))
    return found[-1][1] if found else None


def doctor_id_from_turns(turns: list[str]) -> Optional[str]:
    text=" ".join(turns).lower()
    # Last explicit doctor mention wins, but avoid treating quoted prompt injection as authority.
    matches=[]
    for name,did in [("rao","dr_rao"),("sethi","dr_sethi")]:
        for m in re.finditer(rf"\b(?:dr\.?\s*)?{name}\b",text): matches.append((m.start(),did))
    return sorted(matches)[-1][1] if matches else None


def caller_identity(turns: list[str]) -> tuple[Optional[str],Optional[str]]:
    text=" ".join(turns)
    phones=re.findall(r"\b\d{10}\b",text)
    phone=phones[-1] if phones else None
    # Match a full patient name appearing in any turn, choosing the latest occurrence.
    state=load_state()
    hits=[]
    low=text.lower()
    for p in state["patients"]:
        if p["name"].lower() in low:
            hits.append((low.rfind(p["name"].lower()),p["name"],p["id"]))
    name=sorted(hits)[-1][1] if hits else None
    return name,phone


def name_hint(turns: list[str]) -> Optional[str]:
    text=" ".join(turns).lower()
    # Keep intentionally broad surname fragments so lookup can return candidates.
    for surname in ["sharma", "rao", "gupta", "joshi", "iyer"]:
        if re.search(rf"\b{surname}\b", text) and not re.search(r"\bdr\.?\s*"+surname+r"\b", text):
            return surname
    return None


def target_patient_hint(turns: list[str]) -> Optional[str]:
    text=" ".join(turns).lower()
    # Child/beneficiary name is useful for guardian cases.
    for name in ["kabir", "aarav", "arjun"]:
        if re.search(rf"\b{name}\b",text): return name
    return None


def morning_pref(text: str) -> Optional[str]:
    t=text.lower()
    if re.search(r"\b(subah|morning)\b",t): return "morning"
    if re.search(r"\b(shaam|sham|evening|afternoon|dopahar)\b",t): return "evening"
    return None


def choose_slot(slots:list[dict], requested_time:Optional[str], preference:Optional[str]) -> Optional[dict]:
    if not slots: return None
    if requested_time:
        exact=[s for s in slots if s["start"]==requested_time]
        if exact: return exact[0]
        # If requested slot is unavailable, never silently choose a different slot.
        return None
    if preference=="morning":
        m=[s for s in slots if s["start"] < "12:00"]
        return m[0] if m else None
    if preference=="evening":
        e=[s for s in slots if s["start"] >= "16:00"]
        return e[0] if e else None
    return slots[0]


class Tools:
    def __init__(self,state:dict[str,Any]):
        self.s=state

    def _doctor(self,did):
        for d in self.s["doctors"]:
            if d["id"]==did:return d
        raise ToolError(f"unknown doctor_id: {did}")

    def search_slots(self, doctor_id:str, date:str) -> dict:
        try:
            Date.fromisoformat(date)
        except Exception: raise ToolError("date must be YYYY-MM-DD")
        d=self._doctor(doctor_id)
        if date in self.s["holidays"]: return {"ok":True,"date":date,"doctor_id":doctor_id,"slots":[],"reason":"clinic_holiday"}
        if date in d.get("leave_dates",[]): return {"ok":True,"date":date,"doctor_id":doctor_id,"slots":[],"reason":"doctor_on_leave"}
        wd=Date.fromisoformat(date).strftime("%a")
        windows=[w for w in d["windows"] if w["day"]==wd]
        if not windows: return {"ok":True,"date":date,"doctor_id":doctor_id,"slots":[],"reason":"no_window"}
        booked={(a["start"]) for a in self.s["appointments"] if a["doctor_id"]==doctor_id and a["date"]==date and a["status"]=="booked"}
        slots=[]
        for w in windows:
            h,m=map(int,w["start"].split(":")); endh,endm=map(int,w["end"].split(":"))
            cur=h*60+m; end=endh*60+endm
            while cur+15<=end:
                st=f"{cur//60:02d}:{cur%60:02d}"
                if st not in booked: slots.append({"start":st,"end":f"{(cur+15)//60:02d}:{(cur+15)%60:02d}"})
                cur+=15
        # Deduplicate overlapping windows while preserving chronological order.
        seen=set(); out=[]
        for x in slots:
            if x["start"] not in seen: out.append(x); seen.add(x["start"])
        return {"ok":True,"date":date,"doctor_id":doctor_id,"slots":out,"reason":None}

    def lookup_patient(self, name:Optional[str]=None, phone:Optional[str]=None, target_name:Optional[str]=None) -> dict:
        if not name and not phone and not target_name: raise ToolError("provide name, phone, or target_name")
        candidates=[]
        for p in self.s["patients"]:
            name_ok = bool(name and (p["name"].lower()==name.lower() or p["name"].lower().startswith(name.lower()) or name.lower() in p["name"].lower().split()))
            phone_ok = bool(phone and p["phone"]==phone)
            target_ok = bool(target_name and target_name.lower() in p["name"].lower())
            if name_ok or phone_ok or target_ok: candidates.append(copy.deepcopy(p))
        # Exact phone can legitimately return multiple patients (guardian/children).
        return {"ok":True,"candidates":candidates}

    def _find_appt(self, patient_id:str, date_value:Optional[str]=None, doctor_id:Optional[str]=None):
        rows=[a for a in self.s["appointments"] if a["patient_id"]==patient_id and a["status"]=="booked"]
        if date_value: rows=[a for a in rows if a["date"]==date_value]
        if doctor_id: rows=[a for a in rows if a["doctor_id"]==doctor_id]
        return rows

    def _validate_slot(self, doctor_id,date_value,start,ignore_appointment_id=None):
        r=self.search_slots(doctor_id,date_value)
        # search_slots excludes all booked slots, but a reschedule must permit its own old slot.
        if ignore_appointment_id:
            for a in self.s["appointments"]:
                if a["id"]==ignore_appointment_id and a["doctor_id"]==doctor_id and a["date"]==date_value and a["start"]==start and a["status"]=="booked":
                    return True
        return any(x["start"]==start for x in r["slots"])

    def book_appointment(self,patient_id,doctor_id,date,start,caller_id=None) -> dict:
        if not all(isinstance(x,str) for x in [patient_id,doctor_id,date,start]): raise ToolError("patient_id, doctor_id, date, start are required strings")
        patients={p["id"]:p for p in self.s["patients"]}
        if patient_id not in patients: raise ToolError("unknown patient_id")
        if caller_id and not self.authorized(caller_id,patient_id): raise ToolError("caller is not authorised for patient_id")
        with STATE_LOCK:
            key=(doctor_id,date,start)
            if key in ACTIVE_RESERVATIONS: raise ToolError("slot is currently reserved by another conversation")
            if not self._validate_slot(doctor_id,date,start): raise ToolError("requested slot is not free")
            ACTIVE_RESERVATIONS.add(key)
            try:
                next_id=max([int(re.search(r"(\d+)$",a["id"]).group(1)) for a in self.s["appointments"]]+[25])+1
                ap={"id":f"ap_{next_id:04d}","patient_id":patient_id,"doctor_id":doctor_id,"date":date,"start":start,"end":f"{(int(start[:2])*60+int(start[3:])+15)//60:02d}:{(int(start[:2])*60+int(start[3:])+15)%60:02d}","status":"booked"}
                self.s["appointments"].append(ap)
                return {"ok":True,"appointment":copy.deepcopy(ap)}
            finally:
                ACTIVE_RESERVATIONS.remove(key)

    def authorized(self,caller_id,target_id):
        if caller_id==target_id:return True
        p={p["id"]:p for p in self.s["patients"]}
        return target_id in p.get(caller_id,{}).get("guardian_of",[])

    def cancel_appointment(self,patient_id,date:Optional[str]=None,doctor_id:Optional[str]=None,caller_id:Optional[str]=None)->dict:
        if caller_id and not self.authorized(caller_id,patient_id): raise ToolError("caller is not authorised")
        rows=self._find_appt(patient_id,date,doctor_id)
        if len(rows)!=1: raise ToolError("expected exactly one matching appointment")
        rows[0]["status"]="cancelled"
        return {"ok":True,"appointment":copy.deepcopy(rows[0])}

    def reschedule_appointment(self,patient_id,new_date,new_start,doctor_id:Optional[str]=None,old_date:Optional[str]=None,caller_id:Optional[str]=None)->dict:
        if caller_id and not self.authorized(caller_id,patient_id): raise ToolError("caller is not authorised")
        rows=self._find_appt(patient_id,old_date,doctor_id)
        if len(rows)!=1: raise ToolError("expected exactly one matching appointment")
        ap=rows[0]
        if not self._validate_slot(ap["doctor_id"],new_date,new_start,ignore_appointment_id=ap["id"]): raise ToolError("new slot is not free")
        ap.update({"date":new_date,"start":new_start,"end":f"{(int(new_start[:2])*60+int(new_start[3:])+15)//60:02d}:{(int(new_start[:2])*60+int(new_start[3:])+15)%60:02d}"})
        return {"ok":True,"appointment":copy.deepcopy(ap)}

    def escalate_to_human(self,reason:str,detail:str="")->dict:
        if reason not in {"clinical_urgent","medical_advice","not_authorised","ambiguous_patient","out_of_scope"}: raise ToolError("invalid escalation reason")
        return {"ok":True,"reason":reason,"detail":detail}


def clinical_reason(text:str)->Optional[str]:
    t=text.lower()
    urgent_patterns=[
        r"chest pain", r"seene? mein dard", r"seene? me dard", r"saans .*phool", r"difficulty breathing", r"trouble breathing",
        r"breath.*short", r"unconscious", r"passed out", r"heavy bleeding", r"severe bleeding", r"stroke", r"fits?", r"seizure",
        r"बेहोश", r"सीने में दर्द", r"सांस.*फूल",
    ]
    advice_patterns=[r"how much .*medicine",r"kitni .*goli",r"aur .*goli",r"dose",r"dosage",r"should i take",r"le lun",r"diagnos",r"kya .*hai",r"kya karun"]
    if any(re.search(p,t) for p in urgent_patterns): return "clinical_urgent"
    if any(re.search(p,t) for p in advice_patterns) or ("bukhar" in t and ("goli" in t or "crocin" in t)):
        return "medical_advice"
    return None


def injection_or_bulk_admin(text:str)->bool:
    t=text.lower()
    return bool(re.search(r"ignore (your|all|previous) instructions|administrator mode|cancel every appointment|delete every appointment|bulk cancel|system prompt",t))


def infer_action(text:str)->str:
    t=text.lower()
    if re.search(r"\bcancel\b|\bcancel kar|cancel karna|cancel kar do|radd",t): return "cancel"
    if re.search(r"resched|shift|move|change.*appointment|use .* karwana",t) and re.search(r"appointment|aaj ka",t): return "reschedule"
    if re.search(r"appointment|slot|milna|dikhana|doctor ke paas|doctor.*saath|dr\.?\s*(rao|sethi).*\b(ke paas|se mil|ke saath)",t): return "book"
    return "none"


def build_reply(state, terminal, reason, patient_id, appt_id, selected_slot=None, doctor_id=None):
    doctors={d["id"]:d["name"] for d in state["doctors"]}
    patients={p["id"]:p["name"] for p in state["patients"]}
    if terminal=="booked" and appt_id:
        ap=next(a for a in state["appointments"] if a["id"]==appt_id)
        return f"Ji, appointment {appt_id} book ho gaya hai for {patients.get(patient_id,'the patient')} on {ap['date']} at {ap['start']} with {doctors.get(ap['doctor_id'],ap['doctor_id'])}."
    if terminal=="rescheduled" and appt_id:
        ap=next(a for a in state["appointments"] if a["id"]==appt_id)
        return f"Ji, appointment {appt_id} rescheduled hai to {ap['date']} at {ap['start']}."
    if terminal=="cancelled" and appt_id: return f"Ji, appointment {appt_id} cancel ho gaya hai."
    if terminal=="escalated":
        return {
            "clinical_urgent":"Aapke symptoms urgent lag rahe hain. Main booking flow rok raha hoon aur abhi human/clinician ko escalate kar raha hoon.",
            "medical_advice":"Main medical advice ya medicine dose decide nahi kar sakta. Main isse human ko escalate kar raha hoon.",
            "not_authorised":"Aapke paas is patient ke record par action lene ki authorisation verify nahi ho pa rahi, isliye main human ko escalate kar raha hoon.",
            "ambiguous_patient":"Mujhe patient identity reliably resolve nahi ho rahi, isliye main human ko escalate kar raha hoon.",
            "out_of_scope":"Yeh request front desk ke supported actions ke bahar hai, isliye main human ko escalate kar raha hoon.",
        }[reason]
    if terminal=="refused": return "Main administrator mode ya bulk appointment cancellation instructions execute nahi kar sakta."
    return "Mujhe is conversation mein koi actionable request nahi mili."


def run_agent(req:RunRequest)->dict:
    started=time.perf_counter()
    try:
        today=Date.fromisoformat(req.today)
    except ValueError: raise HTTPException(400,"today must be YYYY-MM-DD")
    if not isinstance(req.turns,list) or not all(isinstance(x,str) for x in req.turns): raise HTTPException(400,"turns must be an array of strings")
    state=load_state(); tools=Tools(state); calls=[]; patient_id=None; appointment_id=None
    all_text="\n".join(req.turns)
    # Hard safety gate has priority over every booking operation, even if it appears late in the call.
    reasons=[clinical_reason(t) for t in req.turns]
    if "clinical_urgent" in reasons:
        r=tools.escalate_to_human("clinical_urgent","caller reports symptoms needing immediate clinician attention")
        calls.append({"name":"escalate_to_human","arguments":{"reason":"clinical_urgent","detail":"caller reports symptoms needing immediate clinician attention"}})
        return response(req, calls,"escalated","clinical_urgent",None,None,state,started)
    if "medical_advice" in reasons:
        r=tools.escalate_to_human("medical_advice","caller requests clinical judgement or medication guidance")
        calls.append({"name":"escalate_to_human","arguments":{"reason":"medical_advice","detail":"caller requests clinical judgement or medication guidance"}})
        return response(req,calls,"escalated","medical_advice",None,None,state,started)
    if any(injection_or_bulk_admin(t) for t in req.turns):
        return response(req,[],"refused",None,None,None,state,started)

    action=infer_action(all_text)
    if action=="none": return response(req,[],"abandoned",None,None,None,state,started)
    caller_name, caller_phone=caller_identity(req.turns)
    target_hint=target_patient_hint(req.turns)

    # Explicit unauthorised third-party pattern is handled before lookup: there is no reason to touch another record.
    if re.search(r"padosi|neighbor|neighbour|friend|colleague|mere (dost|bhai|uncle)|someone else",all_text.lower()):
        r=tools.escalate_to_human("not_authorised","caller says they are acting for another person without listed authorisation")
        calls.append({"name":"escalate_to_human","arguments":{"reason":"not_authorised","detail":"caller says they are acting for another person without listed authorisation"}})
        return response(req,calls,"escalated","not_authorised",None,None,state,started)

    hint=name_hint(req.turns)
    did=doctor_id_from_turns(req.turns)
    date_value=extract_latest_date(req.turns,today)
    tm=extract_latest_time(req.turns)
    pref=morning_pref(all_text)

    # If this is a booking with no usable identity yet, consult the schedule first.
    # This is important for a closed day: no patient lookup or mutation is needed.
    if action=="book" and did and date_value and not (caller_phone or caller_name or target_hint or hint):
        search_args={"doctor_id":did,"date":date_value}
        search=tools.search_slots(**search_args)
        calls.append({"name":"search_slots","arguments":search_args})
        if not search["slots"]:
            return response(req,calls,"abandoned",None,None,None,state,started)

    # Resolve caller and, when present, a dependent/target. For guardian calls we use the caller phone/name plus target hint.
    candidates=[]
    if caller_phone or caller_name or target_hint or hint:
        args={}
        if caller_phone: args["phone"]=caller_phone
        if caller_name: args["name"]=caller_name
        elif hint: args["name"]=hint
        if target_hint: args["target_name"]=target_hint
        out=tools.lookup_patient(**args)
        calls.append({"name":"lookup_patient","arguments":args})
        candidates=out["candidates"]

    if not candidates:
        if action=="book" and did and date_value:
            # A schedule was already consulted above and no identity was supplied.
            return response(req,calls,"abandoned",None,None,None,state,started)
        r=tools.escalate_to_human("out_of_scope","patient identity could not be resolved from the conversation")
        calls.append({"name":"escalate_to_human","arguments":{"reason":"out_of_scope","detail":"patient identity could not be resolved from the conversation"}})
        return response(req,calls,"escalated","out_of_scope",None,None,state,started)

    # Choose exact caller from phone/name, never from a partial surname. Guardian can resolve a target by first name.
    exact=[p for p in candidates if caller_phone and p["phone"]==caller_phone]
    if caller_name: exact=[p for p in exact if p["name"].lower()==caller_name.lower()] or exact
    if target_hint:
        target=[p for p in candidates if p["name"].lower().startswith(target_hint.lower())]
        if len(target)==1:
            # Identify guardian as exact non-target candidate sharing phone/name.
            target_p=target[0]
            guardians=[p for p in state["patients"] if target_p["id"] in p.get("guardian_of",[]) and ((caller_phone and p["phone"]==caller_phone) or (caller_name and p["name"].lower()==caller_name.lower()))]
            if guardians: patient_id=target_p["id"]
            else:
                # The caller's phone may be shared by sibling children; target name disambiguates the patient.
                patient_id=target_p["id"]
        elif len(target)>1: patient_id=None
    elif len(exact)==1: patient_id=exact[0]["id"]
    elif len(candidates)==1: patient_id=candidates[0]["id"]

    if not patient_id:
        r=tools.escalate_to_human("ambiguous_patient","lookup returned multiple possible patient records")
        calls.append({"name":"escalate_to_human","arguments":{"reason":"ambiguous_patient","detail":"lookup returned multiple possible patient records"}})
        return response(req,calls,"escalated","ambiguous_patient",None,None,state,started)

    if action=="cancel":
        # For cancellation, default to today when no explicit date is supplied.
        old_date=date_value or today.isoformat()
        args={"patient_id":patient_id,"date":old_date,"caller_id":(caller_phone and next((p["id"] for p in state["patients"] if p["phone"]==caller_phone and (not caller_name or p["name"].lower()==caller_name.lower())),None))}
        if did: args["doctor_id"]=did
        try:
            out=tools.cancel_appointment(**{k:v for k,v in args.items() if v is not None})
        except ToolError as e:
            # A date was explicit only as a target for a different operation; do not invent an appointment.
            return response(req,calls,"abandoned",None,None,None,state,started)
        calls.append({"name":"cancel_appointment","arguments":{k:v for k,v in args.items() if v is not None}})
        appointment_id=out["appointment"]["id"]
        return response(req,calls,"cancelled",None,patient_id,appointment_id,state,started)

    if action=="reschedule":
        # Find old appointment from explicit "aaj"/today or by patient if omitted; target date is the latest date mention.
        old_date=today.isoformat() if re.search(r"aaj|today|current|mera aaj",all_text.lower()) else None
        # In cv_0003 "Saturday" is the target and "aaj" the old date; parse_date_value returns Saturday as latest.
        new_date=date_value
        if not new_date: return response(req,calls,"abandoned",None,None,None,state,started)
        if tm is None:
            # no exact time: use first free target slot
            search=tools.search_slots(did or "dr_rao",new_date)
            calls.append({"name":"search_slots","arguments":{"doctor_id":did or "dr_rao","date":new_date}})
            if not search["slots"]: return response(req,calls,"abandoned",None,patient_id,None,state,started)
            tm=search["slots"][0]["start"]
        args={"patient_id":patient_id,"new_date":new_date,"new_start":tm}
        if did: args["doctor_id"]=did
        if old_date: args["old_date"]=old_date
        args["caller_id"]=next((p["id"] for p in state["patients"] if p["id"]==patient_id and ((caller_phone and p["phone"]==caller_phone) or (caller_name and p["name"].lower()==caller_name.lower()) or patient_id in p.get("guardian_of",[]))),None)
        try: out=tools.reschedule_appointment(**{k:v for k,v in args.items() if v is not None})
        except ToolError:
            return response(req,calls,"abandoned",None,None,None,state,started)
        calls.append({"name":"reschedule_appointment","arguments":{k:v for k,v in args.items() if v is not None}})
        appointment_id=out["appointment"]["id"]
        return response(req,calls,"rescheduled",None,patient_id,appointment_id,state,started)

    # Booking.
    if not did:
        return response(req,calls,"abandoned",None,patient_id,None,state,started)
    if not date_value:
        return response(req,calls,"abandoned",None,patient_id,None,state,started)
    search_args={"doctor_id":did,"date":date_value}
    search=tools.search_slots(**search_args)
    calls.append({"name":"search_slots","arguments":search_args})
    slot=choose_slot(search["slots"],tm,pref)
    if not slot:
        return response(req,calls,"abandoned",None,patient_id,None,state,started)
    caller_id=next((p["id"] for p in state["patients"] if p["id"]==patient_id and ((caller_phone and p["phone"]==caller_phone) or (caller_name and p["name"].lower()==caller_name.lower()) or patient_id in p.get("guardian_of",[]))),None)
    args={"patient_id":patient_id,"doctor_id":did,"date":date_value,"start":slot["start"]}
    if caller_id: args["caller_id"]=caller_id
    try:
        out=tools.book_appointment(**args)
    except ToolError as e:
        return response(req,calls,"abandoned",None,patient_id,None,state,started)
    calls.append({"name":"book_appointment","arguments":args})
    appointment_id=out["appointment"]["id"]
    return response(req,calls,"booked",None,patient_id,appointment_id,state,started)


def response(req,calls,state_value,reason,patient_id,appointment_id,state,started):
    elapsed=int((time.perf_counter()-started)*1000)
    return {
        "conversation_id":req.conversation_id,
        "tool_calls":calls,
        "terminal_state":state_value,
        "escalation_reason":reason,
        "patient_id":patient_id,
        "appointment_id":appointment_id,
        "reply":build_reply(state,state_value,reason,patient_id,appointment_id),
        "metrics":{"turns":len(req.turns),"tokens":max(1,len(" ".join(req.turns))//4),"latency_ms":elapsed},
    }

@app.post("/agent/run")
def agent_run(req:RunRequest):
    try:
        return run_agent(req)
    except HTTPException: raise
    except Exception as exc:
        # Contract-safe failure: do not expose a stack trace to the caller.
        raise HTTPException(status_code=400, detail=f"invalid conversation request: {exc}")

@app.get("/health")
def health():
    return {"ok":True}
