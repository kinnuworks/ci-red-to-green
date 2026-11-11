import pytest

from crg import guard


@pytest.mark.parametrize("path", [
    "tests/test_dates.py", "test_dates.py", "pkg/dates_test.py", "conftest.py", "src/conftest.py",
    "pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini", ".github/workflows/ci.yml",
    "tests/fixtures/data.json", "pkg/tests/helpers.py", "src/__snapshots__/x.snap",
])
def test_protected(path):
    assert guard.is_protected(path)


@pytest.mark.parametrize("path", ["src/dates.py", "pkg/money.py", "README.md", "dates/parse.py", "testament.py"])
def test_source(path):
    assert not guard.is_protected(path)


def test_changes_and_split():
    before = {"src/a.py": "1", "tests/test_a.py": "2", "gone.py": "3"}
    after = {"src/a.py": "9", "tests/test_a.py": "2", "src/new.py": "4"}
    ch = guard.changes(before, after)
    assert ch == {"src/a.py": "modified", "src/new.py": "added", "gone.py": "deleted"}
    src, prot = guard.split({**ch, "tests/test_a.py": "modified"})
    assert set(src) == {"src/a.py", "src/new.py", "gone.py"}
    assert prot == {"tests/test_a.py": "modified"}


def test_django_style_test_modules_are_protected():
    from crg import guard as g
    assert g.is_protected("app/tests.py") and g.is_protected("pkg/test.py") and not g.is_protected("pkg/testing_utils.py")
