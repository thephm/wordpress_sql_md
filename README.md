# wordpress_sql_md

Convert published posts from a WordPress SQL dump into Markdown files using the `hal_md` post template. Posts are attributed to one configured person and can be linked to people in `People.json`.

This converter is intended for personal blogs with a single author. Every post is attributed to the slug supplied with `-m`; posts written by other authors are not handled.

## Requirements

- Python 3.9 or newer. The converter uses the Python standard library.
- The sibling `hal_md` checkout, which supplies `templates/Post.md`.
- `curl` on `PATH` for URL checks and media downloads. Use `--skip-links` for an offline conversion that does not need `curl`.
- A WordPress SQL dump containing the supported tables. No MySQL server is needed; the converter parses the dump directly.
- A configuration folder containing `People.json` (or `people.json`) and, optionally, `settings.json`.

The SQL dump is decoded as UTF-8 first, with optional BOM support. If strict UTF-8 decoding fails, the entire dump is decoded as Windows-1252 (cp1252). Bytes undefined in Windows-1252 are replaced with the Unicode replacement character and reported as a warning.

When INSERT statements omit column names, the converter uses the dump's `CREATE TABLE` definitions to map values, allowing for differences between schemas. Taxonomies are read from either the legacy `wp_categories`/`wp_post2cat` tables or the modern `wp_terms`/`wp_term_taxonomy`/`wp_term_relationships` tables.

### WSL setup

From the repository directory, create and use the ignored WSL virtual environment:

```bash
python3 -m venv .venv-wsl
.venv-wsl/bin/python wordpress_sql_md.py <file.sql> -c <config-folder> -m <me-slug>
```

The tool has no additional Python package dependencies. `.venv-wsl/` is excluded from Git.

## Usage

```text
.venv-wsl/bin/python wordpress_sql_md.py <file.sql> -c <config-folder> -m <me-slug> [options]
```

The converter reads `settings.json` from the configuration folder; command-line values take precedence. Configure the output folder, media subfolder, and image embedding options there, or override the output folder with `-o`.

| Option | Description |
| --- | --- |
| `<file.sql>` | WordPress SQL dump to convert (required). |
| `-m <me-slug>` | Your slug in `People.json`; added to every post's `people` list. |
| `--skip-links` | Skip URL checks, Wayback lookups, and media downloads. |
| `-c <folder>` | Configuration folder containing `People.json` and optional `settings.json`. |
| `-s <folder>` | Source folder used to resolve a relative SQL dump path. |
| `-o <folder>` | Override the configured output folder. |
| `-d` | Print debug diagnostics. |
| `--version <mode>` | Taxonomy schema: `auto` (default), `legacy`, or `modern`. Auto mode reads whichever supported taxonomy tables are present. |

## Converted content

Only published rows with an empty or `post` post type are converted.
Attachments are not written as posts; they may instead be downloaded when referenced by a post.
Static pages, drafts, private posts, users, links, and WordPress options other
than `siteurl` are out of scope.

Each post is written under `<output-folder>/<people-subfolder>/<my-slug>/`
(by default, `<output-folder>/People/bernie/` for `-m bernie`) as
`YYYY-MM-DD <post_title>.md`, preserving title capitalization and spaces.
Media goes in that person's `media-subfolder`, so relative media links still
work. `-o` overrides the output root, not the person folder. The link cache
stays at the output root, preserving existing cache results on reruns.
Windows-invalid filename characters and trailing dots/spaces are removed.
Empty cleaned titles or case-insensitive filename collisions are reported as
errors rather than silently overwriting posts. Its frontmatter contains:

- `tags`: `post`, category nicenames and (for modern schemas) post-tag slugs, excluding `uncategorized`.
- `url`: the site's `/?p=<ID>` URL.
- `date` and `time`: the stored `post_date` value, with no GMT conversion.
- `people`: the `-m` slug and any matched people.

Frontmatter `people` contains plain slugs (for example, `  - bernie`), not
wikilinks. Matched comment authors still use `[[slug]]` in the body.
Ordinary frontmatter strings are unquoted; values needing YAML protection,
including dates, times, booleans and numeric-looking strings, remain quoted.
- `service`: `wordpress`.

The body starts with the post title and converts the post's HTML to Markdown. WordPress-style paragraph and line-break behavior is applied, and `<!--more-->` markers are removed. Approved comments with a blank `comment_type` are appended in date order. Commenter names can link to `People.json` entries; comment author URLs are validated like other links.

Comment emails and IP addresses are never written to Markdown files.

Comment headings use an ASCII hyphen between the author and date, for example `1. **domenic** - 2006-05-17`, not an em dash.

Comment text beneath each numbered author/date heading is a blockquote. Prefix each content line with two spaces and `> `; blank lines within a comment use `  >` to keep paragraphs in the same quote.

Rendered post and comment bodies use only one empty line between paragraphs. Runs of blank lines, including lines containing spaces or tabs, are collapsed to one; explicit HTML line breaks are preserved.

## People

People are read from `People.json`; it is not modified. Matching uses case-insensitive whole-word matches against a person's full name, `profile_full_name`, or optional `aliases`. Entries marked `ignore` are skipped.

Approved commenters are matched first by exact email against the person's `emails` list, ignoring case and surrounding whitespace. If no email matches, name/alias matching is used. This same result controls comment wikilinks, frontmatter people, and omission from the missing-commenter report. Emails are never written to Markdown.

First-name-only matches produce no suggestions or links. Explicit full-name and alias matches are still linked. Unmatched approved commenters are reported at the end of the run.

## Links and media

Known tracking and session parameters (such as `MSCSProfile`, `utm_*`, and
`fbclid`) are removed before validation and output. Functional parameters are
preserved, including YouTube `v`, `list`, and `t`, product IDs, and WordPress `p`.
Content links to published posts being converted become local wikilinks such as
`[[2005-03-14 Post Title|link text]]`; frontmatter retains the external `?p=<ID>` URL.

By default, URLs in post links, images, and written-comment author links are checked with `curl`. Checks follow redirects, use a 3-second timeout per validation request, retry once, and are cached for subsequent runs. Dead URLs are checked against the Wayback Machine using the post's date. When available, snapshots replace dead link or image URLs; otherwise, links remain at their cleaned URL with a `(dead)` marker, and images use backup-ready local embeds as described below. Media downloads and Wayback API requests retain their separate 10-second timeouts.

Live images and attachments referenced in posts are downloaded to the configured media subfolder. Images are embedded as `![[media/<file>]]`, optionally with the configured image width. A link to a live attachment points to its downloaded local copy; other live links remain external. If an anchor wraps an image that points to the same file, only the image embed is emitted.

With `--skip-links`, tracking/session cleanup and local post-link rewrites still
apply. There are no dead-link markers, Wayback substitutions, or downloads, and
the link cache and deferred Wayback list are not read or written.

## Run output

Wayback requests are normally paced at least one second apart after completion.
On HTTP 429, the converter moves on without waiting or retrying that lookup.
Further lookups during `Retry-After` (or a five-second fallback) are deferred
without making a request. Failed API lookups and those skipped during cooldown
are saved immediately to the link cache as pending, not as missing snapshots.
A deduplicated report, `wordpress_sql_md_wayback_pending.json`, is saved at the
output root with each URL, post date, and reason; its count and location are
printed at the end. The link cache is the authoritative pending state.
Rerunning the conversion with link checks enabled retries pending lookups as
their URLs are encountered, removing them from the report after a successful
API response (whether or not a snapshot exists). A deferred URL/date pair is
not retried again in the same run. Legacy negative archive cache entries are
also retried because they may represent earlier failures.

Images that cannot be downloaded, including dead images without a Wayback
snapshot, use `![[<my-slug>/<media-subfolder>/<original-filename>]]`.
For example, `http://www.whoyouknow.ca/images/MixAndBurn2.JPG` becomes
`![[bernie/media/MixAndBurn2.JPG]]`. Filename case is preserved, URL escapes
are decoded, and Windows-invalid filename characters are replaced.
There is no `(dead)` marker, alt text, or width suffix, regardless of image
embedding settings. A surrounding image-only link is omitted.
No placeholder file is created: restore the matching image from backups into
the person's media folder and Obsidian can resolve the embed automatically.
Download failures are logged. Downloaded images, Wayback images, and
`--skip-links` output retain their existing behavior.

Hostname-resolution failures are remembered immediately in the link cache.
After curl reports an unresolved hostname, other URLs on that exact hostname
skip validation, including on reruns, and only one warning is printed when it
is first recorded. Subdomains are tracked separately. Wayback lookups still
run for individual dead URLs. Delete the link cache to recheck remembered
hosts; `--skip-links` does not read or write the cache.

At completion, missing approved commenters are listed by name and email, once per unique name/email pair; commenters matched to `People.json` and the `-m` person are omitted. Emails appear only in this console output, never in generated Markdown.
No report file is produced.