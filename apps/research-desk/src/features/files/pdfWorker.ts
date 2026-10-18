/**
 * Single source of the pdfjs worker, shared by the in-browser text extraction
 * (`parsing.ts`) and the react-pdf viewer (`PdfViewer.tsx`). Both resolve the
 * SAME pinned `pdfjs-dist`, so one worker is correct for both and the API and
 * worker versions cannot drift apart ("API version does not match Worker
 * version").
 *
 * Loaded via Vite's `?worker` import — the bundler's first-class worker support,
 * correct in dev AND prod — and attached as `GlobalWorkerOptions.workerPort` (a
 * real `Worker` instance). This deliberately avoids the `workerSrc` URL path:
 * pdfjs-dist ships a default `workerSrc` of `"pdf.worker.mjs"` (a bare specifier
 * the browser cannot resolve), and neither the `?url` nor the
 * `new URL(..., import.meta.url)` form makes the Vite-optimized pdfjs pick up a
 * real worker here — it falls back to a fake worker ("Failed to resolve module
 * specifier 'pdf.worker.mjs'"). A real `workerPort` is unambiguous; pdfjs
 * multiplexes all documents over the one shared worker by docId.
 */

import PdfjsWorker from 'pdfjs-dist/build/pdf.worker.min.mjs?worker'

/**
 * Attach the shared worker to a pdfjs instance. Idempotent: the first caller
 * creates the single worker, later calls reuse it (both the extractor and the
 * viewer share one `GlobalWorkerOptions` singleton).
 *
 * Args:
 *   pdfjs: The pdfjs module (or react-pdf's re-exported `pdfjs`), narrowed to
 *     the only field this needs so callers do not depend on the full type.
 */
export function configurePdfWorker(pdfjs: {
  GlobalWorkerOptions: { workerPort: Worker | null }
}): void {
  if (!pdfjs.GlobalWorkerOptions.workerPort) {
    pdfjs.GlobalWorkerOptions.workerPort = new PdfjsWorker()
  }
}
