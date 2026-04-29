# Chaining MCP Operations

How language models actually string together MCP tool calls — written
against [`rcarmo/python-office-mcp-server`][office], because that one's been
beaten on by enough real models against real Office documents that the
patterns have settled.

[office]: https://github.com/rcarmo/python-office-mcp-server

Short version up front, in case you stop reading: the server does most of
the work. The model just walks the breadcrumbs.

## The shape of a chain

Models don't plan. They look at the conversation, scan the tool list, and
reach for whatever rhymes. So if you want chains that finish in the right
place, the *server* has to make the next call obvious at every step.

Three things to get right, roughly in order of payoff:

1. A small, named *core verb set* that covers most of what people actually
   ask for.
2. Output that nominates the next call — not just data, but breadcrumbs.
3. An addressing scheme that survives between calls (anchors, IDs, paths
   — anything but offsets).

Everything else is decoration.

## Core verbs beat surface area

The office server exposes 100+ tools. Its `get_instructions()` aggressively
funnels models toward eight:

> *…start with `office_help`, then prefer `office_read`, `office_inspect`,
> `office_patch`, `office_table`, `office_template`, `office_audit`, and
> `word_insert_at_anchor`. Treat specialised tools as fallback,
> diagnostic, legacy-compatibility, or expert tools when the core flow is
> insufficient.*

That sentence does a fair amount of work. It tells the model there *is* a
recommended path, that the path is verb-shaped (`help → read → inspect →
patch → audit`), and that everything else is opt-in. Without it, models
cheerfully reach for `word_parse_sow_template` when `office_read` would
do, and you end up with five-call detours for one-call jobs.

Be ruthless about which tools you elevate. The server still ships the
specialised ones — it just hides them under a "for experts" framing.

## The canonical chain: discover → inspect → mutate → verify

Almost every successful interaction collapses to:

```
office_help(goal="…")                       # plan
  └─ office_read(file_path)                 # what's in there?
        └─ office_inspect(file_path, …)     # where can I attach things?
              └─ office_patch(…, mode="dry_run")   # rehearse
                    └─ office_patch(…, mode="safe")  # do it
                          └─ office_audit(…)         # did it stick?
```

Four properties make this hold up:

- *Discovery first.* `office_help` returns recommendations, not data, so
  the model spends one cheap call to plan before touching the document.
- *Read before write.* `office_read`/`office_inspect` carry
  `readOnlyHint: true`, so a planner that's been told to favour read-only
  during exploration naturally batches them up front.
- *Dry-run before mutate.* `office_patch` accepts `mode: dry_run |
  best_effort | safe | strict`. Models gravitate to `dry_run` when the
  docstring tells them to, and the dry-run output becomes the prompt for
  the real call.
- *Audit at the end.* `office_audit` exists precisely so the model has
  somewhere to land. Without it, the model declares victory based on the
  patch return value — and that's optimistic.

## Naming is the chain

Models chain whatever rhymes. The office server leans on this rather hard:

- All Word-specific tools are `word_*`, all Excel `excel_*`, all unified
  `office_*`. A model that just called `office_inspect` will reach for
  `office_patch` next, not `word_patch_with_track_changes`, simply
  because the prefix matches.
- Verbs are consistent across surfaces — `list_*`, `get_*`, `inspect_*`
  for reads; `patch`, `insert`, `add`, `set` for writes. The annotation
  inferrer in `aioumcp.py` reads those prefixes to assign `readOnlyHint`
  / `destructiveHint` automatically, so naming discipline turns into
  safety metadata for free.
- Operation-style tools (`office_table(operation="create"|"insert_row"|…)`,
  `office_comment(operation="add"|"reply"|"resolve"|"reopen"|"delete")`)
  collapse what would otherwise be 6–8 separate tools into one. The
  operation set is exposed via the schema's `Literal[...]` enum, so the
  model doesn't have to guess.

If you take one thing from the office server, take this: the prefix is
the plan, the verb is the step.

## Suggested next tools

Easily the highest-ROI trick on small models. Higher-tier models will plan
a chain from a tool list and a goal. Smaller ones won't — they grab the
first plausible-looking tool and stop. Fix: every response, from every
tool, ends with a breadcrumb that names the next call.

The server does this in two places.

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

Why each field is there:

- `recommended[]` is the happy-path chain *with rationale*. The model
  doesn't invent the order — it's right there as an array.
- `why` matters because a low-end model that picks the wrong tool from a
  flat list often picks correctly when each entry comes with a one-line
  reason.
- `fallbacks[]` is the second-best path. When the happy path fails the
  model has somewhere obvious to go, instead of inventing tool names.
- `watch_for[]` lists the *exact* diagnostic strings that show up in the
  next response. Models pattern-match on identical tokens, not
  paraphrases.
- `next_step` is one imperative sentence — the bit a model copies into
  its own "thought" text and then acts on.
- `related_tools[]` widens the planning frontier without polluting the
  happy path.
- `notes[]` carries the opinionated bits ("prefer template-preserving
  edits") that would otherwise live in unread documentation.

The server keeps a single `WORKFLOW_GUIDANCE` table in
`tools/discovery_tools.py`, keyed by intent (`fill_sow_from_markdown`,
`insert_architecture_narrative`, `find_insertion_point`,
`append_table_row`, `patch_estimate_workbook`, …). `office_help` matches
the goal to an entry and returns the record. The cleverness is in the
data, not the code.

### Mutating tools embed compact next-tool hints in their own responses

`office_help` only fires once. The breadcrumbs have to keep coming. The
shortest example is `pptx_recommend_layout`:

```jsonc
{
  "file": "deck.pptx",
  "content_type": "title_and_content",
  "recommended":  { "index": 1, "name": "Title and Content", "score": 0.92 },
  "alternatives": [ … ],
  "usage":        "pptx_add_slide(file_path='deck.pptx', layout_index=1, title='Your Title')",
  "next_tools":   ["pptx_add_slide"]
}
```

Three small fields, large lift:

- `recommended` — structured, machine-readable choice.
- `usage` — a literal pre-formatted call string with the chosen layout
  index already substituted in. A small model that struggles to assemble
  arguments from a schema can copy this verbatim. Easily the highest-ROI
  field of the lot.
- `next_tools[]` — a flat list of tool names the planner is allowed to
  jump to next, so it doesn't shop the whole catalogue.

Mutating tools (`office_patch`, `office_table`, `word_insert_at_anchor`)
carry the same shape via a shared helper (`tools/diagnostics.py`), which
defaults `next_tools` to `["office_read", "office_inspect",
"office_audit"]` on success and `["office_help", "office_inspect",
"office_audit"]` on failure. The audit tools then return another hint
pointing back at `office_help` with a recovery goal if anything's still
wrong. The chain is closed at every node.

### Why this works on small models

A tool call is the only place a small model is *forced* to read
structured output carefully — the response goes straight back into
context as the next turn's input. Suggestion fields aren't decorative;
they become the strongest signal in the next prompt. Three patterns we've
watched models actually exploit:

1. *Verbatim copy.* A model that can't synthesise the next call from a
   schema will happily copy a `usage:` string. Make it copy-paste-ready,
   filename and all.
2. *Token-matching.* `watch_for: ["unmapped_sections"]` survives the next
   round-trip because the diagnostic field in the *producing* tool's
   response uses the same string. The model wires them together by
   identity, not by paraphrase.
3. *Frontier narrowing.* A `next_tools: ["office_audit"]` list of one
   collapses planning from 100+ tools to one. The model doesn't need to
   be smart, it just needs to not be wrong, and being wrong is harder
   when the right answer is the only suggested answer.

## Addressing: anchors, not offsets

The biggest single reason chains break is that the model loses the thread
between calls. "Insert a paragraph after the introduction" is fine in
English but disastrous if you ask the model to remember a character
offset across three tool calls.

The server makes the *server* responsible for stable addresses:

- `word_list_anchors` and `word_document_map` return a structured, named
  map of the document — headings, sections, named bookmarks.
- `word_insert_at_anchor` takes an anchor name and inserts relative to it.
- `office_patch` accepts `section:` targets that resolve through the same
  map (for example `"section:Risks and Mitigations"` or
  `"after:Deliverables"`).

The model never has to remember "byte 4823". It remembers
`"Risks and Mitigations"`, which is a thing it can already see in its
own prior turn. The addressing layer is symbolic and human-legible, so
the model's working memory holds.

Rule of thumb: return identifiers your tools can later accept as input.
If you find yourself returning data the model has to *describe back* to
you in natural language, you've made a chain that will eventually
misfire.

## Diagnostics as the back-edge

Linear chains are easy. Real chains have loops, and loops only happen
when the server invites the model back. The instructions string says it
out loud:

> *If generation results include `unmapped_sections` or `unmatched`
> diagnostics, inspect the template structure with
> `word_parse_sow_template`, `word_list_sections`, `word_list_anchors`,
> `word_document_map`, and `word_list_tables` before retrying.*

Three patterns to lift:

1. *Name the diagnostic field.* `unmapped_sections` is a verbatim string
   the model sees in the response and again in the prompt. Models
   pattern-match better on identical strings than on paraphrases.
2. *Name the recovery tools in the same breath.* Don't make the model
   shop for a fix; tell it which five tools to consider and let it pick.
3. *Make recovery cheap.* All the recovery tools above are read-only.
   The penalty for "I'm not sure, let me look again" is one extra
   round-trip, not a destructive misfire.

In practice, this is return-shape discipline. Every mutating tool returns
the same envelope, courtesy of `build_mutation_diagnostics()`:

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
re-reading the document. `status` is a small enum it can branch on
without any prose comprehension at all.

## Modes turn one tool into four

`office_patch` is the same code path whether you ask for `dry_run`,
`best_effort`, `safe`, or `strict`. From the model's perspective it's
four tools, because the schema enum makes the choice visible and the
docstring spells out the trade-offs:

- `dry_run` — compute the changes, return what *would* happen, write
  nothing.
- `best_effort` — apply what you can, report the rest as `unmatched_*`.
- `safe` — refuses to overwrite the source file (requires a distinct
  `output_path`); fails loudly otherwise.
- `strict` — anything less than total success is a failure, with a
  warning explaining why.

The trade-off:

- *Discovery cost* scales with tool count, not mode count. One tool, four
  modes is one entry in `tools/list`.
- *Prompt cost* stays low because the model doesn't have to remember
  whether `office_patch_dry_run` exists.
- *Behaviour is composable.* `dry_run → safe → strict` is a sensible
  escalation chain that doesn't require any new tools.

If you have N tools that differ only in caution level, collapse them into
one with a `mode` enum and document the escalation in the docstring.
Models walk the ladder.

## Annotations let planners parallelise

The annotations the upstreamed `aioumcp.py` now emits (`readOnlyHint`,
`destructiveHint`, `openWorldHint`) aren't just metadata — they're
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

Strict argument validation:

- forces a visible error early in the chain,
- gives the model a concrete signal to consult `office_help` or the
  schema,
- prevents silent drift between what the model thinks happened and what
  did.

Pair with `additionalProperties: false` on every input schema (also now
upstream) and the tool contracts become genuinely contractual.

## How it got there

Worth telling the story in order, because each trick was put in to fix a
specific failure mode of the previous design — and the order is the only
thing that justifies the complexity.

| # | Commit  | What it added                                     | What it was fixing |
|---|---------|---------------------------------------------------|--------------------|
| 1 | [`e44c64d`][c1] | `section:` targets in `office_patch`          | Models couldn't address sections by name; they invented paragraph indexes and got them wrong. |
| 2 | [`c6f1270`][c2] | `word_insert_at_anchor` (additive insertion)  | `office_patch` overwrote surrounding content; the model wanted to add, not replace. |
| 3 | [`5824f60`][c3] | `unmapped_sections` + `section_diagnostics`   | SOW generation reported "success" while quietly skipping half the sections. The model needed something to *see* the failure. |
| 4 | [`5e90da5`][c4] | "Improve MCP guidance for Word authoring"     | Even with diagnostics, the model didn't know which tool to retry with. Instructions string updated to name the recovery tools by name. |
| 5 | [`4055b8f`][c5] | Structured `office_help`                      | Inline instructions weren't enough — the planning frontier was still 100+ tools wide. `office_help` made planning *a tool call*, with `recommended/why/fallbacks/next_step/watch_for` as the return shape. |
| 6 | [`5efb7d6`][c6] | JSON metadata cache for template analysis     | The recovery loop kept re-parsing the same template; cache by `(path, mtime_ns, size)` so re-inspection is free. Cheap recovery is the only kind that gets used. |
| 7 | [`447f8ca`][c7] | `mode: best_effort \| safe \| strict \| dry_run` | Models destroyed customer files in `best_effort` because the response said "applied" without explaining how loosely. `dry_run` and `safe` give them somewhere cautious to retreat to; `strict` lets them escalate when they're confident. |
| 8 | [`35c7cdb`][c8] | Standardised mutation diagnostics             | Every mutating tool returned a different shape; models couldn't pattern-match across them. Extracted into `tools/diagnostics.py` with a single envelope and default `next_tools`. |
| 9 | [`78357aa`][c9] | `word_list_anchors`, `word_document_map`      | Section-name targets only worked if you knew the section names. Add a tool whose only job is to *return them*. |
|10 | [`5b3d8c1`][c10] | "Formalise core-first Office MCP guidance"  | After all the above, models still dipped into specialised tools. Locked the instructions string into the canonical core-first chain, and listed deprecated tools so they could be hidden from `tools/list`. |
|11 | [`7984a9d`][c11] | "Improve Word table matching diagnostics"   | Same loop as #3 but for tables: when row insertion missed, say which table id, which header, why. |
|12 | [`6551057`][c12] | Fix repeated `office_patch` stacking         | Models would call `office_patch` twice on the same anchor and get duplicated content; the tool now detects the prior insertion and either skips or warns. Repeat-call safety matters because models retry. |

[c1]:  https://github.com/rcarmo/python-office-mcp-server/commit/e44c64d
[c2]:  https://github.com/rcarmo/python-office-mcp-server/commit/c6f1270
[c3]:  https://github.com/rcarmo/python-office-mcp-server/commit/5824f60
[c4]:  https://github.com/rcarmo/python-office-mcp-server/commit/5e90da5
[c5]:  https://github.com/rcarmo/python-office-mcp-server/commit/4055b8f
[c6]:  https://github.com/rcarmo/python-office-mcp-server/commit/5efb7d6
[c7]:  https://github.com/rcarmo/python-office-mcp-server/commit/447f8ca
[c8]:  https://github.com/rcarmo/python-office-mcp-server/commit/35c7cdb
[c9]:  https://github.com/rcarmo/python-office-mcp-server/commit/78357aa
[c10]: https://github.com/rcarmo/python-office-mcp-server/commit/5b3d8c1
[c11]: https://github.com/rcarmo/python-office-mcp-server/commit/7984a9d
[c12]: https://github.com/rcarmo/python-office-mcp-server/commit/6551057

The pattern is unmistakable: every commit adds either a *named failure
mode the model can see*, or a *named recovery path the model can take*,
or both. The model's intelligence is constant; the server's vocabulary
keeps growing.

## Techniques inventory

A quick reference, loosely ordered by payoff:

| Technique | Where it lives | Why it earns its keep |
|---|---|---|
| Structured `office_help` with `recommended/why/fallbacks/next_step/watch_for/related_tools/notes` | `tools/discovery_tools.py` (`WORKFLOW_GUIDANCE`) | Turns planning into a tool call. Single highest-impact change for low-end models. |
| `next_tools[]` and `usage:` strings in every mutating response | `tools/diagnostics.py` (`build_mutation_diagnostics`), various | Forward breadcrumbs at every node. `usage` is copy-paste-ready next-call text. |
| Named diagnostic fields (`unmapped_sections`, `section_diagnostics`, `unmatched_targets`, `skipped_targets`) | `tools/word_advanced_tools.py`, `tools/diagnostics.py` | Makes failures *visible to the model*; `watch_for` token-matches against the same strings. |
| Standard mutation envelope (`success`/`status`/`warnings`/`matched_targets`/`unmatched_targets`/`skipped_targets`/`diagnostics`/`next_tools`) | `tools/diagnostics.py` | Lets the model branch on `status` enum without reading prose. |
| `mode` enum (`dry_run`/`best_effort`/`safe`/`strict`) | `tools/office_unified_tools.py::tool_office_patch` | Four tools for the price of one schema entry; supports a natural escalation chain. |
| `operation` enum on consolidated tools (`office_table`, `office_comment`, `office_template`) | `tools/office_unified_tools.py` | Collapses 6–8 surface verbs per surface into one tool. |
| Prefix-by-surface naming (`word_*`, `excel_*`, `pptx_*`, `office_*`) | everywhere | Models chain whatever rhymes; the prefix narrows the next call. |
| MCP annotations inferred from naming conventions (`readOnlyHint`/`destructiveHint`/`openWorldHint`) | `aioumcp.py::_infer_tool_annotations` | Free scheduling hints, free safety metadata. |
| `Literal[...]` enums for operation/mode/check arguments | throughout | Become real JSON-Schema `enum`s, so the model sees the closed set instead of guessing. |
| Stable symbolic addressing (`section:Name`, `after:Heading`, `slide:1/Title 1`, `'Sheet'!B5`) | `tool_office_patch`, `tool_word_insert_at_anchor` | Addresses the model can already see in its own prior turn. |
| Anchor / map discovery tools (`word_list_anchors`, `word_document_map`, `office_inspect`) | `tools/word_advanced_tools.py`, `tools/office_unified_tools.py` | Returns the addresses the addressing layer accepts. Closes the loop. |
| Strict unknown-arg rejection + `additionalProperties: false` | `aioumcp.py` (now upstream) | Stops silent drift when models invent parameters. |
| Type coercion for stringy clients | `aioumcp.py::_coerce_value` | Some clients send numbers as strings; coerce rather than fail. |
| Deprecated-tool hiding (`DEPRECATED_TOOLS` set filtered from `tools/list`) | `office_server.py` | Keeps the surface area small without deleting code; models only see the recommended set. |
| File-fingerprint metadata cache | `tools/metadata_cache.py` | Cheap recovery loops. Cache by `(path, mtime_ns, size, sha256)` with schema versioning and atomic `.tmp` writes. |
| Atomic `safe_save_*` helpers (write to temp, replace, dedupe ZIP entries) | `tools/save_utils.py` | Prevents Office "this file needs repair" prompts after agent edits — important because the next call is going to *read it back*. |
| `restart_server` tool | `office_server.py` | Lets the agent reload code changes without a human in the loop, which matters for the inner-loop iteration story. |

Most of these are unglamorous. They look like return-shape and naming
discipline because that's what they are.

## A worked example

> *"Add a 'Risks' section to `sow.docx`, with three bullet points pulled
> from the executive summary."*

What a competent planner does:

```
1.  office_help(goal="add risks section, source from exec summary",
                document_type="word")
       → {
           recommended: [
             {tool: "office_read",       why: "..."},
             {tool: "word_list_anchors", why: "..."},
             {tool: "office_patch",      why: "..."},
             {tool: "office_audit",      why: "..."}
           ],
           next_step: "read content, list anchors, patch with section: target, then audit",
           watch_for: ["unmapped_sections", "anchor not found"]
         }

2.  office_read(file_path="sow.docx")
       → executive summary text

3.  word_list_anchors(file_path="sow.docx")
       → ["Executive Summary", "Scope", "Deliverables", "Pricing"]
       (no "Risks" — model now knows it must insert, not patch)

4.  office_patch(file_path="sow.docx",
                  changes=[{target: "after:Deliverables",
                            value:  "## Risks\n- ...\n- ...\n- ..."}],
                  mode="dry_run")
       → {status: "success", matched_targets: [...], unmatched_targets: [],
          next_tools: ["office_read", "office_inspect", "office_audit"]}

5.  office_patch(... same changes ..., mode="safe", output_path="sow.out.docx")
       → {status: "success", output_path: "sow.out.docx",
          next_tools: ["office_audit"]}

6.  office_audit(file_path="sow.out.docx", checks=["completion", "tracking"])
       → confirms the new section is present, properly numbered, and
         track-changes annotations are intact
```

Six calls, no detours, no destructive mistakes, addressable by names the
user themselves used. That's what a well-designed MCP surface looks like.

## Checklist for your own server

- [ ] Pick 5–10 *core verbs* and name them in `get_instructions()`.
- [ ] Use *consistent prefixes* by surface (`word_`, `excel_`, `office_`).
- [ ] Provide a *discovery tool* (`*_help`) that returns recommendations
      as a structured record, not as prose.
- [ ] *Embed forward breadcrumbs in every tool response*: at minimum
      `next_tools: [...]`, plus `usage: "<exact call>"` whenever the
      current tool produced a value the next one needs.
- [ ] Make *read tools read-only* and let the annotation inferrer mark
      them.
- [ ] Provide a *map / anchors tool* so addresses survive between calls.
- [ ] Give every mutating tool a *`mode` enum* including `dry_run`.
- [ ] Return *named diagnostic fields* (`unmapped_*`, `unmatched_*`,
      `skipped_*`) and cite the recovery tools in the docstring.
- [ ] Standardise the mutation envelope so the model can branch on a
      single `status` enum.
- [ ] Reject *unknown arguments* strictly (`additionalProperties: false`).
- [ ] Provide an *audit tool* so the model has somewhere to land at the
      end of the chain.
- [ ] Cache *anything the recovery loop calls more than once*. Cheap
      recovery is the only kind that gets used.
- [ ] Make repeat calls *safe* — models retry, and they should be allowed
      to without doubling the document.

Do the boring work in the schema and the descriptions. The model does
the clever bit.
