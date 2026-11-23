# Releasing

The release workflow does everything except the Marketplace listing, which
GitHub only updates from the release's web page.

1. On a branch: move the `[Unreleased]` entries of `CHANGELOG.md` under
   `## [X.Y.Z] - YYYY-MM-DD`, update the link list at its foot, and for a new
   major version change the `@vN` in the README's examples. Merge to `main`
   with CI green, the `boot` job included.

   The two-guest job, `boot-two`, does not run on pull requests. Push the
   branch and run it there before merging:

   ```bash
   gh workflow run "qemu-tool-action - CI" --ref <branch> -f two-guests=true
   ```

   It runs again on `main` after the merge; wait for that run before
   tagging. Two guests oversubscribe a hosted runner, so rerun it once if
   a guest was lost (`vfio-user: timed out waiting for reply`, or a guest
   that never answered SSH). Any other failure, or a second lost guest,
   is fixed on a new branch before the tag.

2. Tag and push:

   ```bash
   git switch main && git pull --ff-only
   git tag -s vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```

3. Wait for `qemu-tool-action - Release`. It refuses a tag that is not
   `vX.Y.Z`, is not on `main` or a `release/*` branch, has no changelog
   section, or whose README has no example using its major version. Otherwise
   it runs the tests, creates the GitHub Release from the changelog section,
   and moves `vX` and `vX.Y`. A floating tag only moves forward: a patch on
   an older line leaves the newer one where it is.

   A tag on a `release/*` branch is released by the workflow on that
   branch, not the one on `main`. A release branch cut from 2.0.0 or later
   has this workflow. `release/v1` does not: its workflow only moves `v1`
   and `v1.0`, so after pushing a v1.x.y tag create the GitHub Release by
   hand with `gh release create v1.x.y --verify-tag --latest=false
   --notes-file <file>`.

4. By hand, as the repository owner (needs two-factor authentication and the
   accepted GitHub Marketplace Developer Agreement). A release the workflow
   created is not listed on the Marketplace; the listing stays on the
   previous version until this is done.

   - Open `https://github.com/sbates130272/qemu-tool-action/releases/edit/vX.Y.Z`.
   - Tick **Publish this Action to the GitHub Marketplace**. If the box
     reports a problem with `action.yml`, fix it and cut a patch release.
     The box may already appear ticked; the release still has to be saved.
   - Check the categories: Continuous integration (primary), Testing.
   - Leave **Set as the latest release** ticked, then **Update release**.

5. Check <https://github.com/marketplace/actions/qemu-tool> shows vX.Y.Z and
   the new README, and that the floating tags moved:

   ```bash
   git ls-remote --tags origin 'vX*'
   ```

Do not untick the Marketplace box on older releases: `@v1` users find
v1.0.0 in the listing's version picker.
