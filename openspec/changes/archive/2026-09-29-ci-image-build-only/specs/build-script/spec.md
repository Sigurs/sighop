## MODIFIED Requirements

### Requirement: Continuous integration builds every change and publishes the main branch
The repository SHALL carry a CI workflow that builds the image — the build script's image gate
and no other gate — on every pull request and every push to the main branch. It SHALL NOT run
the lock, formatting, lint, type-check, test, smoke, replay or scan gates, and SHALL NOT start a
database for the job; those gates are run by the build script on the developer's host. On a push
to the main branch or a manual run, and only then, it SHALL push the built image to the container
registry tagged `<commit>-<YYYYMMDD>-<HHMMSS>` (UTC), post the image reference and digest to a
Discord notification, and delete all but the newest three versions of the image package. Each
push SHALL produce exactly one package version, so that three versions are three tags. Every
third-party action SHALL be pinned to a commit.

#### Scenario: A pull request
- **WHEN** a pull request is opened or updated
- **THEN** only the image is built, and nothing is pushed, announced or deleted

#### Scenario: A push to the main branch
- **WHEN** a commit is pushed to the main branch and the image builds
- **THEN** the image is pushed as `<commit>-<YYYYMMDD>-<HHMMSS>`, a Discord message names the tag and digest, and only the newest three versions remain in the registry

#### Scenario: No other gate runs in CI
- **WHEN** the workflow runs for any trigger
- **THEN** no lint, type-check, test, smoke, replay or scan gate runs and no database service is started, even when the tree would fail one of those gates

#### Scenario: A gate fails in CI
- **WHEN** the image gate fails
- **THEN** nothing is pushed, announced or deleted
