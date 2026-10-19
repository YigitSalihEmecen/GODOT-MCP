# GODOT-MCP

Two MCP servers for Godot 4.7, kept separate on purpose (read-only docs vs. an editor that can change your project):

- **godot-docs** (repo root): offline documentation lookup.
- **godot-editor** (`editor/`): drives the open editor through a small plugin.

Offline MCP server for the Godot docs (default: `stable` = 4.7). Class reference is parsed from the engine's
class XML (`4.7-stable` tag), the manual from `godot-docs` RST. Everything lands in one SQLite FTS5 file.
No scraping, no network at runtime.

## Setup

```sh
uv run build_db.py            # clones sources into data/ (first run), builds godot.db (~2 s, ~44 MB)
uv run eval.py                # retrieval check against 30 known questions
claude mcp add godot-docs -- uv run --directory "$PWD" server.py
```

Other version: `uv run build_db.py --docs-branch 4.6 --engine-tag 4.6-stable` after deleting `data/` and `godot.db`.
Refresh: delete `data/` and rebuild.

## Tools

| tool | use |
|---|---|
| `search` | FTS over classes, members, manual. Exact class / `Class.member` / member names rank first |
| `get_class` | summary (inheritance, description, name-only member index) or one member kind with signatures |
| `get_member` | full docs of one member, an enum's values, or annotation (`@GDScript`, `@export`) |
| `get_page` | manual page, optionally one heading; long pages truncate with an outline |
| `list_docs` / `list_classes` | browse the manual tree / class list or subclass tree |
| `find_examples` | code blocks for a topic |
| `settings` | Project / Editor Settings lookup (path, type, default, docs) |
| `info` | version + corpus stats |

All text tools take `lang` (`gdscript` default, `csharp`, `all`) and strip the other language's code blocks.

## Editor control (`editor/`)

`editor/addons/godot_mcp_bridge/` is an EditorPlugin: a token-protected JSON-RPC server on `127.0.0.1:6550-6559`
(port + token written to `<project>/.godot/mcp_bridge.json`, removed on exit). `editor/server.py` is the MCP server in front of it.

```sh
cp -R editor/addons/godot_mcp_bridge <your-project>/addons/        # then Project > Project Settings > Plugins > enable
claude mcp add -s user godot-editor -- uv run --project "$PWD" "$PWD/editor/server.py"
```

The project is found from the directory Claude Code runs in (walks up to `project.godot`), or `GODOT_PROJECT`.
`GODOT_BIN` overrides the Godot binary used by `run_game`.

| area | tools |
|---|---|
| editor / scenes | `editor_state`, `scene` (new/open/save), `scene_tree`, `select_nodes`, `editor_play`, `editor_stop`, `screenshot` |
| nodes | `add_node`, `delete_node`, `reparent_node`, `get_properties`, `set_properties`, `call_method`, `connect_signal`, `list_properties` |
| files / project | `list_files`, `read_file`, `write_file`, `delete_file` (to trash), `get_setting`, `set_setting`, `validate_script` |
| escape hatch | `eval_gdscript` runs GDScript inside the editor with full access |
| diagnostics | `editor_logs`, `run_game` / `game_output` / `stop_game` (separate process, output captured, optional `--headless`) |

Node edits go through the editor's undo history. File tools are limited to `res://`. `eval_gdscript` and `call_method` are
full-trust by design: the bridge only listens on localhost with a per-session token, and Claude Code's tool approval is the gate.

`uv run editor/smoke_test.py` (with `GODOT_PROJECT` set and the editor open) exercises every tool end to end.

## Scope of the docs server

Documentation only; pair it with `godot-editor` to act on what it finds.

Docs content: Godot Engine contributors, CC-BY-3.0 (https://docs.godotengine.org). Not redistributed here; fetched at build time.
