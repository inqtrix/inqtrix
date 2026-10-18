# Runtime brand artwork

These six 128 px PNGs are exact copies from `assets/inqtrix-png-logo-kit-v6`.
`BrandMark` chooses the flat or matte pair and uses the monochrome pair in high
contrast. Keep the supplied contour, alpha, and receiving-surface colors intact.

When updating the kit, copy only the required variants here and verify SHA-256
identity. Vite imports them normally; Docker and Helm need no special handling.
Browser badges are exported separately into `public` by the kit's `export-ui.py`.
