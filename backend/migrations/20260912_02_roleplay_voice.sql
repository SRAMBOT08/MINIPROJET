-- Role Play realtime voice: a server-resolved turn-commit path for the single
-- candidate in a 1:1 voice channel. Unlike commit_roleplay_event (a candidate's own
-- authenticated HTTP request), a voice turn arrives from the Agora RTC channel via a
-- backend-verified transcript webhook with no per-request user JWT -- and, unlike
-- Group Discussion, there may be no `users` row for this candidate at all (many
-- candidates never create a portal login), so no actor identity can be resolved even
-- in principle. This function is scoped by session_id alone (a session has exactly
-- one candidate) and independently re-validates status/phase transitions the same
-- way commit_roleplay_event does, rather than trusting the caller's business logic.
BEGIN;

CREATE OR REPLACE FUNCTION public.append_roleplay_voice_turn(p_session_id text,p_event jsonb,p_state jsonb)
RETURNS jsonb LANGUAGE plpgsql SET search_path=public AS $$
DECLARE s public.roleplay_sessions%ROWTYPE; e public.roleplay_events%ROWTYPE; item jsonb;
BEGIN
 SELECT * INTO s FROM public.roleplay_sessions WHERE id=p_session_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'FEATURE_NOT_FOUND';END IF;
 SELECT * INTO e FROM public.roleplay_events WHERE session_id=s.id AND request_id=p_event->>'request_id';
 IF FOUND THEN
  IF e.request_hash IS DISTINCT FROM p_event->>'request_hash' THEN RAISE EXCEPTION 'REPLAY_MISMATCH';END IF;
  RETURN to_jsonb(e);
 END IF;
 IF s.status<>'active' THEN RAISE EXCEPTION 'FEATURE_INVALID';END IF;
 IF p_state->>'status' NOT IN ('active','completed') THEN RAISE EXCEPTION 'FEATURE_INVALID';END IF;
 IF (p_state->>'turn_count')::integer<>(s.state->>'turn_count')::integer+1 THEN RAISE EXCEPTION 'FEATURE_INVALID';END IF;
 FOR item IN SELECT value FROM jsonb_array_elements(p_event->'evidence') LOOP
  IF item->>'event_id' IS DISTINCT FROM p_event->>'id' OR item->>'session_id' IS DISTINCT FROM s.id
   OR item->>'candidate_id' IS DISTINCT FROM s.candidate_id::text OR item->>'source_type'<>'role_play'
   OR position(item->>'quote' IN p_event->>'source_text')=0 THEN RAISE EXCEPTION 'FEATURE_INVALID_EVIDENCE';END IF;
 END LOOP;
 INSERT INTO public.roleplay_events(id,session_id,candidate_id,revision,kind,request_id,request_hash,source_text,response,action,analysis,evidence,policy_context,projection_status)
  VALUES(p_event->>'id',s.id,s.candidate_id,s.revision+1,'turn',p_event->>'request_id',p_event->>'request_hash',p_event->>'source_text',p_event->>'response',
   p_event->'action',p_event->'analysis',p_event->'evidence',p_event->'policy_context',CASE WHEN jsonb_array_length(p_event->'evidence')>0 THEN 'pending' ELSE 'skipped' END) RETURNING * INTO e;
 UPDATE public.roleplay_sessions SET state=p_state,revision=revision+1,updated_at=now() WHERE id=s.id;
 RETURN to_jsonb(e);
END $$;

REVOKE ALL ON FUNCTION public.append_roleplay_voice_turn(text,jsonb,jsonb) FROM PUBLIC,anon,authenticated,authenticator;
GRANT EXECUTE ON FUNCTION public.append_roleplay_voice_turn(text,jsonb,jsonb) TO service_role;
NOTIFY pgrst,'reload schema';
COMMIT;
