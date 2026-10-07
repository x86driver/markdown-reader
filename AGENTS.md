# Markdown Finder project instructions

## Versioning: required for every delivered revision

The user explicitly requires a version bump on every app revision, following
Semantic Versioning (https://semver.org/). Apply this to changes to the app,
packaging, installation, and its delivered documentation. A read-only review or
answer does not need a bump. Bump once per completed user request, not once per
file or intermediate adjustment. Never reuse an already delivered version.

- The only editable version source is `src/markdown_finder/_version.py`.
  `markdown_finder.__version__`, Hatch package metadata, CLI, and GUI derive from
  it. Do not add a second hard-coded version to application code or pyproject.
- Use `MAJOR.MINOR.PATCH` with no leading zeroes. Compatible bug fixes,
  maintenance, and documentation-only revisions increase PATCH. Compatible new
  functionality increases MINOR and resets PATCH to zero.
- The project currently uses `0.x` for initial development. During this phase,
  incompatible changes also increase MINOR and must describe migration impacts.
  Do not claim compatibility guarantees that have not been established.
- Starting at `1.0.0`, incompatible changes increase MAJOR and reset MINOR/PATCH;
  compatible new features increase MINOR; compatible fixes increase PATCH.
  The compatibility surface includes documented CLI behavior, launch/install
  workflows, settings, and saved reading sessions.
- Add an entry to `CHANGELOG.md` for each delivered version, including notable
  changes and any migration steps. Do not silently change historical entries.
- After editing the version, run `uv lock`, then `uv sync --locked`. The lockfile
  and installed metadata must agree with the source. Preserve the uv cache keys
  for both `pyproject.toml` and `_version.py`, so version-only changes rebuild
  the editable package metadata.
- Verify `./run.sh --version`, package metadata, and the version shown in the
  GUI agree. Run checks appropriate to the actual change before delivery, and
  report the new version to the user.

## Git commits: required for every delivered revision

- 使用者要求：每次完成改版並通過相關驗證後，自動建立 Git commit，
  不必再次詢問是否提交。
- 提交標題與內文使用繁體中文，清楚描述此次變更，並在標題標示版本號。
- 提交內容包含此次改版的版本、CHANGELOG、實作、測試及相關文件；
  只加入本次工作的檔案，保留使用者其他尚未提交的修改。
- 提交完成後確認成功，並在交付回覆中提供版本號與 commit 雜湊。
  若 Git 提交失敗，說明實際原因，不得宣稱已提交。

## Installation and saved state

The user's desktop launcher calls this checkout's `run.sh`. Ordinary in-place
updates need an application restart, not a desktop-entry reinstall. Dependency
changes require `uv sync --locked` (the uv branch of run.sh syncs automatically).
Rerun `python3 scripts/install_desktop.py` if the checkout moves or the desktop
entry/icon changes. The `.desktop` field `Version=1.0` is the desktop entry spec
version, not this app's version; do not bump it for application releases.

Keep `QSettings` organization/application names stable to preserve existing
reading sessions. Test against temporary settings and fixture documents, not
the user's real session. Do not force-stop their running app to show an update;
let the user close and reopen it normally so the latest reading state is saved.
