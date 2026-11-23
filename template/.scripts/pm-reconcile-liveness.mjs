#!/usr/bin/env node
// Read-only liveness adapter: reuse Krebs' Candystore turn verdict rather than guessing from ticket age.
// Failure, clipped source or uncertain status is a refusal. Never run tick from this adapter.
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { homedir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const ref = process.argv[2];
if (!/^[A-Z][A-Z0-9]*-[1-9][0-9]*$/u.test(ref ?? "")) process.exit(2);
const root = process.env.PM_RECONCILE_KREBS_ROOT || join(homedir(), "code", "33GOD", "krebs");
const bloodbank = process.env.KREBS_BLOODBANK_N8N_DIR || join(root, "..", "bloodbank", "integrations", "n8n-nodes-bloodbank");
const require = createRequire(import.meta.url);
try {
  const statusRun = spawnSync("krebs", ["status", "--ticket", ref, "--json"], { encoding: "utf8", timeout: 30000, maxBuffer: 8 * 1024 * 1024 });
  if (statusRun.status !== 0) throw new Error("Krebs status unavailable");
  const status = JSON.parse(statusRun.stdout);
  if (status.candystore?.ok !== true) throw new Error("Candystore unavailable");
  const scope = status.scopes?.find((s) => s.board === ref.split("-")[0] && s.tickets?.some((t) => t.key === ref));
  if (!scope || !Array.isArray(scope.inFlight)) throw new Error("scope or in-flight state unavailable");
  if (scope.inFlight.some((turn) => turn.key === ref)) throw new Error("turn queued or running");
  const px = spawnSync("px", ["task", "get", ref, "--json"], { encoding: "utf8", timeout: 30000 });
  if (px.status !== 0) throw new Error("ticket unavailable");
  const t = JSON.parse(px.stdout).ticket;
  if (t?.ref !== ref || !t.id || !t.project) throw new Error("ticket identity unavailable");
  const { candystoreClient, candystoreLiveness } = await import(pathToFileURL(join(root, "tick", "liveness.mjs")).href);
  const fleet = require(join(bloodbank, "dist", "fleet.js"));
  const probe = candystoreLiveness({ client: candystoreClient({ base: status.candystore.url }), fleet });
  const state = await probe({ boardId: t.project, ticketId: t.id, key: ref });
  if (state.unknown || state.live !== false) throw new Error(state.why || "liveness unknown");
  console.log(JSON.stringify({ ref, running: false, queued: false, detached: false, why: state.why }));
} catch (error) {
  console.error(`liveness UNAVAILABLE: ${error.message}`);
  process.exit(2);
}
