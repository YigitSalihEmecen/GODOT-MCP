#!/usr/bin/env python3
"""MCP server that drives a running Godot editor through the godot_mcp_bridge plugin."""
import base64
import json
import os
import re
import shutil
import subprocess
import threading
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP, Image

mcp = FastMCP(
    "godot-editor",
    instructions=(
        "Drives the open Godot editor. Node paths are relative to the edited scene root ('.' = root). "
        "Typed values in props/args can be strings in Godot text format, e.g. 'Vector2(10, 20)', 'Color(1,0,0)'; "
        "resources as 'res://...'. Edits go through the editor's undo history. "
        "Look up unfamiliar APIs with the godot-docs server first. eval_gdscript is the escape hatch for anything else."
    ),
)


def project_dir() -> Path:
    env = os.environ.get("GODOT_PROJECT")
    start = Path(env) if env else Path.cwd()
    for d in [start, *start.parents]:
        if (d / "project.godot").exists():
            return d
    raise RuntimeError("project.godot not found; set GODOT_PROJECT")


def rpc(rpc_method: str, /, **params) -> Any:
    info_file = project_dir() / ".godot" / "mcp_bridge.json"
    if not info_file.exists():
        raise RuntimeError("Editor bridge not running: open the project in Godot and enable the 'Godot MCP Bridge' plugin")
    info = json.loads(info_file.read_text())
    req = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}/rpc",
        data=json.dumps({"method": rpc_method, "params": params}).encode(),
        headers={"X-Token": info["token"], "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.loads(r.read(), strict=False)
    except OSError as e:
        raise RuntimeError(f"Editor bridge unreachable ({e}); is the editor still open?") from e
    if not out["ok"]:
        raise RuntimeError(out["error"])
    return out["result"]


def dumps(x) -> str:
    return json.dumps(x, ensure_ascii=False, indent=None)


# ---------- editor & scene ----------

@mcp.tool()
def editor_state() -> str:
    """Godot version, open scenes, edited scene root, selection, unsaved scenes, play state."""
    return dumps(rpc("editor_state"))


@mcp.tool()
def scene_tree(path: str = ".", depth: int = 6) -> str:
    """Indented node tree of the edited scene (or a subtree): name (Type) [script] [instanced scene]."""
    def fmt(n, ind=0):
        extra = "".join([f" script={n['script']}" if n.get("script") else "", f" scene={n['scene']}" if n.get("scene") else "",
                         f" (+{n['children_hidden']} hidden)" if n.get("children_hidden") else ""])
        return ["  " * ind + f"{n['name']} ({n['type']}){extra}"] + [l for c in n.get("children", []) for l in fmt(c, ind + 1)]
    return "\n".join(fmt(rpc("get_tree", path=path, depth=depth)))


@mcp.tool()
def scene(action: Literal["new", "open", "save"], path: str = "", root_type: str = "Node", name: str = "") -> str:
    """new: create res:// scene with root_type and open it. open: open a scene file. save: save the edited scene (path = save-as)."""
    if action == "new":
        return dumps(rpc("new_scene", path=path, root_type=root_type, name=name))
    if action == "open":
        return dumps(rpc("open_scene", path=path))
    return dumps(rpc("save_scene", path=path))


@mcp.tool()
def add_node(type: str, parent: str = ".", name: str = "", props: dict | None = None) -> str:
    """Add a node. type = Node class name (e.g. 'Sprite2D') or a res:// .tscn to instance. props set before adding
    (e.g. {'position': 'Vector2(10, 20)', 'texture': 'res://icon.svg'}). Returns the new node's path."""
    return dumps(rpc("add_node", type=type, parent=parent, name=name, props=props or {}))


@mcp.tool()
def delete_node(path: str) -> str:
    """Remove a node (undoable in the editor)."""
    return dumps(rpc("delete_node", path=path))


@mcp.tool()
def reparent_node(path: str, new_parent: str, keep_global: bool = True) -> str:
    """Move a node under a new parent."""
    return dumps(rpc("reparent_node", path=path, new_parent=new_parent, keep_global=keep_global))


@mcp.tool()
def get_properties(path: str, names: list[str] | None = None) -> str:
    """Properties of a node, or of a 'res://' resource / 'singleton:Name'. Without names: only non-default values."""
    return dumps(rpc("get_properties", path=path, names=names or []))


@mcp.tool()
def set_properties(path: str, props: dict) -> str:
    """Set properties on a node or resource (undoable). Attach a script with {'script': 'res://x.gd'}; rename with {'name': ...}."""
    return dumps(rpc("set_properties", path=path, props=props))


@mcp.tool()
def call_method(path: str, method: str, args: list | None = None) -> str:
    """Call any method on a node, 'res://' resource or 'singleton:Name' (e.g. 'singleton:EditorInterface').
    String args may be prefixed 'var:' (Godot text format), 'load:' (resource) or 'node:' (scene node)."""
    return dumps(rpc("call_method", path=path, method=method, args=args or []))


@mcp.tool()
def connect_signal(path: str, signal: str, target: str, method: str) -> str:
    """Create a persistent (saved with the scene) signal connection."""
    return dumps(rpc("connect_signal", path=path, signal=signal, target=target, method=method))


@mcp.tool()
def select_nodes(paths: list[str]) -> str:
    """Set the editor selection."""
    return dumps(rpc("select", paths=paths))


@mcp.tool()
def list_properties(class_name: str, own_only: bool = False) -> str:
    """Editor-visible properties of a class from the running engine (name: type)."""
    return "\n".join(rpc("list_properties", class_name=class_name, no_inheritance=own_only))


# ---------- files, settings, scripts ----------

@mcp.tool()
def list_files(dir: str = "res://", ext: str = "") -> str:
    """List project files recursively (max 1000), optionally by extension (e.g. 'gd', 'tscn')."""
    return "\n".join(rpc("list_files", dir=dir, ext=ext))


@mcp.tool()
def read_file(path: str) -> str:
    """Read a text file under res://."""
    return rpc("read_file", path=path)


@mcp.tool()
def write_file(path: str, content: str) -> str:
    """Write a text file under res:// (scripts reload, filesystem rescans). Run validate_script after editing .gd files."""
    return dumps(rpc("write_file", path=path, content=content))


@mcp.tool()
def delete_file(path: str) -> str:
    """Move a res:// file to the OS trash."""
    return dumps(rpc("delete_file", path=path))


@mcp.tool()
def get_setting(name: str) -> str:
    """Read a Project Setting, e.g. 'physics/2d/default_gravity'."""
    return dumps(rpc("get_setting", name=name))


@mcp.tool()
def set_setting(name: str, value: Any, save: bool = True) -> str:
    """Write a Project Setting (autoloads: name='autoload/Game', value='*res://game.gd')."""
    return dumps(rpc("set_setting", name=name, value=value, save=save))


@mcp.tool()
def validate_script(path: str) -> str:
    """Compile-check a GDScript file; returns ok plus any parser errors."""
    return dumps(rpc("validate_script", path=path))


@mcp.tool()
def eval_gdscript(code: str) -> str:
    """Run GDScript inside the editor (full access). Body of `func run(editor, root)`: `editor` is EditorInterface,
    `root` the edited scene root. Use `return x` to get a value back. Changes made this way bypass undo."""
    return dumps(rpc("eval", code=code))


@mcp.tool()
def editor_logs(n: int = 50, level: Literal["", "error", "warning", "info"] = "", since: int = 0) -> str:
    """Recent messages/errors from the editor process (script errors, push_error, print). Not the running game's output."""
    return "\n".join(f"#{l['id']} [{l['level']}] {re.sub(r'\x1b\[[0-9;]*m', '', l['text'])}" for l in rpc("logs", n=n, level=level, since=since))


@mcp.tool()
def screenshot(target: Literal["editor", "2d", "3d"] = "editor") -> Image:
    """Screenshot of the whole editor window or the 2D / 3D viewport."""
    return Image(data=base64.b64decode(rpc("screenshot", target=target)), format="png")


@mcp.tool()
def editor_play(what: str = "main") -> str:
    """Play inside the editor: 'main', 'current', or a res:// scene path. Output is not captured; use run_game for that."""
    return dumps(rpc("play", what=what))


@mcp.tool()
def editor_stop() -> str:
    """Stop the scene started with editor_play."""
    return dumps(rpc("stop"))


# ---------- headless-capable game runs with captured output ----------

_game: subprocess.Popen | None = None
_out: deque[str] = deque(maxlen=2000)


def godot_bin() -> str:
    for c in (os.environ.get("GODOT_BIN"), shutil.which("godot"), "/Applications/Godot.app/Contents/MacOS/Godot"):
        if c and Path(c).exists():
            return c
    raise RuntimeError("Godot binary not found; set GODOT_BIN")


@mcp.tool()
def run_game(scene: str = "", headless: bool = False, wait_seconds: float = 3.0, args: list[str] | None = None) -> str:
    """Launch the game as a separate process with stdout/stderr captured (read with game_output). scene='' runs the main scene.
    headless=True renders nothing (good for logic tests). Waits wait_seconds, returns output so far."""
    global _game
    stop_game()
    cmd = [godot_bin(), "--path", str(project_dir())] + (["--headless"] if headless else []) + ([scene] if scene else []) + (args or [])
    _out.clear()
    _game = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

    def pump(p):
        for line in p.stdout:
            _out.append(line.rstrip())

    threading.Thread(target=pump, args=(_game,), daemon=True).start()
    try:
        _game.wait(timeout=wait_seconds)
    except subprocess.TimeoutExpired:
        pass
    return game_output(80)


@mcp.tool()
def game_output(tail: int = 100) -> str:
    """Captured output of the game started by run_game, plus whether it is still running."""
    state = "running" if _game and _game.poll() is None else f"exited ({_game.returncode if _game else 'never started'})"
    return f"[{state}]\n" + "\n".join(list(_out)[-tail:])


@mcp.tool()
def stop_game() -> str:
    """Terminate the game started by run_game."""
    global _game
    if _game and _game.poll() is None:
        _game.terminate()
        try:
            _game.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _game.kill()
        return "stopped"
    return "not running"


if __name__ == "__main__":
    mcp.run()
