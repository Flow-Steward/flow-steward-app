import { HttpError } from "@/lib/http/errors";
import { beginRequest, isLoadedRead, rememberLoadedRead } from "@/lib/http/request-activity";
import { isApiEnvelope } from "@/lib/http/types";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

function readCsrfToken() {
  if (globalThis.document === undefined) {
    return "";
  }

  const parts = document.cookie.split(";").map((value) => value.trim());
  const csrfCookie = parts.find((value) => value.startsWith("fs_csrf="));
  return csrfCookie ? decodeURIComponent(csrfCookie.split("=", 2)[1] ?? "") : "";
}

async function parseResponseBody(response: Response): Promise<unknown> {
  if (response.status === 204) {
    return undefined;
  }

  const contentType = response.headers.get("content-type") || "";

  if (contentType.includes("application/json")) {
    return response.json();
  }

  const text = await response.text();
  if (!text) {
    return undefined;
  }

  try {
    return JSON.parse(text) as unknown;
  } catch {
    return { message: text };
  }
}

export function buildHttpHeaders(init: RequestInit = {}): Headers {
  const headers = new Headers(init.headers);
  const method = (init.method || "GET").toUpperCase();

  if (!headers.has("Accept")) {
    headers.set("Accept", "application/json");
  }

  if (init.body !== undefined && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  if (UNSAFE_METHODS.has(method) && !headers.has("x-csrf-token")) {
    const csrfToken = readCsrfToken();
    if (csrfToken) {
      headers.set("x-csrf-token", csrfToken);
    }
  }

  return headers;
}

export type FetchJsonOptions = {
  presentation?: "foreground" | "background";
};

function readKey(input: RequestInfo | URL, init: RequestInit): string | null {
  const request = input instanceof Request ? input : null;
  const method = (init.method || request?.method || "GET").toUpperCase();
  if (method !== "GET") {
    return null;
  }
  if (request) {
    return request.url;
  }
  return input instanceof URL ? input.href : stringFromUnknownValue(input);
}

export async function fetchJson<T>(
  input: RequestInfo | URL,
  init: RequestInit = {},
  options: FetchJsonOptions = {},
): Promise<T> {
  const headers = buildHttpHeaders(init);
  const key = readKey(input, init);
  const presentation =
    options.presentation ?? (key !== null && isLoadedRead(key) ? "background" : "foreground");

  const finishRequest = presentation === "background" ? undefined : beginRequest();
  try {
    const response = await fetch(input, {
      ...init,
      credentials: init.credentials || "same-origin",
      headers,
    });

    const payload = await parseResponseBody(response);

    if (isApiEnvelope(payload)) {
      if (!payload.ok) {
        throw new HttpError({
          headers: response.headers,
          status: payload.error?.status ?? response.status,
          url: response.url || stringFromUnknownValue(input),
          payload,
        });
      }
      if (key !== null) rememberLoadedRead(key);
      return payload.data as T;
    }

    if (!response.ok) {
      throw new HttpError({
        headers: response.headers,
        status: response.status,
        url: response.url || stringFromUnknownValue(input),
        payload,
      });
    }

    if (key !== null) rememberLoadedRead(key);
    return payload as T;
  } finally {
    finishRequest?.();
  }
}

/** A text body as it is, with the same credentials and errors as `fetchJson`. */
export async function fetchText(input: RequestInfo | URL, init: RequestInit = {}): Promise<string> {
  const headers = buildHttpHeaders({ ...init, headers: { Accept: "text/plain", ...init.headers } });
  const finishRequest = beginRequest();
  try {
    const response = await fetch(input, {
      ...init,
      credentials: init.credentials || "same-origin",
      headers,
    });
    if (!response.ok) {
      throw new HttpError({
        headers: response.headers,
        status: response.status,
        url: response.url || stringFromUnknownValue(input),
        payload: await parseResponseBody(response),
      });
    }
    return await response.text();
  } finally {
    finishRequest();
  }
}
