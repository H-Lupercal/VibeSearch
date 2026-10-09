# Reader source/CDN verification

Status: PASSED for the bounded Phase C source-compatibility gate. This is not a reader implementation or a universal CDN guarantee.

Checked at UTC: 2026-10-09T17:37:04.552949+00:00 (probe start).

Evidence: [machine-readable results](READER_VERIFICATION_RESULTS.json).

## Scope and method

Inspected the local catalog read-only, fetched the public OpenAPI document, and live-refetched the five highest-ID eligible records in the existing catalog. These are recent locally collected records, not a claim that they were the five newest source entries at probe time. Metadata used the existing paced MetadataClient and configured operator contact/authentication. No credentials were included in evidence.

Fetched page 1 for each sampled gallery from i1.nhentai.net. Additionally fetched the same first gallery's page 1 from i2, i3, and i4 to compare bytes. CDN requests used a separate client, no API credentials/contact header, no cookies, no automatic redirects, a total 30-second deadline and 15 MiB byte cap. Image bytes were inspected in memory and discarded. No media archive or catalog mutation was performed. No access-control bypass was used.

Live requests: one OpenAPI document, five details, eight page-image GETs. The image GET count includes three repeated cross-host checks. Total fetched image bytes: 3,455,178. All five details and all eight image requests returned HTTP 200. There were no observed redirects or 403/404 responses.

## Verified API contract

OpenAPI URL: https://nhentai.net/api/v2/openapi.json

OpenAPI info.version: `2.0.0+3ef54aa`.

Detail route: GET `/api/v2/galleries/{gallery_id}`. Full reader descriptors arrive by default, with no optional include value required. The documented include options are comments, related, favorite, and suggestions, not pages.

Exact root JSON paths:
- `$.id`: gallery integer ID.
- `$.media_id`: media identifier string.
- `$.num_pages`: expected page count.
- `$.pages`: array of PageInfo.
- `$.pages[*].number`: one-based page number.
- `$.pages[*].path`: relative full-page image path.
- `$.pages[*].width`, `$.pages[*].height`: integer metadata dimensions.
- `$.pages[*].thumbnail`, `thumbnail_width`, `thumbnail_height`: optional reader-adapter extras, not needed for the initial full-page reader.

PageInfo's documented required properties are number, path, width, height, thumbnail, thumbnail_width, thumbnail_height. GalleryDetailResponse permits pages to default to an empty array; therefore response schema validity alone does not make a gallery readable. Application validation must check page count and contiguous numbering.

The OpenAPI GalleryListItem schema does not contain a pages array. Listing metadata alone is insufficient for the reader. The probe did not make an additional live listing request; this conclusion is from the retrieved OpenAPI schema plus live detail responses.

Image extension is derived from the final filename suffix in the supplied path. Do not assume one format for every page or reconstruct paths from media_id/page count alone. Thumbnail suffixes can differ, including `1t.jpg.webp`; do not derive full-page extension from thumbnail names.

## Sample results

| Gallery ID | media_id | Descriptor count | First-page path | i1 status | Bytes |
|---|---|---:|---|---:|---:|
| 686333 | 4227024 | 21 | galleries/4227024/1.webp | 200 | 511988 |
| 686332 | 4227005 | 30 | galleries/4227005/1.webp | 200 | 238988 |
| 686331 | 4226996 | 39 | galleries/4226996/1.webp | 200 | 437490 |
| 686330 | 4226989 | 56 | galleries/4226989/1.webp | 200 | 309018 |
| 686329 | 4226979 | 218 | galleries/4226979/1.jpg | 200 | 421730 |

All 364 descriptors across these five live details passed checks for contiguous numbering, count, media-path prefix, supported suffix, and positive metadata dimensions. This is not a full security-validator test.

First-page signatures were four WebP and one JPEG. Parsed WebP header dimensions matched the metadata for all four first pages. The JPEG signature was checked but its dimensions were not independently decoded. No complete decoder, animation, malformed-file, or pixel-budget testing was performed by this source probe; these remain implementation acceptance requirements.

## CDN policy selected for initial implementation

Approved full-page origin: `https://i1.nhentai.net`, port 443/default.

Approved single retry origin: `https://i2.nhentai.net`, port 443/default, only under the bounded retry policy in the consolidated spec. Both served identical bytes for the cross-host sample. i3 and i4 also returned matching bytes for that one sample but are not needed in the initial allowlist.

Construct full-page URL as the approved origin plus `/` plus the validated upstream `pages[*].path`. No hash-based host routing. Cross-host equivalence for one image does not demonstrate universal interchangeability. A failed primary plus failed alternate surfaces an error without trying arbitrary additional hosts.

Automatic redirects remain disabled. Redirects, if encountered at runtime, must pass per-hop validation and remain on the initial approved image-host allowlist. The live probe did not exercise redirect behavior.

Thumbnail CDN hosts were not tested. Initial v2.0 browse does not require thumbnails; do not approve t1..t4 from this evidence. Thumbnail support requires a separate bounded verification before use.

## Existing metadata and migration

At inspection, 100 stored detail records existed; all 100 had a root pages list with length matching num_pages. The five sampled local records also had the same required page fields used in the live responses. The existing Catalog preserves full raw details when supplied by MetadataClient, despite the v1 typed GalleryDetail model ignoring additional page fields.

Phase C migration should first parse and validate existing raw_json pages into pages_json/readable without network. Do not require a blanket metadata refresh. Missing/invalid descriptors remain non-readable and may use the bounded refresh command. Preserve valid source fields and existing embedding contracts; page projection alone does not require re-embedding.

## Limits and decision

This gate establishes the sampled default detail shape, working primary image URL construction, and one bounded alternate host. It does not test every image, PNG/JPEG decoder dimensions, thumbnails, all host/page combinations, access failures, cache correctness, or reader behavior.

Decision: Phase C may begin implementation against this verified adapter contract. Its functional/security/cache acceptance tests still must pass before release. Upstream changes require revalidation; preserve controlled failures rather than inventing new paths or relaxing allowlists.
