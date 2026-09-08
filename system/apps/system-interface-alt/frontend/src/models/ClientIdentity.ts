/**
 * Per-browser client identity.
 *
 * Each browser gets a stable uuid (minted once, kept in localStorage), a
 * device kind derived from the user agent (mobile vs desktop), and an active
 * view id (also persisted per browser so reconnects restore the same view).
 * The identity travels with every chat message and with the WebSocket
 * `client_state` registration, so the server (and agents, via
 * `layout.py context`) can attribute requests to a client and its view.
 */

const CLIENT_ID_STORAGE_KEY = "si-client-id";
const ACTIVE_PROJECT_STORAGE_KEY = "si-active-project-id";

export type DeviceKind = "mobile" | "desktop";

/** Pure UA classifier, separated from the navigator read for unit testing. */
export function classifyDeviceKind(userAgentDataMobile: boolean | undefined, userAgent: string): DeviceKind {
  if (userAgentDataMobile !== undefined) {
    return userAgentDataMobile ? "mobile" : "desktop";
  }
  return /Mobi|Android|iPhone|iPad|iPod/i.test(userAgent) ? "mobile" : "desktop";
}

export function getDeviceKind(): DeviceKind {
  // Fork-only override: this build always reports "mobile" regardless of the
  // real UA, purely to claim the backend's other device-kind storage slot
  // (projects/<id>.mobile.json) instead of "desktop" -- which the real
  // system_interface (sharing this same backend) also writes to. The backend
  // only recognizes "desktop"/"mobile"; there is no third slot without a
  // backend change. This is a deliberate collision risk against an actual
  // mobile client of the real interface, accepted because none is in regular
  // use here; see system/apps/system-interface-alt/README.md.
  return "mobile";
}

let cachedClientId: string | null = null;

export function getClientId(): string {
  if (cachedClientId !== null) {
    return cachedClientId;
  }
  const stored = localStorage.getItem(CLIENT_ID_STORAGE_KEY);
  if (stored) {
    cachedClientId = stored;
    return stored;
  }
  const minted =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `client-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  localStorage.setItem(CLIENT_ID_STORAGE_KEY, minted);
  cachedClientId = minted;
  return minted;
}

// The active view id (a project id, or Everything). Held in module state
// (source of truth while the page lives) and mirrored to localStorage so the
// same browser restores the same view on its next connect. Empty string means
// "not chosen yet" (during startup, before the projects list has been fetched).
let activeProjectId = "";

export function getStoredProjectId(): string {
  return localStorage.getItem(ACTIVE_PROJECT_STORAGE_KEY) ?? "";
}

export function getActiveProjectId(): string {
  return activeProjectId;
}

export function setActiveProjectId(projectId: string): void {
  activeProjectId = projectId;
  localStorage.setItem(ACTIVE_PROJECT_STORAGE_KEY, projectId);
}
