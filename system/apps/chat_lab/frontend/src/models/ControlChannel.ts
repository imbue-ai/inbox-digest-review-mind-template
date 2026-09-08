/**
 * Client for chat-lab's control-plane backend (see chat_lab/runner.py):
 * a machine-local channel that lets something else on this host -- a script,
 * another agent -- tell this tab which chat to show, via
 * `POST /select-agent`. Proxied at `/control` (see vite.config.ts).
 */

import { ReconnectBackoff } from "./backoff";

let onSelectAgent: (agentId: string) => void = () => {};

export function setControlSelectAgentHandler(fn: (agentId: string) => void): void {
  onSelectAgent = fn;
}

export function connectControlChannel(): void {
  const backoff = new ReconnectBackoff();
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${protocol}//${window.location.host}/control/ws`;

  function connect(): void {
    const socket = new WebSocket(url);

    socket.onopen = () => backoff.reset();

    socket.onmessage = (event: MessageEvent) => {
      let data: unknown;
      try {
        data = JSON.parse(event.data as string);
      } catch {
        return;
      }
      const message = data as { type?: unknown; agentId?: unknown };
      if (message?.type === "select-agent" && typeof message.agentId === "string") {
        onSelectAgent(message.agentId);
      }
    };

    socket.onclose = () => {
      setTimeout(connect, backoff.nextDelay());
    };

    socket.onerror = () => {
      socket.close();
    };
  }

  connect();
}
