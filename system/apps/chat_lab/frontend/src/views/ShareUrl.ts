import m from "mithril";
import { icon } from "./icons";

/**
 * This app owns a full browser origin (see origin.ts) -- wherever this page
 * is being viewed from IS the URL that opens the same app elsewhere, whether
 * that is the local workspace origin or, if the workspace is shared, the
 * public one. No derivation needed.
 */
function getShareUrl(): string {
  return `${window.location.origin}/`;
}

async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Fallback for contexts where the async Clipboard API is unavailable.
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch {
      copied = false;
    }
    textarea.remove();
    return copied;
  }
}

export function ShareUrl(): m.Component {
  let copied = false;
  let copyTimer: ReturnType<typeof setTimeout> | null = null;

  return {
    onremove() {
      if (copyTimer !== null) clearTimeout(copyTimer);
    },
    view() {
      const url = getShareUrl();
      return m("div", { class: "chat-lab-share-url flex items-center gap-2 border-t border-border px-3 py-2" }, [
        m(
          "span",
          {
            class: "min-w-0 flex-1 truncate text-xs text-text-secondary",
            title: url,
          },
          url,
        ),
        m(
          "button",
          {
            type: "button",
            class: "chat-lab-share-url-copy shrink-0 rounded p-1 text-text-secondary hover:bg-bg-hover",
            title: "Copy link to this app",
            onclick: async () => {
              const ok = await copyToClipboard(url);
              copied = ok;
              m.redraw();
              if (copyTimer !== null) clearTimeout(copyTimer);
              copyTimer = setTimeout(() => {
                copied = false;
                m.redraw();
              }, 1500);
            },
          },
          copied ? "Copied" : m.trust(icon("copy", { size: 14 })),
        ),
      ]);
    },
  };
}
