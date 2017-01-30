import os
import shutil
import subprocess
import sys
import tempfile
import textwrap

import pytest

DUMMY_LIB = """
pub fn say_hello() {
    println!("Hello, World!");
}
"""
DEPGEN = os.path.join(os.path.dirname(__file__), "cargo-deps.py")


@pytest.fixture
def cargo_toml(request):
    def make_cargo_toml(contents):
        toml = os.path.join(tmpdir, "Cargo.toml")
        with open(toml, "w") as fobj:
            fobj.write(textwrap.dedent(contents))
        return toml

    tmpdir = tempfile.mkdtemp(prefix="cargo-deps-")
    srcdir = os.path.join(tmpdir, "src")
    os.mkdir(srcdir)
    with open(os.path.join(srcdir, "lib.rs"), "w") as fobj:
        fobj.write(DUMMY_LIB)

    def finalize():
        shutil.rmtree(tmpdir)
    request.addfinalizer(finalize)

    return make_cargo_toml


def run(*params):
    cmd = [sys.executable, DEPGEN, *params]
    out = subprocess.check_output(cmd, universal_newlines=True)
    return out.split("\n")[:-1]


@pytest.mark.parametrize("toml,expected", [
    ("""
     [package]
     name = "hello"
     version = "0.0.0"
     """,
     ["crate(hello) = 0.0.0"]),
    ("""
     [package]
     name = "hello"
     version = "1.2.3"

     [features]
     color = []
     """,
     ["crate(hello) = 1.2.3",
      "crate(hello/color) = 1.2.3"]),
])
def test_provides(toml, expected, cargo_toml):
    assert run("--provides", cargo_toml(toml)) == expected
