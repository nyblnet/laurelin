"""Run the tutorials.

Documentation rots quietly. Everything else in this repo is enforced — 594
tests, a benchmark gate, a lint pass — but the pages a newcomer actually reads
first were verified once, by hand, and then outlived by the code. Three times
in recent memory a published claim was true when written and false by the time
anyone read it, including a tutorial that pointed readers at
``data/<name>/v0001/data.parquet`` for months after versions became manifests
of ``parts/<uuid>.parquet``. That step exists to prove your data isn't trapped,
and it produced "No such file or directory".

So the tutorials run here. Each one is assembled into a single bash script —
in document order, so a file block lands on disk before the command that uses
it — and executed against a real workspace and a real server. The tutorials
are a *sequence*: 02 continues 01's workspace, 03 continues 02's, which is
what a reader does, so the whole set shares one workspace.

Fence annotations, all invisible in rendered Markdown (CommonMark takes only
the first word of an info string as the language):

    ```bash                  run it
    ```bash no-run           show it, don't run it (servers, pip install)
    ```csv file=orders.csv   write the block to that path first
    ```text                  expected output, never executed

The rule this encodes: if a command appears in a tutorial without ``no-run``,
it has to work.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

TUTORIALS = Path(__file__).resolve().parent.parent / "docs" / "tutorials"
WORKSPACE = "orders-workspace"
# (tutorial, directory it runs in). Tutorial 01 creates the workspace and cds
# into it; the later two continue inside it, exactly as a reader would.
ORDER = [
    ("01-ingest-transform-build.md", "."),
    ("02-ontology-and-actions.md", WORKSPACE),
    ("03-securing-data.md", WORKSPACE),
]

_FENCE = re.compile(r"^```(\S*)([^\n]*)\n(.*?)^```", re.S | re.M)


def parse_blocks(markdown: str) -> list[tuple[str, dict, str]]:
    """(language, options, body) for every fenced block, in document order."""
    blocks = []
    for lang, info, body in _FENCE.findall(markdown):
        opts: dict[str, str | bool] = {}
        for token in info.split():
            key, _, value = token.partition("=")
            opts[key] = value.strip("\"'") if value else True
        blocks.append((lang, opts, body))
    return blocks


def script_for(markdown: str) -> str:
    """Turn one tutorial into a bash script.

    File blocks become heredocs at the point they appear, so ordering in the
    document is ordering on disk — which is also the order a reader follows.
    """
    lines = ["set -euxo pipefail"]
    for lang, opts, body in parse_blocks(markdown):
        path = opts.get("file")
        if path:
            lines.append(f"mkdir -p \"$(dirname '{path}')\"")
            # A quoted heredoc delimiter: the block is data, not something to
            # expand. A tutorial full of $variables and backticks would
            # otherwise be mangled on its way to disk.
            lines.append(f"cat > '{path}' <<'LAURELIN_EOF'\n{body}LAURELIN_EOF")
        elif lang == "bash" and "no-run" not in opts:
            body = body.rstrip()
            lines.append(negate(body) if "expect-fail" in opts else body)
    return "\n".join(lines) + "\n"


def negate(body: str) -> str:
    """Require every command in the block to *fail*.

    Tutorials demonstrate that validation works by showing calls that are
    supposed to be rejected — a missing parameter, an action that doesn't
    exist. Those are among the most valuable claims in the docs and the
    easiest to break silently, so rather than skipping them, each command is
    prefixed with `!`: under `set -e` the block then passes only if every
    command fails, and starts failing the moment one of them succeeds.
    """
    out, continued = [], False
    for line in body.splitlines():
        stripped = line.strip()
        starts_command = bool(stripped) and not stripped.startswith("#") and not continued
        out.append(f"! {line}" if starts_command else line)
        continued = stripped.endswith("\\")
    return "\n".join(out)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """A real server the curl blocks talk to.

    A live process rather than TestClient: the tutorials tell people to run
    curl against HTTP, and the point is to verify what they were told.
    """
    root = tmp_path_factory.mktemp("tutorials")
    port = free_port()
    laurelin = Path(sys.executable).parent / "laurelin"
    if not laurelin.exists():  # pragma: no cover - depends on the install
        pytest.skip("the `laurelin` console script is not installed")

    # The server has to bind a workspace that exists, and tutorial 01 hasn't
    # run yet. `init` is idempotent, so the tutorial's own init still runs as
    # written rather than being skipped or special-cased.
    subprocess.run(
        [str(laurelin), "init", WORKSPACE, "--name", "Orders"],
        cwd=root, check=True, capture_output=True,
    )
    proc = subprocess.Popen(
        [str(laurelin), "serve", "--workspace", str(root / WORKSPACE),
         "--no-auth", "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(120):
            if proc.poll() is not None:
                pytest.fail(f"server exited early:\n{proc.stdout.read()}")
            try:
                if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.25)
        else:  # pragma: no cover - only on a very slow machine
            pytest.fail("server never became ready")
        yield root, url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


def harden_curl(script: str) -> str:
    """Make curl fail the script on an HTTP error.

    Without this the whole exercise is theatre: curl exits 0 on a 404 or a
    500, so every API block in the tutorials would "pass" while the server
    rejected them. `--fail-with-body` keeps the response visible so the
    failure says *why*.

    Injected here rather than written into the tutorials, because a reader
    following along wants to see the response, not have curl swallow it — the
    flags serve the test, not the docs.
    """
    return re.sub(r"\bcurl\b(?! *--fail)", "curl --fail-with-body --show-error", script)


def run_tutorial(name: str, cwd: Path, url: str) -> None:
    markdown = (TUTORIALS / name).read_text()
    # The tutorials hardcode the default port; the test server is on an
    # ephemeral one so a developer's own server doesn't collide with it.
    script = script_for(markdown).replace("localhost:8787", url.removeprefix("http://"))
    script = harden_curl(script)

    env = {**os.environ, "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}"}
    result = subprocess.run(
        ["bash", "-c", script], cwd=cwd, env=env,
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        pytest.fail(
            f"{name} failed (exit {result.returncode}).\n\n"
            f"--- output ---\n{result.stdout[-4000:]}\n{result.stderr[-4000:]}\n\n"
            "A command in the tutorial no longer does what the tutorial says. "
            "Fix the docs or the code — do not add `no-run` to hide it."
        )


@pytest.mark.parametrize("name, workdir", ORDER)
def test_tutorial_runs(name, workdir, server):
    """Each tutorial, in order, against the workspace the previous one left."""
    root, url = server
    if not shutil.which("bash"):  # pragma: no cover - non-POSIX
        pytest.skip("bash is required to run the tutorials")
    run_tutorial(name, root / workdir, url)


# -- the harness itself -------------------------------------------------------

def test_file_blocks_become_heredocs():
    script = script_for("```csv file=a/b.csv\nx,y\n1,2\n```\n")
    assert "mkdir -p \"$(dirname 'a/b.csv')\"" in script
    assert "cat > 'a/b.csv' <<'LAURELIN_EOF'\nx,y\n1,2\nLAURELIN_EOF" in script


def test_no_run_blocks_are_not_executed():
    script = script_for("```bash no-run\nlaurelin serve\n```\n```bash\necho ok\n```\n")
    assert "laurelin serve" not in script
    assert "echo ok" in script


def test_curl_is_hardened_so_an_http_error_fails_the_run():
    """Without --fail, curl exits 0 on a 500 and every API block passes while
    the server rejects it."""
    assert harden_curl("curl -X POST /x") == "curl --fail-with-body --show-error -X POST /x"
    # An expected-failure block that already says --fail is left alone.
    assert harden_curl("curl --fail /x") == "curl --fail /x"


def test_expect_fail_blocks_require_every_command_to_fail():
    script = script_for(
        "```bash expect-fail\n# a comment\ncurl -X POST /a \\\n  -d '{}'\ncurl /b\n```\n"
    )
    assert "! curl -X POST /a \\" in script
    assert "  -d '{}'" in script and "! -d" not in script, "continuations aren't commands"
    assert "! curl /b" in script
    assert "! # a comment" not in script


def test_output_blocks_are_never_executed():
    """A block showing what a command *printed* must not be run as commands."""
    assert "region  revenue" not in script_for("```text\nregion  revenue\n```\n")


def test_no_run_laurelin_commands_are_still_real_commands():
    """A `no-run` block is shown and never executed, so it is the one place a
    documented command can rot invisibly — and it includes the very first
    command a reader runs (`laurelin serve ...`). Measured: adding a
    nonexistent `--open-browser` to tutorial 01's serve line left this whole
    file green, because nothing ever parsed it.

    So parse it. Every `laurelin` invocation in a no-run block is resolved
    against the real CLI: the subcommand chain must exist and every `--flag`
    must be a declared option of the command it is passed to. This does not
    execute anything — a serve line still needs what a test can't provide —
    it only refuses to *show* readers a command the CLI would reject.
    (`text` expected-output blocks remain unchecked; they carry no commands.)
    """
    import shlex

    from typer.main import get_command

    from laurelin.cli import app as cli_app

    root = get_command(cli_app)
    checked, problems = 0, []
    for name, _ in ORDER:
        for lang, opts, body in parse_blocks((TUTORIALS / name).read_text()):
            if lang != "bash" or "no-run" not in opts:
                continue
            for line in body.splitlines():
                line = line.strip()
                if not line.startswith("laurelin"):
                    continue  # pip install etc.: not ours to validate
                checked += 1
                tokens = shlex.split(line)[1:]
                cmd = root
                # Duck-typed group check: this typer ships its own click shim
                # (typer._click), so TyperGroup is not an isinstance of the
                # installed click.Group — measured, it made this walk a no-op.
                while (
                    tokens
                    and not tokens[0].startswith("-")
                    and getattr(cmd, "commands", None) is not None
                ):
                    sub = cmd.commands.get(tokens[0])
                    if sub is None:
                        problems.append(
                            f"{name}: `laurelin ... {tokens[0]}` is not a "
                            f"command the CLI knows ({line!r})"
                        )
                        break
                    cmd, tokens = sub, tokens[1:]
                else:
                    allowed = {
                        opt
                        for param in cmd.params
                        for opt in (*param.opts, *param.secondary_opts)
                    } | {"--help"}
                    for token in tokens:
                        if token.startswith("--"):
                            flag = token.split("=", 1)[0]
                            if flag not in allowed:
                                problems.append(
                                    f"{name}: `{flag}` is not an option of "
                                    f"that command ({line!r})"
                                )
    assert checked, (
        "no `laurelin` command found in any no-run block — either the "
        "tutorials changed shape or this parser drifted; both deserve a look"
    )
    assert not problems, (
        "documented commands the CLI would reject:\n  " + "\n  ".join(problems)
    )


def test_every_tutorial_is_covered():
    """A new tutorial must be added to ORDER, or it silently goes untested —
    which is the failure this whole module exists to prevent."""
    on_disk = {p.name for p in TUTORIALS.glob("*.md")} - {"README.md"}
    covered = {name for name, _ in ORDER}
    assert on_disk == covered, f"untested tutorials: {on_disk - covered}"
