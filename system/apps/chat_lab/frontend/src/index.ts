import m from "mithril";
import "./style.css";
import { initAgentManager } from "./models/AgentManager";
import { initShellPermissionResolutions } from "./views/permission-card";
import { App } from "./views/App";

function bootstrap(): void {
  initAgentManager();
  // Flip in-chat permission cards as soon as the enclosing shell reports a verdict.
  initShellPermissionResolutions();
  const rootElement = document.getElementById("app");
  if (rootElement) {
    m.mount(rootElement, App);
  }
}

window.addEventListener("load", bootstrap);
