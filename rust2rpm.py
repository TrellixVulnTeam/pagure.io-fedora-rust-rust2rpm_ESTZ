import argparse
import os
import tarfile
import tempfile
import subprocess
import sys

import jinja2
import requests

import cargodeps

API_URL = "https://crates.io/api/v1/"
TEMPLATE = """# Generated by rust2rpm
%bcond_without check

%global crate {{ name }}

Name:           rust-%{crate}
Version:        {{ version }}
Release:        1%{?dist}
Summary:        # FIXME

License:        # FIXME
URL:            https://crates.io/crates/{{ name }}
Source0:        https://crates.io/api/v1/crates/%{crate}/%{version}/download#/%{crate}-%{version}.crate

ExclusiveArch:  %{rust_arches}

BuildRequires:  rust
BuildRequires:  cargo
{% for br in buildrequires %}
BuildRequires:  {{ br }}
{% endfor %}
{% for bc in buildconflicts %}
BuildConflicts: {{ bc }}
{% endfor %}
{% if testrequires|length > 0 %}
%if %{with check}
{% for tr in testrequires %}
BuildRequires:  {{ tr }}
{% endfor %}
{% for tc in testconflicts %}
BuildConflicts: {{ tc }}
{% endfor %}
%endif
{% endif %}

%description
%{summary}.

%package        devel
Summary:        %{summary}
BuildArch:      noarch
{% for prov in provides %}
Provides:       {{ prov }}
{% endfor %}
{% for req in requires %}
Requires:       {{ req }}
{% endfor %}
{% for con in conflicts %}
Conflicts:      {{ con }}
{% endfor %}

%description    devel
%{summary}.

%prep
%autosetup -n %{crate}-%{version}
%cargo_prep

%install
%cargo_install_crate %{crate}-%{version}

%if %{with check}
%check
%cargo_test
%endif

%files devel
%license # FIXME
%{cargo_registry}/%{crate}-%{version}/

%changelog
"""
JINJA_ENV = jinja2.Environment(undefined=jinja2.StrictUndefined,
                               trim_blocks=True, lstrip_blocks=True)


def run_depgen(*params):
    cmd = [sys.executable, cargodeps.__file__, *params]
    out = subprocess.check_output(cmd, universal_newlines=True)
    return out.split("\n")[:-1]

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-t", "--target", choices=("epel-7", "fedora-26"), required=True,
                        help="Distribution target")
    parser.add_argument("crate", help="crates.io name")
    parser.add_argument("version", nargs="?", help="crates.io version")
    args = parser.parse_args()

    if args.version is None:
        # Now we need to get latest version
        url = requests.compat.urljoin(API_URL, "crates/{}/versions".format(args.crate))
        req = requests.get(url)
        req.raise_for_status()
        args.version = req.json()["versions"][0]["num"]

    cratef = "{}-{}.crate".format(args.crate, args.version)
    if not os.path.isfile(cratef):
        url = requests.compat.urljoin(API_URL, "crates/{}/{}/download#".format(args.crate, args.version))
        req = requests.get(url, stream=True)
        req.raise_for_status()
        with open(cratef, "wb") as f:
            # FIXME: should we use req.iter_content() and specify custom chunk size?
            for chunk in req:
                f.write(chunk)

    files = []
    with tempfile.TemporaryDirectory() as tmpdir:
        target_dir = "{}/".format(tmpdir)
        with tarfile.open(cratef, "r") as archive:
            for n in archive.getnames():
                if not os.path.abspath(os.path.join(target_dir, n)).startswith(target_dir):
                    raise Exception("Unsafe filenames!")
            archive.extractall(target_dir)
        toml = "{}/{}-{}/Cargo.toml".format(tmpdir, args.crate, args.version)
        assert os.path.isfile(toml)

        buildrequires = run_depgen("--build-requires", toml)
        buildconflicts = run_depgen("--build-conflicts", toml)
        testrequires = run_depgen("--test-requires", toml)
        testconflicts = run_depgen("--test-conflicts", toml)
        if args.target == "fedora-26":
            # Those are automatically added by dependency generator
            provides = []
            requires = []
            conflicts = []
        else:
            provides = run_depgen("--provides", toml)
            requires = run_depgen("--requires", toml)
            conflicts = run_depgen("--conflicts", toml)

    template = JINJA_ENV.from_string(TEMPLATE)
    print(template.render(name=args.crate, version=args.version,
                          provides=provides,
                          buildrequires=buildrequires, buildconflicts=buildconflicts,
                          testrequires=testrequires, testconflicts=testconflicts,
                          requires=requires, conflicts=conflicts))
