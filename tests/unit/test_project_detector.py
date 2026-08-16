from pathlib import Path

from rinari.projects.detector import detect_project, is_home_root


def test_detect_rinari_marker(tmp_path) -> None:
    project = tmp_path / "proj"
    (project / ".rinari").mkdir(parents=True)
    (project / ".rinari" / "project.toml").write_text("name = 'p'\n", encoding="utf-8")
    result = detect_project(project, tmp_path)
    assert result.project_root == project
    assert result.marker == ".rinari/project.toml"
    assert result.kind == "PROJECT"


def test_detect_git_marker(tmp_path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    result = detect_project(project, tmp_path)
    assert result.project_root == project
    assert result.marker == ".git"


def test_no_marker_is_chat_with_secondary_candidates(tmp_path) -> None:
    project = tmp_path / "loose"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (project / "package.json").write_text("{}", encoding="utf-8")
    result = detect_project(project, tmp_path)
    assert result.project_root is None
    assert result.kind == "CHAT"
    assert set(result.secondary_markers) == {"pyproject.toml", "package.json"}


def test_walk_upwards_finds_parent_marker(tmp_path) -> None:
    project = tmp_path / "repo"
    nested = project / "src" / "deep"
    nested.mkdir(parents=True)
    (project / ".git").mkdir()
    result = detect_project(nested, tmp_path)
    assert result.project_root == project


def test_nearest_marker_wins_in_nested_repos(tmp_path) -> None:
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    (outer / ".git").mkdir()
    (inner / ".git").mkdir()
    assert detect_project(inner, tmp_path).project_root == inner
    assert detect_project(outer, tmp_path).project_root == outer


def test_home_never_becomes_project(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".git").mkdir()
    result = detect_project(home, home)
    assert result.project_root is None
    assert result.kind == "CHAT"


def test_walk_stops_at_home_boundary(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = home / "work"
    workspace.mkdir(parents=True)
    (home / ".git").mkdir()
    # a marker at or above the home boundary must not be picked up
    result = detect_project(workspace, home)
    assert result.project_root is None


def test_marker_inside_home_under_boundary_is_found(tmp_path) -> None:
    home = tmp_path / "home"
    project = home / "code" / "app"
    project.mkdir(parents=True)
    (project / ".git").mkdir()
    result = detect_project(project, home)
    assert result.project_root == project


def test_is_home_root() -> None:
    assert is_home_root(Path("/a/home"), Path("/a/home"))
    assert not is_home_root(Path("/a/home/app"), Path("/a/home"))
