# Releasing a version

Written immediately after cutting `v0.0.1`, which failed twice before it worked. Both
failures are recorded below, because both are structural and will recur.

## What a release actually is

Tagging produces a GitHub Release carrying **two** files:

| file | what it is for |
|---|---|
| `qgis_label_client.<version>.zip` | the plugin, installable directly |
| `plugins.xml` | turns this repository into a **QGIS plugin repository** |

The second is the one that matters for analysts. They add one URL, once:

```
https://github.com/Compute-Visibility-Institute/qgis-label-client/releases/latest/download/plugins.xml
```

and from then on the plugin appears in QGIS's plugin manager and updates itself like an
official one. `latest` is not pinned to a tag, so a new release reaches everybody without
anyone re-sending a link. That URL needs no authentication, which is only true because
this repository is public — the alternative was a VPN-only host, or HTTP Basic auth plus
credential distribution plus a QGIS master password prompt for every new analyst.

## The one thing that makes a release different from a checkout

**The OAuth client secret.** `core/oauth.py` carries `CLIENT_SECRET = ""` in git and
always will: this repository is public. The release workflow substitutes the real value
from the `OAUTH_CLIENT_SECRET` Actions secret into the ZIP only.

That is why a *source* install cannot sign in. It fails with

```
invalid_request: client_secret is missing
```

**after** the analyst has already approved the Google consent screen — the most confusing
place a flow can fail, because everything up to it looks like success. Someone working
from a clone must paste the secret into the plugin's `oauth_client_secret` setting by
hand. Someone installing a release does not.

Google documents an installed-app secret as a client *identifier* rather than a
confidential credential, and PKCE is additional rather than a substitute — the reasoning
that said PKCE made the secret unnecessary was tested against Google and was wrong.

## Before you tag

- [ ] **`metadata.txt` `version=` matches the tag without its `v`.** The workflow refuses a
      mismatch, and it is right to: the plugin manager's upgrade detection keys on
      `version=`, so a release whose metadata disagrees with its tag is one users silently
      never get.
- [ ] **`qgis_label_client/__init__.py` `__version__` matches too.** `tests/test_metadata.py`
      asserts this. It caught a mismatch on `v0.0.1` and the output was thrown away by a
      `pytest | tail` pipeline, which reported the exit status of `tail`. **Run the suite so
      its exit code survives**, or the guard is decorative.
- [ ] **The CHANGELOG has a section for this version**, not just `[Unreleased]`.
      `qgis-plugin-ci` copies the most recent entries into the packaged `metadata.txt`, so
      whatever is written there becomes the plugin's own description of itself in every
      analyst's plugin manager. A false claim there ships further than one in a code
      comment.
- [ ] **No version number is reused.** `0.1.0` existed in the CHANGELOG before anything was
      tagged and had to be renumbered to `0.0.0` to free it. Two sections claiming one
      version is exactly what breaks upgrade detection.

## Cutting it

```sh
git tag -a v0.0.2 -m "v0.0.2 — what changed"
git push origin v0.0.2
```

The push triggers `release.yml`. It can also be re-run without a new tag:

```sh
gh workflow run release.yml --ref main -f tag=v0.0.2
```

That re-run path exists because of the failures below: once a tag is pushed, fixing a
broken release means re-running against the existing tag, not burning a new version
number.

## Verify, because green is not the same as working

```sh
gh release view v0.0.2 --json assets --jq '.assets[].name'
```

Both `plugins.xml` and the ZIP must be present. A release with only one is a failure that
reported success.

Then confirm the secret actually landed — this is the check that separates an installable
plugin from one that fails after consent:

```sh
gh release download v0.0.2 --pattern '*.zip' && unzip -q qgis_label_client.0.0.2.zip
grep -c '^CLIENT_SECRET = ""$' qgis_label_client/core/oauth.py    # must print 0
```

`0` means substituted. `1` means the ZIP carries the empty placeholder and every analyst
who installs it will fail to sign in, having first been sent through a browser.

## The two failures cutting v0.0.1, both now fixed in the workflow

**1. Pushing a tag is not publishing a release.**

```
ERROR release_is_prerelease Release v0.0.1 not found. 404 Not Found
qgispluginci.exceptions.GithubReleaseNotFound
```

`qgis-plugin-ci` *attaches* assets to a release; it does not create one. Given only a tag
it looks the release up by name and 404s. Everything before it — tests, secret
substitution, the tag/metadata check — passes, so the first sign of trouble is the step
that actually publishes. The workflow now creates the release first, idempotently, so a
re-run does not fail on the release it is retrying.

**2. The release-time tree is dirty on purpose.**

```
ERROR create_archive You have uncommitted changes.
qgispluginci.exceptions.UncommitedChanges
```

Caused by the secret substitution itself. `--allow-uncommitted-changes` is therefore
correct rather than a bypass, and what stops it becoming one is the substitution step's
own guard: it refuses unless the file still reads `CLIENT_SECRET = ""`, so the flag cannot
smuggle an unrelated edit into a release. CI checks out clean and that step is the only
writer.

## Version numbering, as used here

`0.0.1` was cut deliberately small. The default API URL baked into it is a `run.app`
hostname that changes when the deployment moves to `europe-west1`, so anyone installing it
gets a version they will have to update.

**`0.1.0` is reserved for the release after that move**, when the default URL is one an
analyst can keep. The number should mark the first version worth settling on.

A semver pre-release suffix (`v0.2.0-beta1`) is treated as experimental by
`qgis-plugin-ci` automatically, which gives a canary channel at no cost: analysts who tick
"Show also experimental plugins" get it, everybody else does not.
