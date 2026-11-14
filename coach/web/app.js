/* PIT WALL — live model reasoning + direct chat lane.
   Reads the coach's real trace file; never writes coach state. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const nf = new Intl.NumberFormat("en-US");
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));

  const clockTime = (epoch) => epoch
    ? new Date(epoch * 1000).toLocaleTimeString("en-GB", { hour12: false })
    : "--:--:--";
  const zulu = (iso) => iso ? String(iso).replace("T", " ").slice(11, 16) + "Z" : "—";
  const localFromZ = (iso) => {
    if (!iso) return "—";
    const d = new Date(iso);
    return isNaN(d) ? "—" : d.toLocaleString("en-GB", { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  };
  const dur = (s) => s == null ? "—" : s < 60 ? `${s.toFixed(s < 10 ? 1 : 0)}s` : `${Math.floor(s / 60)}m${String(Math.round(s % 60)).padStart(2, "0")}s`;
  const mmss = (ms) => {
    const t = Math.max(0, Math.round(ms / 1000));
    const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
    return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  };

  /* ── safe inline formatting: escape first, then decorate ── */
  function fmt(text) {
    let out = esc(text);
    out = out.replace(/```([a-zA-Z0-9_+-]*)\n?([\s\S]*?)```/g, (_, lang, body) =>
      `<pre class="inline-code"${lang ? ` data-lang="${esc(lang)}"` : ""}>${body.replace(/\n$/, "")}</pre>`);
    out = out.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    out = out.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
    return out;
  }

  /* ── event sound: quiet, tied only to things you can see happen ── */
  const Sound = {
    on: localStorage.getItem("pitwall.sound") !== "off",
    ctx: null,
    init() {
      if (this.ctx || !this.on) return;
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;
      try { this.ctx = new AC(); } catch { this.ctx = null; }
    },
    play(kind) {
      if (!this.on) return;
      this.init();
      const ctx = this.ctx;
      if (!ctx) return;
      if (ctx.state === "suspended") ctx.resume().catch(() => {});
      const recipes = {
        tick:  [[880, 0.05, 0.035]],
        think: [[523.25, 0.09, 0.04], [783.99, 0.11, 0.032]],
        done:  [[659.25, 0.07, 0.035], [987.77, 0.1, 0.03]],
        decision: [[523.25, 0.08, 0.04], [659.25, 0.08, 0.036], [880, 0.16, 0.032]],
        error: [[155.56, 0.18, 0.05]],
      };
      const notes = recipes[kind] || recipes.tick;
      let t = ctx.currentTime;
      for (const [freq, len, gain] of notes) {
        const osc = ctx.createOscillator(), amp = ctx.createGain();
        osc.type = kind === "error" ? "sawtooth" : "sine";
        osc.frequency.setValueAtTime(freq, t);
        amp.gain.setValueAtTime(0, t);
        amp.gain.linearRampToValueAtTime(gain, t + 0.012);
        amp.gain.exponentialRampToValueAtTime(0.0001, t + len);
        osc.connect(amp).connect(ctx.destination);
        osc.start(t); osc.stop(t + len + 0.02);
        t += len * 0.72;
      }
    },
  };

  /* ── toasts ── */
  function toast(message, kind = "info", action) {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.innerHTML = `<i></i><span>${esc(message)}</span>`;
    if (action) {
      const btn = document.createElement("button");
      btn.type = "button"; btn.textContent = action.label;
      btn.addEventListener("click", () => { action.fn(); dismiss(); });
      el.appendChild(btn);
    }
    $("toasts").appendChild(el);
    const dismiss = () => {
      el.classList.add("out");
      setTimeout(() => el.remove(), 220);
    };
    setTimeout(dismiss, action ? 8000 : 4200);
    return dismiss;
  }

  /* ── settings ── */
  const DEFAULTS = {
    model: "", temperature: 0.7, top_p: 0.95, top_k: 40, max_tokens: 4096,
    think: true, stream: true,
    system: "You are the reasoning model behind an autonomous competition coach. Answer from the attached evidence. Name the numbers you compared. If the evidence does not settle a question, say so.",
    ctx: { state: true, rounds: false, decisions: true, events: false },
  };
  let settings = load();
  function load() {
    try {
      const raw = JSON.parse(localStorage.getItem("pitwall.settings") || "{}");
      return { ...DEFAULTS, ...raw, ctx: { ...DEFAULTS.ctx, ...(raw.ctx || {}) } };
    } catch { return { ...DEFAULTS, ctx: { ...DEFAULTS.ctx } }; }
  }
  const save = () => { try { localStorage.setItem("pitwall.settings", JSON.stringify(settings)); } catch {} };

  /* ── app state ── */
  const state = {
    board: null, items: [], sigs: new Map(), open: new Set(),
    filter: "all", query: "", follow: true, pending: 0,
    chat: [], sending: false, abort: null, context: null, ctxChars: 0,
    seen: new Set(), failures: 0, models: [], primed: false, lastManual: {},
  };
  try {
    const saved = JSON.parse(localStorage.getItem("pitwall.chat") || "[]");
    if (Array.isArray(saved)) state.chat = saved.slice(-40);
  } catch {}
  const saveChat = () => { try { localStorage.setItem("pitwall.chat", JSON.stringify(state.chat.slice(-40))); } catch {} };

  /* ── fetch with connection state ── */
  async function api(path, opts) {
    const res = await fetch(path, { cache: "no-store", ...opts });
    if (res.status === 401) { window.location.replace("/"); throw new Error("session expired"); }
    if (!res.ok) throw new Error(`${path} → HTTP ${res.status}`);
    return res;
  }
  function connection(ok, label) {
    const el = $("conn");
    el.className = "conn " + (ok ? "live" : "down");
    el.querySelector("span").textContent = label;
  }

  /* ── board ── */
  async function pollBoard() {
    try {
      const data = await (await api("/api/board")).json();
      state.board = data; state.failures = 0;
      connection(true, "live");
      renderBoard(data);
    } catch (err) {
      state.failures++;
      connection(false, "offline");
      if (state.failures === 1) toast("Console lost the coach server. Retrying.", "bad");
    }
  }

  function renderBoard(b) {
    $("statClock").querySelector("b").textContent = b.clock || clockTime(b.at);
    const srv = $("statServer");
    srv.className = "stat " + (b.omlx?.ok ? "ok" : "bad");
    srv.querySelector("b").textContent = b.omlx?.ok ? `${b.omlx?.provider || "model"} up` : "model unavailable";
    srv.title = b.omlx?.url || "";
    $("statModel").querySelector("b").textContent = b.omlx?.default || "—";
    const coach = $("statCoach");
    coach.className = "stat " + (b.coach_pid ? "ok" : "bad");
    coach.querySelector("b").textContent = b.coach_pid ? `pid ${b.coach_pid}` : "stopped";

    if (b.omlx?.models?.length && b.omlx.models.join() !== state.models.join()) {
      state.models = b.omlx.models;
      const sel = $("modelSel");
      sel.innerHTML = state.models.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join("");
      if (!settings.model || !state.models.includes(settings.model)) settings.model = b.omlx.default || state.models[0];
      sel.value = settings.model;
      save();
    }
    renderRound(b.rounds || {});
    for (const game of ["racing", "fps"]) renderGame(game, b.games?.[game] || {}, b.rounds?.[game] || {});
    renderSuggestions();
  }

  function renderRound(rounds) {
    const pick = rounds.racing?.next_start ? rounds.racing : rounds.fps?.next_start ? rounds.fps : null;
    const tag = $("roundTag");
    if (!pick) { tag.textContent = "no data"; tag.className = "tag"; return; }
    const start = new Date(pick.next_start).getTime();
    const interval = (pick.interval || 7200) * 1000;
    const now = Date.now();
    const remaining = start - now;
    const progress = clamp(1 - remaining / interval, 0, 1);

    $("countdown").textContent = remaining > 0 ? mmss(remaining) : "live";
    $("countdownLabel").textContent = remaining > 0 ? "to next round" : "round open";
    const fill = $("dialFill");
    fill.style.strokeDashoffset = String(327 * (1 - progress));
    $("nextStart").textContent = `${zulu(pick.next_start)} · ${localFromZ(pick.next_start)}`;

    const errs = Object.entries(rounds).filter(([, r]) => r && r.scheduling_error);
    const states = Object.values(rounds).map((r) => r?.round_state).filter(Boolean);
    if (errs.length) {
      tag.textContent = errs[0][1].scheduling_error; tag.className = "tag bad";
      $("schedErr").textContent = errs.map(([k, r]) => `${k}: ${r.scheduling_error}`).join(" · ");
      $("schedErr").className = "bad";
    } else {
      const st = states[0] || "unknown";
      tag.textContent = st; tag.className = "tag " + (st === "running" ? "busy" : st === "completed" ? "ok" : st === "failed" ? "bad" : "");
      $("schedErr").textContent = states.length ? [...new Set(states)].join(" · ") : "—";
      $("schedErr").className = states.includes("failed") ? "bad" : "ok";
    }
  }

  function slotHtml(letter, s, watching) {
    const fields = Object.entries(s.fields || {}).map(([k, v]) =>
      `${esc(k)} <b>${esc(typeof v === "object" ? JSON.stringify(v) : v)}</b>`).join(" · ");
    const result = s.rank != null
      ? `${s.result_period ? `${s.result_period} result · ` : ""}P${s.rank + 1}${s.score != null ? ` · score ${s.score}` : ""}${s.rounds?.length ? ` · ${s.rounds.join("-")}` : ""}`
      : "no result yet";
    const combat = s.kda ? ` · recorded K/D/A ${s.kda.kills ?? "?"}/${s.kda.deaths ?? "?"}/${s.kda.assists ?? "?"} · K/D ${s.kda.kd_ratio == null ? "?" : Number(s.kda.kd_ratio).toFixed(2)} · ${s.kda.version || "version unknown"}` : "";
    return `<div class="slot${watching ? " watching" : ""}">
      <div class="slot-top">
        <span class="slot-letter">${letter}</span>
        <span class="slot-name" title="${esc(s.name || "")}">${esc(s.name || "empty")}</span>
        <span class="slot-live ${s.live ? "on" : "off"}">${s.live ? "live" : "written"}</span>
      </div>
      <div class="slot-fields">${fields || "no fields"}</div>
      <div class="slot-last">${esc(result + combat)} · from <b>${zulu(s.active_from)}</b>${s.hash ? ` · ${esc(s.hash)}` : ""}</div>
    </div>`;
  }

  function renderGame(game, g, round) {
    const tag = $(game + "State");
    if (g.busy) { tag.textContent = "reasoning"; tag.className = "tag busy"; tag.title = g.busy.text; }
    else if (round?.round_state === "running") { tag.textContent = "match live"; tag.className = "tag ok"; }
    else { tag.textContent = g.phase === "play_B" ? "watching B" : "watching A"; tag.className = "tag"; }
    $(game + "Gen").textContent = g.counted_series ? `v${g.draft?.series_version || g.counted_series.next_version}` : g.generation ?? "—";
    $(game + "Phase").textContent = g.phase || "—";
    const streakInfo = g.loss_streak_info;
    $(game + "Streak").textContent = g.counted_series ? String(g.counted_series.loss_streak) : `${g.loss_streak ?? "—"}${streakInfo && !streakInfo.exact && g.loss_streak ? "+" : ""}`;
    $(game + "Streak").title = g.counted_series ? `Counted-series losses only · starts ${new Date(g.counted_series.starts_at).toLocaleString()} · ${g.counted_series.matches} matches; trial history retained separately.` : streakInfo ? `Consecutive completed AGX match losses; ${streakInfo.counted_matches} distinct matches recorded.${streakInfo.exact ? "" : " Earlier history is incomplete; this is a lower bound."}` : "Consecutive match losses";
    $(game + "Slots").innerHTML = slotHtml("A", g.A || {}, g.watching === "A") + slotHtml("B", g.B || {}, g.watching === "B");
    const impBtn = document.querySelector(`[data-improve="${game}"]`);
    const session = g.session;
    if (impBtn) {
      impBtn.disabled = !!session?.enabled || !!g.manual_pending || !!g.manual_running || !!g.busy;
      impBtn.textContent = session?.enabled ? "Session running" : g.manual_pending ? "Queued…" : (g.manual_running || g.busy) ? "Working…" : "Run sim + improve";
    }
    const stopBtn = document.querySelector(`[data-stop-session="${game}"]`);
    if (stopBtn) { stopBtn.hidden = !session?.enabled; stopBtn.disabled = !!g.manual_pending; }
    const sessionStatus = $(game + "SessionStatus");
    if (sessionStatus) {
      sessionStatus.hidden = !session;
      sessionStatus.textContent = session ? `Session ${session.status}: ${session.attempts || 0} cycles · ${session.accepted || 0} validated revisions · scheduled upload ${session.upload_at ? new Date(session.upload_at).toLocaleString() : "—"} · one draft per round${session.last_error ? ` · ${session.last_error}` : ""}` : "";
    }
    const busy = !!g.manual_pending || !!g.manual_running || !!g.busy;
    const uploadBtn = document.querySelector(`[data-upload="${game}"]`);
    const draft = g.draft;
    if (uploadBtn) {
      uploadBtn.disabled = busy || !draft?.ready || !!draft?.uploaded;
      uploadBtn.textContent = draft?.uploaded ? "Uploaded" : "Upload to AGX";
    }
    const draftStatus = $(game + "DraftStatus");
    if (draftStatus) {
      draftStatus.hidden = !draft;
      const fields = draft?.fields ? Object.entries(draft.fields).map(([key, value]) => `${key}=${value}`).join(" · ") : "";
      draftStatus.textContent = draft ? `${draft.series_version ? `Counted v${draft.series_version} · build ${draft.revision}` : `Draft ${draft.revision}`}: ${draft.name}${draft.total ? ` · ${draft.passed}/${draft.total} checks` : ""}${draft.uploaded ? " · uploaded" : draft.ready ? " · local only" : " · held by checks"}${draft.behavior?.calls ? ` · ${draft.behavior.changed_actions}/${draft.behavior.calls} recorded actions changed · ${draft.behavior.status}${draft.behavior.holdout_traces ? ` · ${draft.behavior.holdout_traces} validation matches` : ""}` : ""}${draft.validation_stage === "offline_experiment" ? " · experimental · awaiting match evidence" : ""}${fields ? ` · ${fields}` : ""}` : "";
    }
    const st = $(game + "ImproveStatus");
    const mr = g.manual_result;
    if (st) {
      if (mr) {
        st.hidden = false;
        const label = { uploaded: "UPLOADED", held: "UPLOAD NOT COMPLETED", no_upload: "VALIDATED DRAFT · NOT UPLOADED", no_candidate: "NO NEW REVISION · PREVIOUS DRAFT RETAINED", upload_failed: "UPLOAD FAILED · DRAFT RETAINED", data_wait: "WAITING FOR DATA OR MODEL SERVICE · DRAFT RETAINED", improve_failed: "IMPROVEMENT FAILED · DRAFT RETAINED" }[mr.status] || mr.status;
        st.className = "improve-status " + (mr.status === "uploaded" ? "ok" : mr.status === "held" ? "warn" : "info");
        let extra = "";
        if (mr.status === "uploaded" && mr.active_from) extra = " · live from " + new Date(mr.active_from).toISOString().slice(11, 16) + "Z";
        else if (mr.status === "held") extra = " · check coach logs";
        if (mr.error) extra += " · " + mr.error;
        const checks = mr.candidate_total ? ` · ${mr.candidate_passed}/${mr.candidate_total} checks` : "";
        const blockers = mr.blockers?.length ? ` · blocked: ${mr.blockers.join(", ")}` : "";
        const name = mr.status === "no_candidate" ? "" : ` · ${mr.name || ""}`;
        st.textContent = `${label}${name}${checks}${blockers}${extra}`;
        const at = mr.at || "";
        if (state.lastManual[game] !== at) {
          const first = state.lastManual[game] === undefined;
          state.lastManual[game] = at;
          if (!first) {
            toast(mr.error ? mr.error : mr.status === "uploaded" ? `Uploaded '${mr.name}' for the next round.`
              : mr.status === "held" ? `Improved '${mr.name}' — upload not completed. Check coach logs.`
              : mr.status === "no_candidate" ? `Repair incomplete${checks}${blockers}; live version retained.`
              : `Validated '${mr.name}' (no upload).`, mr.status === "uploaded" ? "ok" : "warn");
            Sound.play(mr.status === "uploaded" ? "done" : "think");
          }
        }
      } else { st.hidden = true; }
    }
  }

  /* ── pulse sparkline ── */
  function renderPulse() {
    const calls = state.items.filter((i) => i.kind === "call" && i.state === "done" && i.seconds != null).slice(0, 26).reverse();
    const svg = $("spark");
    $("pulseCalls").textContent = nf.format(state.items.filter((i) => i.kind === "call").length);
    if (!calls.length) {
      svg.innerHTML = `<line x1="0" y1="52" x2="260" y2="52" stroke="#17454f" stroke-width="1"/>
        <text x="130" y="34" text-anchor="middle" fill="#5d858b" font-size="10" font-family="monospace">awaiting first traced call</text>`;
      $("pulseAvg").textContent = "—"; $("pulseThink").textContent = "—"; $("pulseTag").textContent = "idle";
      return;
    }
    const max = Math.max(...calls.map((c) => c.seconds), 1);
    const step = 260 / Math.max(calls.length - 1, 1);
    const pts = calls.map((c, i) => [i * step, 58 - (c.seconds / max) * 48]);
    const line = calls.length === 1
      ? `M0,${pts[0][1].toFixed(1)} L260,${pts[0][1].toFixed(1)}`
      : pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
    const area = `${line} L260,62 L0,62 Z`;
    const bars = calls.map((c, i) => {
      const col = c.source === "chat" ? "#b07bff" : c.game === "racing" ? "#ff3d7f" : c.game === "fps" ? "#4da3ff" : "#c9ff3d";
      return `<rect x="${(i * step - 1.6).toFixed(1)}" y="${(58 - (c.seconds / max) * 48).toFixed(1)}" width="3.2" height="${((c.seconds / max) * 48 + 2).toFixed(1)}" fill="${col}" opacity=".55" rx="1"/>`;
    }).join("");
    svg.innerHTML = `
      <defs><linearGradient id="pg" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#c9ff3d" stop-opacity=".38"/><stop offset="100%" stop-color="#c9ff3d" stop-opacity="0"/>
      </linearGradient></defs>
      <line x1="0" y1="58" x2="260" y2="58" stroke="#17454f" stroke-width="1"/>
      <line x1="0" y1="34" x2="260" y2="34" stroke="#113640" stroke-width="1" stroke-dasharray="3 4"/>
      <path d="${area}" fill="url(#pg)"/>${bars}
      <path d="${line}" fill="none" stroke="#c9ff3d" stroke-width="1.6" stroke-linejoin="round"/>
      ${pts.length ? `<circle cx="${pts[pts.length - 1][0].toFixed(1)}" cy="${pts[pts.length - 1][1].toFixed(1)}" r="3" fill="#c9ff3d"/>` : ""}`;
    const avg = calls.reduce((a, c) => a + c.seconds, 0) / calls.length;
    $("pulseAvg").textContent = avg.toFixed(0) + "s";
    $("pulseThink").textContent = nf.format(calls.reduce((a, c) => a + (c.thinking_chars || 0), 0));
    $("pulseTag").textContent = `max ${max.toFixed(0)}s`;
  }

  /* ── reasoning feed ── */
  async function pollFeed() {
    try {
      const data = await (await api("/api/reasoning")).json();
      const items = data.items || [];
      const first = !state.primed;
      state.primed = true;
      let fresh = 0, chime = null;
      for (const it of items) {
        if (state.seen.has(it.id)) continue;
        state.seen.add(it.id);
        if (first) continue;
        fresh++;
        if (chime) continue;
        chime = it.kind === "decision" ? "decision"
          : it.source === "chat" ? "done"
          : it.state === "error" ? "error"
          : it.state === "done" ? "think" : null;
      }
      if (chime) Sound.play(chime);
      state.items = items;
      renderFeed(fresh);
      renderPulse();
    } catch { /* board poll reports the connection */ }
  }

  function matches(it) {
    const f = state.filter;
    if (f === "racing" && it.game !== "racing") return false;
    if (f === "fps" && it.game !== "fps") return false;
    if (f === "call" && it.kind !== "call") return false;
    if (f === "decision" && it.kind !== "decision") return false;
    if (f === "chat" && it.source !== "chat") return false;
    if (state.query) {
      const hay = [it.label, it.thinking, it.content, it.thesis, it.model, it.error,
        ...(it.changed || []), ...(it.overridden || [])].join(" ").toLowerCase();
      if (!hay.includes(state.query)) return false;
    }
    return true;
  }

  function lane(id, name, title, body, count, openDefault) {
    if (!body) return "";
    const key = `${id}:${name}`;
    const isOpen = state.open.has(key) || (openDefault && !state.open.has(key + ":closed"));
    return `<div class="lane ${name}${isOpen ? " open" : ""}" data-lane="${key}">
      <button class="lane-head" type="button" data-toggle="${key}">
        <span class="caret">▸</span>${esc(title)}<span class="lane-count">${esc(count)}</span>
      </button>
      <div class="lane-body"><pre>${esc(body)}</pre></div>
    </div>`;
  }

  function cardHtml(it) {
    const badges = [];
    if (it.kind === "decision") badges.push(`<span class="badge decision">decision</span>`);
    else if (it.source === "chat") badges.push(`<span class="badge chat">chat</span>`);
    else badges.push(`<span class="badge call">model call</span>`);
    if (it.game) badges.push(`<span class="badge ${it.game}">${it.game}</span>`);
    if (it.state === "running") badges.push(`<span class="badge running">thinking</span>`);
    if (it.state === "error") badges.push(`<span class="badge error">error</span>`);

    const meta = [];
    if (it.model) meta.push(`<span class="mchip">${esc(it.model)}</span>`);
    const s = it.sampling || {};
    if (s.temperature != null) meta.push(`<span class="mchip${s.temperature >= 0.8 ? " hot" : ""}">temp <b>${esc(s.temperature)}</b></span>`);
    if (s.top_p != null) meta.push(`<span class="mchip">top-p <b>${esc(s.top_p)}</b></span>`);
    if (s.top_k != null) meta.push(`<span class="mchip">top-k <b>${esc(s.top_k)}</b></span>`);
    if (it.think != null) meta.push(`<span class="mchip">thinking <b>${it.think ? "on" : "off"}</b></span>`);
    if (it.seconds != null) meta.push(`<span class="mchip good">took <b>${dur(it.seconds)}</b></span>`);
    else if (it.state === "running") meta.push(`<span class="mchip hot" data-elapsed="${it.at}">elapsed <b>0s</b></span>`);
    if (it.usage?.completion_tokens) meta.push(`<span class="mchip">out <b>${nf.format(it.usage.completion_tokens)}</b> tok</span>`);
    if (it.usage?.prompt_tokens) meta.push(`<span class="mchip">in <b>${nf.format(it.usage.prompt_tokens)}</b> tok</span>`);
    if (it.generation != null) meta.push(`<span class="mchip">gen <b>${esc(it.generation)}</b></span>`);
    if (it.loss_streak != null) meta.push(`<span class="mchip${it.loss_streak >= 3 ? " hot" : ""}">loss streak <b>${esc(it.loss_streak)}</b></span>`);
    if (it.via) meta.push(`<span class="mchip">via <b>${esc(it.via)}</b></span>`);

    let body = "";
    if (it.kind === "decision") {
      if (it.thesis) body += `<div class="thesis"><div class="thesis-label">Model thesis</div><p>${esc(it.thesis)}</p></div>`;
      if (it.changed?.length) body += `<div class="diffs"><div class="dlabel">what changed · live best → plan B</div>${it.changed.map(diffLine).join("")}</div>`;
      if (it.overridden?.length) body += `<div class="diffs"><div class="dlabel over">harness overrode</div>${it.overridden.map(diffLine).join("")}</div>`;
      if (it.code) body += it.game === "racing" && /unchanged|frozen/.test(it.code)
        ? `<div class="card-note">Driver frozen · setup-book changes shown above</div>`
        : `<div class="card-note${/unchanged|frozen/.test(it.code) ? " warn" : ""}">agent.py · ${esc(it.code)}</div>`;
      if (it.gate) body += `<div class="card-note${/passed/.test(it.gate) ? "" : " warn"}">local gate · ${esc(it.gate)}</div>`;
      if (it.note) body += `<div class="card-note">${esc(it.note)}</div>`;
    } else {
      if (it.state === "error") body += `<div class="card-note bad">${esc(it.error || "call failed")}</div>`;
      body += lane(it.id, "think", "thinking", it.thinking,
        it.thinking_chars ? `${nf.format(it.thinking_chars)} chars` : "streaming", it.state === "running");
      body += lane(it.id, "answer", it.source === "chat" ? "reply" : "visible reply", it.content,
        it.content_chars ? `${nf.format(it.content_chars)} chars` : "—", it.state === "done" && !it.thinking);
      body += lane(it.id, "prompt", "prompt sent", it.prompt,
        it.prompt_chars ? `${nf.format(it.prompt_chars)} chars` : "—", false);
      if (it.state === "running" && !it.thinking) {
        body += `<div class="card-note warn">call in flight · reasoning appears when the model returns</div>`;
      }
    }

    return `<div class="card-head">${badges.join("")}
        <span class="card-label" title="${esc(it.label || "")}">${esc(it.label || "call")}</span>
        <span class="card-time">${clockTime(it.at)}</span>
      </div>
      ${meta.length ? `<div class="card-meta">${meta.join("")}</div>` : ""}${body}`;
  }

  function diffLine(text) {
    const m = String(text).match(/^([^:]+):\s*([\s\S]*?)\s*->\s*([\s\S]*)$/);
    if (!m) return `<div class="diff">${esc(text)}</div>`;
    const cut = (v) => v.length > 150 ? v.slice(0, 150) + "…" : v;
    return `<div class="diff"><b>${esc(m[1])}</b>: ${esc(cut(m[2]))} <span class="arrow">→</span> ${esc(cut(m[3]))}</div>`;
  }

  function renderFeed(fresh) {
    const feed = $("feed");
    const visible = state.items.filter(matches);
    $("feedEmpty").hidden = visible.length > 0;
    const wanted = new Set(visible.map((i) => i.id));

    for (const el of [...feed.children]) {
      if (!wanted.has(el.dataset.id)) { el.remove(); state.sigs.delete(el.dataset.id); }
    }

    let prev = null;
    for (const it of visible) {
      const sig = JSON.stringify([it.state, it.seconds, it.thinking_chars, it.content_chars,
        it.thesis, it.error, (it.changed || []).length, (it.overridden || []).length, it.usage]);
      let el = feed.querySelector(`[data-id="${CSS.escape(it.id)}"]`);
      if (!el) {
        el = document.createElement("article");
        el.className = "card";
        el.dataset.id = it.id;
        el.style.animation = "rise .3s ease both";
      }
      if (state.sigs.get(it.id) !== sig) {
        const scrolls = {};
        el.querySelectorAll(".lane").forEach((l) => { scrolls[l.dataset.lane] = l.querySelector(".lane-body").scrollTop; });
        el.innerHTML = cardHtml(it);
        el.querySelectorAll(".lane").forEach((l) => {
          const body = l.querySelector(".lane-body");
          if (scrolls[l.dataset.lane]) body.scrollTop = scrolls[l.dataset.lane];
          else if (l.classList.contains("open") && it.state === "running") body.scrollTop = body.scrollHeight;
        });
        state.sigs.set(it.id, sig);
      }
      el.dataset.game = it.game || "";
      el.dataset.source = it.source || "";
      el.dataset.kind = it.kind || "";
      el.dataset.state = it.state || "";
      el.classList.toggle("running", it.state === "running");
      el.classList.toggle("error", it.state === "error");
      if (prev) { if (prev.nextElementSibling !== el) feed.insertBefore(el, prev.nextElementSibling); }
      else if (feed.firstElementChild !== el) feed.insertBefore(el, feed.firstChild);
      prev = el;
    }

    if (fresh > 0) {
      if (state.follow) { feed.scrollTop = 0; state.pending = 0; }
      else state.pending += fresh;
    }
    const jump = $("jumpBtn");
    jump.hidden = state.follow || state.pending === 0;
    $("jumpCount").textContent = state.pending;
    $("liveBadge").innerHTML = `<i></i>${state.items.some((i) => i.state === "running") ? "model thinking" : "tracing"}`;
  }

  function tickElapsed() {
    const now = Date.now() / 1000;
    document.querySelectorAll("[data-elapsed]").forEach((el) => {
      el.querySelector("b").textContent = dur(now - Number(el.dataset.elapsed));
    });
    if (state.board?.rounds) renderRound(state.board.rounds);
  }

  /* ── chat ── */
  async function loadContext() {
    try {
      state.context = await (await api("/api/context")).json();
    } catch { state.context = null; }
    updateContextSize();
  }

  function contextText() {
    const c = state.context;
    if (!c) return "";
    const parts = [];
    if (settings.ctx.state) parts.push("CURRENT COACH STATE\n" + JSON.stringify(c.games, null, 1));
    if (settings.ctx.rounds) parts.push("PLATFORM ROUNDS\n" + JSON.stringify(c.rounds, null, 1));
    if (settings.ctx.decisions) parts.push("RECENT DECISIONS\n" + JSON.stringify(
      (c.decisions || []).map((d) => ({ at: d.at, game: d.game, mode: d.mode, generation: d.generation,
        thesis: d.thesis, changed: d.changed, overridden: d.overridden })), null, 1));
    if (settings.ctx.events) parts.push("RECENT COACH LOG\n" + (c.events || []).map((e) => `${e.at} ${e.game}: ${e.text}`).join("\n"));
    return parts.join("\n\n");
  }

  function updateContextSize() {
    const text = contextText();
    state.ctxChars = text.length + settings.system.length;
    $("ctxSize").textContent = `context ≈ ${nf.format(state.ctxChars)} chars` +
      (settings.think ? " · thinking spends the token budget" : "");
    const row = $("attachRow");
    const names = { state: "coach state", rounds: "rounds", decisions: "decisions", events: "coach log" };
    const on = Object.entries(settings.ctx).filter(([, v]) => v).map(([k]) => names[k]);
    row.innerHTML = on.map((n) => `<span class="attach-chip">${esc(n)}</span>`).join("") +
      `<span class="attach-chip size">${nf.format(state.ctxChars)} chars</span>` +
      `<span class="attach-chip size">temp ${settings.temperature.toFixed(2)}</span>` +
      `<span class="attach-chip size">${settings.think ? "thinking on" : "thinking off"}</span>`;
    $("chatSub").textContent = `${settings.model || "model"} · ${settings.think ? "thinking" : "direct"} · ${settings.stream ? "stream" : "block"}`;
  }

  function renderChat() {
    const log = $("chatLog");
    log.innerHTML = "";
    $("chatEmpty").hidden = state.chat.length > 0;
    for (const m of state.chat) log.appendChild(messageEl(m));
    log.scrollTop = log.scrollHeight;
  }

  function messageEl(m) {
    const el = document.createElement("div");
    el.className = `msg ${m.role === "user" ? "user" : "bot"}`;
    el.dataset.mid = m.id;
    const stat = m.role === "user" ? "" :
      `<span class="msg-stat">${m.seconds != null ? dur(m.seconds) : ""}${m.thinking ? ` · ${nf.format(m.thinking.length)} think chars` : ""}${m.stopped ? " · stopped" : ""}</span>`;
    let inner = `<div class="msg-role">${m.role === "user" ? "you" : "model"}${stat}</div>`;
    if (m.thinking) {
      inner += `<details class="think-lane"${m.streaming ? " open" : ""}>
        <summary>reasoning<span class="tcount">${nf.format(m.thinking.length)} chars</span></summary>
        <div class="think-body"><pre>${esc(m.thinking)}</pre></div></details>`;
    }
    const text = m.content || (m.streaming ? "" : "(no reply)");
    inner += `<div class="bubble${m.error ? " err" : m.empty ? " warn" : ""}">${fmt(text)}${m.streaming ? '<span class="caret-blink"></span>' : ""}</div>`;
    if (m.role !== "user" && !m.streaming) {
      inner += `<div class="msg-tools"><button type="button" data-copy="${esc(m.id)}">copy</button><button type="button" data-regen="${esc(m.id)}">regenerate</button>${m.empty ? `<button type="button" data-nothink="${esc(m.id)}">answer directly</button>` : ""}</div>`;
    }
    el.innerHTML = inner;
    return el;
  }

  function patchMessage(m) {
    const el = $("chatLog").querySelector(`[data-mid="${CSS.escape(m.id)}"]`);
    if (!el) { renderChat(); return; }
    const nearBottom = $("chatLog").scrollHeight - $("chatLog").scrollTop - $("chatLog").clientHeight < 90;
    const fresh = messageEl(m);
    const oldBody = el.querySelector(".think-body");
    const scroll = oldBody ? oldBody.scrollTop : 0;
    const wasOpen = el.querySelector(".think-lane")?.open;
    el.replaceWith(fresh);
    const lane = fresh.querySelector(".think-lane");
    if (lane && wasOpen) lane.open = true;
    const body = fresh.querySelector(".think-body");
    if (body) body.scrollTop = m.streaming ? body.scrollHeight : scroll;
    if (nearBottom) $("chatLog").scrollTop = $("chatLog").scrollHeight;
  }

  function renderSuggestions() {
    const box = $("suggests");
    if (box.dataset.done === "1" || !state.board) return;
    const r = state.board.games?.racing, f = state.board.games?.fps;
    const ideas = [];
    if (r?.A?.name) ideas.push(`Why did ${r.A.name} finish P${(r.A.rank ?? 0) + 1}? Compare it with the best race in the series.`);
    if (r?.A?.fields?.preview != null) ideas.push(`What does preview ${r.A.fields.preview} do to the wet racing line, and is it the right lever next?`);
    if (f?.A?.name) ideas.push(`The FPS seat plants zero times. Which field in agent.py actually controls planting, and what should change?`);
    ideas.push("Read the attached decisions. Which harness override lost information, and why?");
    box.innerHTML = ideas.slice(0, 4).map((t) => `<button class="suggest" type="button">${esc(t)}</button>`).join("");
    box.dataset.done = "1";
  }

  async function send(text) {
    if (state.sending) return;
    const prompt = (text ?? $("prompt").value).trim();
    if (!prompt) { toast("Write a message first.", "warn"); return; }
    Sound.init();
    if (!state.context) await loadContext();

    const user = { id: "u" + Date.now(), role: "user", content: prompt };
    const bot = { id: "b" + Date.now(), role: "assistant", content: "", thinking: "", streaming: true };
    state.chat.push(user, bot);
    if (text == null) $("prompt").value = "";
    renderChat(); saveChat();
    setSending(true);

    const ctx = contextText();
    const system = settings.system + (ctx ? `\n\n--- ATTACHED LIVE CONTEXT ---\n${ctx}` : "");
    const history = state.chat.filter((m) => !m.streaming && m.content && !m.error)
      .slice(-12).map((m) => ({ role: m.role, content: m.content }));
    const payload = {
      model: settings.model, think: settings.think, max_tokens: settings.max_tokens,
      sampling: { temperature: settings.temperature, top_p: settings.top_p, top_k: settings.top_k },
      messages: [{ role: "system", content: system }, ...history],
    };

    state.abort = new AbortController();
    const started = performance.now();
    let chunkCount = 0;
    try {
      const res = await fetch("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload), signal: state.abort.signal,
      });
      if (res.status === 401) { window.location.replace("/"); return; }
      if (!res.ok || !res.body) throw new Error(`chat → HTTP ${res.status}`);
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let finished = false;
      while (!finished) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop() || "";
        for (const part of parts) {
          const line = part.split("\n").find((l) => l.startsWith("data:"));
          if (!line) continue;
          let ev; try { ev = JSON.parse(line.slice(5).trim()); } catch { continue; }
          if (ev.type === "thinking") { bot.thinking += ev.text; chunkCount++; }
          else if (ev.type === "content") { bot.content += ev.text; chunkCount++; }
          else if (ev.type === "error") { bot.error = true; bot.content = (bot.content ? bot.content + "\n\n" : "") + ev.error; }
          else if (ev.type === "done") { bot.seconds = ev.seconds; bot.usage = ev.usage; finished = true; }
          if (settings.stream && chunkCount % 3 === 0) patchMessage(bot);
        }
      }
      try { await reader.cancel(); } catch {}
    } catch (err) {
      if (err.name === "AbortError") { bot.stopped = true; }
      else { bot.error = true; bot.content = (bot.content ? bot.content + "\n\n" : "") + `Request failed: ${err.message}. Is the coach UI server running?`; Sound.play("error"); }
    } finally {
      bot.streaming = false;
      if (bot.seconds == null) bot.seconds = (performance.now() - started) / 1000;
      if (!bot.error && !bot.stopped && !bot.content.trim() && bot.thinking) {
        bot.empty = true;
        bot.content = "The model spent its whole token budget inside thinking and returned no visible reply. Raise max tokens, or turn thinking off for a direct answer.";
      }
      state.sending = false; state.abort = null;
      setSending(false);
      patchMessage(bot); saveChat();
      if (!bot.error && !bot.stopped) Sound.play("done");
      if (bot.error) toast("The model did not answer. Check the model server.", "bad");
      pollFeed();
    }
  }

  function setSending(on) {
    state.sending = on;
    $("sendBtn").disabled = on;
    $("stopBtn").hidden = !on;
    $("composerHint").textContent = on ? "streaming… Esc stops" : "⌘/Ctrl + Enter to send · Esc stops";
  }

  function stop() { if (state.abort) { state.abort.abort(); toast("Generation stopped.", "warn"); } }

  function exportChat(kind) {
    if (!state.chat.length) { toast("Nothing to export yet.", "warn"); return; }
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
    let blob, name;
    if (kind === "json") {
      blob = new Blob([JSON.stringify({ exported: new Date().toISOString(), settings, messages: state.chat }, null, 2)], { type: "application/json" });
      name = `model-chat-${stamp}.json`;
    } else {
      const md = [`# Model direct lane — ${new Date().toLocaleString()}`, "",
        `Model \`${settings.model}\` · temp ${settings.temperature} · top-p ${settings.top_p} · top-k ${settings.top_k} · thinking ${settings.think ? "on" : "off"}`, ""];
      for (const m of state.chat) {
        md.push(`## ${m.role === "user" ? "You" : "Model"}${m.seconds != null ? ` · ${dur(m.seconds)}` : ""}`, "");
        if (m.thinking) md.push("<details><summary>reasoning</summary>", "", m.thinking, "", "</details>", "");
        md.push(m.content || "_(no reply)_", "");
      }
      blob = new Blob([md.join("\n")], { type: "text/markdown" });
      name = `model-chat-${stamp}.md`;
    }
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1500);
    return name;
  }

  /* ── controls ── */
  function bindRange(id, outId, key, format) {
    const el = $(id), out = $(outId);
    el.value = settings[key];
    out.textContent = format(settings[key]);
    el.addEventListener("input", () => {
      settings[key] = Number(el.value);
      out.textContent = format(settings[key]);
      save(); updateContextSize();
    });
  }
  function bindSwitch(id, get, set) {
    const el = $(id);
    const paint = () => { const on = get(); el.classList.toggle("is-on", on); el.setAttribute("aria-checked", String(on)); };
    paint();
    el.addEventListener("click", () => { set(!get()); paint(); save(); updateContextSize(); Sound.play("tick"); });
    return paint;
  }

  function wire() {
    $("telemetryBtn").addEventListener("click", () => {
      const rail = $("telemetry");
      rail.hidden = !rail.hidden;
      $("telemetryBtn").setAttribute("aria-expanded", String(!rail.hidden));
      $("telemetryBtn").textContent = rail.hidden ? "Show telemetry" : "Hide telemetry";
      document.querySelector(".shell").classList.toggle("show-telemetry", !rail.hidden);
    });
    bindRange("temp", "tempOut", "temperature", (v) => v.toFixed(2));
    bindRange("topP", "topPOut", "top_p", (v) => v.toFixed(2));
    bindRange("topK", "topKOut", "top_k", (v) => String(v));
    bindRange("maxTok", "maxTokOut", "max_tokens", (v) => nf.format(v));

    $("modelSel").addEventListener("change", (e) => { settings.model = e.target.value; save(); updateContextSize(); Sound.play("tick"); });
    $("system").value = settings.system;
    $("system").addEventListener("input", (e) => { settings.system = e.target.value; save(); updateContextSize(); });

    bindSwitch("thinkSw", () => settings.think, (v) => { settings.think = v; });
    bindSwitch("streamSw", () => settings.stream, (v) => { settings.stream = v; });
    const ctxPaints = [
      bindSwitch("ctxState", () => settings.ctx.state, (v) => { settings.ctx.state = v; loadContext(); }),
      bindSwitch("ctxRounds", () => settings.ctx.rounds, (v) => { settings.ctx.rounds = v; loadContext(); }),
      bindSwitch("ctxDecisions", () => settings.ctx.decisions, (v) => { settings.ctx.decisions = v; loadContext(); }),
      bindSwitch("ctxEvents", () => settings.ctx.events, (v) => { settings.ctx.events = v; loadContext(); }),
    ];

    $("resetBtn").addEventListener("click", () => {
      const model = settings.model;
      settings = { ...DEFAULTS, model, ctx: { ...DEFAULTS.ctx } };
      save();
      $("temp").value = settings.temperature; $("tempOut").textContent = settings.temperature.toFixed(2);
      $("topP").value = settings.top_p; $("topPOut").textContent = settings.top_p.toFixed(2);
      $("topK").value = settings.top_k; $("topKOut").textContent = settings.top_k;
      $("maxTok").value = settings.max_tokens; $("maxTokOut").textContent = nf.format(settings.max_tokens);
      $("system").value = settings.system;
      ["thinkSw", "streamSw"].forEach((id) => { $(id).classList.add("is-on"); $(id).setAttribute("aria-checked", "true"); });
      ctxPaints.forEach((p) => p());
      updateContextSize(); Sound.play("tick");
      toast("Control deck reset to defaults.", "ok");
    });

    $("randomBtn").addEventListener("click", () => {
      const rnd = (a, b, step) => Math.round((a + Math.random() * (b - a)) / step) * step;
      settings.temperature = Number(rnd(0.2, 1.3, 0.05).toFixed(2));
      settings.top_p = Number(rnd(0.7, 1, 0.01).toFixed(2));
      settings.top_k = rnd(15, 80, 1);
      save();
      $("temp").value = settings.temperature; $("tempOut").textContent = settings.temperature.toFixed(2);
      $("topP").value = settings.top_p; $("topPOut").textContent = settings.top_p.toFixed(2);
      $("topK").value = settings.top_k; $("topKOut").textContent = settings.top_k;
      updateContextSize(); Sound.play("think");
      toast(`Randomized · temp ${settings.temperature.toFixed(2)} · top-p ${settings.top_p.toFixed(2)} · top-k ${settings.top_k}`, "ok");
    });

    $("filters").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-filter]");
      if (!btn) return;
      state.filter = btn.dataset.filter;
      [...$("filters").children].forEach((c) => c.classList.toggle("is-on", c === btn));
      state.sigs.clear(); renderFeed(0); $("feed").scrollTop = 0; Sound.play("tick");
    });
    document.querySelectorAll("[data-filter-game]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const target = $("filters").querySelector(`[data-filter="${btn.dataset.filterGame}"]`);
        target?.click();
        document.querySelector(".stream").scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });
    document.querySelectorAll("[data-improve]").forEach((btn) => {
      btn.addEventListener("click", async (event) => {
        event.preventDefault();
        event.stopPropagation();
        const game = btn.dataset.improve;
        if (game !== btn.closest("[data-game]")?.dataset.game) {
          toast("Button game does not match its panel. Reload the page.", "bad");
          return;
        }
        const request_id = crypto.randomUUID();
        btn.disabled = true;
        try {
          const res = await api(`/api/games/${encodeURIComponent(game)}/session`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ game, request_id }),
          });
          const data = await res.json();
          if (data.ok) {
            toast(`${game} session queued. Local improvements repeat; one draft uploads 12 minutes before the round.`, "ok");
            Sound.play("think");
          } else {
            toast(data.error || "Could not queue the improvement.", "bad");
            btn.disabled = false;
          }
        } catch {
          toast("Could not reach the coach server.", "bad");
          btn.disabled = false;
        }
      });
    });
    document.querySelectorAll("[data-stop-session]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const game = btn.dataset.stopSession;
        const res = await api(`/api/games/${encodeURIComponent(game)}/stop-session`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({game, request_id: crypto.randomUUID()}),
        });
        const data = await res.json();
        toast(data.ok ? "Session will stop after the current cycle; scheduled upload is cancelled." : data.error, data.ok ? "ok" : "bad");
      });
    });
    document.querySelectorAll("[data-upload]").forEach((btn) => {
      btn.addEventListener("click", async (event) => {
        event.preventDefault(); event.stopPropagation();
        const game = btn.dataset.upload;
        const draft = state.board?.games?.[game]?.draft;
        if (!draft?.ready || game !== btn.closest("[data-game]")?.dataset.game) return;
        btn.disabled = true;
        try {
          const res = await api(`/api/games/${encodeURIComponent(game)}/upload`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ game, draft_id: draft.id, request_id: crypto.randomUUID() }),
          });
          const data = await res.json();
          toast(data.ok ? `Upload queued for ${game}: ${draft.name}.` : data.error || "Upload could not be queued.", data.ok ? "ok" : "bad");
          if (!data.ok) btn.disabled = false;
        } catch { toast("Could not reach the upload service.", "bad"); btn.disabled = false; }
      });
    });
    $("clearFilters").addEventListener("click", () => {
      $("filters").querySelector('[data-filter="all"]').click();
      $("search").value = ""; state.query = ""; renderFeed(0);
    });
    $("search").addEventListener("input", (e) => { state.query = e.target.value.trim().toLowerCase(); state.sigs.clear(); renderFeed(0); });

    $("feed").addEventListener("scroll", () => {
      const atTop = $("feed").scrollTop < 40;
      state.follow = atTop;
      $("autoBtn").setAttribute("aria-pressed", String(atTop));
      if (atTop) { state.pending = 0; $("jumpBtn").hidden = true; }
    });
    $("autoBtn").addEventListener("click", () => {
      state.follow = !state.follow;
      $("autoBtn").setAttribute("aria-pressed", String(state.follow));
      if (state.follow) { $("feed").scrollTop = 0; state.pending = 0; $("jumpBtn").hidden = true; }
      Sound.play("tick");
    });
    $("jumpBtn").addEventListener("click", () => {
      state.follow = true; state.pending = 0; $("jumpBtn").hidden = true;
      $("autoBtn").setAttribute("aria-pressed", "true");
      $("feed").scrollTop = 0;
    });

    $("feed").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-toggle]");
      if (!btn) return;
      const key = btn.dataset.toggle;
      const laneEl = btn.closest(".lane");
      const open = !laneEl.classList.contains("open");
      laneEl.classList.toggle("open", open);
      if (open) { state.open.add(key); state.open.delete(key + ":closed"); }
      else { state.open.delete(key); state.open.add(key + ":closed"); }
      Sound.play("tick");
    });

    $("soundBtn").addEventListener("click", () => {
      Sound.on = !Sound.on;
      localStorage.setItem("pitwall.sound", Sound.on ? "on" : "off");
      $("soundBtn").setAttribute("aria-pressed", String(Sound.on));
      $("soundBtn").title = Sound.on ? "Event sound on" : "Event sound muted";
      if (Sound.on) { Sound.init(); Sound.play("think"); }
      toast(Sound.on ? "Event sound on." : "All sound muted.", Sound.on ? "ok" : "warn");
    });
    $("soundBtn").setAttribute("aria-pressed", String(Sound.on));
    $("soundBtn").title = Sound.on ? "Event sound on" : "Event sound muted";

    $("sendBtn").addEventListener("click", () => send());
    $("stopBtn").addEventListener("click", stop);
    $("prompt").addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); send(); }
    });
    $("suggests").addEventListener("click", (e) => {
      const btn = e.target.closest(".suggest");
      if (!btn) return;
      $("prompt").value = btn.textContent;
      $("prompt").focus();
      Sound.play("tick");
    });
    $("chatLog").addEventListener("click", (e) => {
      const copy = e.target.closest("[data-copy]");
      if (copy) {
        const m = state.chat.find((x) => x.id === copy.dataset.copy);
        if (!m) return;
        const text = (m.thinking ? `REASONING\n${m.thinking}\n\nREPLY\n` : "") + m.content;
        (navigator.clipboard?.writeText(text) ?? Promise.reject())
          .then(() => toast("Copied to clipboard.", "ok"))
          .catch(() => {
            const ta = document.createElement("textarea");
            ta.value = text; document.body.appendChild(ta); ta.select();
            try { document.execCommand("copy"); toast("Copied to clipboard.", "ok"); }
            catch { toast("Copy blocked by the browser.", "bad"); }
            ta.remove();
          });
        return;
      }
      const regen = e.target.closest("[data-regen], [data-nothink]");
      if (regen) {
        const direct = regen.hasAttribute("data-nothink");
        const idx = state.chat.findIndex((x) => x.id === (regen.dataset.regen || regen.dataset.nothink));
        if (idx < 0) return;
        const preceding = state.chat.slice(0, idx).filter((m) => m.role === "user").pop();
        state.chat = state.chat.slice(0, idx);
        if (direct) {
          settings.think = false; save();
          const sw = $("thinkSw"); sw.classList.remove("is-on"); sw.setAttribute("aria-checked", "false");
          updateContextSize();
          toast("Thinking off for a direct answer.", "warn");
        }
        saveChat(); renderChat();
        if (preceding) send(preceding.content);
      }
    });
    $("clearChat").addEventListener("click", () => {
      if (!state.chat.length) { toast("The lane is already empty.", "warn"); return; }
      const snapshot = state.chat.slice();
      state.chat = []; saveChat(); renderChat();
      toast("Conversation cleared.", "warn", { label: "Undo", fn: () => { state.chat = snapshot; saveChat(); renderChat(); toast("Conversation restored.", "ok"); } });
    });
    $("exportBtn").addEventListener("click", () => {
      const name = exportChat("md");
      if (name) toast(`Saved ${name}`, "ok", {
        label: "JSON",
        fn: () => { const n = exportChat("json"); if (n) toast(`Saved ${n}`, "ok"); },
      });
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && state.sending) { stop(); return; }
      if (e.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) {
        e.preventDefault(); $("search").focus();
      }
    });
    document.addEventListener("pointerdown", () => Sound.init(), { once: true });
  }

  /* ── boot ── */
  function boot() {
    wire();
    renderChat();
    updateContextSize();
    loadContext();
    pollBoard();
    pollFeed();
    setInterval(pollBoard, 4000);
    setInterval(pollFeed, 2000);
    setInterval(tickElapsed, 1000);
    setInterval(loadContext, 30000);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
