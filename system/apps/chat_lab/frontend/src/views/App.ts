import m from "mithril";
import { addAgentsUpdatedListener, getAgentById, getAgents, removeAgentsUpdatedListener } from "../models/AgentManager";
import type { AgentState } from "../models/AgentManager";
import { ChatPanel } from "./ChatPanel";
import { setAgentOpener } from "./dockview-shim";
import { ShareUrl } from "./ShareUrl";
import { connectControlChannel, setControlSelectAgentHandler } from "../models/ControlChannel";

/** Which agent is showing, persisted per browser so a reload lands back on it. */
const SELECTED_AGENT_KEY = "chat-lab.selected-agent-id";

function getSelectedAgentId(): string | null {
  return localStorage.getItem(SELECTED_AGENT_KEY);
}

function setSelectedAgentId(agentId: string): void {
  localStorage.setItem(SELECTED_AGENT_KEY, agentId);
}

/**
 * External "open this chat" signals, for driving the view from outside the
 * page -- a POC for window-change control. Three channels, all cheap because
 * they just call the same `selectAgent` the sidebar's onclick calls:
 *
 * - `?agent=<id>` in the URL, read once on load. Fits this workspace's
 *   existing "replace an iframe's URL" layout op (see manage-layout) --
 *   another agent can point chat-lab's tab at a specific chat without any
 *   new plumbing.
 * - `postMessage({type: "chat-lab:select-agent", agentId})`, for a live
 *   switch with no reload -- e.g. from a parent frame holding a reference to
 *   this window. Not origin-restricted yet; tighten before this leaves POC.
 * - The control-plane backend (ControlChannel.ts / chat_lab/runner.py): a
 *   process anywhere on this machine can `POST /select-agent` and every open
 *   chat-lab tab switches live, no browser-side access needed at all.
 */
function getAgentIdFromUrl(): string | null {
  return new URLSearchParams(window.location.search).get("agent");
}

export function App(): m.Component {
  let selectedAgentId: string | null = getAgentIdFromUrl() ?? getSelectedAgentId();

  function selectAgent(agentId: string): void {
    selectedAgentId = agentId;
    setSelectedAgentId(agentId);
    m.redraw();
  }

  const handleAgentsUpdated = (): void => {
    // If nothing is selected yet (first load, or the saved id no longer
    // exists), default to the most recently active agent.
    if (selectedAgentId !== null && getAgentById(selectedAgentId) !== undefined) {
      return;
    }
    const agents = getAgents();
    if (agents.length > 0) {
      selectAgent(agents[0].id);
    } else {
      m.redraw();
    }
  };

  const handleMessage = (event: MessageEvent): void => {
    const data = event.data as { type?: unknown; agentId?: unknown };
    if (data?.type === "chat-lab:select-agent" && typeof data.agentId === "string") {
      selectAgent(data.agentId);
    }
  };

  return {
    oninit() {
      setAgentOpener(selectAgent);
      addAgentsUpdatedListener(handleAgentsUpdated);
      window.addEventListener("message", handleMessage);
      setControlSelectAgentHandler(selectAgent);
      connectControlChannel();
    },
    onremove() {
      removeAgentsUpdatedListener(handleAgentsUpdated);
      window.removeEventListener("message", handleMessage);
    },
    view() {
      const agents = getAgents();
      return m("div", { class: "chat-lab-layout flex h-screen" }, [
        m("nav", { class: "chat-lab-sidebar flex w-64 shrink-0 flex-col border-r border-border" }, [
          m("div", { class: "chat-lab-sidebar-header px-3 py-3 text-sm font-semibold text-text-secondary" }, "Chats"),
          m(
            "div",
            { class: "min-h-0 flex-1 overflow-y-auto" },
            agents.length === 0
              ? m("div", { class: "px-3 py-2 text-sm text-text-secondary" }, "No agents found.")
              : agents.map((agent: AgentState) =>
                  m(
                    "button",
                    {
                      key: agent.id,
                      class:
                        "chat-lab-agent-row block w-full truncate px-3 py-2 text-left text-sm hover:bg-bg-hover" +
                        (agent.id === selectedAgentId ? " bg-bg-hover font-medium" : ""),
                      onclick: () => selectAgent(agent.id),
                    },
                    agent.display_name ?? agent.name,
                  ),
                ),
          ),
          m(ShareUrl),
        ]),
        m(
          "main",
          { class: "chat-lab-main min-w-0 flex-1" },
          selectedAgentId === null
            ? m("div", { class: "flex h-full items-center justify-center text-text-secondary" }, "Pick a chat")
            : m(ChatPanel, { agentId: selectedAgentId, isVisible: true }),
        ),
      ]);
    },
  };
}
