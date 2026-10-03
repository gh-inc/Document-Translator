/** Wire contracts from app/api/schemas.py and app/core/models.py. */
export type DocumentStatus = 'uploaded' | 'analyzing' | 'extracted' | 'failed';
export type JobStatus = 'queued' | 'running' | 'assembling' | 'done' | 'completed_with_errors' | 'failed';

export interface DocumentUploadResponse {
  id: string;
  filename: string;
  format: string;
  status: DocumentStatus;
  block_count: number;
  analysis_cost_usd: number;
}

export interface CreateJobRequest {
  document_id: string;
  target_languages: string[];
  idempotency_key: string;
}

export interface ErrorResponse {
  error_code: string;
  message: string;
  retryable: boolean;
}

export type JobError = ErrorResponse;

export interface JobSummaryResponse {
  id: string;
  document_id: string;
  batch_id: string;
  target_language: string;
  status: JobStatus;
  total_chunks: number;
  done_chunks: number;
  cache_hit_blocks: number;
  cache_miss_blocks: number;
  cost_usd: number;
  error: JobError | null;
}

export interface BatchResponse {
  batch_id: string;
  jobs: JobSummaryResponse[];
}

export interface RetryRequest {
  raised_cost_cap_usd?: number | null;
}

export interface ServerSentEvent {
  job_id: string;
  event: string;
  status: JobStatus;
  done_chunks: number;
  total_chunks: number;
  cache_hit_blocks: number;
  cache_miss_blocks: number;
  cost_usd: number;
  error: JobError | null;
}
