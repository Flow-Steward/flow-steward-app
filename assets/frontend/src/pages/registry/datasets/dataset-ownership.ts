/** Configuration ownership comes only from the host response, not data read mode. */
export function isInstallOwned(record: { source_install_id?: string } | undefined): boolean {
  return Boolean(record?.source_install_id?.trim());
}
