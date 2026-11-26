# Releasing

A release is a tag. Pushing `vX.Y.Z` runs [`.github/workflows/release.yml`](../.github/workflows/release.yml):
it checks that the tag is the version in `pyproject.toml` and that `CHANGELOG.md` has a section for it, runs the
tests, builds the wheel and the source archive, checks the wheel (the page, the catalog, the dictionaries; it
installs and runs), publishes them on PyPI and makes the GitHub release with that CHANGELOG section as its text.

PyPI accepts the upload through [Trusted Publishing](https://docs.pypi.org/trusted-publishers/): it trusts this
repository's release workflow, so there is no PyPI token in the repository secrets or anywhere else.

## Once, before the first release

1. **PyPI**: sign in (with two-factor authentication), *Your account → Publishing → Add a new pending
   publisher*, GitHub:
   - PyPI project name: `tvpult`
   - Owner: `pavelmiskevich`, repository: `tvpult`
   - Workflow name: `release.yml`
   - Environment name: `pypi`

   "Pending" because the project doesn't exist on PyPI yet; the first upload creates it, owned by this account.
2. **GitHub**: *Settings → Environments → New environment* `pypi`. Under *Deployment protection rules* tick
   *Required reviewers* and add yourself: then every upload waits for your click in the Actions tab, even if
   someone pushes a tag. Under *Deployment branches and tags* allow only tags `v*`.

## Each release

1. On `main`, with CI green:
   - `uv version 0.1.0` — sets the version in `pyproject.toml` and `uv.lock`;
   - `CHANGELOG.md`: rename `## [Unreleased]` to `## [0.1.0] - YYYY-MM-DD` and start a new empty
     `## [Unreleased]` above it.
   Commit through a pull request like any other change.
2. Tag the merged commit and push the tag:

   ```sh
   git switch main && git pull
   git tag -a v0.1.0 -m "tvpult 0.1.0"
   git push origin v0.1.0
   ```
3. Approve the `pypi` deployment in the Actions tab. A few minutes later the version is on
   https://pypi.org/project/tvpult/ and under *Releases* on GitHub.
4. Check it as a user would, on a clean machine (or after `uv tool uninstall tvpult`):
   `uv tool install tvpult`, then `tvpult shortcut`, then double-click the shortcut.

A version with `a`, `b`, `rc` or `dev` in it (`0.1.0rc1`) becomes a GitHub *pre-release*, and `uv tool install
tvpult` skips it unless asked for (`uv tool install tvpult==0.1.0rc1`).

## When something goes wrong

- **The workflow stopped before "Publish on PyPI"** (wrong version, no CHANGELOG section, a failing test):
  nothing was published. Fix it on `main`, then move the tag: `git tag -d v0.1.0`,
  `git push origin :refs/tags/v0.1.0`, tag again and push.
- **It is on PyPI, but broken**: a version on PyPI can't be replaced, only *yanked* (on PyPI, *Manage → Releases*),
  which stops new installs from picking it. Release the fix as the next version (`0.1.1`).
