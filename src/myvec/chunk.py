"""Chunking: tree-sitter for code, headings for markdown, line windows as fallback."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

WINDOW = 60
OVERLAP = 10
MAX_CHUNK_LINES = 120   # larger definitions get split
# embeddinggemma has a 2048-token context: embedded text (path/symbol head + chunk) stays <= ~1500 tokens
# (chars/4 = 6000). The chunk text itself is capped a bit lower to leave room for the head.
EMBED_MAX_CHARS = 6000
MAX_CHUNK_CHARS = 5800  # hard cap of stored/embedded chunk text
MAX_CHUNKS_PER_FILE = 400
# a chunk this small (fewer lines AND fewer chars than both limits) is merged with a neighbour: one-line members
# and standalone comments would otherwise crowd the top of the search results
MIN_CHUNK_LINES = 12
MIN_CHUNK_CHARS = 500


@dataclass
class Chunk:
    start_line: int  # 1-based inclusive
    end_line: int
    symbol: str | None
    text: str


EXT_LANG = {
    ".php": "php", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "tsx", ".vue": "vue", ".py": "python", ".cs": "csharp", ".go": "go",
    ".java": "java", ".rs": "rust", ".sh": "bash", ".bash": "bash", ".md": "markdown",
    ".markdown": "markdown", ".html": "html", ".css": "css", ".scss": "scss", ".json": "json",
    ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".xml": "xml", ".tpl": "smarty", ".twig": "twig",
}
TS_NAME = {"csharp": "csharp"}  # tree-sitter-language-pack names


def detect_lang(path: str) -> str:
    return EXT_LANG.get(PurePosixPath(path).suffix.lower(), "text")


# per-language: definition node types, node types that are transparent wrappers at top level
DEFS: dict[str, set[str]] = {
    "python": {"function_definition", "class_definition", "decorated_definition"},
    "javascript": {"function_declaration", "generator_function_declaration", "class_declaration",
                   "export_statement", "lexical_declaration", "variable_declaration", "method_definition"},
    "php": {"function_definition", "class_declaration", "interface_declaration", "trait_declaration",
            "enum_declaration", "method_declaration"},
    "csharp": {"class_declaration", "interface_declaration", "struct_declaration", "enum_declaration",
               "record_declaration", "method_declaration", "constructor_declaration", "property_declaration"},
    "go": {"function_declaration", "method_declaration", "type_declaration"},
    "java": {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration",
             "method_declaration", "constructor_declaration"},
    "rust": {"function_item", "struct_item", "enum_item", "trait_item", "impl_item", "mod_item"},
    "bash": {"function_definition"},
}
DEFS["typescript"] = DEFS["javascript"] | {
    "interface_declaration", "type_alias_declaration", "enum_declaration", "abstract_class_declaration",
}
DEFS["tsx"] = DEFS["typescript"]
TRANSPARENT: dict[str, set[str]] = {
    "php": {"namespace_definition", "compound_statement"},
    "csharp": {"namespace_declaration", "file_scoped_namespace_declaration", "declaration_list"},
    "rust": set(),
}
_parsers: dict[str, object] = {}


def _parser(lang: str):
    if lang in _parsers:
        return _parsers[lang]
    p = None
    try:
        from tree_sitter_language_pack import get_parser

        p = get_parser(TS_NAME.get(lang, lang))
    except Exception:
        p = None
    _parsers[lang] = p
    return p


def _node_name(node, src: bytes) -> str | None:
    t = node.type
    if t in ("decorated_definition",):
        d = node.child_by_field_name("definition")
        return _node_name(d, src) if d is not None else None
    if t == "export_statement":
        d = node.child_by_field_name("declaration")
        if d is not None:
            return _node_name(d, src)
        return None
    if t in ("lexical_declaration", "variable_declaration"):
        for c in node.children:
            if c.type == "variable_declarator":
                n = c.child_by_field_name("name")
                return src[n.start_byte:n.end_byte].decode("utf8", "replace") if n is not None else None
        return None
    if t == "impl_item":
        n = node.child_by_field_name("type")
        return ("impl " + src[n.start_byte:n.end_byte].decode("utf8", "replace")) if n is not None else "impl"
    if t == "type_declaration":
        for c in node.children:
            if c.type == "type_spec":
                n = c.child_by_field_name("name")
                return src[n.start_byte:n.end_byte].decode("utf8", "replace") if n is not None else None
    n = node.child_by_field_name("name")
    if n is not None:
        return src[n.start_byte:n.end_byte].decode("utf8", "replace")
    return None


def _is_def(node, lang: str, src: bytes) -> bool:
    if node.type not in DEFS[lang]:
        return False
    if node.type in ("lexical_declaration", "variable_declaration"):
        txt = src[node.start_byte:node.end_byte]
        return b"=>" in txt or b"function" in txt
    if node.type == "export_statement":
        return node.child_by_field_name("declaration") is not None
    return True


def _top_nodes(node, lang: str):
    trans = TRANSPARENT.get(lang, set())
    for c in node.children:
        if c.type in trans:
            yield from _top_nodes(c, lang)
        else:
            yield c


def _collect_defs(node, lang: str, src: bytes):
    """Definitions below `node` (not descending into found definitions)."""
    for c in node.children:
        if _is_def(c, lang, src):
            yield c
        else:
            yield from _collect_defs(c, lang, src)


# nodes that belong to the declaration that follows them: comments / doc comments, attributes, decorators
TRIVIA = {"comment", "line_comment", "block_comment", "doc_comment", "documentation_comment",
          "attribute_item", "inner_attribute_item", "attribute_list", "decorator"}


def _lead_start(node) -> int:
    """First line (1-based) of `node` including the comments / doc comments / attributes / decorators directly above
    it (no blank line in between, and not trailing code on the previous line)."""
    start = node.start_point[0]
    p = node.prev_sibling
    cur_row = start
    while p is not None and p.type in TRIVIA and cur_row - p.end_point[0] <= 1:
        pp = p.prev_sibling
        if pp is not None and pp.end_point[0] == p.start_point[0]:  # `code; // note` belongs to the line above
            break
        start = cur_row = p.start_point[0]
        p = pp
    return start + 1


def _lines_text(lines: list[str], s: int, e: int) -> str:
    return "\n".join(lines[s - 1:e])


def _windows(lines: list[str], s: int, e: int, symbol: str | None) -> list[Chunk]:
    """Overlapping line windows (WINDOW lines / OVERLAP), shrunk to fit MAX_CHUNK_CHARS; a single line longer
    than the cap is split into pieces. Nothing is silently truncated."""
    out: list[Chunk] = []
    i = s
    while i <= e:
        j, size = i, len(lines[i - 1]) + 1
        while j < e and j - i + 1 < WINDOW and size + len(lines[j]) + 1 <= MAX_CHUNK_CHARS:
            size += len(lines[j]) + 1
            j += 1
        txt = _lines_text(lines, i, j)
        if len(txt) > MAX_CHUNK_CHARS:  # one huge line
            for k in range(0, len(txt), MAX_CHUNK_CHARS):
                piece = txt[k:k + MAX_CHUNK_CHARS]
                if piece.strip():
                    out.append(Chunk(i, i, symbol, piece))
        elif txt.strip():
            out.append(Chunk(i, j, symbol, txt))
        if j >= e:
            break
        i = j + 1 - OVERLAP if j - i + 1 > OVERLAP else j + 1
    return out


def _emit_range(lines, s, e, symbol) -> list[Chunk]:
    """A range that may be too big -> windows."""
    txt = _lines_text(lines, s, e)
    if not txt.strip():
        return []
    if e - s + 1 <= MAX_CHUNK_LINES and len(txt) <= MAX_CHUNK_CHARS:
        return [Chunk(s, e, symbol, txt)]
    return _windows(lines, s, e, symbol)


def _small(c: Chunk) -> bool:
    return c.end_line - c.start_line + 1 < MIN_CHUNK_LINES and len(c.text) < MIN_CHUNK_CHARS


def _fits(c: Chunk) -> bool:
    return c.end_line - c.start_line + 1 <= MAX_CHUNK_LINES and len(c.text) <= MAX_CHUNK_CHARS


def _combine(group: list[Chunk], lines: list[str], parent_sym: str | None) -> Chunk:
    """One chunk covering the whole line range of `group` (chunks are contiguous, windows may overlap)."""
    if len(group) == 1:
        return group[0]
    s, e = min(c.start_line for c in group), max(c.end_line for c in group)
    syms: list[str] = []
    for c in group:
        if c.symbol and c.symbol not in syms:
            syms.append(c.symbol)
    if len(syms) <= 1:
        sym = syms[0] if syms else parent_sym
    else:
        big = [c for c in group if not _small(c)]
        if len(big) == 1 and big[0].symbol:
            sym = big[0].symbol          # small bits absorbed into one real chunk: it keeps its name
        elif parent_sym:
            sym = parent_sym             # several small members of a class: the class
        else:
            sym = (", ".join(syms[:3]) + (", ..." if len(syms) > 3 else ""))[:80]
    return Chunk(s, e, sym, _lines_text(lines, s, e))


def _merge_small(chunks: list[Chunk], lines: list[str], parent_sym: str | None,
                 glue_only: bool = False) -> list[Chunk]:
    """Merge chunks smaller than the minimum with their neighbours (next first, else previous) while the result
    stays within the maximum. Inside a class (`parent_sym`) every small chunk takes part; at the top level
    (`glue_only`) only symbol-less glue (imports, namespace lines, closing braces) does - small named functions
    keep their own chunk."""
    def eligible(group: list[Chunk]) -> bool:
        return _small(_combine(group, lines, parent_sym)) and (not glue_only or all(c.symbol is None for c in group))

    res: list[Chunk] = []
    i, n = 0, len(chunks)
    while i < n:
        group = [chunks[i]]
        i += 1
        cur = group[0]
        while i < n and eligible(group):
            cand = _combine([*group, chunks[i]], lines, parent_sym)
            if not _fits(cand):
                break
            group.append(chunks[i])
            cur = cand
            i += 1
        if res and eligible([cur]) and (not glue_only or cur.symbol is None):
            cand = _combine([res[-1], cur], lines, parent_sym)
            if _fits(cand):
                res[-1] = cand
                continue
        res.append(cur)
    return res


def _def_chunks(node, lang, src, lines, parent_sym: str | None) -> list[Chunk]:
    s, e = _lead_start(node), node.end_point[0] + 1
    name = _node_name(node, src)
    sym = f"{parent_sym}.{name}" if parent_sym and name else (name or parent_sym)
    txt = _lines_text(lines, s, e)
    if e - s + 1 <= MAX_CHUNK_LINES and len(txt) <= MAX_CHUNK_CHARS:
        return [Chunk(s, e, sym, txt)]
    # too large: split along child definitions (methods of a class), gaps become header/glue chunks
    inner = node  # `export class X {...}` / `@decorator class X`: split the wrapped declaration, not the wrapper itself
    if node.type == "export_statement":
        inner = node.child_by_field_name("declaration") or node
    elif node.type == "decorated_definition":
        inner = node.child_by_field_name("definition") or node
    kids = list(_collect_defs(inner, lang, src))
    if not kids:
        return _windows(lines, s, e, sym)
    out: list[Chunk] = []
    cur = s
    for k in kids:
        ks, ke = _lead_start(k), k.end_point[0] + 1
        if ks > cur:
            out += _emit_range(lines, cur, ks - 1, sym)
        out += _def_chunks(k, lang, src, lines, sym)
        cur = ke + 1
    if cur <= e:
        out += _emit_range(lines, cur, e, sym)
    return _merge_small(out, lines, sym)


def chunk_code(text: str, lang: str) -> list[Chunk] | None:
    """Tree-sitter chunking; None if the language/grammar is unavailable."""
    if lang not in DEFS:
        return None
    parser = _parser(lang)
    if parser is None:
        return None
    src = text.encode("utf8")
    try:
        tree = parser.parse(src)
    except Exception:
        return None
    lines = text.split("\n")
    n = len(lines)
    out: list[Chunk] = []
    glue_start: int | None = None
    glue_end = 0

    def flush_glue():
        nonlocal glue_start
        if glue_start is not None:
            out.extend(_emit_range(lines, glue_start, min(glue_end, n), None))
        glue_start = None

    for node in _top_nodes(tree.root_node, lang):
        s, e = node.start_point[0] + 1, node.end_point[0] + 1
        if _is_def(node, lang, src):
            if glue_start is not None:
                glue_end = min(glue_end, _lead_start(node) - 1)  # comments above the definition travel with it
                if glue_end < glue_start:
                    glue_start = None
            flush_glue()
            out += _def_chunks(node, lang, src, lines, None)
        else:
            if glue_start is None:
                glue_start = s
            glue_end = e
            if glue_end - glue_start + 1 > MAX_CHUNK_LINES:
                flush_glue()
    flush_glue()
    return _merge_small(out, lines, None, glue_only=True)


_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


def chunk_markdown(text: str) -> list[Chunk]:
    lines = text.split("\n")
    sections: list[tuple[int, int, str | None]] = []  # start, end, heading path
    stack: list[tuple[int, str]] = []
    start = 1
    cur_sym: str | None = None
    in_fence = False
    for i, ln in enumerate(lines, 1):
        if ln.lstrip().startswith("```") or ln.lstrip().startswith("~~~"):
            in_fence = not in_fence
        m = None if in_fence else _HEADING.match(ln)
        if m:
            if i > start:
                sections.append((start, i - 1, cur_sym))
            level, title = len(m.group(1)), m.group(2)
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            cur_sym = " > ".join(t for _, t in stack)
            start = i
    sections.append((start, len(lines), cur_sym))
    out: list[Chunk] = []
    pend: int | None = None  # merge tiny sections into the next one
    for s, e, sym in sections:
        body = _lines_text(lines, s, e)
        if not body.strip():
            continue
        if pend is not None:
            s = pend  # tiny preceding sections ride along; the symbol is the latest heading
            body = _lines_text(lines, s, e)
            pend = None
        if len(body.strip()) < 80 and e < len(lines):
            pend = s
            continue
        out += _emit_range(lines, s, e, sym)
    if pend is not None:
        out += _emit_range(lines, pend, len(lines), cur_sym)
    return out


def chunk_file(path: str, text: str) -> tuple[str, list[Chunk]]:
    """Returns (lang, chunks)."""
    lang = detect_lang(path)
    chunks: list[Chunk] | None = None
    if lang == "markdown":
        chunks = chunk_markdown(text)
    elif lang in DEFS:
        chunks = chunk_code(text, lang)
    if chunks is None:
        chunks = _windows(text.split("\n"), 1, len(text.split("\n")), None)
    return lang, chunks[:MAX_CHUNKS_PER_FILE]


def embed_text(path: str, chunk: Chunk) -> str:
    """Text actually embedded: path + symbol prefix (not stored)."""
    head = f"{path}" + (f" :: {chunk.symbol}" if chunk.symbol else "")
    return f"{head}\n{chunk.text}"[:EMBED_MAX_CHARS]
