import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../../api/client';
import { catalogError, getErrorMessage, isAbortError } from '../../api/errors';
import type { JobStatus, JobSummaryResponse } from '../../api/types';
import { useJobEvents } from './useJobEvents';

const statuses: Record<JobStatus, string> = { queued: 'Queued', running: 'Translating', assembling: 'Preparing document', done: 'Completed', completed_with_errors: 'Completed with errors', failed: 'Failed' };
const buttonStyle = 'button-secondary disabled:cursor-wait disabled:opacity-50';

export interface JobCardProps {
  jobId: string;
  initialJob?: JobSummaryResponse;
  live?: boolean;
  showDetailsLink?: boolean;
  onJobUpdate?: (job: JobSummaryResponse) => void;
  /** Document analysis cost, rendered only on the first card of its document. */
  analysisCostUsd?: number | null;
  /** How many translations share that document's analysis. */
  analysisSharedBy?: number;
}

export default function JobCard({ jobId, initialJob, live = true, showDetailsLink = false, onJobUpdate, analysisCostUsd = null, analysisSharedBy = 1 }: JobCardProps) {
  const { job, loading, error, reconnecting, reload } = useJobEvents(jobId, initialJob, live);
  const [action, setAction] = useState<'retry' | 'download' | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const cachedTotal = job ? job.cache_hit_blocks + job.cache_miss_blocks : 0;
  const cachedPercent = job && cachedTotal > 0 ? Math.round(100 * job.cache_hit_blocks / cachedTotal) : null;
  const lifetime = useRef<AbortController>();
  useEffect(() => {
    if (job) onJobUpdate?.(job);
  }, [job, onJobUpdate]);
  useEffect(() => {
    const controller = new AbortController(); lifetime.current = controller;
    setAction(null); setActionError(null);
    return () => controller.abort();
  }, [jobId]);

  async function perform(kind: 'retry' | 'download') {
    const controller = lifetime.current;
    if (!controller || controller.signal.aborted || !job || action) return;
    setAction(kind); setActionError(null);
    try {
      if (kind === 'retry') {
        const retriedJob = await api.retryJob(jobId, controller.signal);
        if (!controller.signal.aborted) {
          onJobUpdate?.(retriedJob);
          reload();
        }
      } else {
        const blob = await api.download(jobId, controller.signal);
        let extension = blob.type.includes('wordprocessingml')
          ? 'docx'
          : blob.type.includes('pdf')
            ? 'pdf'
            : blob.type.includes('markdown') || blob.type === 'text/plain'
              ? 'md'
              : null;
        if (!extension) {
          const document = await api.getDocument(job.document_id, controller.signal);
          const format = document.format.toLowerCase();
          extension = format === 'docx' ? 'docx' : format === 'md' ? 'md' : 'pdf';
        }
        if (controller.signal.aborted) return;
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        try {
          link.href = url;
          link.download = `translation-${job.target_language.replace(/[^a-zA-Z0-9_-]/g, '-')}-${jobId.replace(/[^a-zA-Z0-9_-]/g, '-')}.${extension}`;
          document.body.appendChild(link); link.click();
        } finally { link.remove(); URL.revokeObjectURL(url); }
      }
    } catch (failure) {
      if (!controller.signal.aborted && !isAbortError(failure)) setActionError(getErrorMessage(failure));
    } finally {
      if (!controller.signal.aborted) setAction(null);
    }
  }

  return (
    <article className="job-card border border-white/20 bg-stark-surface" aria-label={job ? `Translation to ${job.target_language}` : 'Translation job'}>
      {loading && <p role="status">Loading translation…</p>}
      {error && <div role="alert"><p>{error}</p><button className={`${buttonStyle} mt-4`} type="button" onClick={reload}>Reload job</button></div>}
      {job && <>
        <h2 className="text-xl font-semibold">{job.target_language}</h2>
        <p aria-live="polite" className="mt-3">{statuses[job.status]}</p>
        <p className="mt-2">{job.done_chunks} / {job.total_chunks} chunks</p>
        {cachedPercent !== null && <p className="mt-2" aria-label={`${job.cache_hit_blocks} of ${cachedTotal} blocks served from the translation cache`}>{cachedPercent}% cached</p>}
        <progress className="mt-2 w-full accent-stark-red" aria-label={`Translation progress for ${job.target_language}`} value={job.done_chunks} max={Math.max(job.total_chunks, 1)} />
        <p className="mt-2">Cost: <span>${job.cost_usd.toFixed(4)}</span></p>
        {analysisCostUsd !== null && analysisCostUsd > 0 && (
          <p className="mt-2 text-neutral-300" aria-label={`Document analysis cost, shared by ${analysisSharedBy} ${analysisSharedBy === 1 ? 'translation' : 'translations'}`}>
            Document analysis: <span>${analysisCostUsd.toFixed(4)}</span>
            <span className="block text-sm">shared by {analysisSharedBy} {analysisSharedBy === 1 ? 'translation' : 'translations'}</span>
          </p>
        )}
        {job.status === 'completed_with_errors' && <p role="status" className="mt-4 text-amber-300">Untranslated blocks remain in the source language. Retry to translate the remaining blocks.</p>}
        {job.error && <p role="alert" className="mt-3">{catalogError(job.error.error_code).message}</p>}
        {reconnecting && <p role="status" className="mt-3">Reconnecting to live progress…</p>}
        <div className="mt-5 flex flex-wrap gap-4">
          {['failed', 'completed_with_errors'].includes(job.status) && <button className={buttonStyle} type="button" disabled={action !== null} onClick={() => void perform('retry')}>{action === 'retry' ? 'Retrying…' : 'Retry translation'}</button>}
          {['done', 'completed_with_errors'].includes(job.status) && <button className={buttonStyle} type="button" disabled={action !== null} onClick={() => void perform('download')}>{action === 'download' ? 'Downloading…' : 'Download translation'}</button>}
          {showDetailsLink && <Link className="detail-link" to={`/jobs/${encodeURIComponent(jobId)}`}>View translation</Link>}
        </div>
      </>}
      {actionError && <p role="alert" className="mt-4">{actionError}</p>}
    </article>
  );
}
