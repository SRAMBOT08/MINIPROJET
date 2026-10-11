from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Request
from app.core.deps import get_supabase
from app.services.workspace_access import current_actor, recruiter_actor
from app.role_play.models import PersonaWrite, ScenarioWrite, SessionCreate, TurnInput, ControlInput
from app.role_play.service import RolePlayService

router=APIRouter(prefix="/roleplay",tags=["role-play"])


def service(sb=Depends(get_supabase)): return RolePlayService(sb)


@router.get("/agents")
async def agents(actor=Depends(recruiter_actor)):
    """Voice agents available to embody the Role Play persona -- from the live
    AgentRegistry (filtered to the "role_play" channel), never hardcoded."""
    return [
        {"agent_id": p.agent_id, "name": p.display_name, "role": p.role, "description": p.description}
        for p in RolePlayService.compatible_agents()
    ]


@router.get("/personas")
async def personas(actor=Depends(recruiter_actor),svc=Depends(service)):
    return await svc.repo.definitions(actor,"persona")


@router.post("/personas",status_code=201)
async def persona(body:PersonaWrite,actor=Depends(recruiter_actor),svc=Depends(service)):
    return await svc.save_definition(actor,"persona",body)


@router.put("/personas/{key}")
async def update_persona(key:UUID,body:PersonaWrite,actor=Depends(recruiter_actor),svc=Depends(service)):
    return await svc.save_definition(actor,"persona",body,str(key))


@router.get("/scenarios")
async def scenarios(actor=Depends(recruiter_actor),svc=Depends(service)):
    return await svc.repo.definitions(actor,"scenario")


@router.post("/scenarios",status_code=201)
async def scenario(body:ScenarioWrite,actor=Depends(recruiter_actor),svc=Depends(service)):
    return await svc.save_definition(actor,"scenario",body)


@router.put("/scenarios/{key}")
async def update_scenario(key:UUID,body:ScenarioWrite,actor=Depends(recruiter_actor),svc=Depends(service)):
    return await svc.save_definition(actor,"scenario",body,str(key))


@router.get("/sessions")
async def sessions(actor=Depends(current_actor),svc=Depends(service)):
    return [svc.project(row,actor) for row in await svc.repo.sessions(actor)]


@router.post("/sessions",status_code=201)
async def create(body:SessionCreate,actor=Depends(recruiter_actor),svc=Depends(service)):
    return svc.project(await svc.create(actor,body),actor)


@router.get("/sessions/{key}")
async def get_session(key:UUID,actor=Depends(current_actor),svc=Depends(service)):
    return await svc.view(actor,str(key))


@router.post("/sessions/{key}/control")
async def control(key:UUID,body:ControlInput,actor=Depends(current_actor),svc=Depends(service)):
    return await svc.control(actor,str(key),body)


@router.post("/sessions/{key}/turns")
async def turn(key:UUID,body:TurnInput,actor=Depends(current_actor),svc=Depends(service)):
    return await svc.turn(actor,str(key),body)


@router.post("/sessions/{key}/rtc-credentials")
async def rtc_credentials(key:UUID,actor=Depends(current_actor),svc=Depends(service)):
    """Issue the assigned candidate's own scoped Agora RTC token to join by voice."""
    return await svc.rtc_credentials(actor,str(key))


@router.post("/sessions/{key}/agora-webhook")
async def agora_webhook(key:UUID,request:Request,svc=Depends(service)):
    """Server-to-server Agora notification receiver for the candidate's voice turns."""
    from app.role_play.webhook import parse_roleplay_voice_turns,verify_agora_signature
    raw=await request.body()
    if not verify_agora_signature(raw,request.headers.get("Agora-Signature-V2")):
        raise HTTPException(status_code=401,detail="Invalid Agora webhook signature")
    import json
    payload=json.loads(raw) if raw else {}
    results=[await svc.ingest_voice_turn(str(key),turn["text"],turn["turn_id"]) for turn in parse_roleplay_voice_turns(payload)]
    return {"status":"ok","processed":len(results),"results":results}


@router.post("/sessions/{key}/report")
async def report(key:UUID,actor=Depends(current_actor),svc=Depends(service)):
    return await svc.generate_report(actor,str(key))


@router.post("/sessions/{key}/evidence/sync")
async def sync_evidence(key:UUID,actor=Depends(current_actor),svc=Depends(service)):
    from app.role_play.evidence_delivery import RolePlayEvidenceDelivery
    session=await svc.require_session(actor,str(key))
    return await RolePlayEvidenceDelivery(svc.repo).deliver(session)
