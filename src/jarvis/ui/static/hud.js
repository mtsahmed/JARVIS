// Jarvis HUD, UI-3 (two-way): shows what Jarvis is doing and sends the user's input (mic button, typed
// requests, approval clicks). Protocol: docs/ui-protocol.md. Every server message is checked before use,
// and all text is inserted with textContent (never as HTML): replies can contain text from web pages and
// emails. The page decides nothing: the server is the source of truth.
"use strict";

(() => {
  const VERSION = 1;
  const PING_EVERY_MS = 15000;
  const PONG_DEADLINE_MS = 30000;
  const MAX_BACKOFF_MS = 30000;
  const LOG_LIMIT = 50;
  const ALERT_MS = 8000;
  const MAX_TEXT = 2000;
  const COUNT_FROM = 1800; // show the character count near the limit

  const MODES = {
    idle: { pill: "IDLE", title: "STANDING BY", hint: "Hold the talk key, tap the mic, or type below." },
    listening: { pill: "LISTENING", title: "LISTENING", hint: "Speak now. Tap the mic again (or release the key) when you are done." },
    thinking: { pill: "THINKING", title: "THINKING", hint: "Working on it…" },
    speaking: { pill: "SPEAKING", title: "SPEAKING", hint: "" },
    confirm: { pill: "APPROVAL", title: "AWAITING APPROVAL", hint: "Say yes, or use the buttons." },
    error: { pill: "ERROR", title: "ERROR", hint: "" },
    offline: { pill: "OFFLINE", title: "CONNECTING", hint: "Waiting for Jarvis. Is it running with the HUD turned on?" },
  };
  const TEXT_MODE_IDLE_HINT = "Type below. (Jarvis is in text mode: no microphone.)";
  const WHO = { user: "YOU", jarvis: "JARVIS", reminder: "REMINDER", briefing: "BRIEFING", notice: "NOTICE", approval: "APPROVAL" };
  const BAR_HEIGHTS = [14, 22, 30, 18, 36, 26, 40, 20, 32, 44, 24, 38, 16, 42, 28, 34,
                       44, 22, 36, 18, 40, 30, 26, 44, 20, 34, 24, 38, 16, 28, 22, 14];

  const $ = (id) => document.getElementById(id);
  const el = {
    pill: $("pill-text"), title: $("state-title"), hint: $("state-hint"), version: $("version"),
    lastUser: $("last-user"), lastReply: $("last-reply"), sources: $("last-sources"), log: $("log"),
    conn: $("conn"), time: $("time"), date: $("date"), wave: $("wave"),
    mic: $("mic"), text: $("text"), send: $("send"), count: $("count"),
    confirm: $("confirm"), confirmAction: $("confirm-action"), confirmTarget: $("confirm-target"),
    confirmTargetRow: $("confirm-target-row"), confirmSummary: $("confirm-summary"), confirmTier: $("confirm-tier"),
    confirmLeft: $("confirm-left"), approve: $("approve"), deny: $("deny"),
  };

  let token = document.querySelector('meta[name="jarvis-session-token"]').content;
  let socket = null;
  let connected = false; // hello_ack received on the current socket
  let backoff = 1000;
  let pingTimer = null;
  let lastPong = 0;
  let alertTimer = null;
  let detail = "";
  let voice = false; // Jarvis runs in voice mode: the mic button works
  let recording = false; // this page started a recording with its mic button
  let pending = null; // the open approval: { id, deadline, timer, returnFocus }

  // --- small helpers ------------------------------------------------------------------------------

  const isObject = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
  const str = (v) => (typeof v === "string" ? v : "");

  function uuid() {
    return crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2);
  }

  function send(type, data) {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ v: VERSION, type, id: uuid(), ts: new Date().toISOString(), data: data || {} }));
      return true;
    }
    return false;
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
    if (mode !== "listening" && recording) setRecording(false); // the recording ended (released, cap, error)
    if (!alertTimer) showHint();
  }

  function showHint() {
    const mode = document.body.dataset.mode;
    const m = MODES[mode] || MODES.offline;
    let hint = mode === "idle" && !voice ? TEXT_MODE_IDLE_HINT : m.hint;
    if (detail) hint = mode === "confirm" ? detail + " " + m.hint : detail;
    el.hint.classList.remove("alert");
    el.hint.textContent = hint;
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
    li.className = role === "user" || role === "notice" || role === "approval" ? role : "jarvis";
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

  function logRole(entry) {
    if (entry.role === "user") return "user";
    if (entry.role === "error") return "notice";
    if (entry.role === "approval") return "approval";
    return replyRole(entry.kind);
  }

  // --- input: mic button, text box ---------------------------------------------------------------

  function updateControls() {
    el.mic.disabled = !connected || !voice;
    el.mic.setAttribute("aria-label", voice ? "Talk: tap to start, tap again to send"
                                            : "Talk (unavailable: Jarvis is in text mode)");
    el.text.disabled = !connected;
    el.send.disabled = !connected || !el.text.value.trim();
    el.text.placeholder = connected ? "Type a request and press Enter" : "Waiting for Jarvis…";
  }

  function setRecording(on) {
    recording = on;
    el.mic.setAttribute("aria-pressed", on ? "true" : "false");
  }

  el.mic.addEventListener("click", () => {
    if (!voice) return;
    if (send("ptt", { action: recording ? "stop" : "start" })) setRecording(!recording);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && recording && !pending) { // throw the recording away
      if (send("ptt", { action: "cancel" })) setRecording(false);
    }
  });

  function sendText() {
    const text = el.text.value.trim();
    if (!text || text.length > MAX_TEXT) return;
    if (send("text_input", { text })) {
      el.text.value = "";
      updateCount();
    }
  }

  function updateCount() {
    const length = el.text.value.length;
    el.count.textContent = length >= COUNT_FROM ? length.toLocaleString() + " / " + MAX_TEXT.toLocaleString() : "";
    el.count.classList.toggle("near", length >= COUNT_FROM);
    el.send.disabled = !connected || !el.text.value.trim();
  }

  el.text.addEventListener("input", updateCount);
  el.text.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      sendText();
    }
  });
  el.send.addEventListener("click", sendText);

  // --- approval dialog ---------------------------------------------------------------------------

  function openConfirm(data) {
    const id = str(data.request_id);
    const seconds = Number(data.expires_in_s);
    if (!id || !Number.isFinite(seconds) || seconds <= 0) return;
    if (pending && pending.id === id) return; // already showing it (snapshot after a reconnect)
    closeConfirm();
    el.confirmAction.textContent = str(data.action) || "Action";
    el.confirmTarget.textContent = str(data.target);
    el.confirmTargetRow.hidden = !str(data.target);
    el.confirmSummary.textContent = str(data.summary);
    el.confirmTier.textContent = str(data.tier) || "CONFIRM";
    el.approve.disabled = el.deny.disabled = false;
    const returnFocus = document.activeElement;
    pending = { id, summary: str(data.summary), deadline: Date.now() + seconds * 1000,
                timer: setInterval(showTimeLeft, 250), returnFocus };
    showTimeLeft();
    el.confirm.hidden = false;
    el.deny.focus(); // the safe choice has the focus: Enter never approves by accident
  }

  function showTimeLeft() {
    if (!pending) return;
    const left = Math.ceil((pending.deadline - Date.now()) / 1000);
    el.confirmLeft.textContent = left > 0 ? "(" + left + " s left)" : "(time is up: waiting for Jarvis)";
  }

  function answer(approved) {
    if (!pending || el.approve.disabled) return;
    if (send("confirm_response", { request_id: pending.id, approved })) {
      el.approve.disabled = el.deny.disabled = true; // the dialog closes on Jarvis's confirm_result
    }
  }

  // The dialog closes only when Jarvis says the request is settled (protocol §6.5), or the connection drops.
  function closeConfirm() {
    if (!pending) return;
    clearInterval(pending.timer);
    const returnFocus = pending.returnFocus;
    pending = null;
    el.confirm.hidden = true;
    if (returnFocus && typeof returnFocus.focus === "function" && !returnFocus.disabled) returnFocus.focus();
  }

  el.approve.addEventListener("click", () => answer(true));
  el.deny.addEventListener("click", () => answer(false));
  el.confirm.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      answer(false);
    } else if (event.key === "Tab") { // keep the focus inside the dialog
      event.preventDefault();
      (document.activeElement === el.deny ? el.approve : el.deny).focus();
    }
  });

  // --- messages from Jarvis -----------------------------------------------------------------------

  function setVoice(value) {
    if (typeof value !== "boolean") return;
    voice = value;
    if (!voice && recording) setRecording(false);
    updateControls();
    if (!alertTimer) showHint();
  }

  const handlers = {
    hello_ack(data) {
      backoff = 1000;
      connected = true;
      el.version.textContent = "v" + (str(data.server_version) || "1");
      el.conn.textContent = "Connected";
      updateControls();
    },
    snapshot(data) {
      el.log.replaceChildren();
      for (const entry of Array.isArray(data.log) ? data.log.slice(-LOG_LIMIT) : []) {
        if (isObject(entry)) addLog(logRole(entry), str(entry.text), entry.sources);
      }
      if (!el.log.children.length) {
        const li = document.createElement("li");
        li.className = "empty";
        li.textContent = "Nothing yet. Hold the talk key, tap the mic, or type below.";
        el.log.append(li);
      }
      el.lastUser.textContent = str(data.last_user);
      showLastReply(isObject(data.last_reply) ? data.last_reply : {});
      setVoice(data.voice);
      setMode(str(data.mode) || "idle", data.detail);
      if (isObject(data.confirm)) openConfirm(data.confirm);
      else closeConfirm();
      el.log.scrollTop = el.log.scrollHeight;
    },
    state(data) {
      setVoice(data.voice);
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
    confirm_request(data) {
      openConfirm(data);
    },
    confirm_result(data) {
      if (!pending || str(data.request_id) !== pending.id) return;
      const outcome = { approved: "approved", denied: "declined", expired: "not answered in time: declined" }[data.outcome];
      const by = { voice: " by voice", screen: " on the HUD", terminal: " in the terminal" }[data.by] || "";
      const summary = pending.summary;
      closeConfirm();
      if (outcome) {
        addLog("approval", summary + " " + outcome + by + "."); // same wording as the server's log
        showAlert(outcome.charAt(0).toUpperCase() + outcome.slice(1) + ".");
      }
    },
    error(data) {
      const message = str(data.message) || "Something went wrong.";
      if (data.code === "unauthorized") {
        refreshToken(); // Jarvis restarted with a new session token
        return;
      }
      if (data.code === "voice_unavailable" && recording) setRecording(false);
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
    connected = false;
    setRecording(false); // the server cancels a recording when its page goes away
    closeConfirm(); // can't be answered from here now; a pending one comes back with the next snapshot
    updateControls();
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
  updateControls();
  setMode("offline");
  connect();
})();
