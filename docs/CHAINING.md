# Chaining MCP Operations

How language models actually string together MCP tool calls in practice
-- written against [`rcarmo/python-office-mcp-server`][office], because
that one's been beaten on by enough real models against real Office
documents that the patterns have settled.

[office]: https://github.com/rcarmo/python-office-mcp-server

The short version: the server does most of the work. The model walks
breadcrumbs.

## What a model is actually doing

Models don't plan. They look at the conversation, scan the tool list,
and reach for whatever rhymes. So if you want chains that finish in the
right place, the *server* has to make the next call obvious -- at every
step, not just the first one.

Three things matter, roughly in order of payoff: a small named *core
verb set* covering most intents, output that nominates the next call,
and an addressing scheme that survives between calls (anchors, IDs,
paths -- anything but offsets). Everything else is decoration.

## Core verbs beat surface area

The office server exposes 100+ tools. Its `get_instructions()`
funnels models toward eight:

> *...start with `office_help`, then prefer `office_read`,
> `office_inspect`, `office_patch`, `office_table`, `office_template`,
> `office_audit`, and `word_insert_at_anchor`. Treat specialised tools
> as fallback, diagnostic, legacy-compatibility, or expert tools when
> the core flow is insufficient.*

That sentence does a fair amount of work. It tells the model there *is*
a recommended path, that the path is verb-shaped (`help -> read ->
inspect -> patch -> audit`), and that everything else is opt-in. Without
it, models cheerfully reach for `word_parse_sow_template` when
`office_read` would do, and you end up with five-call detours for
one-call jobs.

The specialised tools still ship -- they're hidden under a "for experts"
framing, and a handful of legacy ones are filtered out of `tools/list`
entirely via a `DEPRECATED_TOOLS` set in `office_server.py`. The surface
the model *sees* is small; the surface it *can* reach is large. Be
ruthless about which tools you elevate.

## The canonical chain

Almost every successful interaction collapses to *discover -> inspect ->
mutate -> verify*:

```
office_help(goal="...")                       # plan
  office_read(file_path)                      # what's in there?
  office_inspect(file_path, ...)              # where can I attach things?
  office_patch(..., mode="dry_run")           # rehearse
  office_patch(..., mode="safe")              # do it
  office_audit(...)                           # did it stick?
```

The properties that make it hold up: discovery is a single cheap call
before anything is touched; reads are annotated `readOnlyHint: true` so
a planner that's been told to favour read-only during exploration
batches them up front; mutating tools accept a `mode` enum that lets the
model rehearse before committing; and the audit step exists *purely* so
the model has somewhere to land at the end. Without the audit, the model
declares victory based on the patch return value, which is optimistic.

## Naming is the chain

Models chain whatever rhymes. The office server leans on this rather
hard.

All Word-specific tools are `word_*`, all Excel `excel_*`, all unified
`office_*`. A model that just called `office_inspect` will reach for
`office_patch` next, not `word_patch_with_track_changes`, because the
prefix matches. Verbs are consistent across surfaces: `list_*`, `get_*`,
`inspect_*` for reads; `patch`, `insert`, `add`, `set` for writes. The
annotation inferrer in `aioumcp.py` reads those prefixes to assign
`readOnlyHint`/`destructiveHint` automatically, so naming discipline
turns into safety metadata for free.

Operation-style tools (`office_table(operation="create"|"insert_row"|...)`,
`office_comment(operation="add"|"reply"|"resolve"|"reopen"|"delete")`)
collapse what would otherwise be six or eight separate tools per surface
into one. The operation set is exposed via the schema's `Literal[...]`
enum -- which becomes a real JSON-Schema `enum` -- so the model sees the
closed set instead of guessing.

If you take one thing, take this: the prefix is the plan, the verb is
the step.

## Suggested next tools

The single change that made the server behave reliably on smaller
models. Higher-tier models will plan a chain from a tool list and a
goal; smaller ones won't -- they grab the first plausible-looking tool
and stop. The fix: every response, from every tool, ends with a
breadcrumb that names the next call.

The server does this in two complementary places.

### `office_help` returns a structured plan, not prose

```jsonc
{
  "summary": "Fill a consulting SOW template from markdown, then audit gaps before cleanup.",
  "recommended": [
    { "tool": "word_create_sow_from_markdown",
      "why":  "Best first-pass workflow for consulting SOW templates driven by markdown." },
    { "tool": "word_insert_at_anchor",
      "why":  "Use for additive narrative insertion when a section needs more prose without replacing the body." },
    { "tool": "word_audit_completion",
      "why":  "Confirms whether required sections, tables, and placeholders were actually completed." }
  ],
  "fallbacks":     ["word_generate_sow", "office_patch", "office_audit"],
  "watch_for":     ["unmapped_sections", "section_diagnostics.matched=false", "table_diagnostics.matched=false"],
  "next_step":     "Run word_create_sow_from_markdown first, review diagnostics, then fill remaining gaps with word_insert_at_anchor or office_patch(section:...).",
  "related_tools": ["word_parse_sow_template", "word_get_section_guidance", "office_template"],
  "notes":         ["Prefer template-preserving edits over rebuilding from scratch."]
}
```

Each field exists for a specific reason. `recommended[]` is the
happy-path chain *with* rationale, so the model doesn't invent the
order. `why` matters because a small model that picks badly from a flat
list often picks correctly when each entry comes with a one-line reason.
`fallbacks[]` is the second-best path when the happy one fails.
`watch_for[]` lists the *exact* diagnostic strings the next response
will carry -- models pattern-match on identical tokens, not paraphrases.
`next_step` is one imperative sentence the model copies into its own
"thought" text. `related_tools[]` widens the planning frontier without
polluting the happy path. `notes[]` carries the opinionated bits that
would otherwise live in unread documentation.

The whole thing is data. `tools/discovery_tools.py` keeps a single
`WORKFLOW_GUIDANCE` table keyed by intent (`fill_sow_from_markdown`,
`insert_architecture_narrative`, `find_insertion_point`,
`append_table_row`, `patch_estimate_workbook`, ...). `office_help`
matches the user's goal to an entry and returns the record. The
cleverness is in the table, not the code.

### Mutating tools embed compact next-tool hints in their own responses

`office_help` only fires once. The breadcrumbs have to keep coming. The
shortest example is `pptx_recommend_layout`:

```jsonc
{
  "file": "deck.pptx",
  "content_type": "title_and_content",
  "recommended":  { "index": 1, "name": "Title and Content", "score": 0.92 },
  "alternatives": [ ... ],
  "usage":        "pptx_add_slide(file_path='deck.pptx', layout_index=1, title='Your Title')",
  "next_tools":   ["pptx_add_slide"]
}
```

Three small fields. `recommended` is the structured machine-readable
choice. `usage` is a literal pre-formatted call string with the chosen
layout index already substituted in -- a model that struggles to
assemble arguments from a schema can copy it verbatim. `next_tools[]`
is a flat list of tool names the planner is allowed to jump to next, so
it doesn't shop the whole catalogue.

Mutating tools (`office_patch`, `office_table`, `word_insert_at_anchor`)
carry the same shape via a shared helper in `tools/diagnostics.py`,
which defaults `next_tools` to `["office_read", "office_inspect",
"office_audit"]` on success and `["office_help", "office_inspect",
"office_audit"]` on failure. The audit tools then return another hint
pointing back at `office_help` with a recovery goal if anything's still
wrong. The chain is closed at every node.

### Why this works on small models

A tool call is the only place a small model is *forced* to read
structured output carefully -- the response goes straight back into
context as the next turn's input. So suggestion fields stop being
decorative; they become the strongest signal in the next prompt. Models
exploit this in three obvious ways: they copy `usage:` strings
verbatim, they token-match `watch_for` strings against the next
response, and they collapse a 100-tool planning frontier to a
one-element `next_tools` list and just take it.

## More ways into the catalogue

`office_help` is the obvious entry point, but the office server treats
discovery as a small surface in its own right rather than a single tool.
Worth knowing what's actually there.

The tool accepts three shapes of input. `goal="fill_sow_from_markdown"`
is the canonical key. `goal="generate_statement_of_work"` is an alias
resolved through a small `GOAL_ALIASES` synonym table -- so the
plausible-but-wrong key still lands somewhere useful. `task="I need to
write up a statement of work from these notes"` is free text,
keyword-scored against a `TASK_KEYWORDS` table of `(goal, "space
separated keywords")` tuples; the highest-scoring goal wins. The model
can reach the same workflow via the structured key it learned from a
prior call, the wrong synonym it guessed, or the messy English the user
originally typed.

Called with no arguments, `office_help()` returns the catalogue --
`common_goals: [...]` plus the core tool list and the tier taxonomy --
with a `next_step` telling the model to call again with a goal. So
browsing is a single cheap call. Called with an unknown goal, it
returns `supported_goals: [...]` rather than an error string. The
failure response *is* the catalogue. This matters because models
handle a typo gracefully when the next-turn input contains the right
spelling; they don't when it just contains "error".

Document type is inferred when not given. `_infer_document_type()`
looks at the goal, the file extension, and the task text -- `"slide"`
or `"deck"` lands on PowerPoint, `"workbook"` or `"cell"` on Excel,
`"docx"` or `"section"` on Word. Saves the model a parameter it would
otherwise have to either fill in or omit defensively.

The response is shapeable. `format="detailed"` extends the payload
with full `notes` and an explicit `workflow_steps` array (`"1. Start
with X. 2. If needed, continue with Y. 3. Validate with Z."`); the
default `summary` keeps it compact. `constraints=["preserve_template_structure"]`
or `constraints=["additive_narrative_edits"]` re-rank the
`recommended[]` list so the matching tools come first. The same goal
produces different chains depending on caution level, which means the
model doesn't need a separate "safe" goal for every existing one.

Every `office_help` payload carries the tier taxonomy in a `tool_model`
field: `core_tools`, plus `advanced_tool_classes` partitioned into
`fallback`, `diagnostic`, `legacy_compatibility`, and
`expert_specialized`. The model sees the philosophy on every call --
not just *where* to go, but *which tier* the destination sits in. That
turns out to matter, because a model that's just been told to prefer
core tools will reliably ignore a `legacy_compatibility` entry it would
otherwise have grabbed.

Domain-specific recommenders sit alongside the global one.
`pptx_recommend_layout(file_path, content_type)` is the worked example:
it returns a scored choice, ranked alternatives, a copy-paste `usage:`
string, and a `next_tools` hint. Other domains could grow their own
(`excel_recommend_chart_type`, `word_recommend_section_target`) without
disturbing the central catalogue. "Smart pickers" inside a domain are
cheaper than asking the model to guess.

Deprecated tools are hidden, not deleted. The `DEPRECATED_TOOLS` set in
`office_server.py` filters legacy names out of `tools/list`, so the
model never sees them in the catalogue. They still work if explicitly
called, for backwards compatibility, but they're invisible to a fresh
planner. Smaller surface, no broken integrations.

The docstrings themselves are part of the discovery surface. Every
consolidated tool carries a `Replaces:` line listing the legacy tools
it subsumes and an `Examples:` block with three or four representative
calls. Both are visible to the model through MCP's standard tool
descriptions, so the catalogue teaches as it lists. The model that
sees `office_audit`'s docstring saying *"Replaces:
excel_audit_placeholders, word_audit_completion, word_audit_sow,
pptx_audit_placeholders"* will not go looking for those names. Cheap
lesson.

### What deliberately isn't there

The absences are as informative as the presences, because they're
exactly the things a server author would reach for next.

There's no `tools/search?q=...` over the catalogue. There's no
`tools/describe(tool_name)` for fetching the full schema of a single
tool on demand -- the model has to scan the `tools/list` payload.
There's no semantic / embedding-based goal lookup; it's all keyword
tables and aliases. There's no "tools related to this one" graph
beyond the per-goal `related_tools[]` list. And there's no usage
telemetry feeding back into the recommendations.

The choice is consistent. The keyword table is small enough to
maintain by hand and easy to read in a code review, which is the
point -- discoverability that depends on a model the maintainer can't
inspect tends to drift. If the catalogue ever outgrows hand-curation,
a semantic layer is the obvious next move; until then, every routing
decision is a line in a Python file.

## Addressing: anchors, not offsets

The biggest reason chains break is the model losing the thread between
calls. "Insert a paragraph after the introduction" is fine in English
but disastrous if you ask the model to remember a character offset
across three tool calls.

The server makes the *server* responsible for stable addresses.
`word_list_anchors` and `word_document_map` return a structured, named
map of the document -- headings, sections, named bookmarks.
`word_insert_at_anchor` takes an anchor name and inserts relative to it.
`office_patch` accepts `section:` targets that resolve through the same
map (`"section:Risks and Mitigations"`, `"after:Deliverables"`).

The model never has to remember "byte 4823". It remembers `"Risks and
Mitigations"`, which is a thing it can already see in its own prior
turn. The addressing layer is symbolic and human-legible, so the model's
working memory holds.

Rule of thumb: return identifiers your tools will later accept as
input. If you find yourself returning data the model has to *describe
back* to you in natural language, you've made a chain that will
eventually misfire.

## Diagnostics as the back-edge

Linear chains are easy. Real chains have loops, and loops only happen
when the server invites the model back. The instructions string says it
out loud:

> *If generation results include `unmapped_sections` or `unmatched`
> diagnostics, inspect the template structure with
> `word_parse_sow_template`, `word_list_sections`, `word_list_anchors`,
> `word_document_map`, and `word_list_tables` before retrying.*

Three things to lift. Name the diagnostic field -- `unmapped_sections`
is a verbatim string the model sees in the response and again in the
prompt, and identical strings beat paraphrases for pattern-matching.
Name the recovery tools in the same breath, so the model doesn't have to
shop for a fix. And keep recovery cheap: every tool listed there is
read-only, so the penalty for "I'm not sure, let me look again" is one
extra round-trip, not a destructive misfire.

In practice this is mostly return-shape discipline. Every mutating tool
returns the same envelope, courtesy of `build_mutation_diagnostics()`:

```jsonc
{
  "success": true|false,
  "status":  "success" | "partial_success" | "skipped" | "failed",
  "warnings":          [...],
  "matched_targets":   [...],
  "unmatched_targets": [...],
  "skipped_targets":   [...],
  "diagnostics":       {...},
  "next_tools":        [...]
}
```

The model can decide whether to commit, retry, or escalate without
re-reading the document. `status` is a small enum it branches on without
needing prose comprehension.

## Modes turn one tool into four

`office_patch` is the same code path whether you ask for `dry_run`,
`best_effort`, `safe`, or `strict`. From the model's perspective it's
four tools, because the schema enum makes the choice visible and the
docstring spells out the trade-offs.

`dry_run` computes the changes and returns what *would* happen, writing
nothing. `best_effort` applies what it can and reports the rest as
`unmatched_*`. `safe` refuses to overwrite the source file -- it
requires a distinct `output_path` and fails loudly otherwise.
`strict` treats anything less than total success as a failure, with a
warning explaining why.

Discovery cost scales with tool count, not mode count, so one tool with
four modes is one entry in `tools/list`. Prompt cost stays low because
the model doesn't have to remember whether `office_patch_dry_run`
exists. And `dry_run -> safe -> strict` is a sensible escalation
chain that doesn't require any new tools. If you have N tools that
differ only in caution level, collapse them and document the escalation
in the docstring.

## Annotations let planners parallelise

The annotations the upstreamed `aioumcp.py` now emits (`readOnlyHint`,
`destructiveHint`, `openWorldHint`) aren't just metadata -- they're
scheduling hints. A planner can run all read-only calls in parallel
during discovery, serialise destructive calls behind a confirmation, and
isolate open-world calls (`web_*`, `azure_*`) so a network blip doesn't
knock over the local document workflow.

You get this for free from naming conventions. The cost is being honest
about which of your tools mutate state. Lying here is the most common
cause of "why did the model just delete my doc?" incidents.

## Strictness keeps chains from drifting

The fork's `aioumcp.py` rejects unknown arguments outright. Sounds
unfriendly until you've watched a model invent a `force=True` parameter
your tool silently ignores, then write a confident summary saying it
forced the operation.

Strict argument validation forces a visible error early in the chain,
gives the model a concrete signal to consult `office_help` or the
schema, and prevents silent drift between what the model thinks
happened and what did. Pair it with `additionalProperties: false` on
every input schema (also now upstream) and your tool contracts become
genuinely contractual.

## How the design got there

It's worth saying out loud what these tricks are *for*, because each one
exists in response to a specific failure mode and the shape of the fix
is dictated by the shape of the failure.

The first failure is the *silent partial success*. Generation tools
returned "applied N sections" while quietly skipping half of what the
user asked for. The model couldn't see the gap, so it confidently
reported success. The fix is to name the gap: explicit
`unmapped_sections`, per-target `matched: bool`, an envelope-level
`status` enum the model can branch on. Diagnostics are useless if the
model has to infer them from prose; they have to be fields with
predictable names.

The second failure is *blind destructive writes*. The model would call
the patch tool, the patch would partially apply, and the source file
would already be munged before anyone realised the addressing was wrong.
The fix is the mode enum: `dry_run` lets the model rehearse, `safe`
makes overwriting the source file an explicit opt-in, `strict` lets it
escalate when it's confident. The model now has somewhere cautious to
retreat to.

The third failure is *the planning frontier*. Even with good
diagnostics, a model staring at 100+ tools doesn't know where to start.
Prose instructions in `get_instructions()` weren't enough -- the model
read them once and then ignored them. So planning became a tool call:
`office_help(goal=...)` returns a structured record with a recommended
chain, fallbacks, and a list of diagnostic strings to watch for. Models
take the suggestion because it's the cheapest thing to do.

The fourth failure is *recovery getting too expensive*. Once the model
started looping back through inspection tools after a partial failure,
those tools became a hot path. Re-parsing the same template on every
retry burned tokens and time, so the heavier inspection tools learned to
cache: a JSON metadata cache in `tools/metadata_cache.py`, keyed by
file fingerprint (path + mtime + size + sha256), with schema versioning
and atomic writes. Cheap recovery is the only kind that gets used.

The fifth failure is *addressing drift*. Section-name targets only work
if the model knows the section names, and asking it to remember them
across calls is a losing bet. So the inspection tools were extended to
hand back the exact addresses the patch tool will accept --
`word_list_anchors`, `word_document_map`, the structured `aspect`
options on `office_inspect`. The model never has to invent an address;
it picks one out of the previous response.

The sixth failure is more boring but bites hardest in production: *file
corruption from agent edits*. python-docx and openpyxl can produce ZIPs
with duplicate entries when edits happen in quick succession, leaving
Office prompting the user to "repair" the file. `tools/save_utils.py`
saves to a temp file and atomically replaces the original, deduplicating
ZIP entries on the way. The model can't see this trick at all -- which
is the point. Reliability the model relies on without knowing it.

The pattern across all six: every change either *names a failure mode
the model can see* or *names a recovery path the model can take*, or
both. The model's intelligence is constant; the server's vocabulary
keeps growing.

## Techniques inventory

A flat reference, loosely ordered by payoff:

| Technique | Where it lives | What it earns |
|---|---|---|
| Structured `office_help` (`recommended`/`why`/`fallbacks`/`next_step`/`watch_for`/`related_tools`/`notes`) | `tools/discovery_tools.py` (`WORKFLOW_GUIDANCE`) | Turns planning into a tool call. Highest payoff for small models. |
| Free-text `task=` -> goal via keyword scoring + `goal=` aliases | `tools/discovery_tools.py` (`TASK_KEYWORDS`, `GOAL_ALIASES`) | Same workflow reachable from the canonical key, the wrong synonym, or messy English. |
| No-arg `office_help()` returns the catalogue | `tools/discovery_tools.py` | Browsing is one cheap call. |
| Unknown-goal response returns `supported_goals` | `tools/discovery_tools.py` | The failure response *is* the catalogue; models self-correct. |
| `format="detailed"` and `constraints=[...]` re-shape the response | `tools/discovery_tools.py` | One goal, multiple chains, no extra goal entries. |
| `tool_model.{core_tools,advanced_tool_classes}` carried in every help payload | `tools/discovery_tools.py` (`CORE_TOOLS`, `ADVANCED_TOOL_CLASSES`) | Tier philosophy visible on every call, not just in the README. |
| Domain-specific recommenders with `usage:` and `next_tools` | `tools/pptx_advanced_tools.py` (`pptx_recommend_layout`) | "Smart pickers" inside a domain are cheaper than asking the model to guess. |
| Docstring conventions (`Replaces:`, `Examples:`) | throughout | The catalogue teaches as it lists; models stop reaching for the names listed under `Replaces:`. |
| `next_tools[]` and `usage:` in every mutating response | `tools/diagnostics.py`, various | Forward breadcrumbs at every node; `usage` is copy-paste-ready next-call text. |
| Named diagnostic fields (`unmapped_sections`, `section_diagnostics`, `unmatched_targets`, `skipped_targets`) | `tools/word_advanced_tools.py`, `tools/diagnostics.py` | Makes failures visible to the model; `watch_for` token-matches the same strings. |
| Standard mutation envelope (`success`/`status`/`warnings`/`matched_targets`/`unmatched_targets`/`skipped_targets`/`diagnostics`/`next_tools`) | `tools/diagnostics.py` | Branch on a `status` enum without reading prose. |
| `mode` enum (`dry_run`/`best_effort`/`safe`/`strict`) | `tools/office_unified_tools.py` (`tool_office_patch`) | Four tools for the price of one schema entry; supports a natural escalation chain. |
| `operation` enum on consolidated tools (`office_table`, `office_comment`, `office_template`) | `tools/office_unified_tools.py` | Collapses six to eight surface verbs per surface into one tool. |
| Prefix-by-surface naming (`word_*`, `excel_*`, `pptx_*`, `office_*`) | everywhere | Models chain whatever rhymes; the prefix narrows the next call. |
| MCP annotations inferred from naming (`readOnlyHint`/`destructiveHint`/`openWorldHint`) | `aioumcp.py::_infer_tool_annotations` | Free scheduling hints, free safety metadata. |
| `Literal[...]` enums for operation/mode/check arguments | throughout | Become real JSON-Schema `enum`s, so the model sees the closed set instead of guessing. |
| Symbolic addressing (`section:Name`, `after:Heading`, `slide:1/Title 1`, `'Sheet'!B5`) | `tool_office_patch`, `tool_word_insert_at_anchor` | Addresses the model can already see in its own prior turn. |
| Map / anchor discovery (`word_list_anchors`, `word_document_map`, `office_inspect`) | `tools/word_advanced_tools.py`, `tools/office_unified_tools.py` | Returns the addresses the addressing layer accepts. |
| Strict unknown-arg rejection + `additionalProperties: false` | `aioumcp.py` (now upstream) | Stops silent drift when models invent parameters. |
| Type coercion for stringy clients | `aioumcp.py::_coerce_value` | Some clients send numbers as strings; coerce rather than fail. |
| Deprecated-tool hiding (`DEPRECATED_TOOLS` filtered from `tools/list`) | `office_server.py` | Keeps the surface area small without deleting code. |
| File-fingerprint metadata cache | `tools/metadata_cache.py` | Cheap recovery loops via `(path, mtime_ns, size, sha256)` keys, schema versioning, atomic writes. |
| Atomic `safe_save_*` helpers (write-temp, replace, dedupe ZIP entries) | `tools/save_utils.py` | Prevents Office "repair" prompts after agent edits -- important because the next call reads the file back. |
| `restart_server` tool | `office_server.py` | Lets the agent reload code changes without a human in the loop. |

Most of these look like return-shape and naming discipline because
that's what they are.

## What it looks like end to end

> *"Add a 'Risks' section to `sow.docx`, with three bullet points
> pulled from the executive summary."*

A competent planner against this server lands roughly here:

```
office_help(goal="add risks section, source from exec summary",
            document_type="word")
  -> {recommended: [office_read, word_list_anchors, office_patch, office_audit],
      next_step: "read content, list anchors, patch with section: target, then audit",
      watch_for: ["unmapped_sections", "anchor not found"]}

office_read(file_path="sow.docx")
  -> the executive summary text

word_list_anchors(file_path="sow.docx")
  -> ["Executive Summary", "Scope", "Deliverables", "Pricing"]
     (no "Risks" -- so insert, not patch)

office_patch(file_path="sow.docx",
             changes=[{target: "after:Deliverables",
                       value:  "## Risks\n- ...\n- ...\n- ..."}],
             mode="dry_run")
  -> {status: "success", matched_targets: [...], unmatched_targets: [],
      next_tools: ["office_read", "office_inspect", "office_audit"]}

office_patch(... same changes ..., mode="safe", output_path="sow.out.docx")
  -> {status: "success", output_path: "sow.out.docx",
      next_tools: ["office_audit"]}

office_audit(file_path="sow.out.docx", checks=["completion", "tracking"])
  -> confirms the new section is present, properly numbered, and that
     track-changes annotations are intact
```

Six calls, no detours, no destructive mistakes, addressable by names the
user themselves used. That's the shape a well-designed MCP surface
encourages.

## Checklist for your own server

* Pick five to ten *core verbs* and name them in `get_instructions()`.
* Use *consistent prefixes* by surface (`word_`, `excel_`, `office_`).
* Provide a *discovery tool* (`*_help`) that returns recommendations as
  a structured record, not as prose.
* Make the discovery tool browseable -- no-arg call returns the
  catalogue, unknown input returns the supported set.
* Accept free-text input alongside structured keys, with a small
  keyword/alias table doing the routing.
* Embed forward breadcrumbs in every tool response: at minimum
  `next_tools: [...]`, plus `usage: "<exact call>"` whenever the
  current tool produced a value the next one needs.
* Make read tools read-only and let the annotation inferrer mark them.
* Provide a *map / anchors tool* so addresses survive between calls.
* Give every mutating tool a *`mode` enum* including `dry_run`.
* Return *named diagnostic fields* (`unmapped_*`, `unmatched_*`,
  `skipped_*`) and cite the recovery tools in the docstring.
* Standardise the mutation envelope so the model can branch on a single
  `status` enum.
* Reject unknown arguments strictly (`additionalProperties: false`).
* Provide an audit tool so the model has somewhere to land at the end
  of the chain.
* Cache anything the recovery loop calls more than once.
* Make repeat calls safe -- models retry, and they should be allowed to
  without doubling the document.

Do the boring work in the schema and the descriptions. The model does
the clever bit.

---

> I made the mistake of asking Opus to revise this and had to beat it back into submission. Apologies for the cognitive assault.
