import argparse
from datetime import datetime, timezone
import difflib
import os
import shutil
import tarfile
import tempfile
import time
import subprocess

import jinja2
import requests
import tqdm

from . import Metadata

DEFAULT_EDITOR = "vi"
XDG_CACHE_HOME = os.getenv("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
CACHEDIR = os.path.join(XDG_CACHE_HOME, "rust2rpm")
API_URL = "https://crates.io/api/v1/"
TEMPLATE = """# Generated by rust2rpm
%bcond_without check
{% if not include_debug %}
%global debug_package %{nil}
{% endif %}

%global crate {{ md.name }}

Name:           {{ name }}
Version:        {{ md.version }}
Release:        1%{?dist}
{% if md.description is none %}
Summary:        # FIXME
{% else %}
{% set description_lines = md.description.split("\n") %}
Summary:        {{ description_lines|join(" ")|trim }}
{% endif %}

License:        {{ md.license|default("# FIXME") }}
URL:            https://crates.io/crates/{{ md.name }}
Source0:        https://crates.io/api/v1/crates/%{crate}/%{version}/download#/%{crate}-%{version}.crate
{% if patch_file is not none %}
# Initial patched metadata
Patch0:         {{ patch_file }}
{% endif %}

ExclusiveArch:  %{rust_arches}

BuildRequires:  rust
BuildRequires:  cargo
{% if include_build_requires %}
{% if md.requires|length > 0 %}
# [dependencies]
{% for req in md.requires|sort(attribute="name") %}
BuildRequires:  {{ req }}
{% endfor %}
{% endif %}
{% if md.build_requires|length > 0 %}
# [build-dependencies]
{% for req in md.build_requires|sort(attribute="name") %}
BuildRequires:  {{ req }}
{% endfor %}
{% endif %}
{% if md.test_requires|length > 0 %}
%if %{with check}
# [dev-dependencies]
{% for req in md.test_requires|sort(attribute="name") %}
BuildRequires:  {{ req }}
{% endfor %}
%endif
{% endif %}
{% endif %}

%description
%{summary}.

{% if name_devel is not none %}
%package     {{ name_devel }}
Summary:        %{summary}
BuildArch:      noarch
{% if include_provides %}
{% for prv in md.provides %}
Provides:       {{ prv }}
{% endfor %}
{% endif %}
{% if include_requires %}
{% if md.requires|length > 0 %}
# [dependencies]
{% for req in md.requires|sort(attribute="name") %}
Requires:       {{ req }}
{% endfor %}
{% endif %}
{% if md.build_requires|length > 0 %}
# [build-dependencies]
{% for req in md.build_requires|sort(attribute="name") %}
Requires:       {{ req }}
{% endfor %}
{% endif %}
{% endif %}

%description {{ name_devel }}
{% if md.description is none %}
%{summary}.
{% else %}
{{ md.description|wordwrap|trim }}
{% endif %}

This package contains library source intended for building other packages
which use %{crate} from crates.io.

{% endif %}
%prep
%autosetup -n %{crate}-%{version} -p1
%cargo_prep

%build
%cargo_build

%install
%cargo_install

%if %{with check}
%check
%cargo_test
%endif

{% if include_main %}
%files
{% if md.license_file is not none %}
%license {{ md.license_file }}
{% endif %}
{% for bin in bins %}
%{_bindir}/{{ bin.name }}
{% endfor %}

{% endif %}
{% if name_devel is not none %}
%files       {{ name_devel }}
{% if md.license_file is not none %}
%license {{ md.license_file }}
{% endif %}
%{cargo_registry}/%{crate}-%{version}/

{% endif %}
%changelog
* {{ date }} {{ packager|default("rust2rpm <nobody@fedoraproject.org>") }} - {{ md.version }}-1
- Initial package
"""
JINJA_ENV = jinja2.Environment(undefined=jinja2.StrictUndefined,
                               trim_blocks=True, lstrip_blocks=True)

def detect_editor():
    terminal = os.getenv("TERM")
    terminal_is_dumb = terminal is None or terminal == "dumb"
    editor = None
    if not terminal_is_dumb:
        editor = os.getenv("VISUAL")
    if editor is None:
        editor = os.getenv("EDITOR")
    if editor is None:
        if terminal_is_dumb:
            raise Exception("Terminal is dumb, but EDITOR unset")
        else:
            editor = DEFAULT_EDITOR
    return editor

def detect_packager():
    rpmdev_packager = shutil.which("rpmdev-packager")
    if rpmdev_packager is not None:
        return subprocess.check_output(rpmdev_packager, universal_newlines=True).strip()

    git = shutil.which("git")
    if git is not None:
        name = subprocess.check_output([git, "config", "user.name"], universal_newlines=True).strip()
        email = subprocess.check_output([git, "config", "user.email"], universal_newlines=True).strip()
        return "{} <{}>".format(name, email)

    return None

def file_mtime(path):
    t = datetime.fromtimestamp(os.stat(path).st_mtime, timezone.utc)
    return t.astimezone().isoformat()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-", "--stdout", action="store_true",
                        help="Print spec and patches into stdout")
    parser.add_argument("-t", "--target", action="store",
                        choices=("plain", "fedora"), default="fedora",
                        help="Distribution target")
    parser.add_argument("-p", "--patch", action="store_true",
                        help="Do initial patching of Cargo.toml")
    parser.add_argument("crate", help="crates.io name")
    parser.add_argument("version", nargs="?", help="crates.io version")
    args = parser.parse_args()

    if args.patch:
        editor = detect_editor()

    if args.version is None:
        # Now we need to get latest version
        url = requests.compat.urljoin(API_URL, "crates/{}/versions".format(args.crate))
        req = requests.get(url)
        req.raise_for_status()
        args.version = req.json()["versions"][0]["num"]

    if not os.path.isdir(CACHEDIR):
        os.mkdir(CACHEDIR)
    cratef_base = "{}-{}.crate".format(args.crate, args.version)
    cratef = os.path.join(CACHEDIR, cratef_base)
    if not os.path.isfile(cratef):
        url = requests.compat.urljoin(API_URL, "crates/{}/{}/download#".format(args.crate, args.version))
        req = requests.get(url, stream=True)
        req.raise_for_status()
        total = int(req.headers["Content-Length"])
        with open(cratef, "wb") as f:
            for chunk in tqdm.tqdm(req.iter_content(), "Downloading {}".format(cratef_base),
                                   total=total, unit="B", unit_scale=True):
                f.write(chunk)

    with tempfile.TemporaryDirectory() as tmpdir:
        target_dir = "{}/".format(tmpdir)
        with tarfile.open(cratef, "r") as archive:
            for n in archive.getnames():
                if not os.path.abspath(os.path.join(target_dir, n)).startswith(target_dir):
                    raise Exception("Unsafe filenames!")
            archive.extractall(target_dir)
        toml_relpath = "{}-{}/Cargo.toml".format(args.crate, args.version)
        toml = "{}/{}".format(tmpdir, toml_relpath)
        assert os.path.isfile(toml)

        if args.patch:
            mtime_before = file_mtime(toml)
            with open(toml, "r") as fobj:
                toml_before = fobj.readlines()
            subprocess.check_call([editor, toml])
            mtime_after = file_mtime(toml)
            with open(toml, "r") as fobj:
                toml_after = fobj.readlines()
            diff = list(difflib.unified_diff(toml_before, toml_after,
                                             fromfile=toml_relpath, tofile=toml_relpath,
                                             fromfiledate=mtime_before, tofiledate=mtime_after))

        metadata = Metadata.from_file(toml)

    template = JINJA_ENV.from_string(TEMPLATE)

    if args.patch and len(diff) > 0:
        patch_file = "{}-{}-fix-metadata.diff".format(args.crate, args.version)
    else:
        patch_file = None

    kwargs = {}
    bins = [tgt for tgt in metadata.targets if tgt.kind == "bin"]
    libs = [tgt for tgt in metadata.targets if tgt.kind in ("lib", "proc-macro")]
    is_bin = len(bins) > 0
    is_lib = len(libs) > 0
    if is_bin:
        spec_basename = args.crate
        kwargs["include_debug"] = True
        kwargs["name"] = "%{crate}"
        kwargs["include_main"] = True
        kwargs["bins"] = bins
        if not is_lib:
            kwargs["name_devel"] = None
        else:
            kwargs["name_devel"] = "-n rust-%{crate}-devel"
    elif is_lib:
        spec_basename = "rust-{}".format(args.crate)
        kwargs["include_debug"] = False
        kwargs["name"] = "rust-%{crate}"
        kwargs["include_main"] = False
        kwargs["name_devel"] = "   devel"
    else:
        raise ValueError("No bins and no libs")

    if args.target == "fedora":
        kwargs["include_build_requires"] = True
        kwargs["include_provides"] = False
        kwargs["include_requires"] = False
    elif args.target == "plain":
        kwargs["include_build_requires"] = True
        kwargs["include_provides"] = True
        kwargs["include_requires"] = True
    else:
        assert False, "Unknown target {!r}".format(args.target)

    kwargs["date"] = time.strftime("%a %b %d %Y")
    kwargs["packager"] = detect_packager()

    spec_file = "{}.spec".format(spec_basename)
    spec_contents = template.render(md=metadata, patch_file=patch_file, **kwargs)
    if args.stdout:
        print("# {}".format(spec_file))
        print(spec_contents)
        if patch_file is not None:
            print("# {}".format(patch_file))
            print("".join(diff), end="")
    else:
        with open(spec_file, "w") as fobj:
            fobj.write(spec_contents)
            fobj.write("\n")
        if patch_file is not None:
            with open(patch_file, "w") as fobj:
                fobj.writelines(diff)

if __name__ == "__main__":
    main()
