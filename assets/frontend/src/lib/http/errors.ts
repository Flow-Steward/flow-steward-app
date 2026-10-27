import type { ApiErrorPayload } from "@/lib/http/types";
import { isTruthy } from "@/lib/utils/unknown-values";

export type HttpErrorKind =
  | "unauthorized"
  | "forbidden"
  | "not_found"
  | "conflict"
  | "validation"
  | "server_error"
  | "unknown";

function resolveKind(status: number): HttpErrorKind {
  if (status === 401) return "unauthorized";
  if (status === 403) return "forbidden";
  if (status === 404) return "not_found";
  if (status === 409) return "conflict";
  if (status === 422) return "validation";
  if (status >= 500) return "server_error";
  return "unknown";
}

function detailMessage(value: unknown): string {
  if (typeof value === "string" && value.trim()) {
    return value.trim();
  }
  if (Array.isArray(value)) {
    const messages = value
      .map((item) => {
        if (typeof item === "string") {
          return item.trim();
        }
        if (typeof item === "object" && item !== null) {
          const candidate = (item as ApiErrorPayload).message || (item as ApiErrorPayload).detail;
          return typeof candidate === "string" ? candidate.trim() : "";
        }
        return "";
      })
      .filter(isTruthy);
    return messages.slice(0, 3).join(" ");
  }
  if (typeof value === "object" && value !== null) {
    const payload = value as ApiErrorPayload & {
      errors?: unknown;
      issues?: unknown;
      messages?: unknown;
      validation?: unknown;
    };
    const candidate =
      payload.message ||
      payload.detail ||
      detailMessage(payload.messages) ||
      detailMessage(payload.errors) ||
      detailMessage(payload.issues) ||
      detailMessage(payload.validation);
    if (typeof candidate === "string" && candidate.trim()) {
      return candidate.trim();
    }
  }
  return "";
}

function resolveMessage(status: number, payload: unknown): string {
  if (typeof payload === "object" && payload !== null) {
    const envelopeError = (payload as ApiErrorPayload).error;
    if (typeof envelopeError === "object" && envelopeError !== null) {
      const nestedMessage = (envelopeError as ApiErrorPayload).message;
      if (typeof nestedMessage === "string" && nestedMessage.trim()) {
        return nestedMessage.trim();
      }
      const nestedDetails = detailMessage((envelopeError as ApiErrorPayload).details);
      if (nestedDetails) {
        return nestedDetails;
      }
    }

    const payloadDetail = detailMessage((payload as ApiErrorPayload).detail);
    if (payloadDetail) {
      return payloadDetail;
    }

    const candidate = (payload as ApiErrorPayload).message || envelopeError;
    if (typeof candidate === "string" && candidate.trim()) {
      return candidate.trim();
    }
  }

  return `Request failed with status ${status}.`;
}

export class HttpError extends Error {
  headers: Headers;
  kind: HttpErrorKind;
  payload: unknown;
  retryAfterSeconds?: number;
  status: number;
  url: string;

  constructor({
    headers,
    payload,
    status,
    url,
  }: {
    headers?: HeadersInit;
    status: number;
    url: string;
    payload: unknown;
  }) {
    super(resolveMessage(status, payload));
    this.name = "HttpError";
    this.headers = new Headers(headers);
    this.kind = resolveKind(status);
    this.payload = payload;
    this.status = status;
    this.url = url;
    const retryAfter = Number(this.headers.get("Retry-After"));
    if (Number.isFinite(retryAfter) && retryAfter > 0) {
      this.retryAfterSeconds = retryAfter;
    }
  }
}

export function isHttpError(value: unknown): value is HttpError {
  return value instanceof HttpError;
}
