#!/usr/bin/env python3
"""End-to-end check against a running editor with the bridge enabled. Usage: GODOT_PROJECT=<dir> uv run editor/smoke_test.py
Creates res://_mcp_smoke/ in that project (scene + script), leaves it behind."""
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).parent
GD = "extends CharacterBody2D\nvar speed := 200\nfunc _ready():\n\tprint('smoke ready')\n"


async def main():
    params = StdioServerParameters(command=sys.executable, args=[str(HERE / "server.py")],
                                 env={"GODOT_PROJECT": os.environ["GODOT_PROJECT"], "PATH": os.environ["PATH"]})
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        fails = []

        async def call(tool, expect=None, ok=True, **args):
            res = await s.call_tool(tool, args)
            text = res.content[0].text if res.content and hasattr(res.content[0], "text") else f"<{res.content[0].type}>" if res.content else ""
            good = (not res.isError) == ok and (expect is None or expect in text)
            print(("ok   " if good else "FAIL ") + f"{tool} {json.dumps(args)[:70]} -> {text[:110]!r}")
            if not good:
                fails.append(tool)
            return text

        await call("editor_state", "godot")
        await call("scene", "_mcp_smoke/main.tscn", action="new", path="res://_mcp_smoke/main.tscn", root_type="Node2D", name="Main")
        await call("add_node", "Sprite2D", type="Sprite2D", props={"texture": "res://icon.svg", "position": "Vector2(10, 20)"})
        await call("add_node", "Player", type="CharacterBody2D", name="Player")
        await call("add_node", "Player/CollisionShape2D", type="CollisionShape2D", parent="Player")
        await call("add_node", ok=False, type="NotAClass")
        await call("scene_tree", "CollisionShape2D", )
        await call("get_properties", "Vector2(10, 20)", path="Sprite2D", names=["position"])
        await call("set_properties", path="Sprite2D", props={"position": "Vector2(1, 2)", "modulate": "Color(1, 0, 0, 1)"})
        await call("get_properties", "Vector2(1, 2)", path="Sprite2D", names=["position"])
        await call("write_file", "written", path="res://_mcp_smoke/player.gd", content=GD)
        await call("validate_script", '"ok": true', path="res://_mcp_smoke/player.gd")
        await call("write_file", "written", path="res://_mcp_smoke/bad.gd", content="func (:\n")
        await call("validate_script", '"ok": false', path="res://_mcp_smoke/bad.gd")
        await call("delete_file", "trashed", path="res://_mcp_smoke/bad.gd")
        await call("set_properties", path="Player", props={"script": "res://_mcp_smoke/player.gd"})
        await call("reparent_node", path="Sprite2D", new_parent="Player")
        await call("scene_tree", "Player/Sprite2D" if False else "Sprite2D")
        await call("connect_signal", "connected", path="Player", signal="ready", target=".", method="queue_redraw")
        await call("call_method", path="singleton:OS", method="get_name")
        await call("eval_gdscript", '"result": 1', code="return root.get_child_count()")
        await call("eval_gdscript", ok=False, code="return (")
        await call("get_setting", "980", name="physics/2d/default_gravity")
        await call("list_properties", "position", class_name="Node2D", own_only=True)
        await call("list_files", "player.gd", dir="res://_mcp_smoke")
        await call("delete_node", path="Player/Sprite2D")
        await call("eval_gdscript", '"result": 0', code="return root.get_node('Player').get_child_count() - 1")
        undo = "var ur = editor.get_editor_undo_redo()\nur.get_history_undo_redo(ur.get_object_history_id(root)).undo()\n"
        await call("eval_gdscript", '"result": true', code=undo + "return root.get_node('Player/Sprite2D').owner == root")
        await call("scene", "saved", action="save")
        await call("read_file", "Player", path="res://_mcp_smoke/main.tscn")
        await call("editor_logs")
        await call("run_game", "smoke ready", scene="res://_mcp_smoke/main.tscn", headless=True, wait_seconds=4)
        await call("stop_game")
        print("\nFAILED: " + ", ".join(fails) if fails else "\nall passed")
        return bool(fails)


sys.exit(asyncio.run(main()))
