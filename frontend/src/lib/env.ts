/**
 * Intra AI — Environment Configuration & Validation
 *
 * Validates and provides typed access to public environment variables.
 * Fallbacks are provided for local development convenience.
 */

export interface EnvConfig {
  apiUrl: string;
  agoraAppId: string;
  appEnv: "development" | "staging" | "production";
  isProd: boolean;
  isDev: boolean;
  wsUrl?: string;
}

function resolveApiUrl(url: string | undefined): string {
  if (url && typeof url === "string" && url.trim() !== "") {
    return url.trim().replace(/\/+$/, "");
  }
  if (typeof window !== "undefined") {
    return window.location.origin;
  }
  return process.env.API_BASE_URL || "http://127.0.0.1:8000";
}

function resolveWsUrl(url: string | undefined): string | undefined {
  if (url && typeof url === "string" && url.trim() !== "") {
    return url.trim().replace(/\/+$/, "");
  }
  if (typeof window !== "undefined") {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${window.location.host}/ws`;
  }
  return undefined;
}

function resolveAppEnv(env: string | undefined): "development" | "staging" | "production" {
  if (env === "production" || env === "prod") return "production";
  if (env === "staging") return "staging";
  return "development";
}

const rawApiUrl = process.env.NEXT_PUBLIC_API_URL;
const rawAgoraAppId = process.env.NEXT_PUBLIC_AGORA_APP_ID;
const rawAppEnv = process.env.NEXT_PUBLIC_APP_ENV;
const rawWsUrl = process.env.NEXT_PUBLIC_WS_URL;

export const env: EnvConfig = {
  apiUrl: resolveApiUrl(rawApiUrl),
  agoraAppId: (rawAgoraAppId || "").trim(),
  appEnv: resolveAppEnv(rawAppEnv),
  isProd: resolveAppEnv(rawAppEnv) === "production",
  isDev: resolveAppEnv(rawAppEnv) === "development",
  wsUrl: resolveWsUrl(rawWsUrl),
};

// Defensive check in production browser console
if (typeof window !== "undefined" && env.isProd) {
  if (!env.agoraAppId) {
    console.warn("[Intra AI] NEXT_PUBLIC_AGORA_APP_ID is not configured. Voice sessions may fail to connect.");
  }
}
