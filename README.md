# godot-docs-mcp

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

## Scope

Documentation only. Controlling a running editor/project (scenes, nodes, run/stop) is a different job;
pair this with a Godot control MCP.

Docs content: Godot Engine contributors, CC-BY-3.0 (https://docs.godotengine.org). Not redistributed here; fetched at build time.
