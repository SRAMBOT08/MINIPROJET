"use client";

/**
 * Shared multi-participant Agora RTC voice hook for Group Discussion and Role Play.
 *
 * Unlike the Standard Interview voice hook (`use-voice-assistant.ts`, one candidate +
 * one agent, both endpoints owned by the same flow), this hook only ever manages the
 * *local* participant's own microphone and a set of remote audio tracks it did not
 * create -- other participants (and the AI moderator/persona) join and leave the same
 * Agora channel independently server-side. Speaker identity for transcripts/evidence
 * is resolved authoritatively on the backend from Agora's own webhook (see
 * app.group_discussion.identity / app.group_discussion.webhook); this hook's
 * `speakingUids` is a local UX cue only (Agora's volume indicator), never a source of
 * truth for who contributed what.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { IAgoraRTCClient, IAgoraRTCRemoteUser, IMicrophoneAudioTrack } from "agora-rtc-sdk-ng";
import { createAudioLifecycle, createIdempotentAudioPlayer } from "@/lib/agora-audio-lifecycle";

export interface RtcCredentials {
  agora_app_id: string;
  agora_token: string;
  agora_uid: number;
  channel_name: string;
}

export type RtcVoiceStatus = "idle" | "connecting" | "connected" | "reconnecting" | "ended" | "failed";

export interface RemotePresence {
  uid: number;
  hasAudio: boolean;
  speaking: boolean;
}

export function useMultiPartyVoice(fetchCredentials: () => Promise<RtcCredentials>) {
  const [status, setStatus] = useState<RtcVoiceStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const [muted, setMuted] = useState(false);
  const [remoteUsers, setRemoteUsers] = useState<Record<number, RemotePresence>>({});
  const scope = useRef(createAudioLifecycle());
  const client = useRef<IAgoraRTCClient | null>(null);
  const micTrack = useRef<IMicrophoneAudioTrack | null>(null);
  const play = useRef(createIdempotentAudioPlayer());
  const joining = useRef(false);

  const patchRemote = useCallback((uid: number, changes: Partial<RemotePresence>) => {
    setRemoteUsers((current) => {
      const existing: RemotePresence = current[uid] || { uid, hasAudio: false, speaking: false };
      const next: RemotePresence = Object.assign({}, existing, changes);
      return { ...current, [uid]: next };
    });
  }, []);

  const leave = useCallback(async () => {
    scope.current.dispose();
    scope.current = createAudioLifecycle();
    const activeClient = client.current;
    const activeMic = micTrack.current;
    client.current = null;
    micTrack.current = null;
    try {
      activeMic?.close();
      if (activeClient) {
        await activeClient.unpublish().catch(() => {});
        await activeClient.leave().catch(() => {});
      }
    } finally {
      setRemoteUsers({});
      setMuted(false);
      setStatus("ended");
    }
  }, []);

  const join = useCallback(async () => {
    if (joining.current || client.current) return;
    joining.current = true;
    setError(null);
    setStatus("connecting");
    const localScope = scope.current;
    try {
      const credentials = await fetchCredentials();
      const { default: AgoraRTC } = await import("agora-rtc-sdk-ng");
      const rtcClient = AgoraRTC.createClient({ mode: "rtc", codec: "vp8" });
      if (!localScope.active) return;
      client.current = rtcClient;
      rtcClient.enableAudioVolumeIndicator();

      rtcClient.on("user-published", async (user: IAgoraRTCRemoteUser, mediaType) => {
        if (mediaType !== "audio") return;
        await rtcClient.subscribe(user, mediaType);
        if (!localScope.active) return;
        patchRemote(Number(user.uid), { hasAudio: true });
        if (user.audioTrack) await play.current(user.audioTrack);
      });
      rtcClient.on("user-unpublished", (user: IAgoraRTCRemoteUser, mediaType) => {
        if (mediaType === "audio") patchRemote(Number(user.uid), { hasAudio: false, speaking: false });
      });
      rtcClient.on("user-joined", (user: IAgoraRTCRemoteUser) => patchRemote(Number(user.uid), {}));
      rtcClient.on("user-left", (user: IAgoraRTCRemoteUser) => {
        setRemoteUsers((current) => {
          const next = { ...current };
          delete next[Number(user.uid)];
          return next;
        });
      });
      rtcClient.on("volume-indicator", (volumes: { uid: number | string; level: number }[]) => {
        for (const entry of volumes) {
          patchRemote(Number(entry.uid), { speaking: entry.level > 5 });
        }
      });
      rtcClient.on("connection-state-change", (curState) => {
        if (!localScope.active) return;
        if (curState === "RECONNECTING") setStatus("reconnecting");
        else if (curState === "CONNECTED") setStatus("connected");
        else if (curState === "DISCONNECTED") setStatus((s) => (s === "failed" ? s : "ended"));
      });

      await rtcClient.join(credentials.agora_app_id, credentials.channel_name, credentials.agora_token, credentials.agora_uid);
      if (!localScope.active) return;
      const mic = await AgoraRTC.createMicrophoneAudioTrack();
      if (!localScope.active) { mic.close(); return; }
      micTrack.current = mic;
      localScope.own(() => mic.close());
      await rtcClient.publish(mic);
      if (!localScope.active) return;
      setStatus("connected");
    } catch (exc) {
      if (localScope.active) {
        setStatus("failed");
        setError(exc instanceof Error ? exc.message : "Unable to join the voice channel. Check your microphone permission.");
      }
    } finally {
      joining.current = false;
    }
  }, [fetchCredentials, patchRemote]);

  const toggleMute = useCallback(async () => {
    const mic = micTrack.current;
    if (!mic) return;
    const next = !muted;
    await mic.setEnabled(!next);
    setMuted(next);
  }, [muted]);

  useEffect(() => () => { void leave(); }, [leave]);

  return { status, error, muted, remoteUsers, join, leave, toggleMute };
}
