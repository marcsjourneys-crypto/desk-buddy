"use strict";
// CARL's dashboard. Plain JS, no build step, no outside scripts.
// Everything from CARL's world (what you said, what CARL said, song names) is untrusted
// text: it is only ever put on the page with textContent, never parsed as HTML.

(() => {
  const TOKEN = document.querySelector('meta[name="carl-token"]').content;
  const MAX_ITEMS = 200;
  const $ = (id) => document.getElementById(id);

  // Build an element. Text always goes in as text nodes.
  function el(tag, opts = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(opts)) {
      if (value === undefined || value === null) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = String(value);
      else if (key === "on") for (const [ev, fn] of Object.entries(value)) node.addEventListener(ev, fn);
      else if (key in node) node[key] = value;
      else node.setAttribute(key, String(value));
    }
    for (const child of children) if (child !== null && child !== undefined) node.append(child);
    return node;
  }

  async function api(method, path, body) {
    const options = { method, headers: {} };
    if (method !== "GET") options.headers["X-CARL-Token"] = TOKEN;
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    let data = null;
    try {
      data = await response.json();
    } catch (_) {
      data = null;
    }
    if (!response.ok) {
      const error = new Error((data && data.error) || `${response.status} ${response.statusText}`);
      error.data = data;
      throw error;
    }
    return data;
  }

  function time(ts) {
    const d = ts ? new Date(ts) : new Date();
    return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString();
  }

  // ---------------------------------------------------------------- banner

  const banners = new Map();
  function setBanner(source, message) {
    if (message) banners.set(source, message);
    else banners.delete(source);
    const banner = $("banner");
    banner.textContent = [...banners.values()].join(" ");
    banner.hidden = banners.size === 0;
  }

  // ---------------------------------------------------------------- tabs

  const loaders = { settings: loadSettings, personality: loadPersonality };
  function showTab(name) {
    if (!["live", "settings", "personality"].includes(name)) name = "live";
    for (const button of document.querySelectorAll("[data-tab]")) {
      button.setAttribute("aria-selected", String(button.dataset.tab === name));
    }
    for (const section of document.querySelectorAll(".tab")) {
      section.hidden = section.id !== `tab-${name}`;
    }
    if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
    if (loaders[name]) loaders[name]();
  }
  for (const button of document.querySelectorAll("[data-tab]")) {
    button.addEventListener("click", () => showTab(button.dataset.tab));
  }

  // ---------------------------------------------------------------- the eye

  // Lids: how far each closes (0 open .. 50 shut); gaze in eye units; pupil scale.
  const LOOKS = {
    idle: { top: 8, bottom: 6, x: 0, y: 0, pupil: 1 },
    listening: { top: 0, bottom: 0, x: 0, y: 0, pupil: 1.3 },
    thinking: { top: 14, bottom: 4, x: 9, y: -13, pupil: 0.9 },
    sleep: { top: 47, bottom: 6, x: 0, y: 6, pupil: 0.8 },
    neutral: { top: 8, bottom: 6, x: 0, y: 0, pupil: 1 },
    happy: { top: 10, bottom: 24, x: 0, y: -2, pupil: 1.1 },
    sad: { top: 22, bottom: 4, x: 0, y: 9, pupil: 0.9 },
    surprised: { top: 0, bottom: 0, x: 0, y: 0, pupil: 1.4 },
    sleepy: { top: 32, bottom: 8, x: 0, y: 4, pupil: 0.9 },
    curious: { top: 4, bottom: 4, x: 7, y: -11, pupil: 1.15 },
  };
  LOOKS.thinking_emotion = { top: 16, bottom: 4, x: -10, y: -9, pupil: 0.9 };

  const eye = { state: "idle", emotion: "neutral", online: false, blinking: false };

  function currentLook() {
    if (!eye.online) return LOOKS.sleep;
    if (eye.state === "speaking") {
      return eye.emotion === "thinking" ? LOOKS.thinking_emotion : LOOKS[eye.emotion] || LOOKS.idle;
    }
    return LOOKS[eye.state] || LOOKS.idle;
  }

  function drawEye() {
    const look = currentLook();
    const top = eye.blinking ? 50 : look.top;
    const bottom = eye.blinking ? 0 : look.bottom;
    $("lid-top").style.transform = `translateY(${top}px)`;
    $("lid-bottom").style.transform = `translateY(${-bottom}px)`;
    $("gaze").style.transform = `translate(${look.x}px, ${look.y}px)`;
    $("pupil").style.transform = `scale(${look.pupil})`;
    $("eye").classList.toggle("speaking", eye.online && eye.state === "speaking");
  }

  function blink() {
    if (eye.online && eye.state === "idle" && !eye.blinking) {
      eye.blinking = true;
      drawEye();
      setTimeout(() => {
        eye.blinking = false;
        drawEye();
      }, 140);
    }
    setTimeout(blink, 2500 + Math.random() * 4000);
  }

  function setState(name) {
    eye.state = name;
    $("state").textContent = name;
    $("state").className = `state ${name}`;
    drawEye();
  }

  function setEmotion(name) {
    eye.emotion = name;
    $("emotion").textContent = name ? `[${name}]` : "";
    drawEye();
  }

  // ---------------------------------------------------------------- live lists

  function addItem(listId, emptyId, item) {
    const list = $(listId);
    const stick = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
    list.append(item);
    while (list.children.length > MAX_ITEMS) list.firstElementChild.remove();
    $(emptyId).hidden = true;
    if (stick) list.scrollTop = list.scrollHeight;
  }

  function clearLists() {
    $("transcript").replaceChildren();
    $("actions").replaceChildren();
    $("transcript-empty").hidden = false;
    $("actions-empty").hidden = false;
  }

  function say(who, text, meta, ts) {
    addItem(
      "transcript",
      "transcript-empty",
      el(
        "li",
        { class: `msg ${who}` },
        el("span", { class: "who", text: who === "you" ? "You" : who === "note" ? "" : "CARL" }),
        el("span", { class: "text", text }),
        el("span", { class: "meta", text: [time(ts), meta].filter(Boolean).join(" · ") }),
      ),
    );
  }

  function ms(value) {
    return typeof value === "number" ? `${(value / 1000).toFixed(1)} s` : "–";
  }

  function action(event) {
    addItem(
      "actions",
      "actions-empty",
      el(
        "li",
        { class: `action ${event.outcome}` },
        el(
          "div",
          { class: "head" },
          el("span", { class: "tool", text: event.tool }),
          el("span", { class: "outcome", text: event.outcome }),
          el("span", { class: "meta", text: time(event.ts) }),
        ),
        el("code", { class: "args", text: event.args }),
        el("div", { class: "result", text: event.result }),
      ),
    );
  }

  function handle(event) {
    switch (event.type) {
      case "state":
        setState(event.name);
        break;
      case "emotion":
        setEmotion(event.name);
        break;
      case "heard":
        say("you", event.text, `heard in ${ms(event.stt_ms)}`, event.ts);
        break;
      case "reply":
        say(
          "carl",
          event.text,
          `[${event.emotion}] · first word ${ms(event.first_token_ms)} · first sound ${ms(event.first_audio_ms)}`,
          event.ts,
        );
        break;
      case "line":
        say("carl line", event.text, `[${event.emotion}] signature line`, event.ts);
        break;
      case "interrupted":
        say("note", "Interrupted", "", event.ts);
        break;
      case "action":
        action(event);
        break;
      case "robot":
        loadStatus();
        break;
      default:
        break;
    }
  }

  // ---------------------------------------------------------------- events socket

  let socket = null;
  let delay = 1000;
  let lastId = 0;

  function setOnline(online) {
    eye.online = online;
    const conn = $("conn");
    conn.textContent = online ? "live" : "offline, retrying";
    conn.className = `conn ${online ? "on" : "off"}`;
    if (!online) $("state").textContent = "not connected";
    drawEye();
  }

  function connect() {
    socket = new WebSocket(`ws://${location.host}/api/events`);
    socket.addEventListener("open", () => {
      delay = 1000;
      lastId = 0;
      clearLists(); // the server re-sends its recent history
      setOnline(true);
      loadStatus();
    });
    socket.addEventListener("message", (message) => {
      let event;
      try {
        event = JSON.parse(message.data);
      } catch (_) {
        return;
      }
      if (typeof event.id === "number") {
        if (event.id <= lastId) return; // already shown
        lastId = event.id;
      }
      handle(event);
    });
    socket.addEventListener("close", () => {
      setOnline(false);
      setTimeout(connect, delay);
      delay = Math.min(delay * 2, 30000);
    });
  }

  // ---------------------------------------------------------------- status bar

  function chip(label, on, title) {
    return el("li", { class: `chip ${on ? "on" : "off"}`, text: label, title });
  }

  async function loadStatus() {
    try {
      const s = await api("GET", "/api/status");
      const robot = s.robot || {};
      const robotText = !robot.enabled
        ? "robot off"
        : robot.connected
          ? `robot linked${robot.fw_version ? ` (${robot.fw_version})` : ""}`
          : "robot not linked";
      $("status").replaceChildren(
        chip(`voice: ${s.voice}`, true),
        chip(s.music ? "music on" : "music off", s.music),
        chip(s.notes ? "notes on" : "notes off", s.notes),
        chip(s.computer_control ? "computer control on" : "computer control off", s.computer_control),
        chip(robotText, robot.connected),
        chip(`v${s.version}`, true),
      );
      if (s.state && eye.online) setState(s.state);
    } catch (_) {
      // offline: the socket's retry brings it back
    }
  }

  // ---------------------------------------------------------------- settings

  const SOURCE_TEXT = { dashboard: "set here", env: "from .env", default: "default" };

  async function loadSettings() {
    try {
      renderSettings(await api("GET", "/api/settings"));
    } catch (error) {
      setBanner("settings", `Couldn't load the settings: ${error.message}`);
    }
  }

  function settingInput(item) {
    if (item.kind === "switch") {
      return el("input", { type: "checkbox", checked: Boolean(item.value), "aria-label": item.label });
    }
    if (item.kind === "choice") {
      const select = el("select", { "aria-label": item.label });
      for (const choice of item.choices) {
        select.append(el("option", { value: choice, text: choice, selected: choice === item.value }));
      }
      return select;
    }
    const number = item.kind === "int" || item.kind === "float";
    return el("input", {
      type: number ? "number" : "text",
      step: item.kind === "float" ? "0.05" : number ? "1" : undefined,
      value: item.value === null || item.value === undefined ? "" : String(item.value),
      maxLength: 300,
      spellcheck: false,
      "aria-label": item.label,
    });
  }

  function settingValue(item, input) {
    if (item.kind === "switch") return input.checked;
    if (item.kind === "int") return input.value === "" ? "" : Number.parseInt(input.value, 10);
    if (item.kind === "float") return input.value === "" ? "" : Number.parseFloat(input.value);
    return input.value;
  }

  function renderSettings(data) {
    setBanner("settings", data.error);
    const groups = new Map();
    for (const item of data.settings) {
      if (!groups.has(item.group)) groups.set(item.group, []);
      groups.get(item.group).push(item);
    }
    const container = $("settings");
    container.replaceChildren();
    for (const [group, items] of groups) {
      const fieldset = el("fieldset", { class: "panel" }, el("legend", { text: group }));
      for (const item of items) fieldset.append(settingRow(item));
      container.append(fieldset);
    }
  }

  function settingRow(item) {
    const input = settingInput(item);
    const message = el("span", { class: "msg" });
    const reset = el("button", {
      type: "button",
      class: "secondary",
      text: "Reset",
      hidden: item.source !== "dashboard",
      title: "Back to .env or the default",
    });
    const save = async () => {
      message.textContent = "Saving…";
      message.className = "msg";
      try {
        renderSettings(await api("PUT", "/api/settings", { [item.key]: settingValue(item, input) }));
      } catch (error) {
        const errors = (error.data && error.data.errors) || {};
        message.textContent = errors[item.key] || error.message;
        message.className = "msg error";
      }
    };
    input.addEventListener("change", save);
    reset.addEventListener("click", async () => {
      try {
        renderSettings(await api("DELETE", `/api/settings/${encodeURIComponent(item.key)}`));
      } catch (error) {
        message.textContent = error.message;
        message.className = "msg error";
      }
    });
    return el(
      "div",
      { class: "setting" },
      el("label", { class: "label", text: item.label, title: item.key }),
      input,
      el("span", { class: `source ${item.source}`, text: SOURCE_TEXT[item.source] || item.source }),
      el("span", { class: "applies", text: `applies: ${item.applies}` }),
      reset,
      message,
    );
  }

  // ---------------------------------------------------------------- personality

  const LINE_LABELS = {
    greeting: "Greeting (startup, robot connect)",
    farewell: "Farewell (on Ctrl+C)",
    pondering: "Pondering (a slow reply)",
    task_done: "Task done (sometimes, after an action)",
    error: "Error (no reply possible)",
    refusal: "Refusal",
    forget: "Fresh start (after “forget that”)",
  };
  let personality = null;

  async function loadPersonality() {
    try {
      renderPersonality(await api("GET", "/api/personality"));
    } catch (error) {
      setBanner("personality", `Couldn't load the personality: ${error.message}`);
    }
  }

  function renderPersonality(data) {
    personality = data;
    setBanner("personality", data.error);
    const character = $("character");
    character.maxLength = data.limits.character;
    character.value = data.character.value;
    $("character-reset").hidden = !data.character.custom;
    $("character-msg").textContent = data.character.custom ? "custom" : "default";
    $("character-msg").className = "msg";
    updateCount();
    const lines = $("lines");
    lines.replaceChildren();
    for (const [name, line] of Object.entries(data.lines)) lines.append(lineRow(name, line, data));
    $("preview").textContent = data.preview;
  }

  function updateCount() {
    if (!personality) return;
    const n = $("character").value.length;
    $("character-count").textContent = `${n} / ${personality.limits.character}`;
  }

  function lineRow(name, line, data) {
    const text = el("input", {
      type: "text",
      value: line.text,
      maxLength: data.limits.line,
      "aria-label": LINE_LABELS[name] || name,
    });
    const emotion = el("select", { "aria-label": `${name} emotion` });
    for (const e of data.emotions) emotion.append(el("option", { value: e, text: e, selected: e === line.emotion }));
    const message = el("span", { class: "msg", text: line.custom ? "custom" : "" });
    const save = el("button", { type: "button", text: "Save" });
    const reset = el("button", { type: "button", class: "secondary", text: "Reset", hidden: !line.custom });
    save.addEventListener("click", async () => {
      try {
        renderPersonality(
          await api("PUT", "/api/personality", { lines: { [name]: { text: text.value, emotion: emotion.value } } }),
        );
      } catch (error) {
        const errors = (error.data && error.data.errors) || {};
        message.textContent = errors[`lines.${name}`] || error.message;
        message.className = "msg error";
      }
    });
    reset.addEventListener("click", async () => {
      try {
        renderPersonality(await api("DELETE", `/api/personality/${encodeURIComponent(name)}`));
      } catch (error) {
        message.textContent = error.message;
        message.className = "msg error";
      }
    });
    return el(
      "div",
      { class: "line" },
      el("label", { class: "label", text: LINE_LABELS[name] || name }),
      text,
      emotion,
      save,
      reset,
      message,
      el("span", { class: "default", text: `default: ${line.default.text}` }),
    );
  }

  $("character").addEventListener("input", updateCount);
  $("character-save").addEventListener("click", async () => {
    const message = $("character-msg");
    try {
      renderPersonality(await api("PUT", "/api/personality", { character: $("character").value }));
      message.textContent = "saved: applies at the next question";
    } catch (error) {
      const errors = (error.data && error.data.errors) || {};
      message.textContent = errors.character || error.message;
      message.className = "msg error";
    }
  });
  $("character-reset").addEventListener("click", async () => {
    try {
      renderPersonality(await api("DELETE", "/api/personality/character"));
    } catch (error) {
      $("character-msg").textContent = error.message;
      $("character-msg").className = "msg error";
    }
  });

  // ---------------------------------------------------------------- start

  setOnline(false);
  showTab(location.hash.slice(1));
  connect();
  setTimeout(blink, 3000);
  setInterval(() => {
    if (!document.hidden) loadStatus();
  }, 10000);
})();
