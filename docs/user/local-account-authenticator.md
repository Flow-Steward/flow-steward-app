# Multi-factor authentication for local accounts

Starting with Flow Steward v0.9.23, you can add multi-factor authentication
to an account that signs in with a Flow Steward password. After setup,
each new sign-in requires your password and a code from your authenticator app
or one of your saved recovery codes. For accounts that sign in through single
sign-on, configure the second factor with your identity provider.

## Set up an authenticator app

1. Sign in and open **Settings → Account → Sign-in**.
2. In **Authenticator app**, select **Set up authenticator**. Enter your
   current password and select **Continue**.
3. Scan the QR code in your authenticator app. In **Google Authenticator**,
   tap **+ → Scan a QR code**. In **Microsoft Authenticator**, add an
   **Other account** and scan the code. For manual setup, add a time-based
   account and enter the setup key shown below the QR code. On a compatible
   device, **Open in an authenticator app** can hand off the same key. The
   original v0.9.23 screen offers the setup key and app link.
4. Enter the six-digit code shown in the app and select **Verify and enable**.
5. Save the ten recovery codes somewhere safe before closing the screen. They
   are shown once, and each code can be used once. Your current session stays
   open and your other sessions are signed out.

Other apps that accept time-based one-time passwords work with the same format.
Codes change every 30 seconds and the app can generate them offline. Flow
Steward does not send sign-in codes by email, SMS, or push notification.

## Sign in or recover access

Enter your email and password on the sign-in screen. When prompted, enter a
fresh code in **Authenticator or recovery code**. If you lose access to the
app, enter an unused recovery code in the same field.

To switch devices, open **Settings → Account → Sign-in** and select
**Replace authenticator**. You'll need your password and a fresh code from
the old app or another unused recovery code. Confirm the new app with its
six-digit code. Save the new set of recovery codes. The previous set stops
working as soon as the replacement is confirmed.

If you have lost both the authenticator and all recovery codes, ask an
administrator to reset your authenticator. A password reset keeps the existing
authenticator enrolled.

## Manage another user's authenticator

Open **Settings → Users** and use the actions menu for a local user you manage.

- **Require authenticator** applies an individual sign-in requirement. If the
  user has no authenticator, the next sign-in displays the setup QR code after
  checking their password. The user confirms a code from the new app before
  the session starts and is shown ten new recovery codes.
- **Reset authenticator** removes the old authenticator, invalidates the
  recovery codes, and ends the user's active browser sessions. The next
  sign-in requires the user to complete setup again. Their individual
  requirement stays as configured.
- **Remove individual authenticator requirement** removes that setting for the
  selected user. The installation-wide requirement and any existing
  authenticator stay in effect.

Administrators need access to every account associated with the targeted user
because the authenticator protects the entire identity.

## Require a second factor for an installation

Enrollment is optional by default. An enrolled user always needs a second
factor at sign-in, even while enrollment is optional for everyone else.

1. While enrollment is optional, have every local user who needs access
   complete setup and save their recovery codes. Verify that at least one
   local administrator can sign in with both factors in a new browser session.
2. Set `FS_AUTH_MFA_REQUIRED=1` in the deployment environment and recreate or
   redeploy the application runtime so it receives the new value. On a Docker
   host, apply it using the environment file and Compose configuration for
   your existing installation. On a managed host, use its environment settings
   and redeploy the application.

With enforcement on, local users cannot disable their authenticator. Users
covered only by the installation-wide requirement must already have an
authenticator to sign in. If someone missed setup, an administrator can use
**Require authenticator** for that user so their next sign-in guides them
through enrollment.

Keep the installation's `ENCRYPTION_KEY` available and stable. Flow Steward
uses it to encrypt authenticator keys. For automation that relied on the MCP
client's local-password fallback, configure a scoped programmatic token before
requiring a second factor. See the [MCP authentication guide](../GUIDE.md#authentication).
