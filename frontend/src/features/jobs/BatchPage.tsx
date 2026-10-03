import { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { api } from '../../api/client';
import { getErrorMessage, isAbortError } from '../../api/errors';
import type { BatchResponse, DocumentUploadResponse } from '../../api/types';
import JobCard from './JobCard';

export default function BatchPage() {
  const { batchId } = useParams();
  const [reloadCount, setReloadCount] = useState(0);
  const [state, setState] = useState<{ id?: string; batch?: BatchResponse; analysis?: DocumentUploadResponse; error?: string }>({});
  useEffect(() => {
    const controller = new AbortController();
    setState({ id: batchId });
    if (batchId) void api.getBatch(batchId, controller.signal).then(batch => {
      if (controller.signal.aborted) return;
      setState({ id: batchId, batch });
      const documentId = batch.jobs[0]?.document_id;
      if (documentId) void api.getDocument(documentId, controller.signal).then(analysis => {
        if (!controller.signal.aborted) setState(current => current.id === batchId ? { ...current, analysis } : current);
      }).catch(() => {
        // Document analysis is optional to the batch view; translation jobs remain visible.
      });
    }).catch(error => {
      if (!controller.signal.aborted && !isAbortError(error)) setState({ id: batchId, error: getErrorMessage(error) });
    });
    return () => controller.abort();
  }, [batchId, reloadCount]);
  const current = state.id === batchId ? state : {};
  return (
    <section>
      <h1 className="mb-8 text-3xl font-semibold">Batch progress</h1>
      {!batchId ? <p role="alert">Batch ID is missing.</p> : current.error ? <div role="alert"><p>{current.error}</p><button className="button-secondary mt-4" type="button" onClick={() => setReloadCount(value => value + 1)}>Reload batch</button></div> : !current.batch ? <p role="status">Loading batch…</p> : <>
        {current.analysis && <p className="mb-4 text-neutral-300">Document analysis: <span>${current.analysis.analysis_cost_usd.toFixed(4)}</span></p>}
        <div className="grid gap-6 md:grid-cols-2">
          {current.batch.jobs.length === 0 && <p>This batch has no translation jobs.</p>}
          {current.batch.jobs.map(job => <JobCard key={job.id} jobId={job.id} initialJob={job} showDetailsLink />)}
        </div>
      </>}
    </section>
  );
}
