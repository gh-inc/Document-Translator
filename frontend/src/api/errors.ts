import type { ErrorResponse } from './types';

// Keep in sync with app/core/errors.py. Server messages are never rendered.
const catalog = {
  internal_error: ['Internal server error', true],
  invalid_request: ['Request validation failed', false],
  not_found: ['Requested resource was not found', false],
  conflict: ['Request conflicts with the resource state', false],
  analysis_pending: ['Document analysis is pending; retry shortly', true],
  unsupported_format: ['Document format is unsupported', false],
  size_limit: ['Document exceeds the 50 MiB upload limit', false],
  page_limit: ['Document exceeds the 400-page limit', false],
  text_limit: ['Document exceeds the 10 MiB extracted-text limit', false],
  not_ready: ['Service dependencies are unavailable', true],
  scanned_pdf: ['PDF has no usable text layer; OCR is unsupported', false],
  corrupt_file: ['Document cannot be read or is corrupt', false],
  render_failed: ['Translated document could not be rendered', false],
  cost_cap_exceeded: ['Job translation cost limit reached', false],
  provider_timeout: ['Translation provider timed out', true],
  provider_connection: ['Translation provider is unreachable', true],
  provider_rate_limit: ['Translation provider rate limit reached', true],
  provider_server_error: ['Translation provider is temporarily unavailable', true],
  provider_bad_request: ['Translation provider rejected the request', false],
  provider_auth_error: ['Translation provider authorization failed', false],
  provider_invalid_response: ['Translation provider returned an invalid response', true],
  provider_refusal: ['Translation provider refused the request', false],
} as const;

export type ErrorCode = keyof typeof catalog;
export type ApiErrorKind = 'server' | 'network' | 'invalid_response' | 'aborted';

export function catalogError(code: string): ErrorResponse {
  const error_code: ErrorCode = Object.hasOwn(catalog, code) ? code as ErrorCode : 'internal_error';
  const [message, retryable] = catalog[error_code];
  return { error_code, message, retryable };
}

export function parseErrorResponse(value: unknown): ErrorResponse | null {
  if (typeof value !== 'object' || value === null) return null;
  const record = value as Record<string, unknown>;
  if (typeof record.error_code !== 'string' || typeof record.message !== 'string' || typeof record.retryable !== 'boolean') return null;
  return catalogError(record.error_code);
}

/** Contains only locally controlled messages, including transport failures. */
export class ApiError extends Error implements ErrorResponse {
  readonly error_code: string;
  readonly retryable: boolean;
  constructor(code: string = 'internal_error', readonly status?: number, readonly kind: ApiErrorKind = 'server') {
    const safe = catalogError(code);
    const message = kind === 'network' ? 'Unable to connect to the service. Please try again.'
      : kind === 'invalid_response' ? 'The service returned an invalid response. Please try again.'
      : kind === 'aborted' ? 'Request cancelled' : safe.message;
    super(message);
    this.name = kind === 'aborted' ? 'AbortError' : 'ApiError';
    this.error_code = safe.error_code;
    this.retryable = kind === 'aborted' ? false : safe.retryable;
  }
}

export function isAbortError(error: unknown): boolean {
  return error instanceof ApiError && error.kind === 'aborted'
    || error instanceof DOMException && error.name === 'AbortError';
}

export function getErrorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : catalogError('internal_error').message;
}

export const safeErrorMessage = getErrorMessage;
