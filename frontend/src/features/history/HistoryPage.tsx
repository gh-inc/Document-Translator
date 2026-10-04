import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../../api/client';
import { getErrorMessage, isAbortError } from '../../api/errors';
import type { JobStatus, JobSummaryResponse } from '../../api/types';
import JobCard from '../jobs/JobCard';

const statuses: { value: JobStatus; label: string }[] = [
  { value: 'queued', label: 'Queued' },
  { value: 'running', label: 'Translating' },
  { value: 'assembling', label: 'Preparing document' },
  { value: 'done', label: 'Completed' },
  { value: 'completed_with_errors', label: 'Completed with errors' },
  { value: 'failed', label: 'Failed' },
];

export default function HistoryPage() {
  const [jobs, setJobs] = useState<JobSummaryResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<JobStatus | 'all'>('all');
  const [revision, setRevision] = useState(0);
  const updateJob = useCallback((updatedJob: JobSummaryResponse) => {
    setJobs((current) => current.map((job) => {
      if (job.id !== updatedJob.id) return job;
      // Single-job retry/status responses use a zero default for this document-level cost.
      return job.document_id === updatedJob.document_id && updatedJob.analysis_cost_usd === 0
        ? { ...updatedJob, analysis_cost_usd: job.analysis_cost_usd }
        : updatedJob;
    }));
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void api.listRecentJobs(10, controller.signal).then((recentJobs) => {
      if (!controller.signal.aborted) setJobs(recentJobs);
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted && !isAbortError(failure)) setError(getErrorMessage(failure));
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [revision]);

  const visibleJobs = jobs.filter((job) => status === 'all' || job.status === status);
  // The analysis cost belongs to the document, so it is shown once per document
  // instead of repeating on every language translated from the same upload.
  const groupSizes = new Map<string, number>();
  for (const job of visibleJobs) {
    groupSizes.set(job.document_id, (groupSizes.get(job.document_id) ?? 0) + 1);
  }
  const shownDocuments = new Set<string>();

  return (
    <section aria-labelledby="history-heading">
      <h1 id="history-heading" className="text-3xl font-semibold">Translation history</h1>
      <p className="mt-3 text-white/70">Your 10 most recent translations. Open a translation for live progress.</p>
      <div className="my-6 flex flex-wrap items-end gap-4">
        <div>
          <label htmlFor="history-status" className="mb-2 block">Filter by status</label>
          <select id="history-status" value={status} onChange={(event) => setStatus(event.target.value as JobStatus | 'all')} className="max-w-full rounded border border-neutral-500 bg-stark-surface px-3 py-2">
            <option value="all">All statuses</option>
            {statuses.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
          </select>
        </div>
        <button type="button" disabled={loading} onClick={() => setRevision((current) => current + 1)} className="button-secondary disabled:cursor-wait disabled:opacity-50">Reload history</button>
      </div>
      {loading && <p role="status">Loading translation history…</p>}
      {!loading && error && <p role="alert">{error}</p>}
      {!loading && !error && jobs.length === 0 && <div><p>No translations yet.</p><Link to="/" className="detail-link mt-3">Translate a document</Link></div>}
      {!loading && !error && jobs.length > 0 && visibleJobs.length === 0 && <p role="status">No translations match this status. Choose another status to see recent translations.</p>}
      {!loading && !error && visibleJobs.length > 0 && <ul className="space-y-6" aria-label="Recent translations">
        {visibleJobs.map((job) => {
          const firstOfDocument = !shownDocuments.has(job.document_id);
          shownDocuments.add(job.document_id);
          return <li key={job.id}>
            <JobCard jobId={job.id} initialJob={job} live={false} showDetailsLink onJobUpdate={updateJob}
                     analysisCostUsd={firstOfDocument ? job.analysis_cost_usd : null}
                     analysisSharedBy={groupSizes.get(job.document_id) ?? 1} />
            <Link to={`/batches/${encodeURIComponent(job.batch_id)}`} className="detail-link mt-3">View batch</Link>
          </li>;
        })}
      </ul>}
    </section>
  );
}
