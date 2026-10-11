# wordpress\_sql\_md - Specification

Oct 11, 2026 · @Bob

## Purpose and scope

`wordpress_sql_md` converts the published posts in an old WordPress SQL dump into Markdown files based on the hal\_md `templates/Post.md` template, linking people through `People.json`. The sample dump (`berniejansen_com_blog_2007-02-19.sql`) is a phpMyAdmin export from Feb 2007 of a WordPress 2.0.x database (db\_version 3437) on MySQL 4.0, holding 111 published posts dated 2004-10-02 to 2007-01-28.

It is built for personal blogs with a single author: every post is attributed to the `-m` slug.

**Out of scope:** attachment rows as posts, the static page, private and draft posts, users and password hashes, the `wp_links` blogroll, and all `wp_options` rows except `siteurl` and `db_version`.

## Input and parsing

The input is one `.sql` dump, passed as a positional argument. The tool parses the `INSERT` statements itself; no MySQL server is needed.

- **Tables read:** `wp_posts`, `wp_comments`, `wp_categories`, `wp_post2cat`, `wp_postmeta` and `wp_options` (for `siteurl`).
- **Escaping:** the parser handles `''` and backslash escapes, literal `\r\n`, and values that span lines (comments do).
- **Encoding:** try strict UTF-8 first, accepting and removing an optional UTF-8 BOM. If decoding fails, fall back to cp1252 for the entire dump. In the cp1252 fallback, undefined bytes are replaced with � and reported as a warning instead of aborting. The sample dump uses this fallback. UTF-8 input support does not extend support to newer WordPress schemas.

* **INSERT styles:** both one row per `INSERT` (as in the sample dump) and extended inserts, with many `(...)` tuples in one `VALUES` list, are supported. With a column list (`INSERT INTO t (a, b) VALUES ...`) values are mapped by column name; without one they are mapped by the column order in the dump's own `CREATE TABLE` statement. `INSERT` statements for tables the tool does not read are skipped.

**WordPress version.** The dump has no `wp_version` string, so the tool reads `db_version` from `wp_options`. The sample dump has `db_version = 3437`, which the WordPress changeset history ties to the early 2.0.x series (2.0.1 or later); the exact point release cannot be determined from the number alone. The tool is written and tested only against this schema: `wp_categories` and `wp_post2cat`, no `wp_terms`, no WordPress tags.

At startup the tool prints one line, for example `WordPress db_version 3437`. It warns, and continues, if `db_version` is missing or if a `wp_terms` table exists, because later schemas store categories and tags differently and the tool does not handle them.

## Selection and output files

Only posts with `post_status = 'publish'` are converted (111). The dump also holds 12 attachments, 1 static page, 1 private post and 1 draft, none of which are converted. `post_type` is empty throughout, so `post_status` is the only discriminator.

Each post becomes one file named `YYYY-MM-DD <post_title>.md`, preserving the original title's capitalization (including CamelCase) and spaces rather than using the hyphenated `post_name`. Remove Windows-invalid characters (`< > : " / \ | ? *` and control characters), trim surrounding whitespace and remove trailing dots/spaces. Report an error before writing posts if a cleaned title is empty or filenames collide case-insensitively; do not silently overwrite posts. Files go under `<output-folder>/<people-subfolder>/<my-slug>/` (default `People/<my-slug>`).

## Frontmatter

The fields follow `Post.md`, plus a new `time` field.

Write frontmatter string values without quotes when safe (including ordinary
slugs, tags, service names and URLs). Quote values that could be interpreted
as YAML syntax or non-string types, including booleans, null, numbers, dates
and times. `date` and `time` remain quoted to preserve their string values.

| Field | Source |
| --- | --- |
| `tags` | `post` plus the category nicenames from `wp_post2cat` and `wp_categories`, excluding `uncategorized` (case-insensitive) |
| `url` | `siteurl` + `/?p=<ID>`, preserving the functional `p` parameter (`permalink_structure` is empty and `guid` is unset). Frontmatter retains the original external post URL, not a local wikilink. |
| `date` | `post_date`, as `YYYY-MM-DD` |
| `time` | `post_date`, as `HH:MM` in 24 h with no seconds |
| `people` | YAML list of plain slugs, e.g. `people:` followed by `  - bernie`: the `-m` slug plus matched people. Do not wrap frontmatter slugs in wikilinks; quote only when needed for YAML safety. |
| `service` | `wordpress` |

`date` and `time` both use the stored `post_date` wall-clock value as-is, with no GMT conversion and no `tzlocal` dependency. `Post.md` needs a `time` line added after `date`; check the raw template before editing it.

## Body

The body starts with `# <post_title>`, followed by `post_content` converted from HTML to Markdown.

- WordPress auto-paragraphing is applied: blank lines become paragraphs and single newlines become line breaks.
- `<!--more-->` is dropped (10 posts use it).
- Links and images are handled as described under Links and attachments.

## Categories and comments

Categories become `tags` in the frontmatter (see Frontmatter), except for `uncategorized`, which is omitted case-insensitively. The `post` tag is always retained, even when no other categories remain. The dump has no WordPress tags.

Approved comments are appended under a `## Comments` heading.

- **Included:** `comment_approved = '1'` and a blank `comment_type`.
- **Excluded:** everything else of the 463 comments: spam (384), pending (3), and the 1 approved trackback and 2 approved pingbacks. That leaves 73 comments written.
- **Format:** ordered by `comment_date`; each entry shows the author (linked to `comment_author_url` when present, subject to link validation), the date, and the text converted from HTML to Markdown. Separate the author and date with an ASCII hyphen, not an em dash: `1. **domenic** - 2006-05-17`.
- **Privacy:** emails and IP addresses are never written to the Markdown files.
- **Comment quoting:** beneath each numbered author/date heading, prefix every rendered comment content line with `  > ` (two spaces, a greater-than sign and a space). Use `  >` for blank lines within the comment so multiple paragraphs remain in the same blockquote. The numbered author/date heading is not quoted.
- **Blank lines:** rendered post and comment bodies have only one empty line between paragraphs. Collapse consecutive blank lines, including whitespace-only lines, to one empty line while preserving explicit HTML line breaks.
- **People:** a commenter who matches `People.json` is added to the post's `people` and linked as `[[slug]]` in the comment.

## People

People come from `People.json` (fields per the message\_md guide: `slug`, `first_name`, `last_name`, `profile_full_name`, `ignore`). This tool adds an optional `aliases` field, a list of nicknames, which needs a change to the `Person` class in the hal repo (`person.py`). `People.json` is never modified by the tool.

| Tier | Match | Result |
| --- | --- | --- |
| Linked | Exact, case-insensitive match on word boundaries against `first_name last_name`, `profile_full_name` or any `aliases` entry, in the title, body and approved-comment authors. Entries with `ignore` set are skipped. | Plain `slug` in frontmatter `people`; matched comment authors still use `[[slug]]` in the body |
| Unmatched | First-name-only match | No suggestions or links. Explicit full-name and alias matching still applies. |
| Missing | A commenter not in `People.json` | Listed once at the end of the run (see End-of-run output) |

The `-m` slug is always added to `people` on every post. The blog author's own comments are never listed as missing.

For approved-comment authors, match `comment_author_email` first against each person's optional `emails` list of strings, using exact equality after trimming whitespace and ignoring case. Blank emails do not match. Skip ignored people. If no email matches, fall back to the name/alias rule above. Use this same email-first result for comment wikilinks, frontmatter `people`, and the missing-commenter report; a matching email takes precedence over a conflicting author name. Emails are never written to Markdown.

## Links and attachments

Links and attachments that still work are kept. Validation runs `curl` from the tool, so it needs network access; `--skip-links` bypasses it.

**URL cleanup**

- Remove only known tracking and session parameters, matching parameter names case-insensitively: `MSCSProfile`, `PHPSESSID`, `JSESSIONID`, `sid`, `sessionid`, `session_id`, `fbclid`, `gclid`, `dclid`, `msclkid`, and names starting with `ASPSESSIONID` or `utm_`.
- Preserve all other query parameters, including `product`, `category`, `catalog`, `language`, WordPress `p` and `cat`. Preserve the scheme, host, path and fragment. Do not remove the entire query string merely because validation fails.
- YouTube video, playlist and time parameters (`v`, `list`, `t`) are functional and must be preserved.
- This applies to frontmatter URLs, post and comment content links and images, attachment URLs, comment author URLs, redirect destinations and archive snapshot URLs written to Markdown.
- Cleanup happens before validation, cache lookup, downloads and Wayback lookup. Use the cleaned URL in generated Markdown, cache keys and diagnostic messages.
- For example, `http://www.thesourcecc.com/estore/Product.aspx?language=en-CA&catalog=Online&category=MemoryCardReaders&product=4418965&MSCSProfile=...` becomes `http://www.thesourcecc.com/estore/Product.aspx?language=en-CA&catalog=Online&category=MemoryCardReaders&product=4418965`.
- Cleanup applies even with `--skip-links`. It does not remove the parameters required to call the Wayback availability API; the API's `url` argument contains the cleaned source URL.

**Collection**

- Attachments: the 12 `post_status = 'attachment'` rows. The path after wp-content/uploads/ comes from `_wp_attached_file` in `wp_postmeta` (the stored value is an absolute server path), and the URL is `siteurl/wp-content/uploads/<relative path>`.
- Links and images: every `<a href>` and `<img src>` in the 111 post bodies, including the `wp-content/uploads` references.
- Comment author URLs on the written comments (comment\_author\_url) are validated like any other link.

**Validation**

- `HEAD` request, falling back to `GET` on 403 or 405. Follow redirects, 3 s timeout per request, one retry.
- The 3 s limit applies to link validation. Media downloads and Wayback API requests have separate 10 s timeouts.
- Pace Wayback API requests globally, waiting at least 1 s after each request finishes. On HTTP 429, respect `Retry-After` (seconds or HTTP date), or wait 5 s if it is absent or invalid, then retry once. If the retry is also rate-limited, retain the cooldown for the next lookup. Warn explicitly on API failures and never cache them as "no snapshot." Cache negative results only after a valid successful availability response. Recheck legacy negative cache entries that cannot distinguish API failures from successful empty responses; retain positive entries.
- A URL is live if the final status is 2xx. For attachments and images, a `Content-Type` of `text/html` counts as dead, which catches parked domains and soft 404s.
- Throttle per host and cache results in a JSON file so reruns skip checked URLs.
- If curl reports a hostname-resolution failure (exit code 6), mark that exact hostname unresolved in the JSON cache immediately. Do not retry DNS failures; skip further validation of URLs on that hostname, across paths, HTTP/HTTPS and ports, both during the run and on reruns. Print one warning when recording a newly unresolved hostname, not one warning per URL. Subdomains are tracked independently; failure of `pics.nakama.ca` does not prove that `nakama.ca` or another subdomain is unavailable.
- Cached DNS failures still receive the normal per-URL Wayback lookup and dead-link/image handling. HTTP errors, timeouts, connection failures and proxy-resolution failures do not mark the entire hostname unresolved. Delete the link cache to recheck remembered hosts. `--skip-links` neither reads nor writes these records.

**Outcomes**

- **Links to converted posts:** after URL cleanup and resolving relative URLs against `siteurl`, a link to the blog's own `/?p=<ID>` becomes `[[YYYY-MM-DD cleaned post_title|link text]]` when that ID identifies a published post being converted. The wikilink target uses exactly the generated title-based filename without `.md`; for example, `[[2005-03-14 Post Title|link text]]`. A fragment is preserved as `[[YYYY-MM-DD cleaned post_title#fragment|link text]]`. These links need no network validation or Wayback lookup. Links to other sites or unconverted posts remain external and follow normal link handling. This rewrite applies to post and comment content, not frontmatter.
- **Live attachments and images** are downloaded to the `media-subfolder` under their original filenames, with collisions suffixed, and embedded as Obsidian `![[media/<file>]]` (with `|<width>` when `image-width` is set).
- **Live links** keep their cleaned URLs. A permanent redirect (301) is rewritten to the cleaned final URL.
- **Dead links** are looked up in the Wayback Machine availability API (`archive.org/wayback/available`), passing the post's `date` as the `timestamp` and the cleaned source URL. If a snapshot exists the link becomes `[text](archive_url)` with a cleaned snapshot URL. If none exists it stays `[text](cleaned_url) (dead)`.
- **Unavailable images** use the cleaned Wayback snapshot as the image URL when one exists. If no snapshot exists, or a live image download fails, write a backup-ready Obsidian embed `![[<my-slug>/<media-subfolder>/<original-filename>]]`, for example `![[bernie/media/MixAndBurn2.JPG]]`. Derive the filename from the cleaned original image URL path, decoding URL escapes, preserving case, replacing Windows-invalid characters and trimming trailing dots/spaces as for downloaded media. Do not add alt text, width, or `(dead)`, regardless of image embedding settings. Create no placeholder file: restoring the matching image into the person's media folder should resolve the embed. Log download failures explicitly. If an anchor contains only that unavailable image and whitespace, omit the surrounding anchor even if it points to a different URL. Downloaded images and available Wayback snapshots retain their existing behavior. This fallback does not apply to `--skip-links`.
- **`<a href>` to a live attachment** (a file under `wp-content/uploads`, such as a PDF) links to the downloaded local copy as `[[media/<file>|link text]]`, with no `!`, so it is a link and not an embed. Only `<img src>` is embedded with `![[...]]`. When an `<a href>` wraps an `<img>` and both point to the same file, only the embed is written. Live links to other sites stay external and are not downloaded.

**`--skip-links`**

Skips all curl and Wayback checks. Known tracking and session parameters are still removed and links to converted posts are still rewritten locally. Other links and images retain their cleaned source URLs, with no `(dead)` markers, no archive substitutions and no downloads. The cache file is neither read nor written.

## CLI and configuration

The tool follows the pattern of the other converters (`linkedin_md`, `email_md`): it calls `message_md.setup()` and `message_md.get_markdown()`, reads `settings.json`, and lets command-line values override it. The code adds the sibling `hal` and `message_md` repos to `sys.path`.

| Option | Purpose |
| --- | --- |
| `<file.sql>` | Positional: the SQL dump to convert |
| `-m <me-slug>` | Overrides `my-slug`; identifies you in `People.json` |
| `--skip-links` | Skip link validation, Wayback lookups and downloads, for offline runs |
| `-c`, `-s`, `-o`, `-d` and the other message\_md options | Config, source and output folders and debug, as in the other converters (listed in the message\_md repo) |

Settings reused from message\_md: `output-folder`, `people-subfolder` (default `People`), `media-subfolder`, `image-embed` and `image-width`.

**Output layout:** posts go under `<output-folder>/<people-subfolder>/<my-slug>/`,
for example `<output-folder>/People/bernie/`. The `-m` override determines
the person folder, and `-o` overrides only the output root. Media is stored
under that person's `media-subfolder`, keeping `media/<file>` references
relative to the posts. The JSON link cache remains at the output root so
previously cached URL and DNS results are reused. Existing files written at
the output root are not moved or deleted automatically.

## End-of-run output and README

There is no report file. At the end of the run the tool prints this block and nothing else:

```
Missing people:
<name> <email>
<name> <email>
```

- Names and emails come from `comment_author` and `comment_author_email` on the written comments, de-duplicated by name and email.
- Anyone who matches `People.json`, and the `-m` person, is left out.
- A comment with no email prints the name only.
- If nobody is missing, the block is not printed.
- Emails appear only in this console output, never in the Markdown files.
- First-name suggestions are not printed, including in debug mode. `-d` adds debug messages.

**README note:** the README states that the tool is for personal blogs with a single author, that every post is attributed to the `-m` slug, and that posts by other authors are not handled.

**README, supported versions:** this tool has only been tested against a WordPress 2.0.x database dump (`db_version` 3437, a MySQL 4.0 phpMyAdmin export). WordPress 2.3 and later moved categories and tags into `wp_terms`, so dumps from those versions are not supported. The tool prints the `db_version` it finds so you can check your dump.
