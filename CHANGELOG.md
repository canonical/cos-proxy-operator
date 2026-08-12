# Changelog

Changes on `track/3.0` since the common ancestor with `track/2` (`1779942`).

## Features

- feat: add ubuntu@26.04 base ([#251](https://github.com/canonical/cos-proxy-operator/pull/251))
- feat: add charms blueprint ([#243](https://github.com/canonical/cos-proxy-operator/pull/243))
- feat: add charm tracing support ([#237](https://github.com/canonical/cos-proxy-operator/pull/237))
- feat: also build for Ubuntu 24.04 ([#234](https://github.com/canonical/cos-proxy-operator/pull/234))
- feat: change default track to 'dev' in release workflow ([f186dd1](https://github.com/canonical/cos-proxy-operator/commit/f186dd1c373550f3a1ad36d6586955fd26fdf1aa))

## Fixes

- fix: update docs link ([#239](https://github.com/canonical/cos-proxy-operator/pull/239))
- fix: utest ([#235](https://github.com/canonical/cos-proxy-operator/pull/235))

## Others

- chore(blueprints): refresh charms.just ([e59321f](https://github.com/canonical/cos-proxy-operator/commit/e59321f4452c4fd9049b140b5eeb80cb27686aa0))
- chore: refresh charms.just from canonical/observability ([fd7d219](https://github.com/canonical/cos-proxy-operator/commit/fd7d219634f73933690c3ce86bf102303a44b6c6))
- test: stabilize tracing integration test wait condition for opentelemetry-collector (backport #242) ([#244](https://github.com/canonical/cos-proxy-operator/pull/244))
- chore: update .wokeignore ([513b029](https://github.com/canonical/cos-proxy-operator/commit/513b02951d28e9e8886297105584f6c1e1c57a3f))
- ci: allow manually calling the release workflow ([344836f](https://github.com/canonical/cos-proxy-operator/commit/344836fe0feae8421cf1cf337e456a40d9f9bb2c))
- perf: reduce time complexity ([#240](https://github.com/canonical/cos-proxy-operator/pull/240))
- ci: add explicit workflow permissions for CodeQL and release ([#238](https://github.com/canonical/cos-proxy-operator/pull/238))
- ci: add workflow_dispatch trigger to pull request workflow ([e02036b](https://github.com/canonical/cos-proxy-operator/commit/e02036bac3440398f9ce7935e76d8f1504f26a67))
- docs: improve charmcraft.yaml description field ([#230](https://github.com/canonical/cos-proxy-operator/pull/230))
- refactor: INTEGRATING.md to include NRPE relation ([#229](https://github.com/canonical/cos-proxy-operator/pull/229))
- Remove legend from nrpe alert annotation ([#225](https://github.com/canonical/cos-proxy-operator/pull/225))

