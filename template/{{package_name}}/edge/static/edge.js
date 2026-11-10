// SPDX-License-Identifier: GPL-3.0-or-later
// The edge layer's panels, for any tool's page: put
//   <section id="edge-connections"></section>
//   <section id="edge-chat"></section>          (optional)
//   <script src="/edge-static/edge.js" defer></script>
// in templates/index.html (scripts/edge-wire.py does it). Owned by the edge
// layer; plain DOM, no framework, text never inserted as HTML.
(() => {
  "use strict";
  const $ = (sel, root = document) => root.querySelector(sel);
  const el = (tag, attrs = {}, ...kids) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "on") for (const [ev, fn] of Object.entries(v)) e.addEventListener(ev, fn);
      else if (v !== false && v != null) e.setAttribute(k, v === true ? "" : v);
    }
    for (const k of kids.flat()) if (k != null && k !== false) e.append(k.nodeType ? k : String(k));
    return e;
  };
  // the core's loading card (python-tool-template core.js) for what the
  // person starts; reading and polling stay quiet. An older core has none.
  const busy = (d) => { if (window.core && core.busy) core.busy(d); };
  async function api(method, path, body) {
    if (method === "GET") return call(method, path, body);
    busy(1);
    try { return await call(method, path, body); } finally { busy(-1); }
  }
  async function call(method, path, body) {
    const r = await fetch(path, { method, headers: body ? { "Content-Type": "application/json" } : {},
                                  body: body ? JSON.stringify(body) : undefined });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error((data.errors && data.errors[0]) || data.problem || data.description || `${r.status}`);
    return data;
  }
  const host = (u) => { try { return new URL(u).host; } catch { return u || ""; } };

  // ------------------------------------------------------------ connections --
  const box = $("#edge-connections");
  let poll = null;
  async function connections() {
    if (!box) return;
    let st;
    try { st = await api("GET", "/api/cloud"); } catch (e) { box.replaceChildren(el("p", { class: "err" }, e.message)); return; }
    const rows = Object.entries(st.profiles || {}).map(([name, c]) => el("li", { class: `edge-conn${name === st.profile ? " is-current" : ""}` },
      el("span", { class: `edge-dot ${c.linked ? "ok" : "off"}`, title: c.linked ? "linked" : "not linked" }, "●"), " ",
      el("b", {}, host(c.site) || name), " ",
      el("span", { class: "edge-tag" }, name + (name === st.profile ? " · current" : "")),
      c.tenant ? el("span", { class: "edge-tag" }, `${c.tenant.name} · ${c.spaces.length} space${c.spaces.length === 1 ? "" : "s"}`) : null,
      c.no_tenant ? el("span", { class: "edge-tag err" }, "no tenant: link again") : null,
      c.rotate_error ? el("span", { class: "edge-tag err", title: c.rotate_error }, "token renewal failed") : null,
      c.token_expires ? el("span", { class: "edge-tag" }, `token until ${c.token_expires.slice(0, 10)}`) : null,
      name !== st.profile ? el("button", { type: "button", "data-help": "Make this the connection the edge syncs with and the chat talks to.", on: { click: () => api("POST", "/api/cloud/use", { profile: name }).then(connections) } }, "use") : null,
      el("button", { type: "button", "data-help": "Forget this connection's token on this edge. The cloud keeps its data; link again to come back.", on: { click: () => { if (confirm(`Forget ${name} on this edge?`)) api("POST", "/api/cloud/unlink", { profile: name }).then(connections); } } }, "unlink")));
    const link = st.link || {};
    const form = el("form", { class: "edge-link", on: { submit: (ev) => {
      ev.preventDefault();
      const f = ev.target;
      api("POST", "/api/cloud/link", { cloud: f.cloud.value.trim(), profile: f.profile.value.trim() || null })
        .then(connections).catch((e) => alert(e.message));
    } } },
      el("input", { name: "cloud", placeholder: "edge API address (the cloud's About page)", required: true, size: 48 }),
      el("input", { name: "profile", placeholder: "name, e.g. prod", pattern: "[a-z][a-z0-9-]{0,31}", size: 10 }),
      el("button", { type: "submit", "data-help": "Show a code that an admin of your tenant approves on the cloud's /link page. No password is typed here." }, "+ Add connection"));
    const pairing = link.state === "waiting" ? el("p", { class: "edge-pairing" },
      "Ask an admin of your tenant to approve code ", el("b", {}, link.user_code || "…"), " on ",
      el("a", { href: link.url, target: "_blank", rel: "noopener noreferrer" }, host(link.url)), ".")
      : link.state === "failed" ? el("p", { class: "err" }, link.error) : null;
    box.replaceChildren(el("h2", {}, "Connections"), el("ul", { class: "edge-conns" }, rows.length ? rows : el("li", {}, "Not linked to a cloud yet.")), pairing, form);
    clearTimeout(poll);
    if (link.state === "waiting") poll = setTimeout(connections, 3000);
  }

  // ------------------------------------------------------------------- chat --
  // Proposals wait for a click (and outlive a restart); the last ten
  // conversations can be read again and continued (History).
  const chatBox = $("#edge-chat");
  async function chatPanel() {
    if (!chatBox) return;
    const st = await api("GET", "/api/chat").catch(() => ({ available: false, why: "no answer" }));
    if (!st.available) { chatBox.replaceChildren(el("h2", {}, "Chat"), el("p", { class: "edge-muted" }, st.why)); return; }
    const log = el("div", { class: "edge-chat__log", "aria-live": "polite" });
    const past = el("div", { class: "edge-chat__history", hidden: true });
    const input = el("textarea", { rows: 2, placeholder: "Ask about this edge…" });
    const say = (who, text) => { const p = el("p", { class: `edge-chat__${who}` }, text); log.append(p); return p; };
    const decide = (id, what, row) => {
      row.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      return api("POST", `/api/chat/proposals/${id}/${what}`)
        .then((r) => row.replaceChildren(row.dataset.summary, " — ", what === "confirm" ? "✓ done" : "dismissed"))
        .catch((e) => row.replaceChildren(row.dataset.summary, " — ", el("span", { class: "err" }, e.message)));
    };
    const proposal = (p) => {
      const row = el("p", { class: "edge-chat__proposal" }, p.summary || p.id, " ",
        el("button", { type: "button", "data-help": "Run this one change now, on this edge.", on: { click: () => decide(p.id, "confirm", row) } }, "Confirm"), " ",
        el("button", { type: "button", "data-help": "Drop it. Nothing changes, and the chat hears it was dismissed.", on: { click: () => decide(p.id, "dismiss", row) } }, "Dismiss"));
      row.dataset.summary = p.summary || p.id;
      log.append(row);
    };
    (st.waiting || []).forEach(proposal);
    async function send() {
      const message = input.value.trim();
      if (!message) return;
      input.value = "";
      say("me", message);
      const answer = say("them", "");
      const r = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, lang: (document.documentElement.lang || "en").slice(0, 2) }) }).catch(() => null);
      if (!r || !r.ok || !r.body) { answer.textContent = `error ${r ? r.status : "no answer"}`; return; }
      const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
          let ev; try { ev = JSON.parse(line); } catch { continue; }
          if (ev.t === "text") answer.append(ev.text);
          else if (ev.t === "proposal") proposal(ev);
          else if (ev.t === "done" && ev.error) log.append(el("p", { class: "err" }, ev.error));
        }
      }
      connections();
    }
    async function history() {
      if (!past.hidden) { past.hidden = true; return; }
      let h;
      try { h = await api("GET", "/api/chat/history"); } catch (e) { past.replaceChildren(el("p", { class: "err" }, e.message)); past.hidden = false; return; }
      const when = (v) => { try { return new Date(v).toLocaleString(); } catch { return v || ""; } };
      const go = async (session, btn) => {
        btn.disabled = true;
        let r;
        try { r = await api("POST", `/api/chat/history/${encodeURIComponent(session)}/resume`, {}); }
        catch (e) { btn.disabled = false; past.append(el("p", { class: "err" }, e.message)); return; }
        past.hidden = true;
        log.replaceChildren(el("p", { class: "edge-muted" }, `Continuing: ${r.title || "a conversation"}`));
        r.messages.forEach((m) => say(m.who === "me" ? "me" : "them", m.text));
      };
      past.replaceChildren(h.items.length ? el("ul", {}, h.items.map((i) => el("li", {},
        el("b", {}, i.title || "(untitled)"), " ",
        el("span", { class: "edge-muted" }, `${when(i.updated)} · ${i.said || 0} messages`), " ",
        i.session === h.current ? el("span", { class: "edge-tag" }, "now")
          : el("button", { type: "button", "data-help": "Go on with this conversation where it stopped.", on: { click: (ev) => go(i.session, ev.target) } }, "Continue"))))
        : el("p", { class: "edge-muted" }, "No conversations yet."));
      past.hidden = false;
    }
    input.addEventListener("keydown", (ev) => { if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); send(); } });
    chatBox.replaceChildren(el("h2", {}, "Chat"), past, log,
      el("form", { class: "edge-chat__ask", on: { submit: (ev) => { ev.preventDefault(); send(); } } }, input,
        el("button", { type: "submit", "data-help": "Send it to Claude on this computer. Enter sends too; Shift+Enter starts a new line." }, "Ask"),
        el("button", { type: "button", "data-help": "Stop the answer being written.", on: { click: () => api("POST", "/api/chat/stop", {}) } }, "Stop"),
        el("button", { type: "button", "data-help": "Start a new conversation. The last one stays in History.", on: { click: () => api("POST", "/api/chat/new", {}).then(() => log.replaceChildren()) } }, "New"),
        el("button", { type: "button", "data-help": "The last ten conversations, kept on this computer. Continue one; Claude remembers what was said.", on: { click: history } }, "History")));
  }

  connections();
  chatPanel();
  setInterval(connections, 60000);
})();
