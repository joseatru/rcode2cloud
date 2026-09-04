"""
migration.sanity
----------------
`--sanity-check` mode: the pre-flight that has to pass before any write.

Validates both admin tokens, project access, every mapped user's identity, and
— the check that matters most — that each mapped PAT holds Owner (access level
50) on the destination project. Without Owner, GitLab accepts issue writes and
silently drops `iid` and `created_at`.

This is the cheap pre-flight for the same failure the read-back fidelity guard
catches at write time. Both exist because a pre-flight can be skipped or go
stale.
"""

import aiohttp

from typing import Optional

import config as cfg_module

from .logging_setup import log


class SanityCheckMixin:
    """
    Credential, project, role, and IID-range pre-flight for GitLabMigration.

    Expects the host object to provide `mappings`, `cfg`, both URLs, both
    tokens, both project IDs, and `user_cache`.
    """

    # ---------------------------------------------------------------- #
    # [8] Sanity Check Mode
    # ---------------------------------------------------------------- #

    # GitLab member access levels. Owner (50) is the minimum that may set
    # `iid` and `created_at` on issues and `created_at` on notes.
    ACCESS_LEVELS = {
        0:  "No access",
        5:  "Minimal access",
        10: "Guest",
        20: "Reporter",
        30: "Developer",
        40: "Maintainer",
        50: "Owner",
    }
    OWNER_ACCESS_LEVEL = 50

    async def _access_level(
        self,
        session: aiohttp.ClientSession,
        user_id: int
    ) -> Optional[int]:
        """
        Return a user's effective access level on the destination project.

        Queries `members/all/:user_id` first so a role inherited from the
        parent group counts, falling back to the direct-membership endpoint on
        instances that do not expose the `all` variant.

        Args:
            session: Active aiohttp session.
            user_id: Destination user ID.

        Returns:
            Integer access level, or None if the user is not a member.
        """
        base = f"{self.dest_url}/api/v4/projects/{self.dest_project}/members"

        for endpoint in (f"{base}/all/{user_id}", f"{base}/{user_id}"):
            try:
                member = await self._get(session, endpoint, self.dest_token)
                return member.get("access_level")
            except aiohttp.ClientResponseError as exc:
                if exc.status == 404:
                    continue
                raise

        return None

    async def run_sanity_check(self, session: aiohttp.ClientSession) -> bool:
        """
        Validate all credentials, project access, roles, and user mappings.

        Checks performed:
          1. Source project is accessible with SOURCE_TOKEN.
          2. Destination project is accessible with DEST_TOKEN, and the
             DEST_TOKEN identity holds Owner on it.
          3. For every mapped user: the destination username exists, the
             per-user PAT authenticates as that user, and that user holds
             Owner on the destination project.
          4. Destination issue state — reports existing issues and the
             highest IID, since IID preservation needs a clear range.

        Owner is not cosmetic: without it GitLab silently ignores `iid` and
        `created_at`, which would produce a renumbered, re-dated migration
        that only becomes visible after hours of writes.

        Args:
            session: Active aiohttp session.

        Returns:
            True if ALL checks pass, False if any check fails.
        """
        log.info("=" * 60)
        log.info("  SANITY CHECK MODE")
        log.info("=" * 60)
        all_passed = True

        # ---- 1. Source project access ---- #
        src_proj_url = (
            f"{self.source_url}/api/v4/projects/{self.source_project}"
        )
        log.info(f"[1/4] Checking source project (ID: {self.source_project})...")
        try:
            proj = await self._get(session, src_proj_url, self.source_token)
            log.info(f"  ✓  Source: {proj.get('path_with_namespace')}")
        except Exception as exc:
            log.error(f"  ✗  Source project check FAILED: {exc}")
            all_passed = False

        # ---- 2. Destination project access and admin Owner role ---- #
        dest_proj_url = (
            f"{self.dest_url}/api/v4/projects/{self.dest_project}"
        )
        log.info(f"[2/4] Checking destination project (ID: {self.dest_project})...")
        try:
            proj = await self._get(session, dest_proj_url, self.dest_token)
            log.info(f"  ✓  Destination: {proj.get('path_with_namespace')}")
        except Exception as exc:
            log.error(f"  ✗  Destination project check FAILED: {exc}")
            all_passed = False

        try:
            me    = await self._get(session, f"{self.dest_url}/api/v4/user",
                                    self.dest_token)
            level = await self._access_level(session, me["id"])
            label = self.ACCESS_LEVELS.get(level, f"level {level}")

            if level == self.OWNER_ACCESS_LEVEL:
                log.info(
                    f"  ✓  DEST_GITLAB_TOKEN authenticates as "
                    f"{me.get('username')} with Owner"
                )
            else:
                log.error(
                    f"  ✗  DEST_GITLAB_TOKEN ({me.get('username')}) holds "
                    f"{label}, not Owner. iid and created_at cannot be set."
                )
                all_passed = False
        except Exception as exc:
            log.error(f"  ✗  Could not verify DEST_GITLAB_TOKEN role: {exc}")
            all_passed = False

        # ---- 3. User mappings, per-user PATs, and Owner roles ---- #
        log.info("[3/4] Checking user mappings, PATs, and Owner roles...")
        users = self.mappings.get("users", {})

        for src_username, mapping in users.items():
            dest_username = mapping.get("dest_username", "")
            pat_env_key   = mapping.get("pat_env", "")
            user_pat      = cfg_module.read_token(pat_env_key) if pat_env_key else None

            # Check that the destination user exists
            user_lookup_url = (
                f"{self.dest_url}/api/v4/users?username={dest_username}"
            )
            dest_user_id = None
            try:
                found = await self._get(session, user_lookup_url, self.dest_token)
                if found:
                    dest_user_id = found[0]["id"]
                    # Pre-populate cache during sanity check
                    self.user_cache[dest_username] = dest_user_id
                    log.info(
                        f"  ✓  {src_username} → {dest_username} "
                        f"(ID: {dest_user_id})"
                    )
                else:
                    log.error(
                        f"  ✗  Destination user NOT found: {dest_username}"
                    )
                    all_passed = False
            except Exception as exc:
                log.error(
                    f"  ✗  User lookup failed for {dest_username}: {exc}"
                )
                all_passed = False

            # Validate the per-user PAT
            if not user_pat:
                log.error(
                    f"  ✗  {pat_env_key or 'pat_env'} is not set for "
                    f"{dest_username}. Their issues and notes would be written "
                    f"by DEST_GITLAB_TOKEN with an attribution header "
                    f"(fidelity=admin_attributed)."
                )
                all_passed = False
            else:
                try:
                    me = await self._get(session, f"{self.dest_url}/api/v4/user",
                                         user_pat)
                    authenticated_as = me.get("username", "?")
                    log.info(
                        f"  ✓  PAT for {dest_username} valid "
                        f"(authenticated as: {authenticated_as})"
                    )
                    if authenticated_as != dest_username:
                        log.error(
                            f"  ✗  {pat_env_key} authenticates as "
                            f"{authenticated_as}, not {dest_username}. "
                            f"Content would be attributed to the wrong user."
                        )
                        all_passed = False
                except Exception as exc:
                    log.error(
                        f"  ✗  PAT validation failed for {dest_username}: {exc}"
                    )
                    all_passed = False

            # Owner role probe — the binding requirement for full fidelity
            if dest_user_id is not None:
                try:
                    level = await self._access_level(session, dest_user_id)
                    label = self.ACCESS_LEVELS.get(level, f"level {level}")

                    if level == self.OWNER_ACCESS_LEVEL:
                        log.info(f"  ✓  {dest_username} holds Owner on destination")
                    elif level is None:
                        log.error(
                            f"  ✗  {dest_username} is NOT a member of the "
                            f"destination project. Owner is required to set "
                            f"iid and created_at."
                        )
                        all_passed = False
                    else:
                        log.error(
                            f"  ✗  {dest_username} holds {label}, not Owner. "
                            f"iid and created_at would be silently ignored."
                        )
                        all_passed = False
                except Exception as exc:
                    log.error(
                        f"  ✗  Role probe failed for {dest_username}: {exc}"
                    )
                    all_passed = False

        # ---- 4. Destination issue state ---- #
        log.info("[4/4] Checking destination issue state...")
        try:
            dest_issues = await self._get_all_pages(
                session,
                f"{self.dest_url}/api/v4/projects/{self.dest_project}"
                f"/issues?state=all",
                self.dest_token
            )
            if not dest_issues:
                log.info("  ✓  Destination has no issues — IID range is clear")
            else:
                max_iid = max(int(i["iid"]) for i in dest_issues)
                log.warning(
                    f"  ⚠  Destination already holds {len(dest_issues)} issue(s), "
                    f"highest IID {max_iid}. Re-running is safe for issues "
                    f"already in the checkpoint; any other collision on a "
                    f"preserved IID will fail with 409."
                )
        except Exception as exc:
            log.error(f"  ✗  Could not read destination issues: {exc}")
            all_passed = False

        # Save any user IDs populated during sanity check
        if self.user_cache:
            self.save_user_cache()

        log.info("=" * 60)
        log.info(f"  SANITY CHECK: {'PASSED ✓' if all_passed else 'FAILED ✗'}")
        log.info("=" * 60)
        return all_passed
