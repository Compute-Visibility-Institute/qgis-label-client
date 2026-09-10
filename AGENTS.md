# Working agreement

For ordinary change requests, implement the change and report the result. Do not
automatically run tests, Ruff, type checks, native QGIS/browser checks, builds,
packaging, or deployment validation. Do not bump versions, tag, publish a plugin,
install a release, or dispatch workflows unless the current request asks for it.
Earlier blanket deployment approval does not authorize future automatic releases.

When the user explicitly requests tests or publication/deployment, run the checks
relevant to the change and the required release checks. Release tags still run the
release workflow's tests and packaging checks. Reuse successful checks for unchanged
code; do not duplicate them locally, through subagents, and in CI without a
concrete reason.

Use established repository commands and [the release procedure](docs/releasing.md).
Avoid new temporary validation scripts or approval paths when existing commands
cover the work. Do not install dependencies or provision test environments during
an ordinary edit. Report whether validation ran without making unrequested checks
a prerequisite for finishing the implementation.

Use subagents for bounded independent work that saves elapsed time, with clear
file ownership and this same validation constraint. Assign each validation step
once. Commits, branch pushes and pull requests do not authorize a release.
