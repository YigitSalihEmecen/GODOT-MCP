#!/usr/bin/env python3
"""Fetch Godot docs + class XML and compile them into a single SQLite (FTS5) file."""
import argparse
import posixpath
import re
import sqlite3
import subprocess
import textwrap
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DB = ROOT / "godot.db"
DOCS_URL = "https://docs.godotengine.org/en/stable"
DOCS_REPO = "https://github.com/godotengine/godot-docs.git"
ENGINE_REPO = "https://github.com/godotengine/godot.git"
SKIP_DIRS = {"classes", "img", "_static", "_templates", "_tools", "_styleguides", "_extensions"}

SCHEMA = """
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE classes(name TEXT PRIMARY KEY, inherits TEXT, api_type TEXT, source TEXT,
                     brief TEXT, doc TEXT, flags TEXT);
CREATE INDEX classes_inh ON classes(inherits);
CREATE TABLE class_links(class TEXT, title TEXT, url TEXT);
CREATE INDEX class_links_c ON class_links(class);
CREATE TABLE members(id INTEGER PRIMARY KEY, class TEXT, kind TEXT, name TEXT, sig TEXT,
                     doc TEXT, grp TEXT, flags TEXT, pos INT);
CREATE INDEX members_cn ON members(class, name);
CREATE TABLE pages(path TEXT PRIMARY KEY, title TEXT, url TEXT, chars INT);
CREATE TABLE sections(id INTEGER PRIMARY KEY, path TEXT, ord INT, level INT, heading TEXT, text TEXT);
CREATE INDEX sections_po ON sections(path, ord);
CREATE VIRTUAL TABLE fts USING fts5(title, body, kind UNINDEXED, ref UNINDEXED, code UNINDEXED,
                                    tokenize="porter unicode61 tokenchars '_@'");
"""


def sh(*cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True)


def fetch(docs_branch, engine_tag):
    DATA.mkdir(exist_ok=True)
    docs, src = DATA / "godot-docs", DATA / "godot-src"
    if not docs.exists():
        sh("git", "clone", "--depth", "1", "-b", docs_branch, DOCS_REPO, str(docs))
    if not src.exists():
        sh("git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", "-b", engine_tag, ENGINE_REPO, str(src))
        sh("git", "sparse-checkout", "set", "--no-cone", "/doc/classes/", "/modules/*/doc_classes/",
           "/platform/*/doc_classes/", cwd=src)
    return docs, src


def split_words(name):
    return " ".join(re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+[A-Z]?", name.replace("_", " ").replace("@", "")))


# ---------- BBCode (class reference) ----------

CODE_LANGS = ("gdscript", "csharp")


def _fence(lang, body):
    body = textwrap.dedent(body.strip("\n")).rstrip()
    return f"\n```{lang}\n{body}\n```\n"


def bbcode(text, current_class=None):
    if not text:
        return ""
    text = textwrap.dedent(text.replace("\t", "    ")).strip("\n")
    stash = []

    def hold(s):
        stash.append(s)
        return f"\x00{len(stash) - 1}\x00"

    def codeblocks(m):
        inner = m.group(1)
        parts = [_fence(lg, b) for lg, b in re.findall(r"\[(gdscript|csharp)[^\]]*\](.*?)\[/\1\]", inner, re.S)]
        return hold("".join(parts))

    text = re.sub(r"\[codeblocks\](.*?)\[/codeblocks\]", codeblocks, text, flags=re.S)
    text = re.sub(r"\[codeblock(?:\s+lang=(\w+))?[^\]]*\](.*?)\[/codeblock\]",
                  lambda m: hold(_fence(m.group(1) or "gdscript", m.group(2))), text, flags=re.S)
    text = re.sub(r"\[code(?:\s[^\]]*)?\](.*?)\[/code\]", lambda m: hold(f"`{m.group(1)}`"), text, flags=re.S)
    text = re.sub(r"\[kbd\](.*?)\[/kbd\]", lambda m: f"`{m.group(1)}`", text)

    def ref(m):
        kind, target = m.group(1), m.group(2).strip()
        if kind == "param":
            return f"`{target}`"
        if kind == "method":
            return f"`{target}()`"
        return f"`{target}`"

    text = re.sub(r"\[(method|member|signal|constant|enum|annotation|theme_item|constructor|operator|param) ([^\]]+)\]", ref, text)
    text = re.sub(r"\[url=([^\]]+)\](.*?)\[/url\]", r"[\2](\1)", text, flags=re.S)
    text = re.sub(r"\[url\](.*?)\[/url\]", r"\1", text)
    text = re.sub(r"\[/?(b|i|u|s|center|right|font|color|bgcolor|fgcolor|img|table|cell|ul|ol|li|outline_color|outline_size)(?:[ =][^\]]*)?\]",
                  lambda m: "**" if m.group(1) == "b" else ("*" if m.group(1) == "i" else ""), text)
    text = text.replace("[br]", "\n").replace("[lb]", "[").replace("[rb]", "]")
    text = re.sub(r"\[([A-Za-z@][A-Za-z0-9_@.]*)\]", r"`\1`", text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)
    text = text.replace("$DOCS_URL", DOCS_URL)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------- class reference ----------

def tname(el):
    t = el.get("enum") or el.get("type") or "Variant"
    return f"BitField[{t}]" if el.get("is_bitfield") == "true" and el.get("enum") else t


def flags_of(el):
    out = []
    if el.get("deprecated") is not None:
        out.append("deprecated" + (f": {el.get('deprecated')}" if el.get("deprecated") else ""))
    if el.get("experimental") is not None:
        out.append("experimental" + (f": {el.get('experimental')}" if el.get("experimental") else ""))
    return "; ".join(out)


def params_sig(el, vararg=False):
    ps = []
    for p in sorted(el.findall("param"), key=lambda p: int(p.get("index", 0))):
        s = f"{p.get('name')}: {tname(p)}"
        if p.get("default") is not None:
            s += f" = {p.get('default')}"
        ps.append(s)
    if vararg:
        ps.append("...")
    return ", ".join(ps)


def method_sig(m, label="func"):
    q = (m.get("qualifiers") or "").split()
    pre = " ".join(x for x in q if x in ("static", "virtual", "vararg", "required"))
    ret = m.find("return")
    r = f" -> {tname(ret)}" if ret is not None else ""
    name = m.get("name")
    return f"{pre + ' ' if pre else ''}{label} {name}({params_sig(m, 'vararg' in q)}){r}"


def parse_class(path, db, source):
    root = ET.parse(path).getroot()
    cls = root.get("name")
    brief = bbcode((root.findtext("brief_description") or ""))
    doc = bbcode(root.findtext("description") or "")
    db.execute("INSERT INTO classes VALUES(?,?,?,?,?,?,?)",
               (cls, root.get("inherits") or "", root.get("api_type") or "core", source, brief, doc, flags_of(root)))
    for l in root.findall("tutorials/link"):
        db.execute("INSERT INTO class_links VALUES(?,?,?)",
                   (cls, l.get("title"), (l.text or "").strip().replace("$DOCS_URL", DOCS_URL)))
    db.execute("INSERT INTO fts VALUES(?,?,?,?,?)",
               (f"{cls} {split_words(cls)}", f"{brief}\n{doc}", "class", cls, "1" if "```" in doc else "0"))

    pos = 0

    def add(kind, name, sig, text, grp="", flags=""):
        nonlocal pos
        pos += 1
        d = bbcode(text, cls)
        cur = db.execute("INSERT INTO members(class,kind,name,sig,doc,grp,flags,pos) VALUES(?,?,?,?,?,?,?,?)",
                         (cls, kind, name, sig, d, grp, flags, pos))
        title = f"{cls}.{name} {cls} {name} {split_words(name)}"
        db.execute("INSERT INTO fts VALUES(?,?,?,?,?)",
                   (title, f"{sig}\n{d}", "member", str(cur.lastrowid), "1" if "```" in d else "0"))

    for m in root.findall("members/member"):
        sig = f"var {m.get('name')}: {tname(m)}"
        if m.get("default") is not None:
            sig += f" = {m.get('default')}"
        acc = [f"{k} {m.get(k)}" for k in ("setter", "getter") if m.get(k)]
        if acc:
            sig += f"  [{', '.join(acc)}]"
        add("property", m.get("name"), sig, m.text or "", m.get("overrides") or "", flags_of(m))
    for m in root.findall("methods/method"):
        add("method", m.get("name"), method_sig(m), m.findtext("description") or "", "", flags_of(m))
    for m in root.findall("constructors/constructor"):
        add("constructor", m.get("name"), method_sig(m, "new"), m.findtext("description") or "")
    for m in root.findall("operators/operator"):
        ret = m.find("return")
        add("operator", m.get("name"), f"{m.get('name')}({params_sig(m)}) -> {tname(ret) if ret is not None else 'Variant'}",
            m.findtext("description") or "")
    for m in root.findall("signals/signal"):
        add("signal", m.get("name"), f"signal {m.get('name')}({params_sig(m)})", m.findtext("description") or "", "", flags_of(m))
    for m in root.findall("constants/constant"):
        add("constant", m.get("name"), f"{m.get('name')} = {m.get('value')}", m.text or "", m.get("enum") or "", flags_of(m))
    for m in root.findall("theme_items/theme_item"):
        sig = f"{m.get('name')}: {m.get('type')}" + (f" = {m.get('default')}" if m.get("default") else "")
        add("theme_item", m.get("name"), sig, m.text or "", m.get("data_type") or "")
    for m in root.findall("annotations/annotation"):
        add("annotation", m.get("name"), f"{m.get('name')}({params_sig(m)})", m.findtext("description") or "")


# ---------- RST (manual / tutorials) ----------

UNDERLINE = re.compile(r"^([=\-~^\"#*+`:.'_<>])\1{2,}\s*$")
DIRECTIVE = re.compile(r"^(\s*)\.\.\s+([\w-]+)::\s*(.*)$")
LABEL = re.compile(r"^\.\.\s+_([^:]+):\s*$")
DROP = {"image", "figure", "video", "youtube", "raw", "index", "meta", "toctree", "contents", "rst-class",
        "highlight", "cssclass", "sectionauthor", "only-not", "tab-set", "versionmodified"}
ADMON = {"note": "Note", "tip": "Tip", "warning": "Warning", "important": "Important", "danger": "Danger",
         "caution": "Caution", "seealso": "See also", "attention": "Attention", "hint": "Hint",
         "versionadded": "Added in", "versionchanged": "Changed in", "deprecated": "Deprecated since"}


def indent_of(line):
    return len(line) - len(line.lstrip())


def take_body(lines, i, base):
    j = i
    while j < len(lines) and (not lines[j].strip() or indent_of(lines[j]) > base):
        j += 1
    body = lines[i:j]
    while body and not body[-1].strip():
        body.pop()
    return body, j


class Rst:
    def __init__(self, labels, page_dir):
        self.labels, self.page_dir = labels, page_dir

    def inline(self, s):
        def ref(m):
            role, body = m.group(1), m.group(2)
            t = re.match(r"(.*?)\s*<([^>]+)>$", body, re.S)
            title, target = (t.group(1), t.group(2)) if t else (None, body)
            if role == "ref":
                if target.startswith("class_"):
                    return title or target[6:]
                hit = self.labels.get(target.lower())
                if hit:
                    return f"{title or hit[1]} (→{hit[0]})"
                return title or target
            if role == "doc":
                p = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(self.page_dir, target))
                return f"{title or p} (→{p})"
            if role == "download":
                return title or target
            return title or target

        s = re.sub(r":(\w+):`([^`]+)`", ref, s)
        s = re.sub(r"`([^`<]+?)\s*<(https?://[^>]+)>`__?", r"[\1](\2)", s)
        s = re.sub(r"``([^`]+)``", r"`\1`", s)
        return s

    def convert(self, lines, top=False):
        """Returns markdown lines; at top level emits '\\x01<level>\\x01Title' heading markers."""
        out, i = [], 0
        levels = self.levels if top else None
        while i < len(lines):
            line = lines[i]
            m = DIRECTIVE.match(line)
            if m:
                base, name, arg = len(m.group(1)), m.group(2), m.group(3).strip()
                body, i = take_body(lines, i + 1, base)
                out += self.directive(name, arg, body)
                continue
            if LABEL.match(line) or (re.match(r"^\s*\.\.\s+\|", line)):
                i += 1
                continue
            if re.match(r"^\s*\.\.(\s|$)", line):
                _, i = take_body(lines, i + 1, indent_of(line))
                continue
            if top and i + 1 < len(lines) and line.strip() and UNDERLINE.match(lines[i + 1]) \
                    and not UNDERLINE.match(line) and indent_of(line) == 0:
                ch = lines[i + 1][0]
                lvl = levels.setdefault(ch, len(levels) + 1)
                out.append(f"\x01{lvl}\x01{self.inline(line.strip())}")
                i += 2
                continue
            if top and UNDERLINE.match(line) and (i == 0 or not lines[i - 1].strip()):
                i += 1
                continue
            stripped = line.rstrip()
            if stripped.endswith("::") and not stripped.lstrip().startswith(".."):
                text = stripped[:-2].rstrip()
                if stripped.strip() != "::":
                    out.append(self.inline(text + (":" if text else "")))
                body, i = take_body(lines, i + 1, indent_of(line))
                out.append(_fence("", "\n".join(body)))
                continue
            out.append(self.inline(stripped))
            i += 1
        return out

    def directive(self, name, arg, body):
        dedented = textwrap.dedent("\n".join(body)).split("\n") if body else []
        if name in ("code-block", "code", "sourcecode", "code-tab"):
            lang = (arg.split() or [""])[0].lower()
            while dedented and (dedented[0].startswith(":") or not dedented[0].strip()):
                dedented.pop(0)
            return [_fence(lang, "\n".join(dedented))]
        if name in ADMON:
            inner = ([arg] if arg else []) + dedented
            conv = [l for l in self.convert(inner) if l is not None]
            label = f"**{ADMON[name]}:**"
            return [f"> {label} " + (conv[0] if conv else "")] + [f"> {l}" for l in conv[1:]]
        if name == "tab":
            return [f"**{arg}**"] + self.convert(dedented)
        if name in DROP:
            return []
        inner = ([arg] if arg and name not in ("only",) else []) + dedented
        return self.convert(inner)

    def parse(self, text):
        self.levels = {}
        text = re.sub(r":\w+:`[^`]+`|`[^`]*<https?://[^>]*>`__?",
                      lambda m: re.sub(r"\s*\n\s*", " ", m.group(0)), text)
        lines = [l.expandtabs(4) for l in text.splitlines()]
        return self.convert(lines, top=True)


def collect_labels(files, docs):
    labels = {}
    for f in files:
        rel = f.relative_to(docs).with_suffix("").as_posix()
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, l in enumerate(lines):
            m = LABEL.match(l)
            if not m:
                continue
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or LABEL.match(lines[j])):
                j += 1
            title = None
            if j + 1 < len(lines) and UNDERLINE.match(lines[j + 1]) and lines[j].strip():
                title = lines[j].strip()
            labels[m.group(1).lower()] = (rel, title or m.group(1))
    return labels


def page_sections(md_lines):
    """Split converted lines on heading markers -> [(level, heading, text)]."""
    secs, cur = [], [1, "", []]
    for l in md_lines:
        m = re.match(r"\x01(\d+)\x01(.*)", l)
        if m:
            if cur[1] or "".join(cur[2]).strip():
                secs.append(cur)
            cur = [int(m.group(1)), m.group(2), []]
        else:
            cur[2].append(l)
    secs.append(cur)
    out = []
    for lvl, head, body in secs:
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(body)).strip()
        if head or text:
            out.append((lvl, head, text))
    return out


def build_pages(docs, db):
    files = sorted(p for p in docs.rglob("*.rst")
                   if not (set(p.relative_to(docs).parts[:-1]) & SKIP_DIRS) and p.name != "404.rst")
    labels = collect_labels(files, docs)
    n_sec = 0
    for f in files:
        rel = f.relative_to(docs).with_suffix("").as_posix()
        rst = Rst(labels, posixpath.dirname(rel))
        secs = page_sections(rst.parse(f.read_text(encoding="utf-8", errors="replace")))
        if not secs:
            continue
        title = next((h for lvl, h, _ in secs if h), rel)
        total = sum(len(t) + len(h) for _, h, t in secs)
        db.execute("INSERT INTO pages VALUES(?,?,?,?)", (rel, title, f"{DOCS_URL}/{rel}.html", total))
        trail = []
        for ord_, (lvl, head, text) in enumerate(secs):
            trail = [(l, h) for l, h in trail if l < lvl] + [(lvl, head)]
            cur = db.execute("INSERT INTO sections(path,ord,level,heading,text) VALUES(?,?,?,?,?)",
                             (rel, ord_, lvl, head, text))
            n_sec += 1
            if len(text) < 40:
                continue
            crumbs = " > ".join(h for _, h in trail if h)
            db.execute("INSERT INTO fts VALUES(?,?,?,?,?)",
                       (f"{crumbs} {rel.replace('/', ' ').replace('_', ' ')}", text, "page",
                        str(cur.lastrowid), "1" if "```" in text else "0"))
    return len(files), n_sec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs-branch", default="stable")
    ap.add_argument("--engine-tag", default="4.7-stable")
    a = ap.parse_args()
    docs, src = fetch(a.docs_branch, a.engine_tag)

    if DB.exists():
        DB.unlink()
    db = sqlite3.connect(DB)
    db.executescript(SCHEMA)

    n_cls = 0
    for xml in sorted(src.rglob("*.xml")):
        rel = xml.relative_to(src).as_posix()
        parts = rel.split("/")
        source = "core" if parts[0] == "doc" else f"{parts[0]}/{parts[1]}"
        parse_class(xml, db, source)
        n_cls += 1
    n_pages, n_sec = build_pages(docs, db)

    commit = lambda p: subprocess.run(["git", "-C", str(p), "rev-parse", "--short", "HEAD"],
                                      capture_output=True, text=True).stdout.strip()
    meta = {"docs_branch": a.docs_branch, "engine_tag": a.engine_tag, "docs_commit": commit(docs),
            "engine_commit": commit(src), "built": time.strftime("%Y-%m-%d"), "classes": n_cls,
            "pages": n_pages, "sections": n_sec,
            "members": db.execute("SELECT count(*) FROM members").fetchone()[0]}
    db.executemany("INSERT INTO meta VALUES(?,?)", [(k, str(v)) for k, v in meta.items()])
    db.commit()
    db.execute("INSERT INTO fts(fts) VALUES('optimize')")
    db.commit()
    db.close()
    print(meta, f"{DB.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
