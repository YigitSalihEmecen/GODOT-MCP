#!/usr/bin/env python3
"""Retrieval check: rank of the expected ref among the default 8 search hits (lower is better)."""
import sys

import server

CASES = [
    ("move a character body and slide along walls", "CharacterBody2D.move_and_slide"),
    ("move_and_slide", "move_and_slide"),
    ("connect a signal to a function", "Signal.connect"),
    ("export a variable to the inspector", "@export"),
    ("get node by path", "Node.get_node"),
    ("change scene", "SceneTree.change_scene_to_file"),
    ("instantiate a packed scene", "PackedScene.instantiate"),
    ("detect key press input", "[class] Input"),
    ("default gravity physics 2d", "default_gravity"),
    ("play an animation with AnimationPlayer", "AnimationPlayer.play"),
    ("raycast from a node", "RayCast"),
    ("create a timer that fires once", "Timer"),
    ("save and load a game", "tutorials/io/saving_games"),
    ("what are autoloads singletons", "tutorials/scripting/singletons_autoload"),
    ("gdscript static typing", "tutorials/scripting/gdscript/static_typing"),
    ("export project to android", "tutorials/export/exporting_for_android"),
    ("multiplayer high level RPC", "tutorials/networking/high_level_multiplayer"),
    ("write a canvas_item shader", "tutorials/shaders"),
    ("navigation agent pathfinding", "NavigationAgent2D"),
    ("tween a property over time", "Tween"),
    ("optimize thousands of instances multimesh", "tutorials/performance/using_multimesh"),
    ("how to use groups", "tutorials/scripting/groups"),
    ("TileMapLayer set cell", "TileMapLayer.set_cell"),
    ("MouseButton enum", "MouseButton"),
    ("lerp between two values", "lerp"),
    ("await a signal coroutine", "Signal"),
    ("custom resource class_name", "tutorials/scripting/resources"),
    ("viewport stretch mode window size", "stretch"),
    ("audio bus effects", "tutorials/audio/audio_buses"),
    ("load a resource at runtime", "ResourceLoader"),
]


def rank(query, expect):
    hits = [h for h in server.search(query, limit=8).split("\n") if h.startswith("[")]
    return next((i + 1 for i, h in enumerate(hits) if expect.lower() in h.lower()), None)


def main():
    ranks = []
    for query, expect in CASES:
        r = rank(query, expect)
        ranks.append(r)
        print(f"{'-' if r is None else r:>2}  {query}" + ("" if r else f"   (want {expect})"))
    for n in (1, 3, 5, 8):
        print(f"top-{n}: {sum(1 for r in ranks if r and r <= n)}/{len(CASES)}")
    sys.exit(any(r is None for r in ranks))


if __name__ == "__main__":
    main()
