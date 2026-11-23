// Isolated collaborator model: input Todo is explicitly constructed by the test,
// NOT PM-applier output (public --apply is disabled). Real injected Krebs runTick
// exercises that model only; no live board, NATS or Candystore is contacted.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const root = process.env.PM_RECONCILE_KREBS_ROOT || join(homedir(), "code", "33GOD", "krebs");
const dependency = async (path) => import(pathToFileURL(join(root, path)).href);
const { loadBloodbank } = await dependency("tick/deps.mjs");
const { scopesFromRegistry } = await dependency("tick/registry.mjs");
const { runTick } = await dependency("tick/run.mjs");
const { MemoryJournal } = await dependency("tick/journal.mjs");
const { FakePlane, NOW, SOLO, issue, registryFixture, fakePublisher, labelId } = await dependency("tick/test/helpers.mjs");
const { fleet, nats, registry: bbRegistry } = loadBloodbank();
const tickets = JSON.parse(readFileSync(process.argv[2], "utf8"));
const registry = registryFixture();
registry.agents["solo-pm"].plane.wip = 2;
const plane = new FakePlane({ [SOLO]: { issues: Object.values(tickets).map((t) => issue({
  seq: Number(t.ref.split("-")[1]),
  state: { Backlog: "backlog", Todo: "todo", "In Progress": "ip" }[t.state_name],
  labels: t.label_names,
  priority: t.priority,
  sort: t.sort_order,
  updated: "2026-11-14T10:00:00Z",
})) } });
const { scopes, skipped } = scopesFromRegistry(registry, { activation: bbRegistry.bloodbankActivation,
  executionMode: () => "legacy", board: "SOLO" });
const publish = fakePublisher();
const report = await runTick({ registry, scopes, skipped, plane, fleet, nats, publish,
  journal: new MemoryJournal(), now: () => NOW, timeZone: "America/New_York" });
assert.equal(report.ok, true, JSON.stringify(report.scopes.map((s) => s.errors)));
const scope = report.scopes.find((s) => s.agentId === "solo-pm");
assert.deepEqual(scope.occupied, [], "two held In Progress tickets hold zero slots");
assert.equal(scope.free, 2);
assert.deepEqual(scope.dispatches.map((d) => [d.key, d.result]), [["SOLO-3", "dispatched"], ["SOLO-4", "dispatched"]]);
assert.deepEqual(plane.writes().map((w) => [w.id, w.fields.state, w.addLabels]), [
  ["i-3", "s-ip", [labelId("agent:working")]],
  ["i-4", "s-ip", [labelId("agent:working")]],
]);
assert.deepEqual(publish.sent.map((c) => [c.data.context.ticket_key, c.data.context.reason,
  c.data.target_agent_id, c.type]), [
  ["SOLO-3", "ticket-delegation", "solo-pm", "bloodbank.agent.invocation.start"],
  ["SOLO-4", "ticket-delegation", "solo-pm", "bloodbank.agent.invocation.start"],
]);
console.log("isolated collaborator model: real injected Krebs runTick dispatched two modeled Todo tickets; NOT PM apply or production acceptance");
