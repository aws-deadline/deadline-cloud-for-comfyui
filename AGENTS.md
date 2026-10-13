# Agent guide

This repository establishes the policies and automation for an AWS Deadline
Cloud integration with ComfyUI. Product implementation, package metadata, and
a product test suite are future work.

Read [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md) before
preparing changes. Use conventional commit messages and PR titles, and sign
off commits for DCO.

Keep changes focused and inspect the staged file list before committing.
Local experiments and generated files may be present outside the tracked
integration.

Declare GitHub Actions permissions explicitly. Third-party actions use full
commit SHA pins. Shared AWS Deadline Cloud workflows use
organization-maintained references.

Validate edited YAML and JSON. Use actionlint and the shared zizmor policy for
workflow changes.

Product CI requires a root `pyproject.toml` with `project.name` and
`build-system.build-backend`, Python source under `src/`, and tests under
`test/` or `tests/` named `test_*.py` or `*_test.py`. Define the Hatch default
environment's `lint` and `test` scripts in `hatch.toml`, which takes precedence,
or `[tool.hatch.envs.default.scripts]` in `pyproject.toml`.

Once ready, the shared workflow runs `hatch -v run lint`, `hatch -v build`,
and `hatch run test` across Linux, Windows, and macOS with Python 3.10–3.13.
CI and CodeQL report skipped until Python source exists. Once source exists,
incomplete build or test configuration fails the CI readiness check.

Before merging product source, make the `Python CI` check from GitHub Actions a required merge check after the workflow first runs.

The root pip Dependabot entry discovers future dependency manifests.
