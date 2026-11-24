// The box's sections, added to the unmodified chia-testnet-monitor page: services, plots, setup, RPC console, logs.
// Everything from the network, logs or files goes through textContent, never innerHTML.
(() => {
  const NS = "http://www.w3.org/2000/svg";
  const el = (tag, props = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (k === "class") n.className = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (k === "tip") { n.setAttribute("data-tip-title", v[0]); n.setAttribute("data-tip", v[1]); }
      else if (v != null && v !== false) n.setAttribute(k, v === true ? "" : v);
    }
    n.append(...kids.filter((k) => k != null && k !== false));
    return n;
  };
  const svg = (tag, props = {}, ...kids) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(props)) n.setAttribute(k, v);
    n.append(...kids);
    return n;
  };
  const byId = (id) => document.getElementById(id);
  const gb = (b) => (b == null ? "–" : b < 1e6 ? `${Math.round(b / 1e3)} kB` : b < 1e9 ? `${Math.round(b / 1e6)} MB` : `${(b / 1e9).toFixed(b < 1e10 ? 2 : 1)} GB`);
  const ago = (s) => (s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : s < 172800 ? `${(s / 3600).toFixed(1)} h` : `${(s / 86400).toFixed(1)} d`);

  let box = null;

  // Where the sections go. If upstream's layout changes, they fall back to the end of the page.
  const main = document.querySelector("main") ?? document.body;
  const block = (id, label, cls = "box-block") => el("section", { id, class: cls, "aria-label": label });
  (document.querySelector(".hero") ?? main).before(el("div", { id: "box-setup" }));
  main.prepend(block("box-services", "Services"));
  (document.querySelector("main > .lower") ?? main.lastElementChild).before(block("box-garden", "Your plots"));
  main.after(el("div", { id: "box-more", class: "wrap" }), el("div", { id: "box-drop" }));

  // ---- talking to the server ----
  const token = () => localStorage.getItem("box-token") || "";
  const auth = () => (token() ? { Authorization: `Bearer ${token()}` } : {});
  function needToken() {
    const t = prompt("This box has an API_TOKEN set. Paste it here:");
    if (t) localStorage.setItem("box-token", t.trim());
    return Boolean(t);
  }
  async function send(method, path, body, headers = {}) {
    if (box?.token_required && !token() && !needToken()) throw new Error("An API token is needed.");
    const r = await fetch(path, { method, body, headers: { ...headers, ...auth() } });
    const text = await r.text();
    if (r.status === 401) localStorage.removeItem("box-token");
    let data;
    try { data = JSON.parse(text); } catch { data = { success: false, error: text.slice(0, 300) || `HTTP ${r.status}` }; }
    return data;
  }
  let toastTimer;
  function toast(msg, keep = false) {
    document.querySelector(".box-toast")?.remove();
    document.body.append(el("div", { class: "box-toast", role: "status" }, msg));
    clearTimeout(toastTimer);
    if (!keep) toastTimer = setTimeout(() => document.querySelector(".box-toast")?.remove(), 6000);
  }
  const copy = (text) => el("button", {
    class: "copy", type: "button", title: "Copy",
    onclick: () => navigator.clipboard.writeText(text).then(() => toast("Copied."), () => toast("Couldn't copy.")),
  }, text);

  // ---- services ----
  const LABELS = { daemon: "Daemon", full_node: "Full node", solver: "Solver", farmer: "Farmer", harvester: "Harvester", wallet: "Wallet", monitor: "Monitor" };
  const JOBS = {
    daemon: "Holds the testnet key for the other services.", full_node: "Follows the chain.", solver: "Turns v2 partial proofs into full ones.",
    farmer: "Submits winning proofs.", harvester: "Looks proofs up in your plots.", wallet: "Holds your TXCH.", monitor: "Collects what this page shows.",
  };
  // Sprout when up, seedling when starting, drooping stem when down.
  function plant(state, n) {
    const g = svg("svg", { class: "plant", viewBox: "0 0 72 92", "aria-hidden": "true" });
    const green = "var(--v2)", dead = "var(--ink-3)";
    const pot = [
      svg("path", { d: "M18 60h36l-5 28a4 4 0 0 1-4 4H27a4 4 0 0 1-4-4Z", fill: "var(--ink-2)" }),
      svg("rect", { x: 14, y: 54, width: 44, height: 9, rx: 3, fill: "var(--ink)" }),
    ];
    const face = (mood) => {
      const c = "var(--paper)";
      const eyes = mood === "down"
        ? [svg("path", { d: "M28 72l4 4m0-4l-4 4M40 72l4 4m0-4l-4 4", stroke: c, "stroke-width": 1.6, "stroke-linecap": "round" })]
        : mood === "starting"
          ? [svg("path", { d: "M28 74h4M40 74h4", stroke: c, "stroke-width": 1.8, "stroke-linecap": "round" })]
          : [svg("circle", { cx: 30, cy: 73, r: 1.9, fill: c }), svg("circle", { cx: 42, cy: 73, r: 1.9, fill: c })];
      const mouth = mood === "down" ? "M32 84q4-4 8 0" : mood === "starting" ? "M34 82h4" : "M32 80q4 4 8 0";
      return [...eyes, svg("path", { d: mouth, stroke: c, "stroke-width": 1.6, fill: "none", "stroke-linecap": "round" })];
    };
    if (state === "down") {
      g.append(svg("path", { d: "M36 56C36 38 44 34 54 42", stroke: dead, "stroke-width": 3, fill: "none", "stroke-linecap": "round" }),
        svg("path", { d: "M54 42q6 2 5 11q-7-2-5-11Z", fill: dead }));
    } else {
      const leaf = svg("g", { class: "leaf" });
      const tall = state === "up";
      leaf.append(svg("path", { d: tall ? "M36 56V24" : "M36 56V44", stroke: green, "stroke-width": 3, "stroke-linecap": "round" }));
      if (tall) {
        leaf.append(
          svg("path", { d: "M36 32C24 32 18 24 17 14c12 0 19 6 19 18Z", fill: green }),
          svg("path", { d: "M36 28C47 28 53 20 54 9C42 9 36 16 36 28Z", fill: green }),
        );
        if (n % 2) leaf.append(svg("path", { d: "M36 46c8 0 13-4 14-11c-9 0-14 4-14 11Z", fill: green, "fill-opacity": ".75" }));
        else leaf.append(svg("path", { d: "M36 46c-8 0-13-4-14-11c9 0 14 4 14 11Z", fill: green, "fill-opacity": ".75" }));
      } else leaf.append(svg("path", { d: "M36 46c-6 0-9-3-10-8c7 0 10 3 10 8ZM36 46c6 0 9-3 10-8c-7 0-10 3-10 8Z", fill: green }));
      g.append(leaf);
    }
    g.append(...pot, ...face(state));
    return g;
  }
  function spark(values, title, body) {
    const n = svg("svg", { class: "mini", viewBox: "0 0 100 22", preserveAspectRatio: "none", role: "img", "aria-label": `${title}: ${body}` });
    if (values.length > 1) {
      const max = Math.max(1, ...values);
      const pts = values.map((v, i) => `${((i / (values.length - 1)) * 100).toFixed(1)},${(20 - (v / max) * 18).toFixed(1)}`).join(" ");
      n.append(svg("polyline", { points: pts, fill: "none", stroke: "var(--ink-3)", "stroke-width": 1.5, "vector-effect": "non-scaling-stroke", "stroke-linejoin": "round" }));
    }
    n.append(svg("rect", { width: 100, height: 22, fill: "transparent", "data-tip-title": title, "data-tip": body }));
    return n;
  }
  const potState = new Map();
  function renderServices() {
    const host = byId("box-services");
    if (!box.services.length) { host.replaceChildren(); potState.clear(); return; }
    if (!host.firstChild) host.append(el("div", { class: "head" }, el("h2", {}, "Services"), el("span", { class: "key" })), el("div", { class: "shelf" }));
    const shelf = host.querySelector(".shelf");
    const up = box.services.filter((s) => s.state === "up").length;
    const totalRss = box.services.reduce((a, s) => a + (s.rss.at(-1) || 0), 0);
    host.querySelector(".key").textContent = `${up} of ${box.services.length} up · ${gb(totalRss)} memory · box running ${ago(box.now - box.started)}`;
    box.services.forEach((s, i) => {
      let p = potState.get(s.name);
      if (!p) {
        p = { node: el("div", { class: "pot" }), state: null };
        potState.set(s.name, p);
        shelf.append(p.node);
      }
      if (p.state !== s.state) {  // rebuild only on a change, so the sway doesn't restart every poll
        p.state = s.state;
        p.node.replaceChildren(
          el("div", { tip: [LABELS[s.name] ?? s.name, JOBS[s.name] ?? ""] }, plant(s.state, i)),
          el("div", { class: "plank" }), el("b", {}, LABELS[s.name] ?? s.name),
          el("div", { class: "st" }), el("div", { class: "sub" }), el("div", { class: "sp" }),
          el("button", { class: "btn small", type: "button", onclick: () => restart(s.name) }, "Restart"),
        );
      }
      const st = p.node.querySelector(".st");
      st.className = `st ${s.state === "down" ? "crit" : s.state === "starting" ? "warn" : ""}`;
      st.textContent = s.state === "up" ? `Up ${ago(s.uptime)}` : s.state === "starting" ? "▲ Starting" : `✕ Down${s.last_exit != null ? ` (exit ${s.last_exit})` : ""}`;
      if (s.restarts) st.textContent += ` · ${s.restarts} restart${s.restarts === 1 ? "" : "s"}`;
      p.node.querySelector(".sub").textContent = s.state === "down" ? "" : `${(s.cpu.at(-1) ?? 0).toFixed(0)}% CPU · ${gb(s.rss.at(-1))}`;
      const peak = Math.max(0, ...s.cpu);
      p.node.querySelector(".sp").replaceChildren(spark(s.cpu, `${LABELS[s.name] ?? s.name} CPU`,
        `Now ${(s.cpu.at(-1) ?? 0).toFixed(0)}%, peak ${peak.toFixed(0)}% over ${ago(s.cpu.length * 5)}\n${s.out.length && s.state === "down" ? s.out.at(-1) : "100% is one core"}`));
    });
  }
  async function restart(name) {
    try {
      const r = await send("POST", `/api/services/${name}/restart`);
      toast(r.success ? `Restarting the ${LABELS[name].toLowerCase()}.` : r.error);
    } catch (e) { toast(e.message); }
  }

  // ---- plots ----
  function sprout(p, farming) {
    const c = p.kind === "v2" ? "var(--v2)" : "var(--v1)";
    const g = svg("svg", { viewBox: "0 0 26 46", role: "img", "aria-label": `${p.kind} plot ${p.name}`,
      "data-tip-title": `${p.kind} plot, ${gb(p.size)}`, "data-tip": `${p.path}\nMade ${new Date(p.mtime * 1000).toLocaleString()}${farming ? "" : "\nNot loaded by the harvester yet"}` });
    if (!farming) g.setAttribute("opacity", ".5");
    g.append(svg("rect", { width: 26, height: 46, fill: "transparent" }), svg("path", { d: "M13 46V18", stroke: c, "stroke-width": 2.4, "stroke-linecap": "round" }));
    // v1 plots are round buds, v2 plots are leaf pairs, so the two differ by shape as well as color.
    if (p.kind === "v1") g.append(svg("circle", { cx: 13, cy: 13, r: 8, fill: c }));
    else g.append(svg("path", { d: "M13 26C5 26 1 20 1 12c8 0 12 5 12 14ZM13 21c7 0 11-5 12-13c-8 0-12 5-12 13Z", fill: c }));
    return g;
  }
  let gardenKey = "";
  function renderGarden() {
    const host = byId("box-garden");
    if (!box.network) { host.replaceChildren(); gardenKey = ""; return; }
    const v1 = box.plots.filter((p) => p.kind === "v1"), v2 = box.plots.filter((p) => p.kind === "v2");
    const v2gb = v2.reduce((a, p) => a + p.size, 0) / 1e9;
    const loaded = state?.farm?.plots ?? 0;
    const job = box.plot_job;
    const key = JSON.stringify([box.plots.map((p) => p.path), loaded, job?.line, box.v2_waiting, Math.round((box.now - (job?.started ?? 0)) / 10), box.network]);
    if (key === gardenKey) return;
    gardenKey = key;
    const bed = el("div", { class: "bed" });
    if (!box.plots.length) bed.append(el("p", {}, job ? "The first plot is on its way." : box.auto_v1_plots || box.auto_v2_plots ? "No plots yet. Starter plots begin once the node is synced." : "No plots yet."));
    // Very large farms show their newest 400.
    bed.append(...box.plots.slice(0, 400).map((p) => sprout(p, loaded >= box.plots.length || p.kind === "v1")));
    const notes = [];
    if (job) notes.push(el("p", { class: "box-note" }, `Making a ${job.kind} k${job.k} plot for ${ago(box.now - job.started)}. ${job.line ?? ""}`));
    if (box.v2_waiting) notes.push(el("p", { class: "box-note" }, `Not making starter v2 plots: ${box.v2_waiting}. You can make them in the browser instead.`));
    if (v2gb > box.v2_limit_gb) notes.push(el("p", { class: "box-note warn" }, `▲ ${v2gb.toFixed(1)} GB of v2 plots is over the ${box.v2_limit_gb} GB the testnet asks for.`));
    if (v2.length && !state?.services?.solver && state?.ready) notes.push(el("p", { class: "box-note warn" }, "▲ The solver isn't connected to the farmer, so v2 plots can't win."));
    host.replaceChildren(
      el("div", { class: "head" }, el("h2", {}, "Garden"),
        el("span", { class: "key" }, `${v1.length} v1 (round) and ${v2.length} v2 (leaf), ${v2gb.toFixed(1)} of ${box.v2_limit_gb} GB v2 allowance used`)),
      bed, ...notes,
      el("div", { class: "box-links" },
        el("a", { class: "btn main", href: "/plotter/", target: "_blank" }, "Make v2 plots in the browser"),
        el("label", { class: "btn" }, "Add plot files",
          el("input", { type: "file", accept: ".plot,.gplot", multiple: true, hidden: true, onchange: (e) => handleFiles([...e.target.files]) }))),
      el("p", { class: "box-note" }, "Plots live in the plots folder next to docker-compose.yml and carry over to every new testnet, since they share this box's key."),
    );
  }

  // ---- setup ----
  const pickConfig = (text, main) => el("label", { class: `btn ${main ? "main" : ""}` }, text,
    el("input", { type: "file", accept: ".zip,.yaml,.yml", hidden: true, onchange: (e) => handleFiles([...e.target.files]) }));
  let setupKey = "";
  // While the node catches up, its height says nothing about the fork, so show the sync instead of the countdown.
  const heroText = ["hero-label", "cd", "hero-caption"].map(byId).filter(Boolean);
  const sync = el("div", { class: "sync", hidden: true }, el("p", { class: "hero-label" }, "Catching up"), el("p", { class: "countdown" }),
    el("div", { class: "track" }, el("i")), el("p", { class: "hero-caption" }));
  heroText[0]?.before(sync);
  const seen = [];
  function renderSync() {
    const n = state?.node, peers = (state?.peers ?? []).filter((p) => p.height != null);
    const behind = Boolean(state?.ready && n && !n.synced);
    sync.hidden = !behind;
    heroText.forEach((x) => (x.hidden = behind));
    if (!behind) { seen.length = 0; return; }
    const tip = Math.max(n.sync_tip || 0, n.height, ...peers.map((p) => p.height));
    const done = tip ? n.height / tip : 0;
    seen.push({ t: state.now, h: n.height });
    while (seen.length > 2 && state.now - seen[0].t > 60) seen.shift();
    const dt = seen.at(-1).t - seen[0].t, rate = dt > 5 ? (seen.at(-1).h - seen[0].h) / dt : 0;
    sync.querySelector(".countdown").textContent = `${Math.floor(done * 100)}%`;
    sync.querySelector("i").style.width = `${done * 100}%`;
    sync.querySelector(".hero-caption").textContent = !tip ? "Looking for peers"
      : `${fmt(n.height)} of ${fmt(tip)} blocks${rate > 0.5 ? `, about ${duration((tip - n.height) / rate)} left` : n.height ? ", waiting for the next batch" : ", checking the weight proof"}`;
  }
  function renderSetup() {
    const host = byId("box-setup");
    const running = box.phase === "running";
    const mon = box.services.find((s) => s.name === "monitor");
    const silent = running && !state?.ready && (mon?.uptime ?? 0) > 60;
    const key = JSON.stringify([box.phase, box.error, silent]);
    document.querySelectorAll(".hero, main > .figures, main > .lower, main > .strength, main > .peers").forEach((n) => (n.hidden = !state?.ready));
    renderSync();
    if (key === setupKey) return;
    setupKey = key;
    if (running && !box.error && !silent) { host.replaceChildren(); return; }
    const kids = running
      ? [el("p", { class: "crit" }, box.error ? `✕ New config ignored: ${box.error}` : "✕ The monitor isn't reporting, so the charts are hidden. The services below are live.")]
      : box.phase.startsWith("waiting")
        ? [el("h2", {}, "Drop initial-config.zip here"),
           el("p", {}, "Get the latest one from the testnet Discord channel and drop it anywhere on this page, or put it in the config folder. The node, farmer, harvester, wallet and solver start by themselves."),
           box.error && el("p", { class: "crit" }, `✕ ${box.error}`),
           el("div", { class: "box-links" }, pickConfig("Choose a file", true))]
        : [el("h2", {}, box.phase[0].toUpperCase() + box.phase.slice(1)),
           el("p", {}, box.error ?? "Creating certificates and keys. This takes a few seconds."),
           box.error && el("div", { class: "box-links" }, pickConfig("Try another config", true))];
    host.replaceChildren(el("div", { class: `setup ${running ? "slim" : ""}` }, el("div", { class: "wrap" }, ...kids.filter(Boolean))));
  }

  async function handleFiles(files) {
    for (const f of files) {
      try {
        if (/\.(zip|ya?ml)$/i.test(f.name)) {
          const r = await send("PUT", "/api/config", f);
          toast(r.success ? `Switching to ${r.network}.` : `Config refused: ${r.error}`);
        } else if (/\.(plot|gplot)$/i.test(f.name)) {
          toast(`Uploading ${f.name} (${gb(f.size)})…`, true);
          const r = await send("PUT", `/api/plots/${encodeURIComponent(f.name)}`, f);
          toast(r.success ? `Added ${f.name}. The harvester loads it within 30 seconds.` : `${f.name}: ${r.error}`);
        } else toast(`${f.name} isn't a config or a plot.`);
      } catch (e) { toast(`${f.name}: ${e.message}`); }
    }
  }
  const drop = byId("box-drop");
  drop.textContent = "Drop an initial-config.zip to switch testnets, or a plot to farm it";
  let depth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types ?? [])].includes("Files");
  addEventListener("dragenter", (e) => { if (hasFiles(e)) { depth++; drop.classList.add("on"); } });
  addEventListener("dragleave", (e) => { if (hasFiles(e) && --depth <= 0) { depth = 0; drop.classList.remove("on"); } });
  addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
  addEventListener("drop", (e) => { if (!hasFiles(e)) return; e.preventDefault(); depth = 0; drop.classList.remove("on"); handleFiles([...e.dataTransfer.files]); });

  // ---- RPC console, logs, facts ----
  const SUGGEST = {
    full_node: ["get_blockchain_state", "get_network_info", "get_connections", "get_all_mempool_items", "get_fee_estimate", "get_coin_records_by_puzzle_hash", "push_tx", "get_routes"],
    wallet: ["get_wallet_balance", "get_wallets", "get_sync_status", "get_next_address", "send_transaction", "get_transactions", "get_routes"],
    farmer: ["get_harvesters_summary", "get_signage_points", "get_reward_targets", "set_reward_targets", "get_routes"],
    harvester: ["get_plots", "refresh_plots", "get_plot_directories", "get_routes"],
    solver: ["get_state", "get_connections", "get_routes"],
  };
  const DEFAULT_BODY = { get_wallet_balance: '{"wallet_id": 1}', get_next_address: '{"wallet_id": 1, "new_address": false}', get_reward_targets: '{"search_for_private_key": false}',
    send_transaction: '{"wallet_id": 1, "address": "txch1…", "amount": 1000000, "fee": 0}', get_transactions: '{"wallet_id": 1}', get_fee_estimate: '{"target_times": [60], "cost": 1000000}' };
  function buildMore() {
    const svc = el("select", { "aria-label": "Service" }, ...Object.keys(SUGGEST).map((s) => el("option", { value: s }, s)));
    const list = el("datalist", { id: "box-endpoints" });
    const method = el("input", { list: "box-endpoints", value: "get_blockchain_state", "aria-label": "Endpoint", spellcheck: "false" });
    const body = el("textarea", { "aria-label": "JSON body", spellcheck: "false" });
    body.value = "{}";
    const out = el("pre", { class: "out", "aria-live": "polite" });
    const curl = el("div", { class: "box-note" });
    const refresh = () => {
      list.replaceChildren(...SUGGEST[svc.value].map((m) => el("option", { value: m })));
      const cmd = `curl -X POST ${location.origin}/rpc/${svc.value}/${method.value}${box?.token_required ? " -H 'Authorization: Bearer …'" : ""} -d '${body.value.trim() || "{}"}'`;
      curl.replaceChildren(copy(cmd));
    };
    svc.onchange = () => { method.value = SUGGEST[svc.value][0]; body.value = DEFAULT_BODY[method.value] ?? "{}"; refresh(); };
    method.onchange = () => { body.value = DEFAULT_BODY[method.value] ?? "{}"; refresh(); };
    body.oninput = refresh;
    const go = async () => {
      out.textContent = "…";
      try { out.textContent = JSON.stringify(await send("POST", `/rpc/${svc.value}/${encodeURIComponent(method.value.trim())}`, body.value, { "Content-Type": "application/json" }), null, 2); }
      catch (e) { out.textContent = e.message; }
    };
    const form = el("form", { class: "console", onsubmit: (e) => { e.preventDefault(); go(); } },
      el("div", { class: "field" }, svc, method, el("button", { class: "btn main", type: "submit" }, "Send")), list, body, curl, out);
    refresh();

    const level = el("select", { "aria-label": "Lowest level" }, ...["INFO", "WARNING", "ERROR"].map((l) => el("option", { value: l }, l)));
    const which = el("select", { "aria-label": "Service" }, el("option", { value: "" }, "all services"), ...["full_node", "farmer", "harvester", "wallet", "solver", "daemon"].map((s) => el("option", { value: s }, s)));
    const logs = el("div", { class: "logs", tabindex: "0", role: "log", "aria-label": "Chia log" });
    async function pollLogs() {
      try {
        const r = await (await fetch(`/api/logs?level=${level.value}&service=${which.value}`)).json();
        const stick = logs.scrollHeight - logs.scrollTop - logs.clientHeight < 40;
        logs.replaceChildren(...r.lines.map((l) => el("div", { class: l.level }, l.text)));
        if (stick) logs.scrollTop = logs.scrollHeight;
      } catch { /* the next poll retries */ }
    }
    level.onchange = which.onchange = pollLogs;
    pollLogs(); setInterval(pollLogs, 4000);

    byId("box-more").append(
      el("div", { class: "box-two" },
        el("section", {}, el("div", { class: "head" }, el("h2", {}, "Talk to it")), form),
        el("section", {}, el("div", { class: "head" }, el("h2", {}, "About this box")), el("dl", { class: "box-facts", id: "box-facts" }),
          el("div", { class: "box-links" }, pickConfig("Switch testnet", false), el("a", { class: "btn", href: "/metrics" }, "Prometheus metrics"), el("a", { class: "btn", href: "/api/state" }, "Stats as JSON")))),
      el("section", {}, el("div", { class: "head" }, el("h2", {}, "Log"), el("span", { class: "field" }, which, level)), logs),
      el("p", { class: "box-note", style: "padding: 28px 0 56px" }, "Dashboard: ",
        el("a", { href: "https://github.com/greimela/chia-testnet-monitor", target: "_blank", rel: "noopener" }, "chia-testnet-monitor"), " by Acevail and Brandt."));
  }
  let factsKey = "";
  function renderFacts() {
    const k = box.key, d = box.disk, db = d.db ?? {};
    const key = JSON.stringify([box.network, k, db, d.data, d.networks]);
    if (key === factsKey) return;
    factsKey = key;
    const rows = [
      ["Network", box.network ?? "–"],
      ["Receive address", k ? copy(k.wallet_address) : "–"],
      ["Key fingerprint", k ? String(k.fingerprint) : "–"],
      ["Farmer public key", k ? copy(k.farmer_pk) : "–"],
      ["Pool public key", k ? copy(k.pool_pk) : "–"],
      ["Plotter commit", el("a", { href: `https://github.com/judeallred/chia-plotter-wasm/commit/${box.plotter_commit}`, target: "_blank", rel: "noopener" }, box.plotter_commit.slice(0, 10))],
      ["Monitor commit", el("a", { href: `https://github.com/greimela/chia-testnet-monitor/commit/${box.monitor_commit}`, target: "_blank", rel: "noopener" }, box.monitor_commit.slice(0, 10))],
      ["Chia commit", el("a", { href: `https://github.com/Chia-Network/chia-blockchain/commit/${box.commit}`, target: "_blank", rel: "noopener" }, box.commit.slice(0, 10))],
      ["Config from", box.config_source ?? "–"],
      ["Node / wallet database", `${gb(db.node)} / ${gb(db.wallet)}`],
      ["Free disk", d.data ? gb(d.data.free) : "–"],
      ["Networks on disk", (d.networks ?? []).join(", ") || "–"],
    ];
    byId("box-facts").replaceChildren(...rows.flatMap(([a, b]) => [el("dt", {}, a), el("dd", {}, b)]));
  }

  buildMore();
  async function pollBox() {
    try {
      box = await (await fetch("/api/box", { cache: "no-store" })).json();
      if (!state?.ready) byId("network").textContent = box.network ?? "no network yet";
      renderSetup(); renderServices(); renderGarden(); renderFacts();
    } catch { /* the monitor's own health line reports a lost connection */ }
  }
  pollBox();
  setInterval(pollBox, 2500);
})();
