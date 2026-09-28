// Jarvis HUD, UI-2 (one-way): shows what Jarvis is doing. Protocol: docs/ui-protocol.md.
// Every server message is checked before use, and all text is inserted with textContent (never as HTML):
// replies can contain text from web pages and emails.
"use strict";

(() => {
  const VERSION = 1;
  const PING_EVERY_MS = 15000;
  const PONG_DEADLINE_MS = 30000;
  const MAX_BACKOFF_MS = 30000;
  const LOG_LIMIT = 50;
  const ALERT_MS = 8000;

  const MODES = {
    idle: { pill: "IDLE", title: "STANDING BY", hint: "Hold the talk key and speak." },
    listening: { pill: "LISTENING", title: "LISTENING", hint: "Speak now. Release the key when you are done." },
    thinking: { pill: "THINKING", title: "THINKING", hint: "Working on it…" },
    speaking: { pill: "SPEAKING", title: "SPEAKING", hint: "" },
    confirm: { pill: "APPROVAL", title: "AWAITING APPROVAL", hint: "Hold the talk key and say yes to approve." },
    error: { pill: "ERROR", title: "ERROR", hint: "" },
    offline: { pill: "OFFLINE", title: "CONNECTING", hint: "Waiting for Jarvis. Is it running with the HUD turned on?" },
  };
  const WHO = { user: "YOU", jarvis: "JARVIS", reminder: "REMINDER", briefing: "BRIEFING", notice: "NOTICE" };
  const BAR_HEIGHTS = [14, 22, 30, 18, 36, 26, 40, 20, 32, 44, 24, 38, 16, 42, 28, 34,
                       44, 22, 36, 18, 40, 30, 26, 44, 20, 34, 24, 38, 16, 28, 22, 14];

  const $ = (id) => document.getElementById(id);
  const el = {
    pill: $("pill-text"), title: $("state-title"), hint: $("state-hint"), version: $("version"),
    lastUser: $("last-user"), lastReply: $("last-reply"), sources: $("last-sources"), log: $("log"),
    conn: $("conn"), time: $("time"), date: $("date"), wave: $("wave"),
  };

  let token = document.querySelector('meta[name="jarvis-session-token"]').content;
  let socket = null;
  let backoff = 1000;
  let pingTimer = null;
  let lastPong = 0;
  let alertTimer = null;
  let detail = "";

  // --- small helpers ------------------------------------------------------------------------------

  const isObject = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
  const str = (v) => (typeof v === "string" ? v : "");

  function uuid() {
    return crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2);
  }

  function send(type, data) {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ v: VERSION, type, id: uuid(), ts: new Date().toISOString(), data: data || {} }));
    }
  }

  function safeHttpsUrl(value) {
    try {
      const url = new URL(str(value));
      return url.protocol === "https:" ? url : null;
    } catch {
      return null;
    }
  }

  // --- rendering (textContent only) ---------------------------------------------------------------

  function setMode(mode, newDetail) {
    if (!(mode in MODES)) return;
    detail = str(newDetail);
    document.body.dataset.mode = mode;
    const m = MODES[mode];
    el.pill.textContent = m.pill;
    el.title.textContent = m.title;
    if (!alertTimer) showHint();
  }

  function showHint() {
    const m = MODES[document.body.dataset.mode] || MODES.offline;
    el.hint.classList.remove("alert");
    el.hint.textContent = detail ? (document.body.dataset.mode === "confirm" ? detail + " " + m.hint : detail) : m.hint;
  }

  function showAlert(text) {
    el.hint.classList.add("alert");
    el.hint.textContent = text;
    clearTimeout(alertTimer);
    alertTimer = setTimeout(() => { alertTimer = null; showHint(); }, ALERT_MS);
  }

  function renderSources(list, sources) {
    list.replaceChildren();
    for (const source of Array.isArray(sources) ? sources.slice(0, 8) : []) {
      if (!isObject(source)) continue;
      const url = safeHttpsUrl(source.url);
      if (!url) continue;
      const a = document.createElement("a");
      a.href = url.href;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = str(source.title) || url.hostname;
      const li = document.createElement("li");
      li.append(a);
      list.append(li);
    }
  }

  function addLog(role, text, sources) {
    const empty = el.log.querySelector(".empty");
    if (empty) empty.remove();
    const li = document.createElement("li");
    li.className = role === "user" ? "user" : role === "notice" ? "notice" : "jarvis";
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = WHO[role] || "JARVIS";
    const body = document.createElement("span");
    body.className = "text";
    body.textContent = text;
    li.append(who, body);
    if (Array.isArray(sources) && sources.length) {
      const list = document.createElement("ul");
      list.className = "sources";
      renderSources(list, sources);
      li.append(list);
    }
    const atBottom = el.log.scrollHeight - el.log.scrollTop - el.log.clientHeight < 40;
    el.log.append(li);
    while (el.log.children.length > LOG_LIMIT) el.log.firstElementChild.remove();
    if (atBottom) el.log.scrollTop = el.log.scrollHeight;
  }

  function showLastReply(reply) {
    el.lastReply.textContent = str(reply.text);
    renderSources(el.sources, reply.sources);
  }

  function replyRole(kind) {
    return kind === "reminder" || kind === "briefing" ? kind : "jarvis";
  }

  // --- messages from Jarvis -----------------------------------------------------------------------

  const handlers = {
    hello_ack(data) {
      backoff = 1000;
      el.version.textContent = "v" + (str(data.server_version) || "1");
      el.conn.textContent = "Connected";
    },
    snapshot(data) {
      el.log.replaceChildren();
      for (const entry of Array.isArray(data.log) ? data.log.slice(-LOG_LIMIT) : []) {
        if (!isObject(entry)) continue;
        const role = entry.role === "user" ? "user" : entry.role === "error" ? "notice" : replyRole(entry.kind);
        addLog(role, str(entry.text), entry.sources);
      }
      if (!el.log.children.length) {
        const li = document.createElement("li");
        li.className = "empty";
        li.textContent = "Nothing yet. Hold the talk key and ask something.";
        el.log.append(li);
      }
      el.lastUser.textContent = str(data.last_user);
      showLastReply(isObject(data.last_reply) ? data.last_reply : {});
      setMode(str(data.mode) || "idle", data.detail);
      el.log.scrollTop = el.log.scrollHeight;
    },
    state(data) {
      setMode(str(data.mode), data.detail);
    },
    transcript(data) {
      if (data.final !== true) return; // partial results arrive in a later update
      el.lastUser.textContent = str(data.text);
      addLog("user", str(data.text));
    },
    reply(data) {
      const role = replyRole(data.kind);
      if (role === "jarvis") showLastReply(data);
      addLog(role, str(data.text), data.sources);
    },
    error(data) {
      const message = str(data.message) || "Something went wrong.";
      if (data.code === "unauthorized") {
        refreshToken(); // Jarvis restarted with a new session token
        return;
      }
      addLog("notice", message);
      showAlert(message);
    },
    pong() {
      lastPong = Date.now();
    },
  };

  function onMessage(event) {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    if (!isObject(message) || message.v !== VERSION || typeof message.type !== "string" || !isObject(message.data)) {
      return;
    }
    const handler = handlers[message.type];
    if (handler) handler(message.data); // unknown types are ignored (protocol §1)
  }

  // --- connection: hello, heartbeat, reconnect with backoff ----------------------------------------

  function connect() {
    socket = new WebSocket("ws://" + location.host + "/ws");
    el.conn.textContent = "Connecting…";
    socket.addEventListener("open", () => {
      send("hello", { token, client: "hud" });
      lastPong = Date.now();
      clearInterval(pingTimer);
      pingTimer = setInterval(() => {
        if (Date.now() - lastPong > PONG_DEADLINE_MS) {
          socket.close(); // no answer for 30 s: reconnect
          return;
        }
        send("ping");
      }, PING_EVERY_MS);
    });
    socket.addEventListener("message", onMessage);
    socket.addEventListener("close", onClose);
  }

  function onClose() {
    clearInterval(pingTimer);
    socket = null;
    setMode("offline");
    el.conn.textContent = "Offline · retrying";
    setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
  }

  async function refreshToken() {
    // Each Jarvis run has a new token; the page asks its own server for the current one.
    try {
      const response = await fetch("/", { cache: "no-store" });
      const page = new DOMParser().parseFromString(await response.text(), "text/html"); // inert: no scripts run
      const fresh = page.querySelector('meta[name="jarvis-session-token"]');
      if (fresh && fresh.content) {
        token = fresh.content;
        backoff = 1000; // Jarvis is back: retry promptly
      }
    } catch {
      // Jarvis is not running; the reconnect loop keeps trying.
    }
  }

  // --- clock, waveform, visibility -----------------------------------------------------------------

  function tick() {
    const now = new Date();
    el.time.textContent = now.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    el.date.textContent = now.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric" }).toUpperCase();
  }

  function buildWave() {
    BAR_HEIGHTS.forEach((height, i) => {
      const bar = document.createElement("span");
      bar.style.setProperty("--h", height + "px");
      bar.style.setProperty("--d", ((i % 8) * 0.07).toFixed(2) + "s");
      el.wave.append(bar);
    });
  }

  const pauseWhenHidden = () => document.body.classList.toggle("hidden", document.hidden);
  document.addEventListener("visibilitychange", pauseWhenHidden);
  pauseWhenHidden();

  buildWave();
  tick();
  setInterval(tick, 1000);
  setMode("offline");
  connect();
})();
