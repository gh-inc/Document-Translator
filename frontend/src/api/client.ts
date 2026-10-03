import { ApiError, isAbortError, parseErrorResponse } from './errors';
import type { BatchResponse, CreateJobRequest, DocumentUploadResponse, JobSummaryResponse, ServerSentEvent } from './types';

type RecordValue = Record<string, unknown>;
type Parser<T> = (value: unknown) => T | null;
const record = (value: unknown): value is RecordValue => typeof value === 'object' && value !== null && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === 'string' && value.length > 0;
const count = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
const money = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0;
const documentStatuses = ['uploaded', 'analyzing', 'extracted', 'failed'];
const jobStatuses = ['queued', 'running', 'assembling', 'done', 'completed_with_errors', 'failed'];

const parseDocument: Parser<DocumentUploadResponse> = (value) => {
  if (!record(value) || !text(value.id) || !text(value.filename) || !text(value.format)
    || !text(value.status) || !documentStatuses.includes(value.status) || !count(value.block_count)) return null;
  return { id: value.id, filename: value.filename, format: value.format, status: value.status as DocumentUploadResponse['status'], block_count: value.block_count };
};

function parseProgress(value: RecordValue) {
  if (!text(value.status) || !jobStatuses.includes(value.status) || !count(value.total_chunks)
    || !count(value.done_chunks) || !count(value.cache_hit_blocks)
    || !count(value.cache_miss_blocks) || !money(value.cost_usd)) return null;
  const error = value.error === null ? null : parseErrorResponse(value.error);
  if (value.error !== null && error === null) return null;
  return { status: value.status as JobSummaryResponse['status'], total_chunks: value.total_chunks, done_chunks: value.done_chunks, cache_hit_blocks: value.cache_hit_blocks, cache_miss_blocks: value.cache_miss_blocks, cost_usd: value.cost_usd, error };
}

const parseJob: Parser<JobSummaryResponse> = (value) => {
  if (!record(value) || !text(value.id) || !text(value.document_id) || !text(value.batch_id) || !text(value.target_language)) return null;
  const progress = parseProgress(value);
  return progress ? { id: value.id, document_id: value.document_id, batch_id: value.batch_id, target_language: value.target_language, ...progress } : null;
};

const parseJobs: Parser<JobSummaryResponse[]> = (value) => {
  if (!Array.isArray(value)) return null;
  const jobs: JobSummaryResponse[] = [];
  for (const item of value) {
    const job = parseJob(item);
    if (!job) return null;
    jobs.push(job);
  }
  return jobs;
};

const parseBatch: Parser<BatchResponse> = (value) => {
  if (!record(value) || !text(value.batch_id)) return null;
  const jobs = parseJobs(value.jobs);
  return jobs ? { batch_id: value.batch_id, jobs } : null;
};

/** Invalid SSE messages are ignored; never pass unvalidated JSON into a view. */
export function parseServerSentEvent(data: string): ServerSentEvent | null {
  try {
    const value: unknown = JSON.parse(data);
    if (!record(value) || !text(value.job_id) || !text(value.event) || !['status', 'progress', 'error', 'done'].includes(value.event)) return null;
    const progress = parseProgress(value);
    return progress ? { job_id: value.job_id, event: value.event as string, ...progress } : null;
  } catch {
    return null;
  }
}

async function responseJson(response: Response): Promise<unknown> {
  try { return await response.json(); } catch { return null; }
}

async function fetchResponse(path: string, init: RequestInit): Promise<Response> {
  try {
    const response = await fetch(`/api${path}`, init);
    if (!response.ok) {
      const envelope = parseErrorResponse(await responseJson(response));
      throw new ApiError(envelope?.error_code, response.status);
    }
    return response;
  } catch (error) {
    if (init.signal?.aborted || isAbortError(error)) throw new ApiError('internal_error', undefined, 'aborted');
    if (error instanceof ApiError) throw error;
    throw new ApiError('internal_error', undefined, 'network');
  }
}

async function request<T>(path: string, parser: Parser<T>, init: RequestInit): Promise<T> {
  const response = await fetchResponse(path, init);
  const value = parser(await responseJson(response));
  if (init.signal?.aborted) throw new ApiError('internal_error', undefined, 'aborted');
  if (value === null) throw new ApiError('internal_error', response.status, 'invalid_response');
  return value;
}

const idPath = (id: string) => encodeURIComponent(id);
const postJson = (body: unknown, signal?: AbortSignal): RequestInit => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal });

export function uploadDocument(file: File, signal?: AbortSignal): Promise<DocumentUploadResponse> {
  const body = new FormData();
  body.append('file', file);
  return request('/documents', parseDocument, { method: 'POST', body, signal });
}

export const getDocument = (id: string, signal?: AbortSignal) => request(`/documents/${idPath(id)}`, parseDocument, { method: 'GET', signal });
export const retryTriage = (id: string, signal?: AbortSignal) => request(`/documents/${idPath(id)}/retry-triage`, parseDocument, { method: 'POST', signal });
export const createJobs = (body: CreateJobRequest, signal?: AbortSignal) => request('/jobs', parseBatch, postJson(body, signal));
export const getJob = (id: string, signal?: AbortSignal) => request(`/jobs/${idPath(id)}`, parseJob, { method: 'GET', signal });
export const retryJob = (id: string, signal?: AbortSignal) => request(`/jobs/${idPath(id)}/retry`, parseJob, postJson({}, signal));
export const getBatch = (id: string, signal?: AbortSignal) => request(`/batches/${idPath(id)}`, parseBatch, { method: 'GET', signal });
export const listRecentJobs = (limit = 10, signal?: AbortSignal) => request(`/jobs?limit=${encodeURIComponent(limit)}`, parseJobs, { method: 'GET', signal });

/** The caller saves the blob using a filename derived from the document/job. */
export async function download(id: string, signal?: AbortSignal): Promise<Blob> {
  const response = await fetchResponse(`/jobs/${idPath(id)}/download`, { method: 'GET', signal });
  try {
    const blob = await response.blob();
    if (signal?.aborted) throw new ApiError('internal_error', undefined, 'aborted');
    return blob;
  } catch (error) {
    if (signal?.aborted || isAbortError(error)) throw new ApiError('internal_error', undefined, 'aborted');
    throw new ApiError('internal_error', undefined, 'network');
  }
}

export const api = { uploadDocument, getDocument, retryTriage, createJobs, getJob, retryJob, getBatch, listRecentJobs, download };
