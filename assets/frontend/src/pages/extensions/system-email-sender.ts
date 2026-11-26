/** This connection sends the platform's own email (Settings -> Email). */
export function isSystemEmailSender(row: Record<string, unknown> | null | undefined): boolean {
  return row?.system_email_sender === true;
}

/**
 * The connection's settings, secrets, enabled state and account are read-only to
 * this viewer: only a platform administrator may change the system sender.
 */
export function isSystemEmailSenderLocked(row: Record<string, unknown> | null | undefined): boolean {
  return row?.system_email_sender_locked === true;
}
