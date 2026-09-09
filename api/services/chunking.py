import dataclasses

from tree_sitter_languages import get_parser

EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".go": "go",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".php": "php",
}

# Node types treated as one indivisible chunk. Nested matches inside an
# already-matched node (e.g. a method inside a class) are not chunked
# separately — the whole class becomes one chunk.
CHUNK_NODE_TYPES: dict[str, set[str]] = {
    "python": {"function_definition", "class_definition"},
    "go": {"function_declaration", "method_declaration", "type_declaration"},
    "javascript": {"function_declaration", "class_declaration", "method_definition"},
    "typescript": {"function_declaration", "class_declaration", "method_definition", "interface_declaration"},
    "tsx": {"function_declaration", "class_declaration", "method_definition", "interface_declaration"},
    "php": {"function_definition", "class_declaration", "method_declaration", "interface_declaration"},
}

FALLBACK_CHUNK_LINES = 100


@dataclasses.dataclass
class Chunk:
    file_path: str
    start_line: int  # 1-indexed, inclusive
    end_line: int  # 1-indexed, inclusive
    function_name: str | None
    language: str
    content: str


def detect_language(file_path: str) -> str | None:
    for ext, language in EXTENSION_TO_LANGUAGE.items():
        if file_path.endswith(ext):
            return language
    return None


def chunk_file(file_path: str, source: str) -> list[Chunk]:
    language = detect_language(file_path)
    if language is None or language not in CHUNK_NODE_TYPES:
        return _chunk_by_lines(file_path, source, language or "text")

    try:
        parser = get_parser(language)
        tree = parser.parse(source.encode("utf-8"))
    except Exception:
        return _chunk_by_lines(file_path, source, language)

    return _chunk_by_ast(file_path, source, language, tree, CHUNK_NODE_TYPES[language])


def _chunk_by_ast(file_path: str, source: str, language: str, tree, boundary_types: set[str]) -> list[Chunk]:
    lines = source.splitlines()
    matches: list[tuple[int, int, str | None]] = []  # (start_line, end_line, name), 1-indexed inclusive

    def walk(node) -> None:
        if node.type in boundary_types:
            matches.append((node.start_point[0] + 1, node.end_point[0] + 1, _extract_name(node, source)))
            return  # don't descend — the whole matched node is one chunk
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    matches.sort(key=lambda m: m[0])

    chunks: list[Chunk] = []
    covered_until = 0  # last line (1-indexed) already emitted as part of a chunk
    for start, end, name in matches:
        if start > covered_until + 1:
            _append_gap_chunk(chunks, file_path, language, lines, covered_until + 1, start - 1)
        chunks.append(
            Chunk(
                file_path=file_path,
                start_line=start,
                end_line=end,
                function_name=name,
                language=language,
                content="\n".join(lines[start - 1 : end]),
            )
        )
        covered_until = max(covered_until, end)

    if covered_until < len(lines):
        _append_gap_chunk(chunks, file_path, language, lines, covered_until + 1, len(lines))

    return chunks


def _append_gap_chunk(
    chunks: list[Chunk], file_path: str, language: str, lines: list[str], start: int, end: int
) -> None:
    gap_lines = lines[start - 1 : end]
    if not any(line.strip() for line in gap_lines):
        return  # whitespace-only gap, nothing worth embedding
    chunks.append(
        Chunk(
            file_path=file_path,
            start_line=start,
            end_line=end,
            function_name=None,
            language=language,
            content="\n".join(gap_lines),
        )
    )


def _extract_name(node, source: str) -> str | None:
    name_node = node.child_by_field_name("name")
    if name_node is None and node.type == "type_declaration":
        # Go: `type Foo struct {...}` — the name lives on the nested type_spec,
        # not on type_declaration itself.
        type_spec = next((c for c in node.children if c.type == "type_spec"), None)
        name_node = type_spec.child_by_field_name("name") if type_spec is not None else None
    if name_node is None:
        return None
    return source.encode("utf-8")[name_node.start_byte : name_node.end_byte].decode("utf-8")


def _chunk_by_lines(file_path: str, source: str, language: str) -> list[Chunk]:
    lines = source.splitlines()
    chunks: list[Chunk] = []
    for start in range(0, len(lines), FALLBACK_CHUNK_LINES):
        end = min(start + FALLBACK_CHUNK_LINES, len(lines))
        chunk_lines = lines[start:end]
        if not any(line.strip() for line in chunk_lines):
            continue
        chunks.append(
            Chunk(
                file_path=file_path,
                start_line=start + 1,
                end_line=end,
                function_name=None,
                language=language,
                content="\n".join(chunk_lines),
            )
        )
    return chunks
