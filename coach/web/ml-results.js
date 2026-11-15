/* Render completed model reports. Synthetic metrics never imply match performance. */
(function (root) {
  "use strict";
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const number = (value, digits = 0) => Number.isFinite(value) ? value.toLocaleString("en-US", { maximumFractionDigits: digits }) : "—";
  const percent = (value) => Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : "—";

  function aggregate(rows) {
    const valid = (rows || []).filter((r) => Number.isFinite(r.arrivals) && Number.isFinite(r.episodes) && r.episodes > 0 && r.arrivals >= 0 && r.arrivals <= r.episodes);
    const arrivals = valid.reduce((sum, r) => sum + r.arrivals, 0);
    const episodes = valid.reduce((sum, r) => sum + r.episodes, 0);
    return { arrivals, episodes, rate: episodes ? arrivals / episodes : null };
  }

  function metric(label, value, note) {
    return `<div class="ml-metric"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(note)}</small></div>`;
  }

  function aimHtml(sim) {
    if (!sim?.learned_test) return "";
    const learned = sim.learned_test;
    return `<div class="ml-aim"><b>Separate aim learner · generation ${number(sim.generation)}</b>
      <span>${number(learned.mean_aligned_requests, 2)} aligned firing requests / scenario · ${number(learned.mean_first_aligned_seconds, 3)}s to first aligned request · ${number(sim.test_episodes)} test scenarios</span>
      <small>Angular-control approximation; these are firing requests, not hits or kills.</small></div>`;
  }

  function view(ml) {
    const kit = ml?.kit_learning;
    if (!kit?.known_map_test) {
      return { badge: ml?.status === "disabled" ? "Disabled" : "Awaiting model", subtitle: "FPS navigation · local training",
        html: `<p class="ml-note">${ml?.status === "disabled" ? "ML training is disabled." : "No completed kit-navigation batch yet. Results will appear here automatically after training finishes."}</p>${aimHtml(ml?.simulation_learning)}` };
    }
    const known = aggregate(kit.known_map_test), unseen = aggregate(kit.unseen_map_test);
    const starting = new Map([...(kit.initial_known_map_test || []), ...(kit.initial_unseen_map_test || [])].map((r) => [r.map, r]));
    const unseenNames = new Set(kit.unseen_test_maps || []);
    const rows = [...kit.known_map_test, ...(kit.unseen_map_test || [])];
    const mapRows = rows.map((r) => {
      const current = aggregate([r]), initial = aggregate([starting.get(r.map)].filter(Boolean));
      const width = Number.isFinite(current.rate) ? Math.min(100, Math.max(0, current.rate * 100)) : 0;
      return `<tr><th scope="row">${esc(r.map)}${unseenNames.has(r.map) ? '<span class="ml-holdout">unseen map</span>' : ""}</th>
        <td>${percent(initial.rate)}</td><td><b>${percent(current.rate)}</b><div class="ml-bar" role="progressbar" aria-label="${esc(r.map)} sampled-route arrival rate" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${width.toFixed(1)}"><i style="width:${width}%"></i></div></td>
        <td>${number(current.arrivals)} / ${number(current.episodes)}</td></tr>`;
    }).join("");
    const history = kit.history || [];
    let selected = -1;
    history.forEach((r, i) => { if (r.selected) selected = i; });
    const cycles = history.map((r, i) => `<tr><th scope="row">${r.teacher_warmup ? "Teacher warm-up" : `Feedback ${number(r.cycle)}`}</th>
      <td>${number(r.training_loss, 4)}</td><td>${percent(aggregate(r.validation).rate)}</td>
      <td>${i === selected ? '<b class="ml-selected">Selected</b>' : r.selected ? "Improved" : "Kept prior"}</td></tr>`).join("");
    const trained = kit.trained_at ? new Date(kit.trained_at) : null;
    const time = trained && !isNaN(trained) ? trained.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "unknown";
    return { badge: `Generation ${number(kit.generation)}`, subtitle: "FPS navigation · kit GridDuel · local CPU",
      html: `<div class="ml-deployment"><span class="ml-local-badge">Local weights only</span><span>Real-match transfer unvalidated</span></div>
        <div class="ml-metrics">${metric("Generated transitions", number(kit.total_generated_transitions), `${number(kit.new_transitions)} in this batch`)}
        ${metric("Replay buffer", number(kit.buffer_transitions), "retained transitions")}
        ${metric("Known-map test", percent(known.rate), `${number(known.arrivals)} / ${number(known.episodes)} arrivals`)}
        ${metric("Unseen-map test", percent(unseen.rate), `${number(unseen.arrivals)} / ${number(unseen.episodes)} arrivals`)}</div>
        <p class="ml-caption">Fresh sampled routes · 8–32 grid cells · arrival rate, not win rate</p>
        <table class="ml-table"><caption>Same test scenarios: batch-start weights → selected checkpoint</caption><thead><tr><th scope="col">Map</th><th scope="col">Batch start</th><th scope="col">Selected</th><th scope="col">Arrivals</th></tr></thead><tbody>${mapRows}</tbody></table>
        <details class="ml-more"><summary>This batch · validation &amp; model details</summary>
        <table class="ml-table"><caption>Checkpoint selection uses validation; final test data never enters training.</caption><thead><tr><th scope="col">Cycle</th><th scope="col">Fit loss</th><th scope="col">Validation</th><th scope="col">Checkpoint</th></tr></thead><tbody>${cycles}</tbody></table>
        <dl class="ml-model-info"><div><dt>Network</dt><dd>${number(kit.network?.features)} → ${esc((kit.network?.hidden_units || []).join(" → "))} → ${number(kit.network?.actions?.length)} actions</dd></div>
        <div><dt>Checkpoint size</dt><dd>${number(kit.model_bytes / 1024, 1)} KB</dd></div>
        <div><dt>Training episodes</dt><dd>${number(kit.total_training_episodes)}</dd></div>
        <div><dt>Speed calibration</dt><dd>${number(kit.calibration?.move_speed_mps, 3)} m/s · ${esc(kit.calibration?.speed_basis || "unknown")}</dd></div></dl>
        ${aimHtml(ml?.simulation_learning)}</details>
        <p class="ml-updated">Last completed ${esc(time)} · batch ${number(kit.wall_seconds, 1)}s</p>
        <p class="ml-note">The kit is a 2D approximation. It omits real shooting, damage and bomb mechanics. Fresh test seeds change each generation, so results can fluctuate. These weights do not replace the published FPS agent.</p>` };
  }

  const api = { aggregate, view };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PitWallMl = api;
})(typeof window !== "undefined" ? window : globalThis);
