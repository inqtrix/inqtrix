# Inqtrix PNG logo kit

A matte identity built around an open inquiry mark and a petrol thread. The thread has a short, rounded horizontal ending. The mark connects inquiry with source-based work; it is inspired by Ariadne's thread, rather than a literal ancient symbol.

## Product copy

**Descriptor:** Self-hosted AI research workspace

**Hero:** From questions to cited reports.

**Supporting copy:** Search the web and your documents. Develop cited reports, then keep working in chat and the editor.

The copy follows the project [README](../../README.md): iterative web research, questions over local documents, cited reports, conversations, and agent-assisted writing. The identity does not add security certifications or guarantees of factual correctness.

## Preview

Open `inqtrix-brand-portfolio-v6.png` for the one-page overview or `inqtrix-brand-portfolio-v6.pptx` for the presentation. The overview displays actual files from this kit. The `previews/` folder contains light and dark lock-screen visual concepts. They illustrate the palette on the existing interface structure; they are not running application screens or implementation specifications.

## Which asset to use

| Use | Asset family |
| --- | --- |
| GitHub README | `inqtrix-github-hero-compact-dark.png` or its light counterpart |
| Rounded README placement | `inqtrix-github-hero-compact-*-rounded.png` |
| README with more explanation | `inqtrix-github-hero-extended-dark.png` |
| Repository social preview | `inqtrix-github-social-preview-1280x640.png` |
| Website hero with copy | `inqtrix-website-hero-*-1920x1080.png` |
| Website art behind live HTML text | `inqtrix-website-background-*-1920x1080.png` |
| Header or navigation | `inqtrix-navbar-*-transparent.png` |
| Header with descriptor | `inqtrix-navbar-with-tagline-*-transparent.png` |
| Large standalone lockup | `inqtrix-hero-lockup-*-transparent.png` |
| Material mark at larger sizes | `inqtrix-mark-lightmode-*` or `inqtrix-mark-darkmode-*` |
| Small interface mark | `inqtrix-mark-flat-light-*` or `inqtrix-mark-flat-dark-*` |
| Single-color reproduction | `inqtrix-mark-mono-navy-*` or `inqtrix-mark-mono-white-*` |
| Desktop application | `inqtrix-desktop-icon-*` |
| Browser icon | `favicon.ico` or the explicit PNG sizes |
| Touch icon | `apple-touch-icon-180.png` |

`light` names the receiving surface: dark lettering on a light surface. `dark` means light lettering on a dark surface. Transparent variants contain no painted background. Rounded banners and desktop icons also preserve transparency outside their frame.

## Color and material

| Role | Flat color | Purpose |
| --- | --- | --- |
| Ink | `#132536` | Structure and the primary reading surface |
| Paper | `#F4F1EA` | Sources and sustained reading |
| Petrol | `#08686D` | The connecting thread on light surfaces |
| Light petrol | `#71C8CD` | The thread on dark surfaces |

These are deliberate meanings within the Inqtrix identity. They are not claims about universal color psychology or historical Greek pigments. Material highlights are bounded to retain contrast; flat variants use the exact colors above. The light-surface accent has a 6.54:1 contrast ratio against white and 5.80:1 against Paper. The dark-surface accent has an 8.06:1 ratio against Ink. These ratios describe the specified flat color pairs, not conformance of an entire interface. Use each accent on its named receiving surface.

Use matte artwork where its texture can be seen. Use flat artwork for compact UI, favicons, and monochrome applications. Keep the internal gaps open, preserve the aspect ratio, and retain the supplied breathing room. A light and a dark mark have the same contour. An additional enclosing frame is not part of the symbol; the one exception is the browser and touch icon, which carries the flat mark on an Ink badge (see Research Desk integration).

## Typography

The PNG wordmarks use Helvetica Neue Bold. Supporting text uses Helvetica Neue Regular. Font files are not bundled. The presentation carries editable captions; the demonstration assets, including their lettering, remain embedded PNGs.

## Source and export details

This is a raster asset kit. `sources/` preserves the generated matte source artwork and the two supporting background sources. The material master preserves the source contour and surface detail. Its thread is recolored deterministically to the receiving surface accent without regenerating the design. The included material reference carries the current palette. Edge masks receive subpixel antialiasing and minor contour cleanup; flat and inverse variants reuse that same mask. Desktop icons, banners, and heroes combine the shared mark with exact native text and fixed output dimensions.

The cropped canonical mark canvas is 909 by 909 pixels. The 1024-pixel mark files are interpolated exports, not independent redraws. Larger compositions resample these raster sources; the background imagery is also softly resampled. No vector master is included.

`manifest.json` records file dimensions, alpha availability, hashes, source dimensions, and palette. `copy.json` contains the English wording. These assets do not change application imports or repository README references automatically.

## Research Desk integration

The app uses byte-identical 128 px flat, matte, and monochrome marks from this
kit in `apps/research-desk/src/brand`. Flat is the default at 20–28 CSS pixels;
the lock screen uses matte at 32 pixels. Artwork stays Petrol in every preset;
high contrast uses monochrome. These copies are inside the existing Vite and
container build boundary, so the portfolio is never shipped with the app.

Browser and touch icons use the same flat dark mark on an Ink badge with a
light edge, legible against light and dark browser chrome. The standalone
transparent flat mark files remain available for other placements.

The README uses the extended dark banner. Its text block spans visible y=79–321,
aligned to the visible logo, with equal gaps between its three lines. The preview
is `previews/inqtrix-readme-hero.png`. The portfolio retains the compact banner
as an alternative application; the lock-screen images remain design concepts.

`export-ui.py` reproduces the README banner, browser/touch badges, public
icon copies, and manifest. Run it explicitly with Python and Pillow on macOS
(Helvetica Neue); it is not part of application builds. It preserves the existing
banner logo pixels and composes the lettering over the supplied background.
