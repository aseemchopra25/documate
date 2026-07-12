# Releasing documate

Publishing a new version to PyPI. The mechanical steps are automated by
`./release.sh X.Y.Z` (gitignored personal tooling); this file is the manual
reference and the source of truth for what that script does.

## One-shot

```bash
./release.sh 0.2.3      # bump, test, build, verify, confirm, publish, tag
```

Safety guards so it can't fire by accident: it refuses to run outside an
interactive terminal (no cron, CI, or piped input reaches any step — it exits
before it tests, commits, or builds), and before the irreversible upload it makes
you **type the exact version**, not just `y`, so `yes |` or a reflex Enter can't
publish. Everything before that prompt is safe to abort.

## Manual steps

1. **Clean and green.** Nothing uncommitted; suite and gate pass.
   ```bash
   git status --porcelain          # empty
   make test
   .venv/bin/documate --check . --base main
   ```

2. **Bump the version** in `pyproject.toml`. Patch (`0.2.2 → 0.2.3`) for fixes,
   minor (`0.2 → 0.3`) when behavior users depend on changes. A version already
   on PyPI can never be reused.

3. **Commit the bump.** The pre-commit hook regenerates docs and re-gates.
   ```bash
   git commit -am "chore(release): 0.2.3"
   ```

4. **Build fresh.** Always clear `dist/` first so no stale artifact can be pushed.
   ```bash
   rm -f dist/*
   uv build
   ```

5. **Verify** before uploading. Use the `dist/*.whl` glob, not a literal filename.
   ```bash
   ls dist/                                            # only the new wheel + sdist
   uv run --with dist/*.whl --no-project documate -h   # installs + runs clean
   ```

6. **Publish.** twine reads `~/.pypirc`; `uv publish` does not.
   ```bash
   uvx twine upload dist/*
   ```

7. **Tag** the release.
   ```bash
   git tag v0.2.3 && git push origin main --tags
   ```

## Gotchas

- **Immutable versions.** A bad upload can only be *yanked*, never replaced —
  step 5 is your last gate.
- **Clear `dist/` every time** (step 4) or twine may upload an old build.
- **Credentials** live in `~/.pypirc` (`[pypi]`, `username = __token__`,
  `password = pypi-…`). Keep the token out of shell history and out of git.
