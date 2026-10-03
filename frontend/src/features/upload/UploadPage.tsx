import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { UploadForm } from './UploadForm';
import { useTranslationSubmission } from './useTranslationSubmission';

export default function UploadPage() {
  const navigate = useNavigate();
  const flow = useTranslationSubmission((batchId) => navigate(`/batches/${encodeURIComponent(batchId)}`));
  const [formVersion, setFormVersion] = useState(0);
  const hasRecovery = ['timeout', 'failed', 'submission-error', 'cancelled'].includes(flow.phase);

  return (
    <section aria-labelledby="upload-heading" className="max-w-3xl space-y-6">
      <h1 id="upload-heading">Translate a document</h1>
      <p className="text-neutral-300">Upload a PDF, DOCX, or Markdown document and choose your target languages. We analyze the document before starting translation.</p>
      <UploadForm key={formVersion} busy={flow.busy} onSubmit={(file, languages) => void flow.start(file, languages)} />
      <div role="status" aria-live="polite" aria-atomic="true">{flow.message}</div>
      <div className="flex flex-wrap gap-4">
        {flow.busy && <button type="button" onClick={flow.cancel} className="button-secondary">Cancel</button>}
        {flow.phase === 'timeout' && <button type="button" onClick={() => void flow.retryAnalysis()} className="button-secondary">Retry analysis</button>}
        {flow.phase === 'submission-error' && flow.canRetrySubmission && <button type="button" onClick={() => void flow.retrySubmission()} className="button-secondary">Retry translation submission</button>}
        {hasRecovery && <button type="button" onClick={() => { flow.reset(); setFormVersion((version) => version + 1); }} className="button-secondary">Choose another document</button>}
      </div>
    </section>
  );
}
