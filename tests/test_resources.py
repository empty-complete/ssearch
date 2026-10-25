"""Offline distribution assets must resolve without an internet connection."""
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse


RESOURCES = Path(__file__).resolve().parents[1] / "resources"


class References(HTMLParser):
    def __init__(self):
        super().__init__()
        self.references = []
        self.scripts = []
        self.image_alts = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        for attribute in ("src", "href", "poster"):
            if attributes.get(attribute):
                self.references.append(attributes[attribute])
        if tag == "script":
            self.scripts.append(attributes)
        if tag == "img":
            self.image_alts.append(attributes.get("alt", ""))


def test_help_assets_resolve_locally_without_scripts():
    page = RESOURCES / "help.html"
    parser = References()
    parser.feed(page.read_text(encoding="utf-8"))
    assert not parser.scripts
    assert parser.image_alts and all(parser.image_alts)
    for reference in parser.references:
        parsed = urlparse(reference)
        if reference.startswith("#") or parsed.scheme == "mailto":
            continue
        assert not parsed.scheme and not parsed.netloc, reference
        assert (page.parent / parsed.path).is_file(), reference


def test_public_licenses_and_installer_preserve_upgrade_identity_without_forced_eula():
    root = RESOURCES.parent
    license_text = (root / "LICENSE.txt").read_text(encoding="utf-8")
    assert license_text.startswith("MIT License\n")
    assert "Copyright (c) 2026 empty-complete (Филипп Белобородов)" in license_text
    assert "Permission is hereby granted, free of charge" in license_text
    assert (root / "packages/atom-search-core/LICENSE.txt").read_text(encoding="utf-8") == license_text
    installer = (root / "packaging/installer.iss").read_text(encoding="utf-8")
    assert "LicenseFile=" not in installer
    assert "AppId={{1EAB882B-9360-4FC5-9538-E86CB53AA2A1}" in installer
    assert 'DefaultDirName={localappdata}\\Programs\\AtomSearch' in installer
    assert 'Filename: "{app}\\ssearch.exe"' in installer
    assert "(PreviousName = 'Atom Search')" in installer
    assert 'Name: "{app}\\AtomSearch.exe"; Check: IsLegacyUpgrade' in installer
    for relative in ("eula.txt", "branding/ksg.png", "branding/astralit.png", "branding/sources.json"):
        assert not (RESOURCES / relative).exists()
        legacy_path = relative.replace("/", "\\")
        assert f'Name: "{{app}}\\_internal\\resources\\{legacy_path}"; Check: IsLegacyUpgrade' in installer
