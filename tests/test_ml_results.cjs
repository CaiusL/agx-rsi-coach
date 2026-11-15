"use strict";
const assert = require("node:assert/strict");
const ml = require("../coach/web/ml-results.js");

assert.equal(ml.aggregate([{ arrivals: 1, episodes: 2 }, { arrivals: 9, episodes: 10 }]).rate, 10 / 12);
assert.equal(ml.aggregate([]).rate, null);
assert.equal(ml.aggregate([{ arrivals: 20, episodes: 2 }]).rate, null);
assert.match(ml.view({ status: "disabled" }).html, /disabled/);

const report = { generation: 3, total_generated_transitions: 100, new_transitions: 20, buffer_transitions: 90,
  known_map_test: [{ map: '<img src=x onerror="alert(1)">', arrivals: 0, episodes: 10 }],
  initial_known_map_test: [{ map: '<img src=x onerror="alert(1)">', arrivals: 2, episodes: 10 }],
  unseen_map_test: [{ map: "mirage", arrivals: 7, episodes: 10 }], unseen_test_maps: ["mirage"],
  history: [{ cycle: 0, selected: true, teacher_warmup: true, training_loss: .4, validation: [{ arrivals: 2, episodes: 10 }] },
    { cycle: 1, selected: true, training_loss: .2, validation: [{ arrivals: 4, episodes: 10 }] }],
  network: { features: 132, hidden_units: [64, 64], actions: [0, 1, 2, 3, 4] }, model_bytes: 50000,
  calibration: { move_speed_mps: 4.85, speed_basis: "own trace" }, total_training_episodes: 100, wall_seconds: 15 };
const view = ml.view({ kit_learning: report });
assert.match(view.html, /0\.0%/);
assert.match(view.html, /70\.0%/);
assert.match(view.html, /unseen map/);
assert.match(view.html, /Local weights only/);
assert.match(view.html, /not win rate/);
assert.doesNotMatch(view.html, /<img|NaN|undefined/);
assert.equal((view.html.match(/class="ml-selected"/g) || []).length, 1);
assert.match(view.html, /Improved/);
console.log("ML results calculations, escaping, empty states and checkpoint labels passed");
