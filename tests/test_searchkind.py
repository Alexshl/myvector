"""Sub-kind of self-search calls (E8-16, ADR-042): labels from the tool_input text alone, never stored."""
import pytest

from myvec import presearch, search, searchkind, tokens as T


def B(cmd):
    return "Bash", {"command": cmd}


@pytest.mark.parametrize("name,inp,want", [
    # Grep tool
    ("Grep", {"pattern": "foo", "output_mode": "files_with_matches"}, ("exact", False)),
    ("Grep", {"pattern": "foo", "path": "src", "glob": "*.py", "-i": True, "-n": True}, ("exact", False)),
    ("Grep", {"pattern": "foo|bar"}, ("explore", False)),
    ("Grep", {"pattern": "def \\w+_x"}, ("explore", False)),
    ("Grep", {"pattern": "foo.*bar", "path": "src"}, ("explore", False)),
    ("Grep", {"pattern": "foo", "output_mode": "count"}, ("count", False)),
    ("Grep", {"pattern": "foo", "path": "src/a.py"}, ("file", False)),
    ("Grep", {"pattern": "foo", "path": "/tmp/x/out.txt"}, ("logs", False)),
    ("Grep", {"pattern": "foo", "path": "/p/acme/logs"}, ("logs", False)),
    ("Grep", {"pattern": "foo", "glob": "*.md"}, ("exact", True)),
    ("Grep", {"pattern": "foo", "type": "md"}, ("exact", True)),
    ("Grep", {"pattern": "foo", "path": "docs/a.md"}, ("file", True)),
    ("Grep", {"pattern": ""}, (None, False)),
    # Glob tool
    ("Glob", {"pattern": "**/*.py"}, ("name", False)),
    ("Glob", {"pattern": "docs/**/*.md"}, ("name", True)),
    ("Glob", {"pattern": "*.py", "path": "/var/log/app"}, ("logs", False)),
    # Bash
    B("grep -rn foo src/") + ((("exact", False)),),
    B("rg foo") + (("exact", False),),
    B("rg 'foo|bar'") + (("explore", False),),
    B("grep -rn 'foo\\|bar' .") + (("explore", False),),
    B("rg -e foo -e bar") + (("explore", False),),
    B("rg 'a.*b' src") + (("explore", False),),
    B("grep -n foo src/a.py") + (("file", False),),
    B("grep -n foo a.py b.py") + (("file", False),),
    B("rg foo src/a.py") + (("file", False),),
    B("rg -c foo") + (("count", False),),
    B("grep -rc foo src") + (("count", False),),
    B("grep -rn foo src | wc -l") + (("count", False),),
    B("rg foo /tmp/build.out") + (("logs", False),),
    B("grep -rn err /var/log/syslog") + (("logs", False),),
    B("grep -n err app.log") + (("logs", False),),
    B("rg err logs/") + (("logs", False),),
    B("cd x && rg foo") + (("exact", False),),
    B("timeout 10 rg 'a|b' src") + (("explore", False),),
    B("rg foo 2>/dev/null") + (("exact", False),),
    B("rg foo --include '*.md'") + (("exact", True),),
    B("rg -t md foo") + (("exact", True),),
    B("rg -g '*.md' foo docs") + (("exact", True),),
    B("grep -rn foo README.md") + (("file", True),),
    B("find . -name '*.py'") + (("name", False),),
    B("find . -name '*.md' -type f") + (("name", True),),
    B("find /tmp -name x") + (("logs", False),),
    B("find . -name x | wc -l") + (("count", False),),
    B("find . -name '*.py' | xargs grep foo") + (("name", False),),
    B("fd foo") + (("name", False),),
    B("fdfind -e md foo docs") + (("name", True),),
    B("ls -R src") + (("name", False),),
    B("ls -laR /tmp/x") + (("logs", False),),
    B("grep foo") + ((None, False),),               # stdin: nothing to judge
    B("rg --files") + ((None, False),),              # unclear arguments
    B("grep -f pats.txt src") + ((None, False),),
    B("echo 'unterminated") + ((None, False),),
    B("ls src") + ((None, False),),
    (None, None, (None, False)),
    ("Bash", {"command": 5}, (None, False)),
    ("Edit", {"pattern": "x"}, (None, False)),
])
def test_kind_of(name, inp, want):
    assert searchkind.kind_of(name, inp) == want


@pytest.mark.parametrize("inp", [None, 1, "x", [], {"command": None}, {"pattern": 3}, {"pattern": "x", "path": 3}])
@pytest.mark.parametrize("name", ["Bash", "Grep", "Glob"])
def test_kind_of_never_raises(name, inp):
    k, d = searchkind.kind_of(name, inp)
    assert k is None or k in searchkind.KINDS
    assert d in (True, False)


def test_labels_are_the_fixed_set():
    assert searchkind.KINDS == ("logs", "count", "file", "explore", "exact", "name")


def test_doc_exts_shared_with_search():
    assert search.DOC_EXTS is searchkind.DOC_EXTS
    assert search.is_doc_path("a/B.MD")


def test_presearch_private_helpers_exist():
    """searchkind reuses presearch internals (E8-11); renaming them silently breaks classification."""
    for n in ("_tokens", "_stages", "_unwrap", "_parse_args", "_alts", "_TREE_TOOLS"):
        assert hasattr(presearch, n), n


def test_read_of_doc_is_classified_read():
    assert T.classify("Read", {"file_path": "/p/docs/ADR.md"}) == "read"
    assert T.classify("Read", {"file_path": "/p/src/a.py"}) is None
    assert T.classify("Read", {"file_path": 3}) is None
    assert T.classify("Read", None) is None
