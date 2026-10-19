@tool
extends EditorPlugin

const PORT_FIRST := 6550
const PORT_COUNT := 10
const MAX_LOG := 500
const CONNECT_TIMEOUT_MS := 10000

class BridgeLog extends Logger:
	var lines: Array[Dictionary] = []
	var next_id := 1
	var mutex := Mutex.new()

	func _push(level: String, text: String) -> void:
		mutex.lock()
		lines.append({"id": next_id, "level": level, "text": text})
		next_id += 1
		if lines.size() > MAX_LOG:
			lines.pop_front()
		mutex.unlock()

	func _log_error(function: String, file: String, line: int, code: String, rationale: String, _editor_notify: bool, error_type: int, _bt: Array[ScriptBacktrace]) -> void:
		var level := "warning" if error_type == ERROR_TYPE_WARNING else "error"
		_push(level, "%s:%d @ %s: %s" % [file, line, function, rationale if rationale else code])

	func _log_message(message: String, error: bool) -> void:
		_push("error" if error else "info", message.strip_edges())

	func since(last_id: int) -> Array:
		mutex.lock()
		var out := lines.filter(func(l): return l.id > last_id)
		mutex.unlock()
		return out

var _server := TCPServer.new()
var _clients: Array[Dictionary] = []
var _token := ""
var _port := 0
var _log := BridgeLog.new()
var _info_path := ""


func _enter_tree() -> void:
	OS.add_logger(_log)
	var rng := RandomNumberGenerator.new()
	rng.randomize()
	_token = "%016x%016x" % [rng.randi() | (rng.randi() << 32), rng.randi() | (rng.randi() << 32)]
	for p in range(PORT_FIRST, PORT_FIRST + PORT_COUNT):
		if _server.listen(p, "127.0.0.1") == OK:
			_port = p
			break
	if _port == 0:
		push_error("MCP bridge: no free port in %d-%d" % [PORT_FIRST, PORT_FIRST + PORT_COUNT - 1])
		return
	_info_path = ProjectSettings.globalize_path("res://.godot/mcp_bridge.json")
	var f := FileAccess.open(_info_path, FileAccess.WRITE)
	if f:
		f.store_string(JSON.stringify({"port": _port, "token": _token, "pid": OS.get_process_id()}))
		f.close()
	print("MCP bridge listening on 127.0.0.1:%d" % _port)


func _exit_tree() -> void:
	_server.stop()
	if _info_path != "" and FileAccess.file_exists(_info_path):
		DirAccess.remove_absolute(_info_path)
	OS.remove_logger(_log)


func _process(_delta: float) -> void:
	if not _server.is_listening():
		return
	while _server.is_connection_available():
		_clients.append({"peer": _server.take_connection(), "buf": PackedByteArray(), "t": Time.get_ticks_msec()})
	for c in _clients.duplicate():
		var peer: StreamPeerTCP = c.peer
		peer.poll()
		if peer.get_status() != StreamPeerTCP.STATUS_CONNECTED:
			_clients.erase(c)
			continue
		var n := peer.get_available_bytes()
		if n > 0:
			c.buf.append_array(peer.get_data(n)[1])
		var req := _parse_request(c.buf)
		if req.is_empty():
			if Time.get_ticks_msec() - c.t > CONNECT_TIMEOUT_MS:
				peer.disconnect_from_host()
				_clients.erase(c)
			continue
		_reply(peer, _dispatch(req))
		peer.disconnect_from_host()
		_clients.erase(c)


func _parse_request(buf: PackedByteArray) -> Dictionary:
	var text := buf.get_string_from_utf8()
	var head_end := text.find("\r\n\r\n")
	if head_end < 0:
		return {}
	var headers := {}
	for line in text.substr(0, head_end).split("\r\n").slice(1):
		var kv := line.split(":", true, 1)
		if kv.size() == 2:
			headers[kv[0].strip_edges().to_lower()] = kv[1].strip_edges()
	var length := int(headers.get("content-length", "0"))
	var body := buf.slice(head_end + 4)
	if body.size() < length:
		return {}
	return {"headers": headers, "body": body.slice(0, length).get_string_from_utf8()}


func _reply(peer: StreamPeerTCP, payload: Dictionary) -> void:
	var body := JSON.stringify(payload).to_utf8_buffer()
	var head := "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % body.size()
	peer.put_data(head.to_utf8_buffer())
	peer.put_data(body)


func _dispatch(req: Dictionary) -> Dictionary:
	if req.headers.get("x-token", "") != _token:
		return {"ok": false, "error": "bad token"}
	var msg = JSON.parse_string(req.body)
	if typeof(msg) != TYPE_DICTIONARY or not msg.has("method"):
		return {"ok": false, "error": "expected {method, params}"}
	var fn := "rpc_" + String(msg.method)
	if not has_method(fn):
		return {"ok": false, "error": "unknown method " + String(msg.method)}
	var res = call(fn, msg.get("params", {}))
	if typeof(res) == TYPE_DICTIONARY and res.has("__error"):
		return {"ok": false, "error": res.__error}
	return {"ok": true, "result": _to_json(res)}


# ---------- helpers ----------

func _err(text: String) -> Dictionary:
	return {"__error": text}


func _root() -> Node:
	return EditorInterface.get_edited_scene_root()


func _resolve(path: String) -> Variant:
	if path.begins_with("singleton:"):
		var s := path.substr(10)
		return Engine.get_singleton(s) if Engine.has_singleton(s) else null
	if path.begins_with("res://"):
		return load(path)
	var root := _root()
	if root == null:
		return null
	if path in ["", ".", String(root.name)]:
		return root
	return root.get_node_or_null(path)


func _rel(node: Node) -> String:
	var root := _root()
	return "." if node == root else String(root.get_path_to(node))


func _to_json(v: Variant, depth := 0) -> Variant:
	match typeof(v):
		TYPE_NIL, TYPE_BOOL, TYPE_INT, TYPE_STRING:
			return v
		TYPE_FLOAT:
			return v if is_finite(v) else str(v)
		TYPE_STRING_NAME, TYPE_NODE_PATH:
			return str(v)
		TYPE_ARRAY:
			return v.map(func(x): return _to_json(x, depth + 1))
		TYPE_DICTIONARY:
			var d := {}
			for k in v:
				d[str(k)] = _to_json(v[k], depth + 1)
			return d
		TYPE_OBJECT:
			if v == null or not is_instance_valid(v):
				return null
			if v is Node:
				return {"$node": _rel(v) if _root() and _root().is_ancestor_of(v) or v == _root() else str(v.get_path()), "type": v.get_class()}
			if v is Resource:
				return {"$res": v.resource_path, "type": v.get_class()}
			return {"$object": v.get_class()}
		TYPE_PACKED_STRING_ARRAY:
			return Array(v)
		TYPE_PACKED_BYTE_ARRAY, TYPE_PACKED_INT32_ARRAY, TYPE_PACKED_INT64_ARRAY, TYPE_PACKED_FLOAT32_ARRAY, TYPE_PACKED_FLOAT64_ARRAY, TYPE_PACKED_VECTOR2_ARRAY, TYPE_PACKED_VECTOR3_ARRAY, TYPE_PACKED_COLOR_ARRAY:
			return var_to_str(v) if v.size() <= 64 else "%s(size=%d)" % [type_string(typeof(v)), v.size()]
		_:
			return var_to_str(v)


func _from_json(v: Variant, want := TYPE_NIL) -> Variant:
	if typeof(v) == TYPE_STRING:
		var s: String = v
		if want == TYPE_OBJECT:
			return load(s) if s.begins_with("res://") else null
		if want != TYPE_NIL and want not in [TYPE_STRING, TYPE_STRING_NAME, TYPE_NODE_PATH]:
			var parsed = str_to_var(s)
			return parsed if parsed != null else s
		if want == TYPE_STRING_NAME:
			return StringName(s)
		if want == TYPE_NODE_PATH:
			return NodePath(s)
		if want == TYPE_NIL:
			if s.begins_with("var:"):
				return str_to_var(s.substr(4))
			if s.begins_with("load:"):
				return load(s.substr(5))
			if s.begins_with("node:"):
				return _resolve(s.substr(5))
		return s
	if want == TYPE_INT and typeof(v) == TYPE_FLOAT:
		return int(v)
	if want == TYPE_FLOAT and typeof(v) == TYPE_INT:
		return float(v)
	if typeof(v) == TYPE_ARRAY:
		return v.map(func(x): return _from_json(x))
	return v


func _prop_types(obj: Object) -> Dictionary:
	var out := {}
	for p in obj.get_property_list():
		out[p.name] = p.type
	return out


func _undo() -> EditorUndoRedoManager:
	return EditorInterface.get_editor_undo_redo()


func _reown(node: Node, owned: Array, root: Node) -> void:
	for n in owned:
		if is_instance_valid(n) and n != root and root.is_ancestor_of(n):
			n.owner = root


func _owned_descendants(node: Node, root: Node, out: Array) -> void:
	for c in node.get_children():
		if c.owner == root:
			out.append(c)
		_owned_descendants(c, root, out)


# ---------- rpc: editor / scene ----------

func rpc_editor_state(_p: Dictionary) -> Variant:
	var root := _root()
	return {
		"godot": Engine.get_version_info().string,
		"project": ProjectSettings.globalize_path("res://"),
		"open_scenes": EditorInterface.get_open_scenes(),
		"edited_scene": root.scene_file_path if root else "",
		"root": {"name": root.name, "type": root.get_class()} if root else null,
		"unsaved": EditorInterface.get_unsaved_scenes(),
		"selection": EditorInterface.get_selection().get_selected_nodes().map(func(n): return _rel(n)) if root else [],
		"playing": EditorInterface.is_playing_scene(),
	}


func rpc_get_tree(p: Dictionary) -> Variant:
	var node = _resolve(p.get("path", ""))
	if not node is Node:
		return _err("node not found: " + str(p.get("path", "")))
	return _tree(node, int(p.get("depth", 6)))


func _tree(n: Node, depth: int) -> Dictionary:
	var d := {"name": n.name, "type": n.get_class()}
	var sc = n.get_script()
	if sc:
		d["script"] = sc.resource_path
	if n.scene_file_path != "" and n != _root():
		d["scene"] = n.scene_file_path
	if depth > 0:
		d["children"] = n.get_children().map(func(c): return _tree(c, depth - 1))
	elif n.get_child_count() > 0:
		d["children_hidden"] = n.get_child_count()
	return d


func rpc_new_scene(p: Dictionary) -> Variant:
	var type: String = p.get("root_type", "Node")
	var path: String = p.path
	if not ClassDB.class_exists(type) or not ClassDB.can_instantiate(type):
		return _err("cannot instantiate " + type)
	var root: Node = ClassDB.instantiate(type)
	root.name = p.get("name", "") if p.get("name", "") != "" else path.get_file().get_basename().to_pascal_case()
	var ps := PackedScene.new()
	ps.pack(root)
	root.free()
	DirAccess.make_dir_recursive_absolute(ProjectSettings.globalize_path(path.get_base_dir()))
	var e := ResourceSaver.save(ps, path)
	if e != OK:
		return _err("save failed: " + error_string(e))
	EditorInterface.get_resource_filesystem().update_file(path)
	EditorInterface.open_scene_from_path(path)
	return {"path": path}


func rpc_open_scene(p: Dictionary) -> Variant:
	if not FileAccess.file_exists(p.path):
		return _err("no such scene " + p.path)
	EditorInterface.open_scene_from_path(p.path)
	return {"path": p.path}


func rpc_save_scene(p: Dictionary) -> Variant:
	if _root() == null:
		return _err("no scene open")
	if p.get("path", "") != "":
		EditorInterface.save_scene_as(p.path)
	else:
		var e := EditorInterface.save_scene()
		if e != OK:
			return _err("save failed: " + error_string(e))
	return {"saved": _root().scene_file_path}


# ---------- rpc: nodes ----------

func rpc_add_node(p: Dictionary) -> Variant:
	var root := _root()
	if root == null:
		return _err("no scene open; use new_scene first")
	var parent = _resolve(p.get("parent", "."))
	if not parent is Node:
		return _err("parent not found: " + str(p.get("parent", ".")))
	var type: String = p.type
	var node: Node
	if type.begins_with("res://"):
		var ps = load(type)
		if not ps is PackedScene:
			return _err("not a scene: " + type)
		node = ps.instantiate()
	elif ClassDB.class_exists(type) and ClassDB.can_instantiate(type) and ClassDB.is_parent_class(type, "Node"):
		node = ClassDB.instantiate(type)
	else:
		return _err("cannot instantiate Node type " + type)
	if p.get("name", "") != "":
		node.name = p.name
	var types := _prop_types(node)
	for k in p.get("props", {}):
		if not types.has(k):
			node.free()
			return _err("%s has no property %s" % [type, k])
		node.set(k, _from_json(p.props[k], types[k]))
	var ur := _undo()
	ur.create_action("MCP: add " + type, UndoRedo.MERGE_DISABLE, root)
	ur.add_do_method(parent, "add_child", node, true)
	ur.add_do_method(node, "set_owner", root)
	ur.add_do_reference(node)
	ur.add_undo_method(parent, "remove_child", node)
	ur.commit_action()
	return {"path": _rel(node), "type": node.get_class()}


func rpc_delete_node(p: Dictionary) -> Variant:
	var root := _root()
	var node = _resolve(p.path)
	if not node is Node or node == root:
		return _err("node not found or is scene root: " + p.path)
	var parent: Node = node.get_parent()
	var owned := []
	_owned_descendants(node, root, owned)
	var idx: int = node.get_index()
	var ur := _undo()
	ur.create_action("MCP: delete " + node.name, UndoRedo.MERGE_DISABLE, root)
	ur.add_do_method(parent, "remove_child", node)
	ur.add_undo_method(parent, "add_child", node, true)
	ur.add_undo_method(parent, "move_child", node, idx)
	ur.add_undo_method(node, "set_owner", root)
	ur.add_undo_method(self, "_reown", node, owned, root)
	ur.add_undo_reference(node)
	ur.commit_action()
	return {"deleted": p.path}


func rpc_reparent_node(p: Dictionary) -> Variant:
	var root := _root()
	var node = _resolve(p.path)
	var np = _resolve(p.new_parent)
	if not node is Node or not np is Node or node == root:
		return _err("node or new parent not found")
	if node == np or node.is_ancestor_of(np):
		return _err("cannot reparent into itself")
	var keep: bool = p.get("keep_global", true)
	var old: Node = node.get_parent()
	var ur := _undo()
	ur.create_action("MCP: reparent " + node.name, UndoRedo.MERGE_DISABLE, root)
	ur.add_do_method(node, "reparent", np, keep)
	ur.add_undo_method(node, "reparent", old, keep)
	ur.commit_action()
	return {"path": _rel(node)}


func rpc_get_properties(p: Dictionary) -> Variant:
	var obj = _resolve(p.path)
	if obj == null:
		return _err("not found: " + p.path)
	var want: Array = p.get("names", [])
	var out := {"$type": obj.get_class()}
	for pr in obj.get_property_list():
		if not (pr.usage & PROPERTY_USAGE_EDITOR) or pr.name.begins_with("_"):
			continue
		if not want.is_empty() and pr.name not in want:
			continue
		var val = obj.get(pr.name)
		if want.is_empty():
			var def = ClassDB.class_get_property_default_value(obj.get_class(), pr.name)
			if typeof(def) == typeof(val) and def == val:
				continue
			if val == null:
				continue
		out[pr.name] = val
	return out


func rpc_set_properties(p: Dictionary) -> Variant:
	var obj = _resolve(p.path)
	if obj == null:
		return _err("not found: " + p.path)
	var types := _prop_types(obj)
	for k in p.props:
		if not types.has(k):
			return _err("%s has no property %s" % [obj.get_class(), k])
	var ur := _undo()
	ur.create_action("MCP: set " + ", ".join(p.props.keys()), UndoRedo.MERGE_DISABLE, _root() if obj is Node else obj)
	for k in p.props:
		ur.add_do_property(obj, k, _from_json(p.props[k], types[k]))
		ur.add_undo_property(obj, k, obj.get(k))
	ur.commit_action()
	return {"set": p.props.keys()}


func rpc_call_method(p: Dictionary) -> Variant:
	var obj = _resolve(p.path)
	if obj == null:
		return _err("not found: " + p.path)
	if not obj.has_method(p.method):
		return _err("%s has no method %s" % [obj.get_class(), p.method])
	var args: Array = p.get("args", []).map(func(a): return _from_json(a))
	return obj.callv(p.method, args)


func rpc_connect_signal(p: Dictionary) -> Variant:
	var src = _resolve(p.path)
	var dst = _resolve(p.target)
	if not src is Node or not dst is Node:
		return _err("source or target not found")
	if not src.has_signal(p.signal):
		return _err("no signal " + p.signal)
	if src.is_connected(p.signal, Callable(dst, p.method)):
		return {"already": true}
	var e: int = src.connect(p.signal, Callable(dst, p.method), CONNECT_PERSIST)
	return {"connected": e == OK}


func rpc_select(p: Dictionary) -> Variant:
	var sel := EditorInterface.get_selection()
	sel.clear()
	for path in p.get("paths", []):
		var n = _resolve(path)
		if n is Node:
			sel.add_node(n)
	return {"selected": sel.get_selected_nodes().size()}


func rpc_list_properties(p: Dictionary) -> Variant:
	if not ClassDB.class_exists(p.class_name):
		return _err("unknown class " + p.class_name)
	var out := []
	for pr in ClassDB.class_get_property_list(p.class_name, p.get("no_inheritance", false)):
		if pr.usage & PROPERTY_USAGE_EDITOR:
			out.append("%s: %s" % [pr.name, type_string(pr.type) if pr.type != TYPE_OBJECT else pr.class_name])
	return out


# ---------- rpc: files / settings / scripts ----------

func _check_res(path: String) -> bool:
	return path.begins_with("res://") and ".." not in path


func rpc_list_files(p: Dictionary) -> Variant:
	var out := []
	_walk(p.get("dir", "res://"), p.get("ext", ""), out)
	return out


func _walk(dir: String, ext: String, out: Array) -> void:
	if out.size() >= 1000:
		return
	var d := DirAccess.open(dir)
	if d == null:
		return
	for f in d.get_files():
		if f.get_extension() in ["uid", "import"] and ext == "":
			continue
		if ext == "" or f.get_extension() == ext.trim_prefix("."):
			out.append(dir.path_join(f))
	for sub in d.get_directories():
		if not sub.begins_with("."):
			_walk(dir.path_join(sub), ext, out)


func rpc_read_file(p: Dictionary) -> Variant:
	if not _check_res(p.path):
		return _err("path must be under res://")
	var f := FileAccess.open(p.path, FileAccess.READ)
	return f.get_as_text() if f else _err("cannot open " + p.path)


func rpc_write_file(p: Dictionary) -> Variant:
	if not _check_res(p.path):
		return _err("path must be under res://")
	DirAccess.make_dir_recursive_absolute(ProjectSettings.globalize_path(p.path.get_base_dir()))
	var f := FileAccess.open(p.path, FileAccess.WRITE)
	if f == null:
		return _err("cannot write " + p.path)
	f.store_string(p.content)
	f.close()
	var fs := EditorInterface.get_resource_filesystem()
	fs.update_file(p.path)
	fs.scan()
	if p.path.get_extension() == "gd":
		var sc = load(p.path)
		if sc is GDScript:
			sc.source_code = p.content
			sc.reload()
	return {"written": p.path, "bytes": p.content.length()}


func rpc_delete_file(p: Dictionary) -> Variant:
	if not _check_res(p.path) or not FileAccess.file_exists(p.path):
		return _err("no such file under res://: " + p.path)
	var e := OS.move_to_trash(ProjectSettings.globalize_path(p.path))
	EditorInterface.get_resource_filesystem().scan()
	return {"trashed": e == OK}


func rpc_get_setting(p: Dictionary) -> Variant:
	if not ProjectSettings.has_setting(p.name):
		return _err("no such setting " + p.name)
	return ProjectSettings.get_setting(p.name)


func rpc_set_setting(p: Dictionary) -> Variant:
	var cur = ProjectSettings.get_setting(p.name) if ProjectSettings.has_setting(p.name) else null
	ProjectSettings.set_setting(p.name, _from_json(p.value, typeof(cur) if cur != null else TYPE_NIL))
	if p.get("save", true):
		ProjectSettings.save()
	return {"set": p.name}


func rpc_validate_script(p: Dictionary) -> Variant:
	if not _check_res(p.path) or not FileAccess.file_exists(p.path):
		return _err("no such script " + p.path)
	var last: int = _log.next_id - 1
	var sc := GDScript.new()
	sc.source_code = FileAccess.get_file_as_string(p.path)
	sc.resource_path = ""
	var e := sc.reload()
	return {"ok": e == OK, "error": error_string(e), "log": _log.since(last)}


func rpc_eval(p: Dictionary) -> Variant:
	var last: int = _log.next_id - 1
	var body := "\n".join(Array(p.code.split("\n")).map(func(l): return "\t" + l))
	var sc := GDScript.new()
	sc.source_code = "@tool\nextends RefCounted\nfunc run(editor, root):\n" + body + "\n\treturn null\n"
	var e := sc.reload()
	if e != OK:
		return _err("compile error: %s\n%s" % [error_string(e), "\n".join(_log.since(last).map(func(l): return l.text))])
	var result = sc.new().run(EditorInterface, _root())
	return {"result": result, "log": _log.since(last)}


func rpc_logs(p: Dictionary) -> Variant:
	var rows := _log.since(int(p.get("since", 0)))
	var level: String = p.get("level", "")
	if level != "":
		rows = rows.filter(func(l): return l.level == level)
	var n := int(p.get("n", 50))
	return rows.slice(maxi(0, rows.size() - n))


# ---------- rpc: play / screenshot ----------

func rpc_play(p: Dictionary) -> Variant:
	match p.get("what", "main"):
		"main":
			EditorInterface.play_main_scene()
		"current":
			EditorInterface.play_current_scene()
		_:
			EditorInterface.play_custom_scene(p.what)
	return {"playing": true}


func rpc_stop(_p: Dictionary) -> Variant:
	EditorInterface.stop_playing_scene()
	return {"playing": false}


func rpc_screenshot(p: Dictionary) -> Variant:
	var vp: Viewport
	match p.get("target", "editor"):
		"2d":
			vp = EditorInterface.get_editor_viewport_2d()
		"3d":
			vp = EditorInterface.get_editor_viewport_3d(0)
		_:
			vp = EditorInterface.get_base_control().get_viewport()
	var tex := vp.get_texture()
	var img: Image = tex.get_image() if tex else null
	if img == null or img.is_empty():
		return _err("no image (headless editor?)")
	if img.get_width() > 1600:
		img.resize(1600, int(img.get_height() * 1600.0 / img.get_width()))
	return Marshalls.raw_to_base64(img.save_png_to_buffer())
