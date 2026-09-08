/**
 * Stand-ins for the dockview-shell navigation this fork does not carry over
 * (see system_interface's DockviewWorkspace.ts for the real versions). This
 * app has one chat visible at a time rather than a tabbed workspace, so
 * "open a tab" becomes "switch the current view".
 */

let onOpenAgent: (agentId: string) => void = () => {};

export function setAgentOpener(fn: (agentId: string) => void): void {
  onOpenAgent = fn;
}

export function openSubagentTab(agentId: string, _subagentSessionId: string, _description: string): void {
  onOpenAgent(agentId);
}

export async function startChatOnAccount(_accountId: string): Promise<void> {
  // The provider chooser modal isn't ported into this fork, so there is
  // nothing to start a chat from here yet.
}
