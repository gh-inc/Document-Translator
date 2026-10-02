import { Link, NavLink, Route, Routes } from 'react-router-dom';
import UploadPage from './features/upload/UploadPage';
import JobPage from './features/jobs/JobPage';
import BatchPage from './features/jobs/BatchPage';
import HistoryPage from './features/history/HistoryPage';

export default function App() {
  return (
    <div className="app-shell">
      <a href="#main-content" className="skip-link">Skip to main content</a>
      <header className="app-header">
        <div className="header-content">
          <Link to="/" className="brand-link" aria-label="Stark Future Document Translator home">
            <img src="/branding/starkfuture-wordmark.svg" alt="Stark Future" width="190" height="36" />
            <span className="brand-caption">Document Translator</span>
          </Link>
          <nav aria-label="Main navigation" className="main-navigation">
            <NavLink to="/" end className="navigation-link">Translate</NavLink>
            <NavLink to="/history" className="navigation-link">History</NavLink>
          </nav>
        </div>
      </header>
      <main id="main-content" tabIndex={-1} className="main-content">
        <Routes>
          <Route path="/" element={<UploadPage />} />
          <Route path="/jobs/:jobId" element={<JobPage />} />
          <Route path="/batches/:batchId" element={<BatchPage />} />
          <Route path="/history" element={<HistoryPage />} />
          <Route path="*" element={<h1>Page not found</h1>} />
        </Routes>
      </main>
      <footer className="app-footer">PDF &amp; DOCX · One document, multiple languages</footer>
    </div>
  );
}
