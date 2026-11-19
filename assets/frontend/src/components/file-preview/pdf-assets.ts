/**
 * pdf.js files served by the app itself, never by a CDN (ADR 0077): the worker,
 * and the JavaScript image decoders pdf.js uses for JPEG 2000 and JBIG2 scans
 * when WebAssembly is off. The app's CSP does not allow compiling WebAssembly,
 * so the previewer turns it off and these decoders take over.
 *
 * vite.config.ts keeps both decoders under their own names in one folder per
 * pdf.js version, because pdf.js loads them as `${wasmUrl}${name}`.
 */
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import jbig2DecoderUrl from "pdfjs-dist/wasm/jbig2_nowasm_fallback.js?url";
import openjpegDecoderUrl from "pdfjs-dist/wasm/openjpeg_nowasm_fallback.js?url";

function sharedFolder(urls: readonly string[]): string {
  const folders = new Set(urls.map((url) => new URL(url.slice(0, url.lastIndexOf("/") + 1), globalThis.location.href).href));
  return folders.size === 1 ? [...folders][0] : "";
}

export const PDF_JS_WORKER_URL: string = workerUrl;

/** The decoders' folder with its trailing slash, or "" when the build split them (then those images stay blank). */
export const PDF_JS_DECODERS_URL: string = sharedFolder([jbig2DecoderUrl, openjpegDecoderUrl]);
