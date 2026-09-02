import textwrap

from api.services import chunking


def test_detect_language_from_extension() -> None:
    assert chunking.detect_language("main.py") == "python"
    assert chunking.detect_language("service.go") == "go"
    assert chunking.detect_language("index.tsx") == "tsx"
    assert chunking.detect_language("README.md") is None


def test_chunks_python_functions_and_classes() -> None:
    source = textwrap.dedent(
        '''\
        def foo():
            return 1


        class Greeter:
            def hello(self):
                return "hi"
        '''
    )

    chunks = chunking.chunk_file("pkg/main.py", source)

    names = [c.function_name for c in chunks if c.function_name]
    assert "foo" in names
    assert "Greeter" in names
    assert "hello" not in names  # nested method is part of the class chunk, not separate
    assert all(c.language == "python" for c in chunks)
    assert all(c.file_path == "pkg/main.py" for c in chunks)


def test_python_chunk_boundaries_do_not_overlap() -> None:
    source = textwrap.dedent(
        """\
        def a():
            return 1

        def b():
            return 2
        """
    )

    chunks = sorted(chunking.chunk_file("m.py", source), key=lambda c: c.start_line)

    for prev, nxt in zip(chunks, chunks[1:]):
        assert prev.end_line < nxt.start_line


def test_chunks_cover_entire_file_without_losing_content() -> None:
    source = textwrap.dedent(
        '''\
        """module docstring"""
        import os

        CONST = 1


        def foo():
            return CONST
        '''
    )
    lines = source.splitlines()

    chunks = chunking.chunk_file("m.py", source)
    covered_lines: set[int] = set()
    for c in chunks:
        covered_lines.update(range(c.start_line, c.end_line + 1))

    for i, line in enumerate(lines, start=1):
        if line.strip():
            assert i in covered_lines, f"line {i} ({line!r}) not covered by any chunk"


def test_chunks_go_functions_and_type_declarations() -> None:
    source = textwrap.dedent(
        """\
        package main

        func Add(a, b int) int {
        \treturn a + b
        }

        type Server struct {
        \tAddr string
        }
        """
    )

    chunks = chunking.chunk_file("main.go", source)

    names = {c.function_name for c in chunks if c.function_name}
    assert names == {"Add", "Server"}


def test_unsupported_extension_falls_back_to_line_chunking() -> None:
    source = "\n".join(f"line {i}" for i in range(5))

    chunks = chunking.chunk_file("README.md", source)

    assert len(chunks) == 1
    assert chunks[0].function_name is None
    assert chunks[0].language == "text"
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == 5


def test_fallback_splits_long_files_into_multiple_chunks() -> None:
    source = "\n".join(f"line {i}" for i in range(250))

    chunks = chunking.chunk_file("data.csv", source)

    assert len(chunks) == 3
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == chunking.FALLBACK_CHUNK_LINES
    assert chunks[-1].end_line == 250


def test_parser_failure_falls_back_to_line_chunking(monkeypatch) -> None:
    def broken_get_parser(_name: str):
        raise RuntimeError("boom")

    monkeypatch.setattr(chunking, "get_parser", broken_get_parser)

    chunks = chunking.chunk_file("m.py", "def foo():\n    return 1\n")

    assert len(chunks) == 1
    assert chunks[0].function_name is None
