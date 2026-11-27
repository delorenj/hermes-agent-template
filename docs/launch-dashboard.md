# The launch dashboard

Every PM in launch mode keeps one launch dashboard current. The dashboard is a runbook for Jarad.
It lists every step the PM cannot finish without him, in order, with each step spelled out. When
every step is checked off, nothing stands between the product and publication in its marketplace
or marketplaces, except agent work that the PM tracks on the same page.

The owner asked for this on 2026-11-20 (FLUME-104). The level is **every launch-mode PM**: any desk
whose repo's `.launch.yaml` says `mode: launch`. It ships once, in the hermes-agent PM template:
`launch-dashboard.py`, the shared marketplace catalog, the plan ops in `launch-pass.py`, and the
rules in the SOUL's `## Launch mode` section. A repo gets it through
`flume remediate hermes.pm-scaffold`. It was dogfooded first on green-in-between, gruvato and tolt.
For a `develop` (legacy `steady`), `polish` or `live` repo, the dashboard is inert: it is
launch-only. In polish mode the pass machinery is shared but the dashboard never renders or
publishes, and a polish plan's `dashboard` ops are refused with a receipt note.

## The rules

These are the owner's rules. The driver enforces each one, so a PM cannot ship a dashboard that
breaks them.

| # | Rule | How it is enforced |
|---|------|--------------------|
| 1 | Every ticket reference is a link. | The renderer links every `ABC-123` it finds in any field, using the ticket's Plane URL. The PM never writes a ticket link by hand. |
| 2 | Every ticket reference has a compact hover preview: the gist. | The renderer reads each ticket at render time and embeds a card. The card shows the title, lane, priority, a one-line gist from the description's goal, and whether the ticket is waiting on Jarad. Hover shows it; on a phone, the first tap shows it and the link inside opens the ticket. |
| 3 | Every item is actionable, and the action is stated explicitly. | `title` is an imperative action. Every item needs `steps` and `done_when`; `reply` (the exact words Jarad sends back) is needed wherever the PM cannot read completion itself. The validator refuses an item that is missing any of them. |
| 4 | Items are laid out in order, like a runbook. | `order` together with `after` (the items that must be done first) gives one numbered sequence. Done items stay in place, collapsed and checked, so the numbering never shifts under Jarad. |
| 5 | Every artifact mention is a link. | `artifacts` takes https URLs only. The renderer also links every repo path that exists in the repo (to its GitHub URL on `main`) and every bare https URL in any text. A `file://` link, a local path outside the repo (`/home/...`, `~/...`) or a bucket key (`projects/<id>/<date>/...`, `builds/...`, `share/...`) is refused. |
| 6 | Steps of reasonable complexity are spelled out verbatim. | Items of kind `account`, `console`, `legal`, `money`, `credential` and `publish` need at least two concrete steps, and the first must name where to go (a full URL). "Acquire a D-U-N-S number" alone is refused. |
| 7 | The set is complete: when every item is checked, the product can be published. | Completeness is computed on every render (see below). Any gap shows on the page as a red banner and in the PM's brief as work to do. The PM closes a gap by adding an item, filing an agent ticket or recording evidence. A gap is never hidden. |

The charter adds three rules from the existing launch-mode rules:

- **No verify-the-build steps.** His play is the gate. An item that asks Jarad to test, verify or
  sign off a build is refused.
- **Owner-only steps carry a recommendation and a default.** `recommend` says what the PM would
  do. `default` is what happens on silence; it is allowed only for a reversible step.
- **A step belongs to Jarad only when no agent path exists.** If the catalog or the repo gives an
  agent a way to do it (an App Store Connect API write, a Play Developer API call, a hub lane), it
  is agent work: an agent ticket, not a step for Jarad. Steps that stay with him are account
  holder actions, legal acceptances, money, credentials and key authorizations, device access,
  product decisions, verification codes, and the final submit or publish press.

## Where it lives

- **Data:** `<desk>/runtime/launch-dashboard.json` (schema `launch-dashboard/1`). The PM writes it
  only through plan ops. It is runtime state, never committed.
- **Page:** a stable public URL:
  `https://s3.delo.sh/projects/<project>/<declared>/launch-dashboard/index.html`, where `<declared>`
  is the declaration's `declared:` date. It is published by the artifacts skill's `publish.py` and
  read back before the URL is printed. `dashboard.json` is published beside it. When a launch is
  re-declared, the URL changes with it.
- **Catalog:** `<desk>/.scripts/lib/launch-marketplaces.json` (schema `launch-marketplaces/1`), the
  shared list of what Google Play and the App Store require before publication. It was checked
  against official documentation on 2026-11-20. It is a template asset: fix it once in flume, and
  every PM inherits the fix.

## The data

```json
{
  "schema": "launch-dashboard/1",
  "marketplaces": ["google_play", "app_store"],
  "items": [{
    "id": "play-create-app",
    "order": 50,
    "title": "Create the Green In Between app in Play Console",
    "why": "Nothing Android can upload, sign or be reviewed until the app exists.",
    "kind": "console",
    "marketplace": "google_play",
    "steps": [
      "Open https://play.google.com/console/u/1/developers and pick the delorenj account.",
      "Press Create app. App name: Green In Between. Default language: English (United States). App or game: Game. Free or paid: Free.",
      "Tick both declarations and press Create app."
    ],
    "reply": "Play app created",
    "done_when": "The Play Developer API lists io.lasertoast.greeninbetween for the account.",
    "recommend": "Do it now: it unblocks POOL-65 and POOL-198.",
    "default": null,
    "refs": ["POOL-198", "POOL-65"],
    "artifacts": [{"label": "Store prep audit", "url": "https://github.com/delorenj/green-in-between/blob/main/docs/audits/pool-184-store-preparation.md"}],
    "after": ["decide-play-account"],
    "covers": ["gp.create-app"],
    "status": "open",
    "evidence": null,
    "verified_at": null
  }],
  "requirements": {
    "gp.data-safety": {"status": "agent", "ref": "POOL-187"},
    "gp.closed-test-12-testers-14-days": {"status": "owner", "item": "play-closed-test"},
    "gp.privacy-policy": {"status": "met", "evidence": "https://greeninbetween.com/privacy/"},
    "gp.payments-profile-merchant": {"status": "na", "why": "Free game, no in-app purchases."}
  }
}
```

`kind` is one of: `account`, `console`, `legal`, `money`, `credential`, `device`, `decision`,
`reply` or `publish`. `status` is `open` or `done`. An item is `done` only after an agent has
checked `done_when`, and `evidence` then holds a link or a one-line read-back. Jarad's reply alone
is a hint to check, never the evidence.

## Completeness, computed

A render reports a gap for each of the following:

1. **Catalog coverage.** Each catalog requirement for a declared marketplace has no entry in
   `requirements`, or has an entry that does not hold up:
   - `met` with no evidence;
   - `agent` whose ticket is not on the board;
   - `owner` whose item does not exist;
   - `na` with no reason.

   Conditional requirements (`applies_when` is not `always`) still need an entry: `na` with the
   reason the condition does not hold.
2. **A launch-critical ticket waiting on Jarad.** An open ticket labelled `lifecycle:needs-input` or
   `lifecycle:human` that no item references. `lifecycle:blocked` alone is not waiting on him (it is usually
   another ticket or scope), and a Done or Cancelled ticket never waits.
   **Tickets that could not be read.** When a ticket card cannot be read and no cached copy exists, one gap names
   them ("N tickets could not be read: REF, REF; the page cannot prove completeness"): a launch-critical ticket
   waiting on him may be among them, so the page never says Complete on missing data.
3. **A missing publish item.** A declared marketplace has no item of kind `publish`.
4. **An item that breaks a rule** (the table above).

Gaps are not errors: the page still renders. But it says "incomplete" and names each gap, and the
brief hands the same list to the PM as its work list. Every publish item also shows, automatically,
the agent work still open on the path: open launch-critical tickets, plus owner-filed High or
Urgent bugs. Checking it off can never hide work in flight. The list is rendered once, as its own section right
before the first publish item; each publish card carries a one-line pointer with the count.

## The page

From top to bottom:

1. **Header.** The product, the finish line, the marketplaces, and the count of steps left, done
   and gaps. It shows when the page was last refreshed (every apply re-renders it), and how to reply: the PM's Telegram bot
   and "or comment on the linked ticket".
2. **Banner.** "Incomplete" with the gap list, or "Complete: every barrier is on this page".
3. **Your runbook.** One numbered card per item. Each card has a checkbox (the PM's verified state),
   the action, why it matters, the verbatim numbered steps, "When done, reply", "The PM checks",
   the recommendation and default, ticket chips with hover cards, artifact links, and "after step N"
   for each dependency. A publish card adds a one-line "Agent work" pointer to the single "Agent work still open"
   section placed before the first publish item.
4. **Store checklist.** One row per catalog requirement for each marketplace. The status is one of:
   met (with an evidence link), agent (with a ticket chip), you (step N), n/a (with the reason),
   or GAP.
5. **Footer.** The schema, the render time, the commit of the driver, and a link to `dashboard.json`.

It works at phone width, in light and dark modes, and needs no sign-in to read.

## Keeping it current (the PM's duty)

- The launch pass's plan carries `"dashboard": [ops]`. Every `--apply` re-renders and republishes
  the page, so ticket states and hover cards refresh with no extra model call. A pass with nothing
  to change sends no dashboard ops.
- The ops:
  - `{"op": "upsert", "item": {...}}` adds or replaces an item.
  - `{"op": "check", "id": "...", "evidence": "..."}` marks it done.
  - `{"op": "uncheck", "id": "...", "why": "..."}` reopens it.
  - `{"op": "remove", "id": "...", "why": "..."}` deletes it.
  - `{"op": "requirement", "id": "gp.x", "status": "...", ...}` sets a catalog row.
  - `{"op": "marketplaces", "value": ["google_play"]}` sets the declared marketplaces.
- A chat or Bloodbank turn can apply the same ops with `launch-dashboard.py --ops FILE`. Do this
  when Jarad says a step is done: check `done_when` first, then `check`.
- Every gap in the brief is work. Close it the same pass when you can: add the item, file the agent
  ticket (in the launch epic), or record the evidence.
- When the last publish item is checked, the finish line is met on evidence. File the exit ticket
  (the charter's exit rule).

## How the driver reads the rules

`template/.scripts/launch-dashboard.py` is the driver. Where the rules above need a precise reading, this is it:

- **Required fields.** `id`, `order`, `title`, `why`, `kind`, `steps`, `done_when` and `recommend` on every item;
  `reply` on every kind except `publish` (the final press is read from the store, not from a reply). A `publish`
  item also names its `marketplace`. Unknown fields are refused, so a typo cannot hide.
- **Default.** Only `decision`, `reply` and `device` items may carry a `default`: those are the steps the PM can
  take on silence. Account, console, legal, money, credential and publish steps need Jarad's own hands, so their
  `default` is `null`.
- **Launch-critical** (the waiting-ticket gap and the publish card's agent list) means the declaration's
  `cut_line.launch_critical` hint plus every open ticket in Todo, since Todo holds only launch-critical work.
- **Agent work still open** (one section before the first publish item, not repeated per card) is those tickets plus
  owner-filed High or Urgent bugs.
- **Inline code.** Text in backticks in any field renders as `<code>` (escaped), on the page and in the hover card's title and gist; an unpaired backtick stays literal.
- **No marketplace declared** is itself a gap, and so is a board the driver could not read: the page never says
  "Complete" on an unproven check. An `agent` requirement whose ticket is Done still holds; a cancelled or
  missing one does not.
- **Ticket links.** `ABC-123` is linked when `ABC` is a ticket prefix of this board or a driven child scope. A
  ref in an item's `refs` must carry this board's prefix. A ref the board listing does not carry (outside the launch
  epic or cut line) is still read by ref and gets a card; "not on the board" is only for a ref that does not resolve. The hover card is built from the ticket at render time.
- **Repo paths** link when `git ls-files` lists them in the repo, or in a child scope that is its own repo, and the
  remote is on GitHub. The link is the `main` blob.
- **Modes.** `--check` validates the data and the catalog (exit 1 on a problem). `--ops FILE` applies ops, then
  renders and publishes. `--render [--out DIR] [--data FILE]` writes `index.html` and `dashboard.json` and touches
  nothing else. `--publish` re-renders from the data file and publishes. `--brief` is the section the launch pass
  hands the PM.
- **Repo-owned notes.** A repo may add `.scripts/launch-pass.repo.md`. `launch-pass.py --brief` prints it right
  after the shared pass contract. The scaffold never writes, compares or removes it (33GOD-114). A repo may also add an
  executable `.scripts/launch-pass.repo-monitor`: its stable stdout lines (no timestamps) end the `--monitor` snapshot
  as `repo|...`, so off-board news (a gate receipt, a store review verdict) wakes the pass. A failing or slow (60 s)
  script is one fixed `repo|UNAVAILABLE` line; at most 40 lines are kept.

- **Ticket reads are bounded.** A render reuses a ticket card read in the last 30 minutes while the ticket's lane group is
  unchanged (`runtime/launch-dashboard-cards.json`) and stops reading after 90 seconds: the remaining cards come from
  that cache (or show "unreadable"), the receipt (reads failed, reads skipped, cards from stale cache, cards unreadable) and the page footer say how many are out of date, and the next apply
  refreshes them. A long board never outlives the pass's tool call.
- **Page privacy.** The page and `dashboard.json` are public at a guessable path. Ticket gists go public with them, and an
  item must never carry tester names, emails or other personal data (the pass prompt says so).
