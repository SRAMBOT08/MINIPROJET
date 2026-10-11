-- Group Discussion realtime voice: a server-resolved, speaker-attributed contribution
-- path. Unlike append_gd_message (a candidate's own authenticated HTTP request,
-- identified by actor email), a voice turn arrives from the Agora RTC channel via a
-- backend-verified transcript webhook with no per-request user JWT at all. The caller
-- (app.group_discussion.service) has already resolved the Agora RTC UID to a
-- participant_id via app.group_discussion.identity before calling this function, so
-- this function trusts p_participant_id the same way append_gd_message trusts the
-- actor-derived participant row -- but it independently re-validates that the
-- participant belongs to this session and is currently joined, exactly like the
-- authenticated path does, rather than trusting the caller's resolution blindly.
BEGIN;

CREATE OR REPLACE FUNCTION public.append_gd_voice_message(p_session_id text,p_participant_id text,p_event jsonb)
RETURNS jsonb LANGUAGE plpgsql SET search_path=public AS $$
DECLARE s public.gd_sessions%ROWTYPE;p public.gd_participants%ROWTYPE;e public.gd_events%ROWTYPE;
BEGIN
 SELECT * INTO s FROM public.gd_sessions WHERE id=p_session_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'FEATURE_NOT_FOUND';END IF;
 SELECT * INTO p FROM public.gd_participants WHERE id=p_participant_id AND session_id=s.id;
 IF NOT FOUND OR p.status<>'joined' OR p.accepted_at IS NULL THEN RAISE EXCEPTION 'FEATURE_FORBIDDEN';END IF;
 SELECT * INTO e FROM public.gd_events WHERE session_id=s.id AND request_id=p_event->>'request_id';
 IF FOUND THEN
  IF e.participant_id<>p.id OR e.request_hash IS DISTINCT FROM p_event->>'request_hash' THEN RAISE EXCEPTION 'REPLAY_MISMATCH';END IF;
  RETURN to_jsonb(e);
 END IF;
 IF s.status<>'active' OR now()>=s.started_at+make_interval(secs=>(s.configuration->>'duration_seconds')::integer) OR s.revision>=990 THEN RAISE EXCEPTION 'FEATURE_INVALID_DISCUSSION_ENDED';END IF;
 -- A live speaker naturally produces more finalized ASR turns per second than the
 -- typed-message rate limit assumes; voice gets a wider (but still bounded) window.
 IF (SELECT count(*) FROM public.gd_events WHERE participant_id=p.id AND kind='message' AND created_at>now()-interval '10 seconds')>=20 THEN RAISE EXCEPTION 'FEATURE_INVALID_MESSAGE_RATE';END IF;
 IF length(btrim(p_event->>'source_text')) NOT BETWEEN 1 AND 4000 THEN RAISE EXCEPTION 'FEATURE_INVALID';END IF;
 INSERT INTO public.gd_events(id,session_id,participant_id,actor_id,candidate_id,sequence,kind,request_id,request_hash,source_text,analysis)
  VALUES(p_event->>'id',s.id,p.id,NULL,p.candidate_id,s.revision+1,'message',p_event->>'request_id',p_event->>'request_hash',p_event->>'source_text','{"status":"pending"}') RETURNING * INTO e;
 UPDATE public.gd_sessions SET revision=revision+1,updated_at=now() WHERE id=s.id;
 RETURN to_jsonb(e);
END $$;

CREATE OR REPLACE FUNCTION public.set_gd_moderator_agent(p_session_id text,p_agora_agent_id text)
RETURNS jsonb LANGUAGE plpgsql SET search_path=public AS $$
DECLARE s public.gd_sessions%ROWTYPE;
BEGIN
 UPDATE public.gd_sessions SET moderator_state=moderator_state||jsonb_build_object('agora_agent_id',p_agora_agent_id),updated_at=now()
  WHERE id=p_session_id RETURNING * INTO s;
 IF NOT FOUND THEN RAISE EXCEPTION 'FEATURE_NOT_FOUND';END IF;
 RETURN to_jsonb(s);
END $$;

REVOKE ALL ON FUNCTION public.append_gd_voice_message(text,text,jsonb),public.set_gd_moderator_agent(text,text) FROM PUBLIC,anon,authenticated,authenticator;
GRANT EXECUTE ON FUNCTION public.append_gd_voice_message(text,text,jsonb),public.set_gd_moderator_agent(text,text) TO service_role;
NOTIFY pgrst,'reload schema';
COMMIT;
