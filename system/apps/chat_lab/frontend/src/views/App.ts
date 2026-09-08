import m from "mithril";
import { addAgentsUpdatedListener, getAgentById, getAgents, removeAgentsUpdatedListener } from "../models/AgentManager";
import type { AgentState } from "../models/AgentManager";
import { ChatPanel } from "./ChatPanel";
import { setAgentOpener } from "./dockview-shim";

/** Which agent is showing, persisted per browser so a reload lands back on it. */
const SELECTED_AGENT_KEY = "chat-lab.selected-agent-id";

function getSelectedAgentId(): string | null {
  return localStorage.getItem(SELECTED_AGENT_KEY);
}

function setSelectedAgentId(agentId: string): void {
  localStorage.setItem(SELECTED_AGENT_KEY, agentId);
}

export function App(): m.Component {
  let selectedAgentId: string | null = getSelectedAgentId();

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

  return {
    oninit() {
      setAgentOpener(selectAgent);
      addAgentsUpdatedListener(handleAgentsUpdated);
    },
    onremove() {
      removeAgentsUpdatedListener(handleAgentsUpdated);
    },
    view() {
      const agents = getAgents();
      return m("div", { class: "chat-lab-layout flex h-screen" }, [
        m("nav", { class: "chat-lab-sidebar flex w-64 shrink-0 flex-col overflow-y-auto border-r border-border" }, [
          m("div", { class: "chat-lab-sidebar-header px-3 py-3 text-sm font-semibold text-text-secondary" }, "Chats"),
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
