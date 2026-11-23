import { useContext } from "react";

import { AuthContext } from "@/lib/auth/context";

/**
 * The largest file the browser previews (`FS_FILE_PREVIEW_MAX_BYTES`, ADR 0080);
 * undefined until the session knows it. The server still refuses a larger
 * preview, so an unknown limit only means the refusal comes from there.
 */
export function useFilePreviewMaxBytes(): number | undefined {
  return useContext(AuthContext)?.session?.file_preview_max_bytes;
}
