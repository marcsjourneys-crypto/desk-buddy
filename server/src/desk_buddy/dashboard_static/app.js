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
    // Every request carries the token: changes need it, and so does listing the apps.
    const options = { method, headers: { "X-CARL-Token": TOKEN } };
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

  const loaders = { settings: loadSettings, personality: loadPersonality, apps: loadApps };
  function showTab(name) {
    if (!["live", "settings", "personality", "apps"].includes(name)) name = "live";
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

  const SOURCE_TEXT = {
    dashboard: "set here",
    env: "from .env",
    default: "default",
    invalid: "saved value not usable",
  };

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
    // A saved value that no longer works (say, a deleted folder): CARL uses the fallback
    // shown, and says why; fix it or reset it.
    const message = el("span", { class: item.error ? "msg error" : "msg", text: item.error || "" });
    const reset = el("button", {
      type: "button",
      class: "secondary",
      text: "Reset",
      hidden: item.source !== "dashboard" && item.source !== "invalid",
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
  const ABOUT_LABELS = {
    name: "Name",
    city: "Town or city",
    region: "Region or state",
    country: "Country (two letters, like GB)",
    notes: "About me",
  };
  let personality = null;

  async function loadPersonality() {
    try {
      renderPersonality(await api("GET", "/api/personality"));
    } catch (error) {
      setBanner("personality", `Couldn't load the personality: ${error.message}`);
    }
  }

  // `saved`: the About field just saved or cleared. Other About fields keep any edits
  // that weren't saved yet, so saving one field never loses another.
  function renderPersonality(data, saved = null) {
    personality = data;
    const unsaved = {};
    for (const input of $("about").querySelectorAll("[data-about]")) {
      const name = input.getAttribute("data-about");
      if (name !== saved && input.value !== input.getAttribute("data-saved")) unsaved[name] = input.value;
    }
    setBanner("personality", data.error);
    const character = $("character");
    character.maxLength = data.limits.character;
    character.value = data.character.value;
    $("character-reset").hidden = !data.character.custom;
    $("character-msg").textContent = data.character.custom ? "custom" : "default";
    $("character-msg").className = "msg";
    updateCount();
    const about = $("about");
    about.replaceChildren();
    for (const [name, label] of Object.entries(ABOUT_LABELS)) {
      about.append(aboutRow(name, label, data.about[name] || "", data.limits.about[name], unsaved[name]));
    }
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

  function aboutRow(name, label, value, limit, unsaved) {
    const shown = unsaved === undefined ? value : unsaved;
    const attrs = { value: shown, maxLength: limit, "aria-label": label, "data-about": name, "data-saved": value };
    const input =
      name === "notes"
        ? el("textarea", { rows: 4, spellcheck: true, ...attrs })
        : el("input", { type: "text", spellcheck: false, ...attrs });
    const status = unsaved !== undefined ? "not saved yet" : value ? "" : "not set";
    const message = el("span", { class: "msg", text: status });
    const save = el("button", { type: "button", text: "Save" });
    const reset = el("button", { type: "button", class: "secondary", text: "Clear", hidden: !value });
    save.addEventListener("click", async () => {
      try {
        renderPersonality(await api("PUT", "/api/personality", { about: { [name]: input.value } }), name);
      } catch (error) {
        const errors = (error.data && error.data.errors) || {};
        message.textContent = errors[`about.${name}`] || error.message;
        message.className = "msg error";
      }
    });
    reset.addEventListener("click", async () => {
      try {
        renderPersonality(await api("DELETE", `/api/personality/${encodeURIComponent(`about.${name}`)}`), name);
      } catch (error) {
        message.textContent = error.message;
        message.className = "msg error";
      }
    });
    return el(
      "div",
      { class: `about-field${name === "notes" ? " notes" : ""}` },
      el("label", { class: "label", text: label }),
      input,
      save,
      reset,
      message,
    );
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

  // ---------------------------------------------------------------- apps and websites

  // Working copies, edited on the page until saved. App titles come from Windows and
  // bookmark names from the user: all of it is shown as text only.
  const apps = { chosen: [], found: null, dirty: false, limits: { apps: 50, names: 3, name: 40 } };
  const sites = { list: [], dirty: false, limits: { bookmarks: 50, name: 40, url: 500 } };

  async function loadApps() {
    try {
      const [appsView, sitesView] = await Promise.all([api("GET", "/api/apps"), api("GET", "/api/bookmarks")]);
      setBanner("apps", appsView.error);
      if (!apps.dirty) setChosen(appsView);
      else apps.limits = appsView.limits;
      if (!sites.dirty) setSites(sitesView);
    } catch (error) {
      setBanner("apps", `Couldn't load the apps: ${error.message}`);
    }
  }

  function setChosen(view) {
    setBanner("apps", view.error);
    apps.limits = view.limits;
    apps.chosen = view.apps.map((a) => ({ app_id: a.app_id, title: a.title, names: a.names.join(", "), error: "" }));
    apps.dirty = false;
    renderChosen();
    renderFound();
  }

  function setSites(view) {
    setBanner("apps", view.error);
    sites.limits = view.limits;
    sites.list = view.bookmarks.map((b) => ({ name: b.name, url: b.url, error: "" }));
    sites.dirty = false;
    renderSites();
  }

  // A first spoken name from an app's title: lowercase, only the allowed characters.
  function spokenName(title) {
    const name = title
      .toLowerCase()
      .replace(/[^\p{L}0-9 \-'.]+/gu, " ")
      .replace(/\s+/g, " ")
      .trim();
    return name.slice(0, apps.limits.name).trim();
  }

  function splitNames(text) {
    return text
      .split(",")
      .map((n) => n.trim())
      .filter(Boolean);
  }

  function appsChanged() {
    apps.dirty = true;
    $("apps-msg").textContent = "not saved yet";
    $("apps-msg").className = "msg";
  }

  function renderChosen() {
    const list = $("apps-chosen");
    list.replaceChildren();
    apps.chosen.forEach((app) => {
      const names = el("input", {
        type: "text",
        value: app.names,
        spellcheck: false,
        maxLength: (apps.limits.name + 2) * apps.limits.names,
        "aria-label": `Spoken names for ${app.title}`,
      });
      names.addEventListener("input", () => {
        app.names = names.value;
        appsChanged();
      });
      const remove = el("button", { type: "button", class: "secondary", text: "Remove" });
      remove.addEventListener("click", () => {
        apps.chosen = apps.chosen.filter((a) => a !== app);
        appsChanged();
        renderChosen();
        renderFound();
      });
      list.append(
        el(
          "li",
          { class: "app" },
          el("span", { class: "label", text: app.title, title: app.app_id }),
          names,
          remove,
          el("span", { class: app.error ? "msg error" : "msg", text: app.error }),
        ),
      );
    });
    $("apps-empty").hidden = apps.chosen.length > 0;
    $("apps-count").textContent = `${apps.chosen.length} / ${apps.limits.apps}`;
  }

  function renderFound() {
    const list = $("apps-found");
    if (apps.found === null) return;
    const filter = $("apps-filter").value.trim().toLowerCase();
    const ticked = new Set(apps.chosen.map((a) => a.app_id));
    list.replaceChildren();
    for (const app of apps.found) {
      if (filter && !app.name.toLowerCase().includes(filter)) continue;
      const box = el("input", { type: "checkbox", checked: ticked.has(app.app_id), "aria-label": app.name });
      box.addEventListener("change", () => {
        if (box.checked) {
          if (apps.chosen.length >= apps.limits.apps) {
            box.checked = false;
            $("apps-msg").textContent = `At most ${apps.limits.apps} apps.`;
            $("apps-msg").className = "msg error";
            return;
          }
          apps.chosen.push({ app_id: app.app_id, title: app.name, names: spokenName(app.name), error: "" });
        } else {
          apps.chosen = apps.chosen.filter((a) => a.app_id !== app.app_id);
        }
        appsChanged();
        renderChosen();
      });
      list.append(el("li", { class: "found-app" }, el("label", {}, box, el("span", { text: app.name }))));
    }
  }

  $("apps-find").addEventListener("click", async () => {
    const message = $("apps-find-msg");
    const button = $("apps-find");
    message.textContent = "Asking Windows…";
    message.className = "msg";
    button.disabled = true;
    try {
      apps.found = (await api("GET", "/api/apps/available")).apps;
      message.textContent = `${apps.found.length} apps found. Tick the ones CARL may open.`;
      $("apps-filter").hidden = false;
      $("apps-found").hidden = false;
      renderFound();
    } catch (error) {
      message.textContent = error.message;
      message.className = "msg error";
    } finally {
      button.disabled = false;
    }
  });
  $("apps-filter").addEventListener("input", renderFound);

  $("apps-save").addEventListener("click", async () => {
    const message = $("apps-msg");
    for (const app of apps.chosen) app.error = "";
    try {
      const body = { apps: apps.chosen.map((a) => ({ app_id: a.app_id, names: splitNames(a.names) })) };
      setChosen(await api("PUT", "/api/apps", body));
      message.textContent = "saved: applies at the next question";
      message.className = "msg";
    } catch (error) {
      const errors = (error.data && error.data.errors) || {};
      apps.chosen.forEach((app, i) => {
        app.error = errors[`apps.${i}`] || "";
      });
      renderChosen();
      message.textContent = errors.apps || (error.data && error.data.errors ? "Not saved: see above." : error.message);
      message.className = "msg error";
    }
  });

  function sitesChanged() {
    sites.dirty = true;
    $("bookmarks-msg").textContent = "not saved yet";
    $("bookmarks-msg").className = "msg";
  }

  function renderSites() {
    const list = $("bookmarks");
    list.replaceChildren();
    sites.list.forEach((site) => {
      const name = el("input", {
        type: "text",
        value: site.name,
        placeholder: "spoken name",
        spellcheck: false,
        maxLength: sites.limits.name,
        "aria-label": "Spoken name",
      });
      const url = el("input", {
        type: "text",
        value: site.url,
        placeholder: "https://…",
        spellcheck: false,
        maxLength: sites.limits.url,
        "aria-label": "Address",
      });
      name.addEventListener("input", () => {
        site.name = name.value;
        sitesChanged();
      });
      url.addEventListener("input", () => {
        site.url = url.value;
        sitesChanged();
      });
      const remove = el("button", { type: "button", class: "secondary", text: "Remove" });
      remove.addEventListener("click", () => {
        sites.list = sites.list.filter((s) => s !== site);
        sitesChanged();
        renderSites();
      });
      list.append(
        el(
          "li",
          { class: "site" },
          name,
          url,
          remove,
          el("span", { class: site.error ? "msg error" : "msg", text: site.error }),
        ),
      );
    });
    $("bookmarks-empty").hidden = sites.list.length > 0;
    $("bookmarks-count").textContent = `${sites.list.length} / ${sites.limits.bookmarks}`;
    $("bookmark-add").disabled = sites.list.length >= sites.limits.bookmarks;
  }

  $("bookmark-add").addEventListener("click", () => {
    sites.list.push({ name: "", url: "", error: "" });
    sitesChanged();
    renderSites();
    const inputs = $("bookmarks").querySelectorAll("input");
    if (inputs.length >= 2) inputs[inputs.length - 2].focus();
  });

  $("bookmarks-save").addEventListener("click", async () => {
    const message = $("bookmarks-msg");
    for (const site of sites.list) site.error = "";
    try {
      const body = { bookmarks: sites.list.map((s) => ({ name: s.name, url: s.url.trim() })) };
      setSites(await api("PUT", "/api/bookmarks", body));
      message.textContent = "saved: applies at the next question";
      message.className = "msg";
    } catch (error) {
      const errors = (error.data && error.data.errors) || {};
      sites.list.forEach((site, i) => {
        site.error = errors[`bookmarks.${i}`] || "";
      });
      renderSites();
      message.textContent =
        errors.bookmarks || (error.data && error.data.errors ? "Not saved: see above." : error.message);
      message.className = "msg error";
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
