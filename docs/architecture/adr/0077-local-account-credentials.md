# ADR 0077. Local account credentials and multi-factor authentication

- Status: Accepted
- Date: 2026-11-20

## Decision

Local accounts can enroll an authenticator app from Account settings under
Sign-in. Enrollment requires the current password and, when replacing an
authenticator, proof of the active factor. Confirmation activates the seed and
shows one-use recovery codes. The seed is encrypted with the installation
encryption key. Only recovery-code hashes are stored.

After a user enrolls, all subsequent local sign-ins require an authenticator or
recovery code. Verification, replay prevention, and session issuance use the
same transaction. Account management operations require the browser session
and the existing CSRF check. Proof failures use the shared authentication
rate limiter. Password changes cancel pending enrollment and preserve verified
authenticators.

Administrators can set `FS_AUTH_MFA_REQUIRED=1` for their installation. The
default leaves enrollment optional. Before setting it, have all active local
users finish enrollment and store their recovery codes offline. Confirm that
an administrator can sign in with the authenticator, then apply the setting to
the running stack. Unenrolled local accounts must complete enrollment before
the setting is enabled. If an administrator misses enrollment, an operator
with access to installation configuration can return to optional mode,
complete setup, and re-enable the requirement.
For workflow calls in this mode, configure a scoped programmatic token instead
of the local password fallback for the MCP client.

Enforce a minimum of 12 characters and require an uppercase letter, a
lowercase letter, a number, and a special character whenever a new local
password is written. `FS_AUTH_PASSWORD_MIN_LENGTH` may raise this floor.
The password expires 365 days after its hash last changed. Successful sign-in
warns during the final 14 days. At expiration, sign-in requires a replacement,
and prior browser sessions stop resolving. The existing password reset path
can establish a new password when recovery email is configured.

The migration uses the account creation date for existing passwords because
prior versions did not track password age. Older accounts can therefore
require a change at their first sign-in after upgrade. A database trigger
records subsequent password hash changes across the supported write paths.
Sessions already expired by the old password deadline stay expired after rotation.
Programmatic credentials retain their own lifetime and rotation rules.
