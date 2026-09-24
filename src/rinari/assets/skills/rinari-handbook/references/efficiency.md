# Working with few calls

Every call costs a round trip and context. Before calling, know what you will do with the answer.

## Choose the right tool
- Typed tools before shell: `fs.*`, `search.*`, `git.*`, `ssh.inspect`, `rinari.*` return structured, bounded data; shell output is raw and long.
- On-demand tools: one `capability.search` with `load=true` both finds and exposes them. Activating a skill exposes the tools it requires in the same call.
- Questions about Rinari's own state go to `rinari.*`, never to SQL over the home or to guessing.

## Batch and narrow
- `fs.read` takes up to 16 paths in one call; read the files you need together, not one per turn.
- Search before reading: `search.regex` / `search.files` / `fs.search_text` locate the lines; then read only those ranges (`fs.read_lines`).
- Paginate instead of re-reading: use `next_offset` / `next_line` and stop when you have the answer.
- `rinari.status` lists models only with `provider` or `include_models`; `rinari.turn` adds raw events only with `detail: "events"`.

## Do not repeat work
- Never re-read a file or re-run a command whose result is already in context and has not changed.
- Parallelize independent reads in one step; chain only what depends on a previous result.
- Background long processes (`shell.exec` with `background: true` or `process.start`) and read them with `process.output` instead of waiting in a loop.

## Stop
- Stop when the evidence answers the question; one more confirming call is waste.
- When stuck after two attempts on the same approach, change the approach or report the exact blocker; do not retry blindly.
