<p align="center">
  <img src="resources/branding/atom-search.png" width="88" alt="ssearch icon">
</p>

<h1 align="center">ssearch</h1>
<p align="center"><strong>Simple Search</strong><br>Find the file you remember, even when you forget its name.</p>

<p align="center">
  <a href="LICENSE.txt"><img src="https://img.shields.io/badge/license-MIT-89b4fa" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/platform-Windows-74c7ec" alt="Platform: Windows">
  <a href="https://github.com/empty-complete/ssearch/actions/workflows/windows-build.yml"><img src="https://github.com/empty-complete/ssearch/actions/workflows/windows-build.yml/badge.svg" alt="Windows build status"></a>
</p>

<p align="center">
  <a href="https://github.com/empty-complete/ssearch/releases">Releases</a> ·
  <a href="https://github.com/empty-complete/ssearch/issues">Report a problem</a> ·
  <a href="CONTRIBUTING.md">Contribute</a>
</p>

ssearch is an independent document search app for large personal and shared disks on Windows. Its index and search model run on your computer; searching does not require a cloud service. Network folders are optional sources, and accessing them still requires their network connection and your permissions.

## When it helps

- **Large disks and archives.** Search an index of selected personal or corporate folders instead of navigating a deep folder tree every time.
- **Many files with the same name.** Distinguish documents such as `Specification.pdf` or `Contract.docx` by their project and full path.
- **You remember the project or purpose.** Combine that context with a document type; local semantic search uses names, paths and metadata when the model is available.
- **Projects have several names.** Confirmed, configured aliases connect a full project name with an abbreviation used in folder names.
- **Files live in several places.** Search selected local folders and optional UNC/DFS shares, then reveal the result in Windows Explorer. The index and search stay local, with no mandatory cloud account.

For example, a project called **Северный завод** may store a file at `СЗ/Вентиляция/Спецификация.pdf`. With **СЗ** configured as its project alias, you can search for `Северный завод спецификация вентиляции` and use the result's path to identify the right document.

Current search uses file metadata and project context. Reading the contents of PDF/Word documents and searching AtomDB notes are planned capabilities.

## Try it

**The first public development installer is being prepared.** There is no Stable release yet. Windows 10/11 are the target platforms; final native acceptance is still pending. The `build-inputs-v1` and `build-inputs-v2` releases contain build dependencies, not application installers.

Once an application prerelease is available:

1. Open [Releases](https://github.com/empty-complete/ssearch/releases) and read that build's notes.
2. Download `ssearch-Setup-<version>.exe` from its assets and install it.
3. Choose the folders to index, let the initial scan finish, then search and open a result in Explorer.

Development installers come from reviewed source and GitHub Actions. Updates are currently manual; the planned automatic updater is not available yet.

## Where it is heading

The current implementation uses Python, PostgreSQL/pgvector and a Qt desktop interface. An Electron interface with React, HeroUI and TypeScript is planned around the same separate search service.

Also planned: document-content indexing, a settings editor with custom theme palettes, and an optional AtomDB integration. These are roadmap items, not features promised by the current development build.

## Contribute

Bug reports, small fixes, search examples using synthetic files, and UI improvements are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md) for the fork → pull request → CI → independent review workflow. English and Russian are both welcome.

## CI build inputs

Windows CI pins its dependencies in [build-inputs.json](resources/ci/build-inputs.json). The PostgreSQL asset in [build-inputs-v2](https://github.com/empty-complete/ssearch/releases/tag/build-inputs-v2) is the exact archive tested in [source proof 40218661396](https://github.com/empty-complete/ssearch/actions/runs/40218661396), without repacking. Its five unsigned executables add a neutral-language `RT_MANIFEST #1` with UTF-8 process code pages and new assembly identities; none had an embedded application manifest before. Executable code sections, other PE sections and the remaining runtime files are unchanged. The original v1 release remains available unchanged.

The embedded runtime [README](resources/postgresql/README.md) and [provenance.json](resources/postgresql/provenance.json) remain historical records of the original preparation and upstream inputs. They do not describe the transformed executable bytes; the derivation, five original/new hashes, tool commit and exact proof receipt are pinned separately in `build-inputs.json`. The receipt retains its experimental source-proof fields. This input promotion does not establish installer, Full-suite or Windows 10/11 native acceptance. The manifest feature starts at Windows 10 1903; the Windows 10 1809 legacy fallback remains open, and the product's platform floor is unchanged.

## License

[MIT](LICENSE.txt) · Филипп Белобородов ([empty-complete](https://github.com/empty-complete))

[filipp.beloborodoff@yandex.ru](mailto:filipp.beloborodoff@yandex.ru)

You can use, modify and fork ssearch. Third-party components and assets keep their own licenses and notices.
