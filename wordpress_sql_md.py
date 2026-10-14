#!/usr/bin/env python3
"""Convert published posts from a WordPress SQL dump to Markdown."""

from __future__ import annotations

import argparse
import html
from html.parser import HTMLParser
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import (
    parse_qs, quote, unquote, unquote_plus, urlencode, urljoin, urlparse,
    urlsplit, urlunsplit,
)
from urllib.request import Request, urlopen


INSERT_RE = re.compile(
    r"\bINSERT\s+(?:(?:LOW_PRIORITY|DELAYED|HIGH_PRIORITY)\s+)?INTO\s+"
    r"(?:(?:`[^`]+`|[A-Za-z0-9_$]+)\s*\.\s*)?`?([A-Za-z0-9_$]+)`?",
    re.IGNORECASE,
)
BLOCK_TAGS = {"address", "article", "aside", "blockquote", "div", "dl",
              "fieldset", "footer", "form", "h1", "h2", "h3", "h4", "h5",
              "h6", "header", "hr", "main", "nav", "ol", "p", "pre",
              "section", "table", "ul"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}
LINK_TIMEOUT_SECONDS = 3
TRACKING_PARAMETERS = {
    "mscsprofile", "phpsessid", "jsessionid", "aspsessionid", "sid",
    "sessionid", "session_id", "fbclid", "gclid", "dclid", "msclkid",
}

SUPPORTED_TABLE_COLUMNS = {
    "wp_posts": [
        "ID", "post_author", "post_date", "post_date_gmt", "post_content",
        "post_title", "post_category", "post_excerpt", "post_lat", "post_lon",
        "post_status", "comment_status", "ping_status", "post_password",
        "post_name", "to_ping", "pinged", "post_modified",
        "post_modified_gmt", "post_content_filtered", "post_parent", "guid",
        "menu_order", "post_mime_type",
    ],
    "wp_comments": [
        "comment_ID", "comment_post_ID", "comment_author",
        "comment_author_email", "comment_author_url", "comment_author_IP",
        "comment_date", "comment_content", "comment_karma", "comment_approved",
        "comment_agent", "comment_type", "comment_parent",
    ],
    "wp_categories": [
        "cat_ID", "cat_name", "category_nicename", "category_description",
        "category_parent",
    ],
    "wp_post2cat": ["rel_id", "post_id", "category_id"],
    "wp_terms": ["term_id", "name", "slug", "term_group"],
    "wp_term_taxonomy": [
        "term_taxonomy_id", "term_id", "taxonomy", "description", "parent", "count",
    ],
    "wp_term_relationships": ["object_id", "term_taxonomy_id", "term_order"],
    "wp_postmeta": ["meta_id", "post_id", "meta_key", "meta_value"],
    "wp_options": [
        "option_id", "blog_id", "option_name", "option_can_override",
        "option_value",
    ],
}


@dataclass
class PersonName:
    slug: str
    first_name: str = ""
    last_name: str = ""
    profile_full_name: str = ""
    aliases: list[str] = field(default_factory=list)
    ignore: bool = False
    emails: list[str] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        return [
            name.strip()
            for name in (
                f"{self.first_name} {self.last_name}",
                self.profile_full_name,
                *self.aliases,
            )
            if name and name.strip()
        ]


@dataclass
class Comment:
    author: str
    email: str
    url: str
    date: str
    content: str


@dataclass
class Post:
    id: str
    title: str
    slug: str
    date: str
    time: str
    content: str
    categories: list[str] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)


@dataclass
class HtmlNode:
    tag: str
    attrs: dict[str, str]
    children: list[HtmlNode | str] = field(default_factory=list)


class HtmlTreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = HtmlNode("root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = HtmlNode(tag.lower(), {key.lower(): value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag.lower() not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag.lower():
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(data)


def _decode_sql_string(value: str) -> str:
    translations = {
        "0": "\0", "b": "\b", "n": "\n", "r": "\r", "t": "\t",
        "Z": "\x1a",
    }
    output: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            index += 1
            escaped = value[index]
            output.append(translations.get(escaped, escaped))
        else:
            output.append(char)
        index += 1
    return "".join(output)


def split_sql_statements(text: str) -> list[str]:
    statements: list[str] = []
    start = 0
    quote_char = ""
    backtick = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if escaped:
            escaped = False
        elif quote_char:
            if char == "\\":
                escaped = True
            elif char == quote_char:
                if index + 1 < len(text) and text[index + 1] == quote_char:
                    index += 1
                else:
                    quote_char = ""
        elif backtick:
            if char == "`":
                if index + 1 < len(text) and text[index + 1] == "`":
                    index += 1
                else:
                    backtick = False
        elif char in {"'", '"'}:
            quote_char = char
        elif char == "`":
            backtick = True
        elif char == ";":
            statements.append(text[start:index])
            start = index + 1
        index += 1
    if text[start:].strip():
        statements.append(text[start:])
    return statements


def _parse_columns(prefix: str) -> list[str] | None:
    open_index = prefix.find("(")
    if open_index < 0:
        return None
    close_index = prefix.rfind(")")
    if close_index < open_index:
        return None
    return [
        item.strip().strip("`").strip()
        for item in prefix[open_index + 1:close_index].split(",")
    ]


def _parse_value_list(values: str, index: int) -> tuple[list[Any], int]:
    row: list[Any] = []
    while index < len(values):
        while index < len(values) and values[index].isspace():
            index += 1
        if index >= len(values):
            break
        if values[index] == ")":
            return row, index + 1

        if values[index] in {"'", '"'}:
            quote_char = values[index]
            index += 1
            raw: list[str] = []
            while index < len(values):
                char = values[index]
                if char == "\\" and index + 1 < len(values):
                    raw.extend((char, values[index + 1]))
                    index += 2
                    continue
                if char == quote_char:
                    if index + 1 < len(values) and values[index + 1] == quote_char:
                        raw.append(quote_char)
                        index += 2
                        continue
                    index += 1
                    break
                raw.append(char)
                index += 1
            row.append(_decode_sql_string("".join(raw)))
        else:
            start = index
            while index < len(values) and values[index] not in {",", ")"}:
                index += 1
            raw_value = values[start:index].strip()
            if raw_value.upper() == "NULL":
                row.append(None)
            else:
                row.append(raw_value)

        while index < len(values) and values[index].isspace():
            index += 1
        if index < len(values) and values[index] == ",":
            index += 1
    raise ValueError("Malformed SQL INSERT row: missing closing parenthesis")


def parse_insert(
    statement: str, schemas: dict[str, list[str]] | None = None
) -> tuple[str, list[dict[str, Any]]] | None:
    match = INSERT_RE.search(statement)
    if not match:
        return None
    table = match.group(1).lower()
    if table not in SUPPORTED_TABLE_COLUMNS:
        return None
    values_match = re.search(r"\bVALUES?\b", statement[match.end():], re.IGNORECASE)
    if not values_match:
        return None
    values_start = match.end() + values_match.end()
    prefix = statement[match.end():match.end() + values_match.start()]
    columns = (
        _parse_columns(prefix)
        or (schemas or {}).get(table)
        or SUPPORTED_TABLE_COLUMNS.get(table, [])
    )
    values = statement[values_start:].strip().removesuffix(";")
    rows: list[dict[str, Any]] = []
    index = 0
    while index < len(values):
        while index < len(values) and (values[index].isspace() or values[index] == ","):
            index += 1
        if index >= len(values):
            break
        if values[index] != "(":
            raise ValueError(f"Malformed SQL INSERT for {table}: expected '('")
        row, index = _parse_value_list(values, index + 1)
        if (_parse_columns(prefix) or table in (schemas or {})) and len(row) != len(columns):
            raise ValueError(
                f"SQL INSERT for {table} has {len(row)} values but "
                f"its column definition has {len(columns)} columns"
            )
        if columns:
            rows.append({
                column: row[position] if position < len(row) else None
                for position, column in enumerate(columns)
            })
    return table, rows


def parse_dump(path: Path) -> dict[str, list[dict[str, Any]]]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
        replacement_count = text.count("\ufffd")
        if replacement_count:
            logging.warning(
                "Replaced %d byte(s) that are undefined in Windows-1252 while reading %s",
                replacement_count,
                path,
            )
    tables: dict[str, list[dict[str, Any]]] = {}
    statements = split_sql_statements(text)
    schemas: dict[str, list[str]] = {}
    for statement in statements:
        definition = re.search(
            r"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
            r"`?([A-Za-z0-9_$]+)`?\s*\(",
            statement,
            re.IGNORECASE | re.MULTILINE,
        )
        if definition and definition.group(1).lower() in SUPPORTED_TABLE_COLUMNS:
            table = definition.group(1).lower()
            columns = re.findall(
                r"^\s*`([^`]+)`\s+\w+", statement[definition.end():],
                re.MULTILINE,
            )
            if not columns:
                raise ValueError(f"No columns found in CREATE TABLE {definition.group(1)}")
            schemas[table] = columns
            tables.setdefault(table, [])
    for statement in statements:
        parsed = parse_insert(statement, schemas)
        if parsed:
            table, rows = parsed
            tables.setdefault(table, []).extend(rows)
    return tables


def _value(row: dict[str, Any], name: str, default: str = "") -> str:
    value = row.get(name, default)
    return default if value is None else str(value)


def _person_from_json(data: dict[str, Any]) -> PersonName:
    first_name = str(data.get("first_name", data.get("first-name", "")) or "")
    last_name = str(data.get("last_name", data.get("last-name", "")) or "")
    aliases = data.get("aliases", [])
    if not isinstance(aliases, list):
        raise ValueError(f"Person aliases must be a list for slug {data.get('slug', '')!r}")
    emails = data.get("emails", [])
    if not isinstance(emails, list) or any(not isinstance(email, str) for email in emails):
        raise ValueError(f"Person emails must be a list of strings for slug {data.get('slug', '')!r}")
    return PersonName(
        slug=str(data.get("slug", "")),
        first_name=first_name,
        last_name=last_name,
        profile_full_name=str(
            data.get("profile_full_name", data.get("profile-full-name", "")) or ""
        ),
        aliases=[str(alias) for alias in aliases if alias],
        emails=[email.strip().casefold() for email in emails if email.strip()],
        ignore=data.get("ignore", False) is True
        or str(data.get("ignore", "")).casefold() in {"1", "true", "yes"},
    )


def load_people(config_folder: Path) -> list[PersonName]:
    people_file = next(
        (entry for entry in config_folder.iterdir()
         if entry.is_file() and entry.name.casefold() == "people.json"),
        None,
    )
    if people_file is None:
        raise FileNotFoundError(f"People.json not found in {config_folder}")
    raw_people = json.loads(people_file.read_text(encoding="utf-8"))
    if not isinstance(raw_people, list):
        raise ValueError(f"{people_file} must contain a JSON array")
    people = [_person_from_json(item) for item in raw_people if isinstance(item, dict)]
    if any(not person.slug for person in people):
        raise ValueError(f"Every person in {people_file} must have a slug")
    return people


def _whole_word_match(name: str, text: str) -> bool:
    if not name.strip():
        return False
    return re.search(rf"(?<!\w){re.escape(name.strip())}(?!\w)", text, re.IGNORECASE) is not None


def matching_people(people: list[PersonName], text: str) -> list[PersonName]:
    return [
        person for person in people
        if not person.ignore and any(_whole_word_match(name, text) for name in person.names)
    ]


def matching_commenters(people: list[PersonName], comment: Comment) -> list[PersonName]:
    email = comment.email.strip().casefold()
    if email:
        matched = [
            person for person in people
            if not person.ignore
            and any(address.strip().casefold() == email for address in person.emails)
        ]
        if matched:
            return matched
    return matching_people(people, comment.author)


def _wp_autop(content: str) -> str:
    content = content.replace("<!--more-->", "")
    if re.search(r"<(?:p|div|ul|ol|li|h[1-6]|blockquote|pre|table)\b", content, re.I):
        return content
    paragraphs = re.split(r"\r?\n[ \t]*\r?\n+", content.strip())
    converted: list[str] = []
    for paragraph in paragraphs:
        paragraph = re.sub(r"\r?\n", "<br />", paragraph.strip())
        if paragraph:
            converted.append(f"<p>{paragraph}</p>")
    return "\n\n".join(converted)


def _text(children: list[HtmlNode | str]) -> str:
    output = []
    for child in children:
        if isinstance(child, str):
            output.append(child)
        elif child.tag not in {"script", "style"}:
            output.append(_text(child.children))
    return "".join(output)


def _escape_url(url: str) -> str:
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def clean_url(url: str) -> str:
    parts = urlsplit(url.strip())
    parameters = []
    for parameter in parts.query.split("&"):
        name = unquote_plus(parameter.split("=", 1)[0]).casefold()
        if name in TRACKING_PARAMETERS or name.startswith(("utm_", "aspsessionid")):
            continue
        parameters.append(parameter)
    return urlunsplit(parts._replace(query="&".join(parameters)))


class HostResolutionError(RuntimeError):
    def __init__(self, url: str, detail: str) -> None:
        self.hostname = (urlparse(url).hostname or "").casefold()
        super().__init__(f"curl could not resolve {self.hostname}: {detail}")


class LinkChecker:
    def __init__(self, output_folder: Path, skip_links: bool, debug: bool = False) -> None:
        self.skip_links = skip_links
        self.debug = debug
        self.cache_path = output_folder / "wordpress_sql_md_link_cache.json"
        self.pending_path = output_folder / "wordpress_sql_md_wayback_pending.json"
        self.cache: dict[str, Any] = {}
        self.host_last_request: dict[str, float] = {}
        self.wayback_next_request = 0.0
        self.wayback_rate_limit_until = 0.0
        self.wayback_deferred: set[str] = set()
        self.curl = shutil.which("curl")
        if not skip_links and not self.curl:
            raise FileNotFoundError("curl is required to validate links; use --skip-links offline")
        if not skip_links and self.cache_path.exists():
            cached = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if not isinstance(cached, dict):
                raise ValueError(f"Link cache must contain a JSON object: {self.cache_path}")
            self.cache = cached

    def _curl(self, url: str, method: str, follow: bool) -> tuple[int, str, str, str]:
        assert self.curl is not None
        url = clean_url(url)
        command = [self.curl, "-sS", "--max-time", str(LINK_TIMEOUT_SECONDS), "--max-redirs", "10"]
        if method == "HEAD":
            command.append("-I")
        else:
            command.extend(["-X", "GET", "-D", "-", "-o", os.devnull])
        if follow:
            command.append("-L")
        command.extend(["-w", "\\n%{http_code}\\t%{content_type}\\t%{url_effective}\\t%{redirect_url}", url])
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=LINK_TIMEOUT_SECONDS + 2, check=False,
        )
        if result.returncode:
            if result.returncode == 6:
                raise HostResolutionError(url, result.stderr.strip())
            raise RuntimeError(f"curl failed for {url}: {result.stderr.strip()}")
        lines = result.stdout.rsplit("\n", 1)
        if len(lines) != 2:
            raise RuntimeError(f"curl returned no response metadata for {url}: {result.stderr.strip()}")
        fields = lines[1].split("\t")
        if len(fields) != 4:
            raise RuntimeError(f"curl returned malformed response metadata for {url}")
        status = int(fields[0] or 0)
        redirect_url = fields[3]
        if not redirect_url:
            for line in lines[0].splitlines():
                if line.casefold().startswith("location:"):
                    redirect_url = urljoin(url, line.split(":", 1)[1].strip())
        return (
            status, fields[1].split(";", 1)[0].strip().lower(),
            clean_url(fields[2]), clean_url(redirect_url),
        )

    def check(self, url: str) -> dict[str, Any]:
        url = clean_url(url)
        if self.skip_links:
            return {"live": True, "status": 0, "final_url": url, "content_type": ""}
        parsed_url = urlparse(url)
        if parsed_url.scheme.casefold() not in {"http", "https"} or not parsed_url.netloc:
            return {"live": True, "status": 0, "final_url": url, "content_type": ""}
        hostname = (parsed_url.hostname or "").casefold()
        if self.cache.get(f"dns:{hostname}") == {"unresolved": True}:
            return {"live": False, "status": 0, "final_url": url, "content_type": ""}
        cached = self.cache.get(url)
        if isinstance(cached, dict) and "live" in cached:
            return {**cached, "final_url": clean_url(str(cached["final_url"]))}

        host = urlparse(url).netloc.casefold()
        elapsed = time.monotonic() - self.host_last_request.get(host, 0)
        if host and elapsed < 0.25:
            time.sleep(0.25 - elapsed)
        last_error = ""
        for attempt in range(2):
            try:
                current_url = url
                permanent_redirect = False
                for redirect_count in range(11):
                    current_hostname = (urlparse(current_url).hostname or "").casefold()
                    if self.cache.get(f"dns:{current_hostname}") == {"unresolved": True}:
                        result = {"live": False, "status": 0, "final_url": url, "content_type": ""}
                        self.cache[url] = result
                        return result
                    elapsed = time.monotonic() - self.host_last_request.get(
                        urlparse(current_url).netloc.casefold(), 0
                    )
                    if elapsed < 0.25:
                        time.sleep(0.25 - elapsed)
                    status, content_type, effective_url, redirect_url = self._curl(current_url, "HEAD", False)
                    if status in {403, 405}:
                        status, content_type, effective_url, redirect_url = self._curl(current_url, "GET", False)
                    self.host_last_request[urlparse(current_url).netloc.casefold()] = time.monotonic()
                    if not (300 <= status < 400 and redirect_url):
                        break
                    if redirect_count == 10:
                        raise RuntimeError(f"Too many redirects for {url}")
                    permanent_redirect = permanent_redirect or status == 301
                    current_url = clean_url(urljoin(current_url, redirect_url))
                self.host_last_request[host] = time.monotonic()
                result = {
                    "live": 200 <= status < 300,
                    "status": status,
                    "final_url": clean_url(effective_url or current_url) if permanent_redirect else url,
                    "content_type": content_type,
                }
                self.cache[url] = result
                return result
            except HostResolutionError as error:
                self.cache[f"dns:{error.hostname}"] = {"unresolved": True}
                result = {"live": False, "status": 0, "final_url": url, "content_type": ""}
                self.cache[url] = result
                logging.warning(
                    "Could not resolve host %s; skipping further validation on this host "
                    "(remembered in link cache)", error.hostname,
                )
                self.save()
                return result
            except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as error:
                last_error = str(error)
                self.host_last_request[host] = time.monotonic()
                if attempt == 0:
                    time.sleep(0.25)
                    continue
        logging.warning("Could not validate %s after one retry: %s", url, last_error)
        result = {"live": False, "status": 0, "final_url": url, "content_type": ""}
        self.cache[url] = result
        return result

    def _defer_archive(self, url: str, date: str, reason: str) -> None:
        key = f"archive:{date}:{url}"
        self.cache[key] = {
            "pending": True, "source_url": url, "date": date, "reason": reason,
        }
        self.wayback_deferred.add(key)
        self.save()

    def archive(self, url: str, date: str) -> str | None:
        url = clean_url(url)
        if self.skip_links:
            return None
        key = f"archive:{date}:{url}"
        cached = self.cache.get(key)
        if isinstance(cached, dict) and (cached.get("url") or cached.get("checked")):
            return clean_url(cached["url"]) if cached.get("url") else None
        if key in self.wayback_deferred:
            return None
        if time.monotonic() < self.wayback_rate_limit_until:
            self._defer_archive(url, date, "Wayback rate-limit cooldown")
            return None
        self.cache.pop(key, None)
        timestamp = date.replace("-", "")
        query = urlencode({"url": url, "timestamp": timestamp})
        request = Request(
            f"https://archive.org/wayback/available?{query}",
            headers={"User-Agent": "wordpress_sql_md/1.0"},
        )
        wait = self.wayback_next_request - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            with urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict) or not isinstance(
                payload.get("archived_snapshots"), dict
            ):
                raise ValueError("Invalid Wayback availability response")
        except HTTPError as error:
            if error.code == 429:
                retry_after = error.headers.get("Retry-After", "") if error.headers else ""
                delay = 5.0
                if retry_after:
                    try:
                        delay = max(0.0, float(int(retry_after)))
                    except ValueError:
                        try:
                            delay = max(0.0, parsedate_to_datetime(retry_after).timestamp() - time.time())
                        except (ValueError, TypeError, OverflowError):
                            logging.warning("Invalid Wayback Retry-After header: %r", retry_after)
                self.wayback_rate_limit_until = time.monotonic() + max(1.0, delay)
                logging.warning(
                    "Wayback rate-limited %s; deferring without retry "
                    "(lookups during the next %.0f s will also be deferred)",
                    url, max(1.0, delay),
                )
            else:
                logging.warning("Wayback lookup failed for %s: %s", url, error)
            self._defer_archive(url, date, f"HTTP {error.code}")
            return None
        except (URLError, TimeoutError, ValueError, UnicodeDecodeError) as error:
            logging.warning("Wayback lookup failed for %s: %s", url, error)
            self._defer_archive(url, date, str(error))
            return None
        finally:
            self.wayback_next_request = time.monotonic() + 1.0
        snapshot = payload.get("archived_snapshots", {}).get("closest", {})
        snapshot_url = snapshot.get("url") if snapshot.get("available") else None
        if snapshot_url:
            snapshot_url = clean_url(snapshot_url)
        self.cache[key] = {"url": snapshot_url, "checked": True}
        return snapshot_url

    def save(self) -> None:
        if self.skip_links:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.cache_path.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps(self.cache, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        temporary_path.replace(self.cache_path)
        pending = [
            {"url": entry["source_url"], "date": entry["date"], "reason": entry["reason"]}
            for entry in self.cache.values()
            if isinstance(entry, dict) and entry.get("pending") is True
        ]
        if pending or self.pending_path.exists():
            temporary_path = self.pending_path.with_suffix(".tmp")
            temporary_path.write_text(
                json.dumps(pending, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            temporary_path.replace(self.pending_path)


class MediaStore:
    def __init__(self, output_folder: Path, media_subfolder: str, checker: LinkChecker) -> None:
        self.output_folder = output_folder
        self.media_subfolder = Path(media_subfolder)
        self.folder = output_folder / self.media_subfolder
        self.checker = checker
        self.files: dict[str, str] = {}
        self.used_names: set[str] = set()

    @staticmethod
    def is_upload(url: str) -> bool:
        return "/wp-content/uploads/" in urlparse(url).path.casefold()

    @staticmethod
    def filename_for(url: str) -> str:
        name = unquote(Path(urlparse(url).path).name).strip()
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
        return name.rstrip(". ") or "media"

    def _name_for(self, url: str) -> str:
        name = self.filename_for(url)
        candidate = name
        stem, suffix = os.path.splitext(name)
        number = 2
        while candidate.casefold() in self.used_names:
            candidate = f"{stem} ({number}){suffix}"
            number += 1
        self.used_names.add(candidate.casefold())
        return candidate

    def download(self, url: str, final_url: str | None = None) -> str:
        url = clean_url(url)
        source = clean_url(final_url or url)
        key = url
        if key in self.files:
            return self.files[key]
        if self.checker.skip_links:
            return source
        if source not in self.files:
            filename = self._name_for(source)
            self.folder.mkdir(parents=True, exist_ok=True)
            target = self.folder / filename
            curl = shutil.which("curl")
            if not curl:
                raise FileNotFoundError("curl is required to download media")
            result = subprocess.run(
                [curl, "-fsSL", "--max-time", "10", "--retry", "1", "--output", str(target), source],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=22, check=False,
            )
            if result.returncode:
                target.unlink(missing_ok=True)
                raise RuntimeError(
                    f"Could not download media {source}: {result.stderr.strip()}"
                )
            local_reference = (self.media_subfolder / filename).as_posix()
            self.files[source] = local_reference
        self.files[key] = self.files[source]
        return self.files[key]

    def existing(self, url: str) -> str | None:
        return self.files.get(clean_url(url))


def post_file_targets(posts: list[Post]) -> dict[str, str]:
    targets: dict[str, str] = {}
    used: set[str] = set()
    for post in posts:
        title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", post.title).strip().rstrip(". ")
        if not title:
            raise ValueError(f"Post {post.id} has no usable filename after cleaning its title")
        target = f"{post.date} {title}"
        if target.casefold() in used:
            raise ValueError(f"Post title produces a duplicate filename: {target}.md")
        used.add(target.casefold())
        targets[post.id] = target
    return targets


class MarkdownRenderer:
    def __init__(
        self,
        checker: LinkChecker,
        media: MediaStore,
        siteurl: str,
        image_width: Any,
        image_embed: bool,
        posts: list[Post] | None = None,
        me_slug: str = "",
    ) -> None:
        self.checker = checker
        self.media = media
        self.siteurl = clean_url(siteurl).rstrip("/")
        self.image_width = image_width
        self.image_embed = image_embed
        self.me_slug = me_slug
        self.dead_images: set[str] = set()
        self.post_targets = post_file_targets(posts or [])

    def _url_result(self, url: str, post_date: str, media_url: bool) -> tuple[str, bool, bool]:
        url = clean_url(url)
        absolute = clean_url(urljoin(self.siteurl + "/", html.unescape(url.strip())))
        if self.checker.skip_links:
            return url, True, False
        check = self.checker.check(absolute)
        if media_url and check["live"] and check["content_type"] != "text/html":
            return absolute, True, False
        if check["live"]:
            return clean_url(str(check["final_url"])), True, False
        archived = self.checker.archive(absolute, post_date)
        if archived:
            return clean_url(archived), False, True
        return absolute, False, False

    def _image(self, node: HtmlNode, post_date: str) -> str:
        source = clean_url(node.attrs.get("src", ""))
        if not source:
            return ""
        absolute = clean_url(urljoin(self.siteurl + "/", html.unescape(source.strip())))
        if self.checker.skip_links or urlparse(absolute).scheme.casefold() not in {"http", "https"}:
            return f"![{node.attrs.get('alt', '')}]({_escape_url(source)})"
        cached_media = self.media.existing(absolute)
        if cached_media:
            local_path = cached_media
            live = True
        else:
            check = self.checker.check(absolute)
            live = check["live"] and check["content_type"] != "text/html"
            if live:
                try:
                    local_path = self.media.download(absolute, str(check["final_url"]))
                except (RuntimeError, subprocess.TimeoutExpired) as error:
                    logging.warning("%s; using a backup-ready image embed for %s", error, absolute)
                    return self._backup_image(absolute)
            else:
                snapshot = self.checker.archive(absolute, post_date)
                if snapshot:
                    return f"![{node.attrs.get('alt', '')}]({_escape_url(clean_url(snapshot))})"
                return self._backup_image(absolute)
        if self.image_embed:
            suffix = f"|{self.image_width}" if self.image_width else ""
            return f"![[{local_path}{suffix}]]"
        return f"![{node.attrs.get('alt', '')}]({_escape_url(local_path)})"

    def _backup_image(self, url: str) -> str:
        self.dead_images.add(url)
        target = (
            Path(self.me_slug) / self.media.media_subfolder / self.media.filename_for(url)
        ).as_posix()
        return f"![[{target}]]"

    def _link(self, node: HtmlNode, post_date: str) -> str:
        href = clean_url(node.attrs.get("href", ""))
        label = self._render_children(node.children, post_date)
        image_nodes = [child for child in node.children
                       if isinstance(child, HtmlNode) and child.tag == "img"]
        href_absolute = clean_url(urljoin(self.siteurl + "/", html.unescape(href.strip())))
        image_absolute = (
            clean_url(urljoin(self.siteurl + "/", html.unescape(image_nodes[0].attrs.get("src", "").strip())))
            if len(image_nodes) == 1 else ""
        )
        if (
            image_absolute in self.dead_images
            and all(
                (isinstance(child, str) and not child.strip())
                or (isinstance(child, HtmlNode) and child.tag == "img")
                for child in node.children
            )
        ):
            return label
        if len(image_nodes) == 1 and href_absolute == image_absolute:
            return label
        if not href:
            return label
        absolute = href_absolute
        destination = urlparse(absolute)
        blog = urlparse(self.siteurl)
        if (
            destination.scheme.casefold() in {"http", "https"}
            and destination.netloc.casefold() == blog.netloc.casefold()
            and destination.path.rstrip("/") == blog.path.rstrip("/")
        ):
            post_ids = parse_qs(destination.query).get("p", [])
            if len(post_ids) == 1 and post_ids[0] in self.post_targets:
                target = self.post_targets[post_ids[0]]
                if destination.fragment:
                    target += f"#{destination.fragment}"
                return f"[[{target}|{label}]]"
        if self.checker.skip_links:
            return f"[{label}]({_escape_url(href)})"

        is_attachment = MediaStore.is_upload(absolute)
        resolved, live, archived = self._url_result(href, post_date, is_attachment)
        if not live and not archived:
            return f"[{label}]({_escape_url(resolved)}) (dead)"
        if is_attachment and live:
            check = self.checker.check(absolute)
            if check["content_type"] == "text/html":
                snapshot = self.checker.archive(absolute, post_date)
                if snapshot:
                    return f"[{label}]({_escape_url(clean_url(snapshot))})"
                return f"[{label}]({_escape_url(absolute)}) (dead)"
            local_path = self.media.download(absolute, str(check["final_url"]))
            return f"[[{local_path}|{label}]]"
        return f"[{label}]({_escape_url(resolved)})"

    def _render_node(self, node: HtmlNode, post_date: str, list_depth: int = 0) -> str:
        tag = node.tag
        if tag in {"script", "style"}:
            return ""
        if tag == "img":
            return self._image(node, post_date)
        if tag == "a":
            return self._link(node, post_date)
        if tag == "br":
            return "  \n"
        if tag == "hr":
            return "\n\n---\n\n"
        if tag in {"strong", "b"}:
            return f"**{self._render_children(node.children, post_date)}**"
        if tag in {"em", "i"}:
            return f"*{self._render_children(node.children, post_date)}*"
        if tag == "del" or tag == "s" or tag == "strike":
            return f"~~{self._render_children(node.children, post_date)}~~"
        if tag == "code":
            value = _text(node.children)
            return f"`{value}`"
        if tag == "pre":
            value = _text(node.children).strip("\n")
            return f"\n\n```\n{value}\n```\n\n"
        if tag in {"ul", "ol"}:
            items = []
            for index, child in enumerate(node.children, 1):
                if isinstance(child, HtmlNode) and child.tag == "li":
                    marker = f"{index}." if tag == "ol" else "-"
                    items.append(self._render_list_item(child, marker, post_date, list_depth))
            return "\n\n" + "\n".join(items) + "\n\n"
        if tag == "li":
            return self._render_list_item(node, "-", post_date, list_depth)
        content = self._render_children(node.children, post_date, list_depth)
        if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
            return f"\n\n{'#' * int(tag[1])} {content.strip()}\n\n"
        if tag == "blockquote":
            return "\n\n" + "\n".join(f"> {line}" for line in content.strip().splitlines()) + "\n\n"
        if tag in BLOCK_TAGS:
            return f"\n\n{content.strip()}\n\n" if content.strip() else ""
        return content

    def _render_list_item(
        self, node: HtmlNode, marker: str, post_date: str, depth: int
    ) -> str:
        text_parts: list[str] = []
        nested: list[str] = []
        for child in node.children:
            if isinstance(child, HtmlNode) and child.tag in {"ul", "ol"}:
                nested.append(self._render_node(child, post_date, depth + 1).strip())
            else:
                rendered = (
                    child if isinstance(child, str)
                    else self._render_node(child, post_date, depth + 1)
                )
                text_parts.append(rendered)
        prefix = "  " * depth + marker + " "
        result = prefix + re.sub(r"\s*\n\s*", " ", "".join(text_parts)).strip()
        if nested:
            result += "\n" + "\n".join(nested)
        return result

    def _render_children(
        self, children: list[HtmlNode | str], post_date: str, list_depth: int = 0
    ) -> str:
        return "".join(
            child if isinstance(child, str)
            else self._render_node(child, post_date, list_depth)
            for child in children
        )

    def html_to_markdown(self, source: str, post_date: str) -> str:
        parser = HtmlTreeParser()
        parser.feed(_wp_autop(source))
        parser.close()
        result = self._render_children(parser.root.children, post_date)
        result = re.sub(r"\r?\n(?:[ \t]*\r?\n)+", "\n\n", result)
        return result.strip()


def _attachment_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    marker = "/wp-content/uploads/"
    index = normalized.casefold().find(marker)
    if index >= 0:
        return normalized[index + len(marker):].lstrip("/")
    return normalized.lstrip("/")


def _option_value(options: list[dict[str, Any]], name: str) -> str:
    for option in options:
        if _value(option, "option_name") == name:
            return _value(option, "option_value")
    return ""


def _post_datetime(date_string: str) -> tuple[str, str]:
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}):\d{2}", date_string)
    if not match:
        raise ValueError(f"Unexpected WordPress post_date value: {date_string!r}")
    return match.group(1), match.group(2)


def _make_posts(
    tables: dict[str, list[dict[str, Any]]], version: str = "auto"
) -> list[Post]:
    post_categories: dict[str, list[str]] = {}
    if version not in {"auto", "legacy", "modern"}:
        raise ValueError(f"Unsupported WordPress schema version: {version}")

    if version in {"auto", "legacy"}:
        categories = {
            _value(row, "cat_ID"): _value(row, "category_nicename")
            for row in tables.get("wp_categories", [])
        }
        for relation in tables.get("wp_post2cat", []):
            category = categories.get(_value(relation, "category_id"))
            if category:
                post_categories.setdefault(_value(relation, "post_id"), []).append(category)

    if version in {"auto", "modern"}:
        terms = {
            _value(row, "term_id"): _value(row, "slug")
            for row in tables.get("wp_terms", [])
        }
        taxonomies = {
            _value(row, "term_taxonomy_id"): (
                _value(row, "term_id"), _value(row, "taxonomy")
            )
            for row in tables.get("wp_term_taxonomy", [])
        }
        for relation in tables.get("wp_term_relationships", []):
            term_id, taxonomy = taxonomies.get(
                _value(relation, "term_taxonomy_id"), ("", "")
            )
            if taxonomy in {"category", "post_tag"}:
                slug = terms.get(term_id)
                if slug:
                    post_categories.setdefault(
                        _value(relation, "object_id"), []
                    ).append(slug)

    posts: list[Post] = []
    for row in tables.get("wp_posts", []):
        if (
            _value(row, "post_status") != "publish"
            or _value(row, "post_type").strip().casefold() not in {"", "post"}
        ):
            continue
        post_id = _value(row, "ID")
        date, post_time = _post_datetime(_value(row, "post_date"))
        posts.append(Post(
            id=post_id,
            title=_value(row, "post_title"),
            slug=_value(row, "post_name"),
            date=date,
            time=post_time,
            content=_value(row, "post_content"),
            categories=list(dict.fromkeys(post_categories.get(post_id, []))),
        ))

    comments_by_post: dict[str, list[Comment]] = {}
    for row in tables.get("wp_comments", []):
        if _value(row, "comment_approved") != "1" or _value(row, "comment_type").strip():
            continue
        comments_by_post.setdefault(_value(row, "comment_post_ID"), []).append(
            Comment(
                author=_value(row, "comment_author"),
                email=_value(row, "comment_author_email"),
                url=_value(row, "comment_author_url"),
                date=_value(row, "comment_date"),
                content=_value(row, "comment_content"),
            )
        )
    for post in posts:
        post.comments = sorted(
            comments_by_post.get(post.id, []),
            key=lambda comment: comment.date,
        )
    return posts


def _post_people(post: Post, people: list[PersonName], me_slug: str) -> list[str]:
    corpus = "\n".join([
        post.title,
        _text(_parse_html(post.content).children),
    ])
    slugs = [me_slug]
    matched = matching_people(people, corpus)
    for comment in post.comments:
        matched.extend(matching_commenters(people, comment))
    for person in matched:
        if person.slug not in slugs:
            slugs.append(person.slug)
    return slugs


def _parse_html(content: str) -> HtmlNode:
    parser = HtmlTreeParser()
    parser.feed(content)
    parser.close()
    return parser.root


def _yaml_string(value: str) -> str:
    if (
        re.fullmatch(r"[A-Za-z][A-Za-z0-9 _./:?&=%+#@~-]*", value)
        and value == value.strip()
        and ": " not in value
        and " #" not in value
        and not value.endswith(":")
        and value.casefold() not in {
            "true", "false", "yes", "no", "on", "off", "y", "n", "null",
        }
    ):
        return value
    return json.dumps(value, ensure_ascii=False)


def _post_template() -> str:
    template_path = Path(__file__).resolve().parent.parent / "hal_md" / "templates" / "Post.md"
    if not template_path.is_file():
        raise FileNotFoundError(f"Post template not found: {template_path}")
    return template_path.read_text(encoding="utf-8")


def _replace_template_line(template: str, field_name: str, replacement: str) -> str:
    pattern = rf"^{re.escape(field_name)}:[^\r\n]*$"
    updated, replacements = re.subn(
        pattern, lambda _: replacement, template, count=1, flags=re.MULTILINE
    )
    if not replacements:
        raise ValueError(f"Post template is missing the {field_name!r} field")
    return updated


def _render_post(
    post: Post,
    people: list[PersonName],
    me_slug: str,
    siteurl: str,
    renderer: MarkdownRenderer,
) -> str:
    template = _post_template()
    tags = ["post", *(category for category in post.categories
                      if category.casefold() != "uncategorized")]
    person_slugs = _post_people(post, people, me_slug)
    tag_block = "tags:\n" + "\n".join(f"  - {_yaml_string(tag)}" for tag in tags)
    people_block = "people:\n" + "\n".join(
        f"  - {_yaml_string(slug)}"
        for slug in person_slugs
    )
    template = re.sub(
        r"^tags:\r?\n(?:[ \t]*-[^\r\n]*(?:\r?\n|$))*",
        lambda _: tag_block + "\n",
        template,
        count=1,
        flags=re.MULTILINE,
    )
    template = _replace_template_line(
        template, "url", f"url: {_yaml_string(clean_url(siteurl).rstrip('/') + '/?p=' + post.id)}"
    )
    template = _replace_template_line(template, "date", f"date: {_yaml_string(post.date)}")
    template = _replace_template_line(template, "time", f"time: {_yaml_string(post.time)}")
    template = _replace_template_line(template, "people", people_block)
    template = _replace_template_line(template, "service", "service: wordpress")
    template = template.replace("# Post", f"# {post.title}", 1)
    body = renderer.html_to_markdown(post.content, post.date)
    lines = [template.rstrip(), "", body]
    if post.comments:
        lines.extend(["", "## Comments", ""])
        for index, comment in enumerate(post.comments, 1):
            matched = matching_commenters(people, comment)
            if any(person.slug == me_slug for person in matched):
                author = f"[[{me_slug}]]"
            elif matched:
                author = f"[[{matched[0].slug}]]"
            elif comment.url:
                resolved, live, archived = renderer._url_result(
                    comment.url, post.date, False
                )
                if live or archived:
                    author = f"[{comment.author}]({_escape_url(resolved)})"
                else:
                    author = f"[{comment.author}]({_escape_url(resolved)}) (dead)"
            else:
                author = comment.author
            body = renderer.html_to_markdown(comment.content, post.date)
            lines.extend([
                f"{index}. **{author}** - {comment.date[:10]}",
                "",
                *[f"  > {line}" if line else "  >" for line in body.splitlines()],
                "",
            ])
    return "\n".join(lines).rstrip() + "\n"


def _missing_commenters(posts: list[Post], people: list[PersonName], me_slug: str) -> list[tuple[str, str]]:
    known = {person.slug: person for person in people}
    me = known.get(me_slug)
    entries: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for post in posts:
        for comment in post.comments:
            matched = matching_commenters(people, comment)
            if any(person.slug == me_slug for person in matched):
                continue
            if me and not me.ignore and any(
                _whole_word_match(name, comment.author) for name in me.names
            ):
                continue
            if matched:
                continue
            key = (comment.author.casefold(), comment.email.casefold())
            if key not in seen:
                seen.add(key)
                entries.append((comment.author, comment.email))
    return entries


def _find_option(settings: dict[str, Any], name: str, default: Any) -> Any:
    value = settings.get(name, default)
    return value if value is not None else default


def _load_settings(config_folder: Path) -> dict[str, Any]:
    settings_file = config_folder / "settings.json"
    if not settings_file.exists():
        return {}
    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    if not isinstance(settings, dict):
        raise ValueError(f"{settings_file} must contain a JSON object")
    return settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert published posts in a WordPress SQL dump to Markdown."
    )
    parser.add_argument("file", help="WordPress SQL dump to convert")
    parser.add_argument("-m", "--my-slug", dest="my_slug", help="Your People.json slug")
    parser.add_argument("-c", "--config", default=".", help="Folder containing settings.json and People.json")
    parser.add_argument("-s", "--source-folder", default=".", help="Folder containing the SQL dump")
    parser.add_argument("-o", "--output-folder", help="Override output-folder setting")
    parser.add_argument(
        "--version", choices=("auto", "legacy", "modern"), default="auto",
        help="WordPress taxonomy schema (default: auto-detect from dump tables)",
    )
    parser.add_argument("-d", "--debug", action="store_true", help="Print debug diagnostics")
    parser.add_argument("--skip-links", action="store_true", help="Skip all link checks and downloads")
    return parser


def _run(args: argparse.Namespace) -> None:
    config_folder = Path(args.config).expanduser().resolve()
    settings = _load_settings(config_folder)
    source = Path(args.file).expanduser()
    if not source.is_absolute():
        source = Path(args.source_folder).expanduser() / source
    source = source.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"SQL dump not found: {source}")

    people = load_people(config_folder)
    me_slug = args.my_slug or str(settings.get("my-slug", ""))
    if not me_slug:
        raise ValueError("Specify your People.json slug with -m or my-slug in settings.json")
    if not any(person.slug == me_slug for person in people):
        raise ValueError(f"Your slug {me_slug!r} was not found in People.json")

    output_setting = args.output_folder or _find_option(settings, "output-folder", ".")
    output_folder = Path(str(output_setting)).expanduser()
    if not output_folder.is_absolute():
        output_folder = Path.cwd() / output_folder
    output_folder = output_folder.resolve()
    people_subfolder = str(_find_option(settings, "people-subfolder", "People"))
    if (
        not me_slug or re.search(r'[<>:"/\\|?*\x00-\x1f]', me_slug)
        or me_slug in {".", ".."} or me_slug.endswith((".", " "))
    ):
        raise ValueError("my-slug must be a valid single folder name")
    if (
        not people_subfolder or Path(people_subfolder).is_absolute()
        or ".." in people_subfolder.replace("\\", "/").split("/")
    ):
        raise ValueError("people-subfolder must be a relative folder within output-folder")
    person_folder = output_folder / people_subfolder / me_slug
    media_subfolder = str(_find_option(settings, "media-subfolder", "media"))
    image_embed = str(_find_option(settings, "image-embed", True)).casefold() not in {
        "", "0", "false", "no", "none",
    }
    image_width = _find_option(settings, "image-width", "")
    siteurl = ""

    tables = parse_dump(source)
    options = tables.get("wp_options", [])
    siteurl = clean_url(_option_value(options, "siteurl")).rstrip("/")
    if not siteurl:
        raise ValueError("The SQL dump has no wp_options siteurl value")

    posts = _make_posts(tables, args.version)
    if not posts:
        raise ValueError("The SQL dump contains no published posts")
    post_file_targets(posts)

    person_folder.mkdir(parents=True, exist_ok=True)
    checker = LinkChecker(output_folder, args.skip_links, args.debug)
    media = MediaStore(person_folder, media_subfolder, checker)

    attachment_urls: dict[str, str] = {}
    for row in tables.get("wp_posts", []):
        if (
            _value(row, "post_type").strip().casefold() != "attachment"
            and _value(row, "post_status").strip().casefold() != "attachment"
        ):
            continue
        post_id = _value(row, "ID")
        for meta in tables.get("wp_postmeta", []):
            if _value(meta, "post_id") == post_id and _value(meta, "meta_key") == "_wp_attached_file":
                relative = _attachment_path(_value(meta, "meta_value"))
                if relative:
                    attachment_urls[f"{siteurl}/wp-content/uploads/{quote(relative, safe='/')}"] = relative
                break

    for attachment_url in attachment_urls:
        result = checker.check(attachment_url)
        if result["live"] and result["content_type"] != "text/html":
            media.download(attachment_url, str(result["final_url"]))

    renderer = MarkdownRenderer(
        checker, media, siteurl, image_width, image_embed, posts, me_slug
    )
    for post in posts:
        filename = person_folder / f"{renderer.post_targets[post.id]}.md"
        markdown = _render_post(post, people, me_slug, siteurl, renderer)
        filename.write_text(markdown, encoding="utf-8")
        if args.debug:
            logging.debug("Wrote %s", filename)

    checker.save()
    if not args.skip_links and checker.pending_path.exists():
        pending = sum(
            isinstance(entry, dict) and entry.get("pending") is True
            for entry in checker.cache.values()
        )
        if pending:
            print(f"Deferred Wayback lookups: {pending} (saved to {checker.pending_path})")
    missing = _missing_commenters(posts, people, me_slug)
    if missing:
        print("Missing people:")
        for name, email_address in missing:
            print(f"{name} {email_address}".rstrip())


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )
    try:
        _run(args)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        logging.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
