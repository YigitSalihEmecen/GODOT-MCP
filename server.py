#!/usr/bin/env python3
"""MCP server exposing the compiled Godot docs (godot.db) to agents."""
import re
import sqlite3
import sys
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

DB = Path(__file__).parent / "godot.db"
if not DB.exists():
    sys.exit(f"{DB} missing: run `uv run build_db.py` first")
db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, check_same_thread=False)
db.row_factory = sqlite3.Row
META = dict(db.execute("SELECT k, v FROM meta").fetchall())

mcp = FastMCP(
    "godot-docs",
    instructions=(
        f"Offline Godot {META['engine_tag']} documentation (class reference + manual). "
        "Workflow: search -> get_class / get_member / get_page. Prefer get_member over reading a whole class. "
        "Default language is GDScript; pass lang='csharp' or 'all' to change."
    ),
)

STOP = set("a an the how do does did i you to in of is are what can for with on and or my when why which should it this that be use using".split())
KIND_ORDER = ["property", "method", "signal", "constant", "theme_item", "annotation", "constructor", "operator"]
KIND_TITLE = {"property": "Properties", "method": "Methods", "signal": "Signals", "constant": "Constants & enums",
              "theme_item": "Theme items", "annotation": "Annotations", "constructor": "Constructors",
              "operator": "Operators"}
SECTION_KIND = {"properties": "property", "methods": "method", "signals": "signal", "constants": "constant",
                "theme": "theme_item", "annotations": "annotation", "constructors": "constructor",
                "operators": "operator"}
FENCE = re.compile(r"```(\w*)\n(.*?)\n```", re.S)
Lang = Literal["gdscript", "csharp", "all"]


def q(sql, *args):
    return db.execute(sql, args).fetchall()


def one(sql, *args):
    return db.execute(sql, args).fetchone()


def by_lang(text, lang):
    if lang == "all":
        return text
    drop = "csharp" if lang == "gdscript" else "gdscript"
    return FENCE.sub(lambda m: "" if m.group(1) == drop else m.group(0), text)


def clip(text, n):
    return text if len(text) <= n else text[:n].rstrip() + f"\n… [truncated {len(text) - n} chars]"


def first_line(text, n=160):
    line = next((l.strip() for l in text.split("\n") if l.strip() and not l.startswith("```")), "")
    return line if len(line) <= n else line[:n].rsplit(" ", 1)[0] + "…"


def snippet(text, terms, width=220):
    flat = re.sub(r"\s+", " ", re.sub(r"```\w*", "", text))
    lo = flat.lower()
    pos = min((p for p in (lo.find(t.lower()) for t in terms) if p >= 0), default=0)
    start = max(0, pos - width // 3)
    s = flat[start:start + width].strip()
    return ("…" if start else "") + s + ("…" if start + width < len(flat) else "")


def terms_of(query):
    return [t for t in re.findall(r"[@\w]+", query) if t.lower() not in STOP] or re.findall(r"[@\w]+", query)


def fts_match(terms, op):
    return f" {op} ".join('"' + t.replace('"', "") + '"' + ("*" if len(t) >= 4 else "") for t in terms)


def fts(query, kind=None, code=False, limit=8, extra="", args=()):
    terms = terms_of(query)
    if not terms:
        return []
    sql = ("SELECT fts.kind, fts.ref, fts.title, fts.body, bm25(fts, 6.0, 1.0, 0, 0, 0) AS rank FROM fts {join} WHERE fts MATCH ? "
           + ("AND fts.kind=? " if kind else "") + ("AND fts.code='1' " if code else "") + extra
           + "ORDER BY bm25(fts, 6.0, 1.0, 0, 0, 0) LIMIT ?")
    join = "JOIN members m ON m.id = CAST(fts.ref AS INT) AND fts.kind='member'" if "m." in extra else ""
    for op in ("AND", "OR"):
        a = [fts_match(terms, op)] + ([kind] if kind else []) + list(args) + [limit]
        rows = q(sql.format(join=join), *a)
        if len(rows) >= min(limit, 3) or op == "OR":
            return rows
    return rows


# ---------- lookups ----------

def get_class_row(name):
    return one("SELECT * FROM classes WHERE name=? COLLATE NOCASE", name.strip())


def ancestors(name):
    out, seen = [], set()
    while name and name not in seen:
        seen.add(name)
        r = one("SELECT inherits FROM classes WHERE name=?", name)
        name = r["inherits"] if r else ""
        if name:
            out.append(name)
    return out


def class_suggestions(name):
    like = f"%{name.strip()}%"
    rows = q("SELECT name FROM classes WHERE name LIKE ? ORDER BY length(name) LIMIT 8", like)
    if not rows:
        rows = [r for r in fts(name, kind="class", limit=5)]
        return [r["ref"] for r in rows]
    return [r["name"] for r in rows]


def member_label(m):
    return f"{m['class']}.{m['name']}"


def render_member(m, lang, doc_chars=2500):
    head = f"## {member_label(m)} ({m['kind']}{', enum ' + m['grp'] if m['kind'] == 'constant' and m['grp'] else ''})"
    parts = [head, m["sig"]]
    if m["flags"]:
        parts.append(f"⚠ {m['flags']}")
    parts.append(clip(by_lang(m["doc"], lang), doc_chars) or "(no description)")
    return "\n".join(p for p in parts if p)


def members_of(cls, kind=None):
    if kind:
        return q("SELECT * FROM members WHERE class=? AND kind=? ORDER BY pos", cls, kind)
    return q("SELECT * FROM members WHERE class=? ORDER BY pos", cls)


# ---------- tools ----------

@mcp.tool()
def search(query: str, kind: Literal["any", "class", "member", "page"] = "any", limit: int = 8) -> str:
    """Full-text search over classes, class members (methods/properties/signals/enums/...) and manual pages.
    Returns compact hits; follow up with get_class / get_member / get_page using the shown names.
    Exact class names, `Class.member` and bare member names (e.g. move_and_slide) are matched first."""
    limit = max(1, min(limit, 25))
    seen, lines = set(), []

    def add(key, line):
        if key not in seen and len(lines) < limit:
            seen.add(key)
            lines.append(line)

    raw = re.findall(r"[@\w.]+", query)
    if kind in ("any", "class"):
        for t in raw:
            c = one("SELECT name, brief FROM classes WHERE name=? COLLATE NOCASE", t.strip("."))
            if c:
                add(("c", c["name"]), f"[class] {c['name']} — {first_line(c['brief'])}")
    if kind in ("any", "member"):
        for t in raw:
            if "." in t and not t.endswith("."):
                cname, mname = t.split(".", 1)
                rows = q("SELECT * FROM members WHERE class=? COLLATE NOCASE AND name=? COLLATE NOCASE", cname, mname)
            elif "_" in t or t.startswith("@"):
                rows = q("SELECT * FROM members WHERE name=? COLLATE NOCASE LIMIT 4", t)
            else:
                rows = q("SELECT * FROM members WHERE grp=? AND kind='constant' LIMIT 1", t)
                rows = [dict(r, name=t, sig=f"enum {t}", kind="enum", doc="") for r in rows]
            for m in rows:
                add(("m", m["class"], m["name"], m["kind"]),
                    f"[{m['kind']}] {m['class']}.{m['name']} — {m['sig'] if m['kind'] != 'enum' else m['sig'] + ' (get_member for values)'}")
    words = [w.lower() for w in re.findall(r"[A-Za-z0-9@]+", query)]
    if kind in ("any", "class", "member"):
        for n in (3, 2):
            for i in range(len(words) - n + 1):
                gram = words[i:i + n]
                c = one("SELECT name, brief FROM classes WHERE name=? COLLATE NOCASE", "".join(gram))
                if c and kind != "member":
                    add(("c", c["name"]), f"[class] {c['name']} — {first_line(c['brief'])}")
                for m in q("SELECT * FROM members WHERE name=? COLLATE NOCASE LIMIT 3", "_".join(gram)) if kind != "class" else []:
                    add(("m", m["class"], m["name"], m["kind"]), f"[{m['kind']}] {member_label(m)} — {m['sig']}")
    cands = words + ["".join(words[i:i + n]) for n in (2, 3) for i in range(len(words) - n + 1)]
    named = [r["name"] for r in (one("SELECT name FROM classes WHERE name=? COLLATE NOCASE", w) for w in cands) if r] \
        if kind in ("any", "member") else []
    if kind in ("any", "member"):
        for n in (3, 2):
            for i in range(len(words) - n + 1):
                named += [r["name"] for r in q("SELECT name FROM classes WHERE name LIKE ? ORDER BY length(name) LIMIT 2",
                                               "".join(words[i:i + n]) + "%")]
    for cname in dict.fromkeys(named):
        rest = [w for w in terms_of(query) if w.lower() != cname.lower() and w.lower() not in cname.lower()]
        if not rest:
            continue
        for r in fts(" ".join(rest), kind="member", limit=3, extra="AND m.class=? ", args=(cname,)):
            m = one("SELECT * FROM members WHERE id=?", int(r["ref"]))
            add(("m", m["class"], m["name"], m["kind"]), f"[{m['kind']}] {member_label(m)} — {m['sig']}\n    {first_line(m['doc'])}")
    ts = terms_of(query)
    if kind == "any":
        api = sorted(fts(query, "class", limit=limit) + fts(query, "member", limit=limit), key=lambda r: r["rank"])[:limit]
        pages = fts(query, "page", limit=limit)
        ranked = [r for pair in zip(api, pages) for r in pair] + api[len(pages):] + pages[len(api):]
    else:
        ranked = fts(query, kind, limit=limit * 2)
    for r in ranked:
        if r["kind"] == "class":
            c = one("SELECT name, brief FROM classes WHERE name=?", r["ref"])
            add(("c", c["name"]), f"[class] {c['name']} — {first_line(c['brief'])}")
        elif r["kind"] == "member":
            m = one("SELECT * FROM members WHERE id=?", int(r["ref"]))
            add(("m", m["class"], m["name"], m["kind"]),
                f"[{m['kind']}] {member_label(m)} — {m['sig']}\n    {first_line(m['doc'])}")
        else:
            s = one("SELECT path, heading, text FROM sections WHERE id=?", int(r["ref"]))
            title = one("SELECT title FROM pages WHERE path=?", s["path"])["title"]
            add(("p", s["path"], s["heading"]),
                f"[page] {s['path']} § {s['heading'] or title}\n    {snippet(s['text'], ts)}")
    return "\n".join(lines) or f"No results for {query!r}."


@mcp.tool()
def get_class(name: str, section: Literal["summary", "properties", "methods", "signals", "constants", "theme",
                                          "annotations", "constructors", "operators"] = "summary",
              inherited: bool = False, lang: Lang = "gdscript") -> str:
    """Class reference. 'summary' = inheritance, description, tutorials and a name-only member index.
    Any other section lists that member kind with signatures + one-line docs (inherited=True adds ancestor members).
    Use get_member for the full text of a single member."""
    c = get_class_row(name)
    if not c:
        return f"No class {name!r}. Similar: {', '.join(class_suggestions(name)) or 'none'}"
    cname = c["name"]
    chain = ancestors(cname)
    if section != "summary":
        kind = SECTION_KIND[section]
        out = []
        for owner in [cname] + (chain if inherited else []):
            rows = members_of(owner, kind)
            if not rows:
                continue
            out.append(f"### {owner}" + (" (inherited)" if owner != cname else ""))
            for m in rows:
                tag = f"{m['grp']}." if kind == "constant" and m["grp"] else ""
                flag = " ⚠" + m["flags"].split(":")[0] if m["flags"] else ""
                out.append(f"- {tag}{m['sig']}{flag} — {first_line(by_lang(m['doc'], lang), 120)}")
        return "\n".join(out) or f"{cname} has no {section}."

    head = f"# {cname}" + (f" (inherits {' < '.join(chain)})" if chain else "") + f" [{c['source'] if c['api_type'] == 'core' else c['api_type'] + ', ' + c['source']}]"
    out = [head]
    if c["flags"]:
        out.append(f"⚠ {c['flags']}")
    out.append(f"Docs: {META['docs_branch']} https://docs.godotengine.org/en/stable/classes/class_{cname.lower()}.html")
    out.append(first_line(c["brief"], 400))
    out.append(clip(by_lang(c["doc"], lang), 3500))
    links = q("SELECT title, url FROM class_links WHERE class=?", cname)
    if links:
        out.append("Tutorials: " + "; ".join(f"{l['title']} ({l['url'].replace('https://docs.godotengine.org/en/stable/', '').removesuffix('.html')})"
                                             if l["url"].startswith("https://docs.godotengine.org") else f"{l['title']} ({l['url']})"
                                             for l in links))
    for kind in KIND_ORDER:
        rows = members_of(cname, kind)
        if not rows:
            continue
        if kind == "constant":
            enums, plain = {}, []
            for m in rows:
                (enums.setdefault(m["grp"], []) if m["grp"] else plain).append(m["name"])
            items = [f"enum {g} {{{', '.join(v)}}}" for g, v in enums.items()] + plain
            out.append(f"{KIND_TITLE[kind]}: " + "; ".join(items))
        else:
            out.append(f"{KIND_TITLE[kind]} ({len(rows)}): " + ", ".join(dict.fromkeys(m["name"] for m in rows)))
    kids = [r["name"] for r in q("SELECT name FROM classes WHERE inherits=? ORDER BY name", cname)]
    if kids:
        out.append(f"Direct subclasses ({len(kids)}): " + ", ".join(kids))
    if chain:
        out.append("Inherited members: get_class(name, section=..., inherited=True)")
    return "\n\n".join(out)


@mcp.tool()
def get_member(class_name: str, member: str = "", lang: Lang = "gdscript") -> str:
    """Full docs for one method/property/signal/constant/enum/theme item/annotation. Accepts ('Node2D','look_at')
    or ('Node2D.look_at',''). Searches ancestors if the class itself lacks it. An enum name (e.g. 'MouseMode')
    returns all its values. Use class '@GDScript' for annotations like @export, '@GlobalScope' for global funcs/enums."""
    if "." in class_name and not member:
        class_name, member = class_name.split(".", 1)
    c = get_class_row(class_name)
    if not c:
        return f"No class {class_name!r}. Similar: {', '.join(class_suggestions(class_name)) or 'none'}"
    cname, member = c["name"], member.strip()
    for owner in [cname] + ancestors(cname):
        rows = q("SELECT * FROM members WHERE class=? AND name=? COLLATE NOCASE ORDER BY pos", owner, member)
        if not rows and member and not member.startswith("@"):
            rows = q("SELECT * FROM members WHERE class=? AND name=? COLLATE NOCASE ORDER BY pos", owner, "@" + member)
        if rows:
            note = f"(found on ancestor {owner})\n" if owner != cname else ""
            return note + "\n\n".join(render_member(m, lang) for m in rows)
        enum = q("SELECT * FROM members WHERE class=? AND grp=? COLLATE NOCASE AND kind='constant' ORDER BY pos", owner, member)
        if enum:
            return f"## enum {owner}.{enum[0]['grp']}\n" + "\n".join(
                f"- {m['sig']} — {first_line(by_lang(m['doc'], lang), 200)}" for m in enum)
    others = q("SELECT class, kind, name FROM members WHERE name=? COLLATE NOCASE OR grp=? COLLATE NOCASE LIMIT 6", member, member)
    near = q("SELECT name FROM members WHERE class=? AND name LIKE ? LIMIT 8", cname, f"%{member}%")
    hint = []
    if near:
        hint.append("In " + cname + ": " + ", ".join(r["name"] for r in near))
    if others:
        hint.append("Elsewhere: " + ", ".join(f"{r['class']}.{r['name']}" for r in others))
    return f"No member {member!r} on {cname} or its ancestors. " + " | ".join(hint)


def find_page(path):
    path = path.strip().strip("/").removesuffix(".html").removesuffix(".rst")
    p = one("SELECT * FROM pages WHERE path=?", path)
    if p:
        return [p]
    rows = q("SELECT * FROM pages WHERE path LIKE ? OR title LIKE ? COLLATE NOCASE ORDER BY length(path) LIMIT 8",
             f"%{path}%", f"%{path}%")
    return rows


@mcp.tool()
def get_page(path: str, section: str = "", max_chars: int = 6000, lang: Lang = "gdscript") -> str:
    """Read a manual/tutorial page by path (e.g. 'tutorials/2d/2d_movement'; fuzzy matched on path/title).
    section = heading text (substring match) returns only that heading and its subsections.
    Long pages are truncated with an outline; request a section to read the rest."""
    hits = find_page(path)
    if not hits:
        return f"No page matching {path!r}. Try search or list_docs."
    if len(hits) > 1:
        return "Ambiguous, pick one:\n" + "\n".join(f"- {h['path']} — {h['title']}" for h in hits)
    p = hits[0]
    secs = q("SELECT level, heading, text FROM sections WHERE path=? ORDER BY ord", p["path"])
    if section:
        s = section.lower()
        idx = next((i for i, x in enumerate(secs) if x["heading"].lower() == s), None)
        if idx is None:
            idx = next((i for i, x in enumerate(secs) if s in x["heading"].lower()), None)
        if idx is None:
            return f"No section {section!r} in {p['path']}. Sections: " + "; ".join(x["heading"] for x in secs if x["heading"])
        end = next((j for j in range(idx + 1, len(secs)) if secs[j]["level"] <= secs[idx]["level"]), len(secs))
        secs = secs[idx:end]
    body = "\n\n".join((f"{'#' * x['level']} {x['heading']}\n" if x["heading"] else "") + x["text"] for x in secs)
    body = by_lang(body, lang)
    out = f"# {p['title']}  ({p['path']})  {p['url']}\n\n{clip(body, max_chars)}"
    if len(body) > max_chars:
        out += "\n\nOutline (use section=...): " + "; ".join(
            f"{'  ' * (x['level'] - 2) if x['level'] > 2 else ''}{x['heading']} [{len(x['text'])}]"
            for x in secs if x["heading"])
    return out


@mcp.tool()
def list_docs(prefix: str = "") -> str:
    """Browse the manual tree. '' lists top-level areas; 'tutorials/2d' lists subfolders and pages beneath it."""
    prefix = prefix.strip("/")
    rows = q("SELECT path, title FROM pages WHERE path LIKE ? ORDER BY path", f"{prefix}/%" if prefix else "%")
    if not rows:
        return f"Nothing under {prefix!r}."
    groups, pages = {}, []
    for r in rows:
        rest = r["path"][len(prefix) + 1:] if prefix else r["path"]
        if "/" in rest:
            groups[rest.split("/")[0]] = groups.get(rest.split("/")[0], 0) + 1
        else:
            pages.append(r)
    out = [f"{g}/ ({n} pages)" for g, n in groups.items()] + [f"{r['path']} — {r['title']}" for r in pages]
    return "\n".join(out)


@mcp.tool()
def list_classes(contains: str = "", base: str = "", source: str = "", limit: int = 200) -> str:
    """List class names. contains = substring filter; base = only descendants of that class (shown as a tree);
    source = 'core', or e.g. 'modules/openxr'. Editor-only classes are tagged [editor]."""
    if base:
        c = get_class_row(base)
        if not c:
            return f"No class {base!r}."
        out = []

        def walk(n, depth):
            for r in q("SELECT name, api_type FROM classes WHERE inherits=? ORDER BY name", n):
                if len(out) < limit:
                    out.append("  " * depth + r["name"] + (" [editor]" if r["api_type"] == "editor" else ""))
                walk(r["name"], depth + 1)

        walk(c["name"], 0)
        return f"{c['name']} descendants ({len(out)}{'+' if len(out) >= limit else ''}):\n" + "\n".join(out)
    sql, args = "SELECT name, api_type FROM classes WHERE name LIKE ?", [f"%{contains}%"]
    if source:
        sql, args = sql + " AND source LIKE ?", args + [f"{source}%"]
    rows = q(sql + " ORDER BY name LIMIT ?", *args, limit)
    return f"{len(rows)} classes: " + ", ".join(r["name"] + (" [editor]" if r["api_type"] == "editor" else "") for r in rows)


@mcp.tool()
def find_examples(topic: str, limit: int = 4, lang: Lang = "gdscript") -> str:
    """Find code snippets about a topic from the manual and class reference. Returns the code blocks themselves."""
    limit = max(1, min(limit, 10))
    out = []
    for r in fts(topic, code=True, limit=limit * 3):
        if r["kind"] == "page":
            s = one("SELECT path, heading FROM sections WHERE id=?", int(r["ref"]))
            src = f"{s['path']} § {s['heading']}"
        elif r["kind"] == "member":
            src = member_label(one("SELECT class, name FROM members WHERE id=?", int(r["ref"])))
        else:
            src = r["ref"]
        blocks = [m.group(0) for m in FENCE.finditer(by_lang(r["body"], lang)) if m.group(2).strip()]
        if blocks:
            out.append(f"### {src}\n" + clip("\n".join(blocks[:2]), 1500))
        if len(out) >= limit:
            break
    return "\n\n".join(out) or f"No code examples for {topic!r}."


@mcp.tool()
def settings(query: str, scope: Literal["project", "editor"] = "project", limit: int = 8, lang: Lang = "gdscript") -> str:
    """Look up Project Settings (or Editor Settings) by name fragment or topic, e.g. 'gravity', 'physics/2d'.
    Returns setting path, type, default and description."""
    cls = "ProjectSettings" if scope == "project" else "EditorSettings"
    limit = max(1, min(limit, 20))
    rows = q("SELECT * FROM members WHERE class=? AND kind='property' AND name LIKE ? ORDER BY length(name) LIMIT ?",
             cls, f"%{query.strip().replace(' ', '%')}%", limit)
    if len(rows) < limit:
        seen = {r["id"] for r in rows}
        for r in fts(query, kind="member", limit=limit * 3, extra="AND m.class=? ", args=(cls,)):
            m = one("SELECT * FROM members WHERE id=?", int(r["ref"]))
            if m["id"] not in seen and len(rows) < limit:
                seen.add(m["id"])
                rows.append(m)
    return "\n\n".join(f"{m['sig']}\n{clip(by_lang(m['doc'], lang), 500)}" for m in rows) or f"No {scope} settings match {query!r}."


@mcp.tool()
def info() -> str:
    """Docs version, build metadata and corpus size."""
    srcs = q("SELECT source, count(*) n FROM classes GROUP BY source ORDER BY n DESC")
    return "\n".join([f"{k}: {v}" for k, v in META.items()] +
                     ["class sources: " + ", ".join(f"{r['source']}={r['n']}" for r in srcs)])


if __name__ == "__main__":
    mcp.run()
