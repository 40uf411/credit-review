# 1. PDF library licence (PyMuPDF, AGPL-3.0)

**Status:** accepted for the prototype; to be revisited before any client deployment.

## Context

PDF text extraction, line positions and page rendering use PyMuPDF. PyMuPDF is dual-licensed:
**AGPL-3.0** or a commercial licence from Artifex. The AGPL's network clause means that offering
this software as a service to others obliges you to publish the complete source of the service.

That is acceptable for an open prototype. It is not acceptable for a client's internal credit
platform without either a commercial licence or a replacement.

## Decision

- Keep PyMuPDF for the prototype. Its quality on the case pack (text layer with positions,
  rendering of scanned payslips) is what the evidence location in `credit_review/pdf.py` relies on.
- Make the decision visible in CI. The licence check in `.github/workflows/ci.yml` fails on any
  copyleft licence, with PyMuPDF as the single named exception. Dependency review on pull requests
  does the same for newly added packages.
- All PyMuPDF use is confined to `credit_review/pdf.py`, so a replacement touches one module.

## Options for a client deployment

| Option | Licence | Trade-off |
|---|---|---|
| PyMuPDF commercial licence | Commercial | No code change; licence cost |
| pypdfium2 | Apache-2.0 / BSD | Text with positions and rendering; character-level API, so line grouping needs more code |
| pdfplumber (pdfminer.six) | MIT | Good positions; slower; no rendering, so scans need another tool |
| Azure Document Intelligence (Read/Layout) | Managed service | OCR, layout and bounding boxes, including on scans; per-page cost; data leaves the process |

## Also noted

PyMuPDF 2.0 adds onnxruntime as a dependency, which accounts for most of the container image size.
None of the features that need it are used here. Constraining to `pymupdf<2` or choosing an option
above would remove it.
