import pathlib, re, tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())


def test_runtime_dependencies_are_exact_pins_matching_requirements_txt():
    deps = PYPROJECT["project"]["dependencies"]
    assert deps and all(re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][^=<>~!,]*", d) for d in deps), deps
    reqs = [l.strip() for l in (ROOT / "requirements.txt").read_text().splitlines() if l.strip()]
    assert sorted(deps) == sorted(reqs)


def test_build_backend_is_pinned():
    requires = PYPROJECT["build-system"]["requires"]
    assert requires and all("==" in r for r in requires), requires
