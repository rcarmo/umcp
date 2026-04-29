# Chaining MCP Operations

A short, opinionated note on how language models actually *string together* MCP
tool calls in practice — written using [`rcarmo/python-office-mcp-server`][office]
as the worked example, because it has been beaten on by real models against real
Office documents and the patterns have settled into something defensible.

[office]: https://github.com/rcarmo/python-office-mcp-server

## The basic shape of a chain

A model holding an MCP toolbox doesn't plan the way a programmer does. It
greedily picks the next call that looks plausible from the current conversation
and the tool descriptions it can see. So if you want chains that actually
terminate in the right place, the *server* has to make the next-best call
obvious at every step. Three things matter:

1. **A small, named "core" verb set** that covers ~80% of intents.
2. **Output that nominates the next call** — not just data, but breadcrumbs.
3. **An addressing scheme** that survives between calls (anchors, IDs, paths).

Everything else is decoration.

## Core verbs beat surface area

The office server exposes 100+ tools, but its `get_instructions()` aggressively
funnels models toward eight:

> *Use a core-first model … start with `office_help`, then prefer `office_read`,
> `office_inspect`, `office_patch`, `office_table`, `office_template`,
> `office_audit`, and `word_insert_at_anchor`. Treat specialized tools as
> fallback, diagnostic, legacy-compatibility, or expert tools when the core flow
> is insufficient.*

That sentence does an enormous amount of work. It tells the model:

- there *is* a recommended path,
- the path is verb-shaped (`help → read → inspect → patch → audit`),
- everything else is opt-in.

Without that guidance, models cheerfully reach for `word_parse_sow_template`
when `office_read` would do, and you end up with five-call detours for one-call
jobs. Be ruthless about which tools you elevate.

## The canonical chain: discover → inspect → mutate → verify

Almost every successful interaction in the office server collapses to this
shape:

```
office_help(goal="add a risks section to the SOW")
  └─ office_read(file_path)                    # what's in there?
        └─ office_inspect(file_path,           # where can I attach things?
                          aspect="anchors")
              └─ office_patch(file_path,       # do the thing
                              changes=[...],
                              mode="dry_run")
                    └─ office_patch(...,       # do it for real
                                    mode="safe")
                          └─ office_audit(file_path)   # did it stick?
```

Four properties make this work:

- **Discovery first.** `office_help` returns recommendations, not data, so the
  model spends one cheap call to *plan* before touching the document.
- **Read before write.** `office_read` and `office_inspect` are read-only and
  annotated as such (`readOnlyHint: true`), so a planner that's been told to
  prefer read-only calls during exploration naturally batches them up front.
- **Dry-run before mutate.** `office_patch` accepts
  `mode: "dry_run" | "best_effort" | "safe" | "strict"`. Models gravitate to
  `dry_run` when uncertain because the tool description says so. The result of
  the dry-run becomes the input prompt for the real call.
- **Audit at the end.** `office_audit` exists *purely* so the model has
  something to call that closes the loop. Without it, the model declares
  victory based on the patch return value, which is optimistic.

## Naming is the chain

Models will chain whatever rhymes. The office server leans into this
shamelessly:

- All Word-specific tools are `word_*`, all Excel `excel_*`, all unified
  `office_*`. A model that has just called `office_inspect` will reach for
  `office_patch` next, not `word_patch_with_track_changes`, simply because
  the prefix matches.
- Verbs are consistent across surfaces: `list_*`, `get_*`, `inspect_*` for
  reads; `patch`, `insert`, `add`, `set` for writes. The annotation inferrer in
  `aioumcp.py` actually uses these prefixes to assign `readOnlyHint` /
  `destructiveHint` automatically — so naming discipline turns into safety
  metadata for free.
- Operation-style tools like `office_table(operation="create"|"insert_row"|…)`
  and `office_comment(operation="add"|"reply"|"resolve")` collapse what would
  otherwise be 6–8 separate tools into one. Models discover the operation set
  via the schema's `Literal[...]` enum (now an actual JSON Schema `enum` thanks
  to the schema work upstreamed from the office fork), so they don't have to
  guess.

If you take one thing from the office server, take this: **the prefix is the
plan**, and the verb is the step.

## Addressing: anchors, not offsets

The single biggest reason chains break is that the model loses the thread
between calls. "Insert a paragraph after the introduction" is fine in English
but disastrous if you ask the model to remember a character offset across three
tool calls.

The office server solves this by making the *server* responsible for stable
addresses:

- `word_list_anchors` and `word_document_map` return a structured, named map
  of the document — headings, sections, named bookmarks.
- `word_insert_at_anchor` takes an anchor name and inserts relative to it.
- `office_patch` accepts `section:` targets that resolve through the same map.

The model never has to remember "byte 4823". It remembers `"Risks and
Mitigations"`, which is a thing it can already see in its own prior turn. This
is what makes long chains tractable: the addressing layer is symbolic and
human-legible, so the model's working memory holds.

The umcp pattern that supports this is just: **return identifiers your tools
can later accept as input**. If you find yourself returning data that the model
then has to *describe back* to you in natural language, you've made a chain
that will eventually misfire.

## Diagnostics as the back-edge

Linear chains are easy. Real chains have loops, and loops only happen when the
server *invites* the model back. The office server's instructions explicitly
hand-hold this:

> *If generation results include `unmapped_sections` or `unmatched` diagnostics,
> inspect the template structure with `word_parse_sow_template`,
> `word_list_sections`, `word_list_anchors`, `word_document_map`, and
> `word_list_tables` before retrying.*

Three patterns to steal:

1. **Name the diagnostic field.** `unmapped_sections` is a verbatim string the
   model will see in the response and again in the prompt. Models pattern-match
   far better on identical strings than on paraphrases.
2. **Name the recovery tools in the same breath.** Don't make the model shop
   for a fix; tell it which five tools to consider and let it pick.
3. **Make recovery cheap.** All the recovery tools above are read-only. The
   penalty for "I'm not sure, let me look again" is one extra round-trip, not
   a destructive misfire.

Implementation-wise this is mostly *return shape* discipline: every mutating
tool should return `{ ok, applied: [...], unapplied: [...], hints: [...] }` or
similar, so the model can decide whether to commit, retry, or escalate without
re-reading the whole document.

## Modes turn one tool into four

`office_patch` is the same code path whether you ask for `dry_run`,
`best_effort`, `safe`, or `strict`. From the model's perspective it's four
tools, because the schema enum makes the choice visible and the docstring
spells out the trade-offs. This is enormously efficient:

- **Discovery cost** scales with tool count, not mode count. One tool, four
  modes is one entry in `tools/list`.
- **Prompt cost** stays low because the model doesn't have to remember which of
  `office_patch_dry_run`, `office_patch_safe`, etc. exists.
- **Behaviour is composable.** A planner can choose mode per-call:
  `dry_run → safe → strict` is a perfectly sensible escalation chain that
  doesn't require any new tools.

If you have N tools that differ only in caution level, collapse them into one
with a `mode` enum and document the escalation in the docstring. Models will
walk the ladder.

## Annotations let planners parallelise

The annotations the upstreamed `aioumcp.py` now emits
(`readOnlyHint` / `destructiveHint` / `openWorldHint`) aren't just metadata —
they're scheduling hints. A model with a half-decent planner can:

- run all read-only calls in parallel during the discovery phase,
- serialise destructive calls behind a confirmation,
- isolate open-world calls (`web_*`, `azure_*`) so a network blip doesn't
  knock over the local document workflow.

You get this for free from naming conventions. The cost is being honest about
which of your tools mutate state. Lying here is the most common cause of
"why did the model just delete my doc?" incidents.

## Strictness keeps chains from drifting

The fork's `aioumcp.py` rejects unknown arguments outright. This sounds
unfriendly until you've watched a model invent a `force=True` parameter that
your tool silently ignores, then write a confident summary saying it forced the
operation. Strict argument validation:

- forces a visible error early in the chain,
- gives the model a concrete signal to consult `office_help` or the schema,
- prevents silent drift between what the model thinks happened and what did.

Pair this with `additionalProperties: false` on every input schema (also now
upstream) and your tool contracts become genuinely contractual.

## A worked example, end to end

> User: *"Add a 'Risks' section to `sow.docx`, with three bullet points pulled
> from the executive summary."*

What a competent planner does against this server:

```
1.  office_help(goal="add risks section, source from exec summary",
                document_type="word")
       → "use office_read for content, word_list_anchors for placement,
          office_patch with section: targets, then office_audit"

2.  office_read(file_path="sow.docx", aspect="text")
       → returns the executive summary text

3.  word_list_anchors(file_path="sow.docx")
       → returns ["Executive Summary", "Scope", "Deliverables", "Pricing"]
       (no "Risks" — model now knows it must insert, not patch)

4.  office_patch(file_path="sow.docx",
                  changes=[{section: "after:Deliverables",
                            insert: "## Risks\n- ...\n- ...\n- ..."}],
                  mode="dry_run")
       → returns {applied:[...], unapplied:[], anchors_resolved:[...]}

5.  office_patch(... same changes ..., mode="safe")
       → returns {applied:[...], output_path:"sow.docx"}

6.  office_audit(file_path="sow.docx")
       → confirms the new section is present, properly numbered, and that
         track-changes annotations are intact
```

Six calls, no detours, no destructive mistakes, addressable by anchor names
the user themselves used. That is what a well-designed MCP surface buys you.

## Checklist for your own server

- [ ] Pick 5–10 **core verbs** and name them in `get_instructions()`.
- [ ] Use **consistent prefixes** by surface (`word_`, `excel_`, `office_`).
- [ ] Provide a **discovery tool** (`*_help`) that returns recommendations.
- [ ] Make **read tools read-only** and let the annotation inferrer mark them.
- [ ] Provide a **map / anchors tool** so addresses survive between calls.
- [ ] Give every mutating tool a **`mode` enum** including `dry_run`.
- [ ] Return **named diagnostic fields** (`unmapped_*`, `unmatched_*`) and
      cite the recovery tools in the docstring.
- [ ] Reject **unknown arguments** strictly (`additionalProperties: false`).
- [ ] Provide an **audit tool** so the model has somewhere to land at the end
      of the chain.

Do the boring work in the schema and the descriptions; the model will do the
clever bit.
