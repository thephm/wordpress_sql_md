import tempfile
import unittest
from pathlib import Path
import json
import contextlib
import io
import subprocess
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import wordpress_sql_md as converter


class UrlHandlingTests(unittest.TestCase):
    def test_wayback_rate_limit_defers_without_sleep_or_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            checker = converter.LinkChecker(Path(directory), True)
            checker.skip_links = False
            checker.cache["archive:2005-01-02:http://example.com"] = {"url": None}
            error = HTTPError("http://archive.org", 429, "Too many requests",
                              {"Retry-After": "7"}, None)
            with patch("wordpress_sql_md.urlopen") as request, \
                 patch("wordpress_sql_md.time.monotonic", return_value=100), \
                 patch("wordpress_sql_md.time.sleep") as sleep:
                request.side_effect = error
                with self.assertLogs(level="WARNING"):
                    self.assertIsNone(checker.archive("http://example.com", "2005-01-02"))
                sleep.assert_not_called()
                self.assertEqual(request.call_count, 1)
                self.assertEqual(checker.wayback_rate_limit_until, 107)
                checker.archive("http://example.com", "2005-01-02")
                checker.archive("http://example.com/next", "2005-01-02")
                self.assertEqual(request.call_count, 1)
                sleep.assert_not_called()
            pending = json.loads(checker.pending_path.read_text(encoding="utf-8"))
            self.assertEqual(pending, [
                {"url": "http://example.com", "date": "2005-01-02", "reason": "HTTP 429"},
                {"url": "http://example.com/next", "date": "2005-01-02",
                 "reason": "Wayback rate-limit cooldown"},
            ])
            self.assertNotIn("checked", checker.cache["archive:2005-01-02:http://example.com"])

    def test_wayback_api_failures_are_not_negative_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            checker = converter.LinkChecker(Path(directory), True)
            checker.skip_links = False
            with patch("wordpress_sql_md.urlopen", side_effect=URLError("temporary")), \
                 self.assertLogs(level="WARNING"):
                self.assertIsNone(checker.archive("http://example.com", "2005-01-02"))
            entry = checker.cache["archive:2005-01-02:http://example.com"]
            self.assertTrue(entry["pending"])
            self.assertNotIn("checked", entry)

    def test_wayback_deferred_lookup_is_rechecked_on_next_run(self):
        with tempfile.TemporaryDirectory() as directory:
            checker = converter.LinkChecker(Path(directory), True)
            checker.skip_links = False
            error = HTTPError("http://archive.org", 429, "rate limit", {}, None)
            with patch("wordpress_sql_md.urlopen", side_effect=error) as request, \
                 patch("wordpress_sql_md.time.monotonic", return_value=100), \
                 patch("wordpress_sql_md.time.sleep") as sleep, \
                 self.assertLogs(level="WARNING"):
                self.assertIsNone(checker.archive("http://example.com", "2005-01-02"))
                self.assertEqual(request.call_count, 1)
                sleep.assert_not_called()
            self.assertEqual(checker.wayback_rate_limit_until, 105)
            with patch("wordpress_sql_md.shutil.which", return_value="curl"):
                reloaded = converter.LinkChecker(Path(directory), False)
            with patch("wordpress_sql_md.urlopen") as request:
                request.return_value.__enter__.return_value.read.return_value = (
                    b'{"archived_snapshots":{"closest":{"available":true,'
                    b'"url":"https://web.archive.org/web/20050102/http://example.com"}}}'
                )
                self.assertEqual(
                    reloaded.archive("http://example.com", "2005-01-02"),
                    "https://web.archive.org/web/20050102/http://example.com",
                )
                request.assert_called_once()
                reloaded.archive("http://example.com", "2005-01-02")
                request.assert_called_once()
            reloaded.save()
            self.assertEqual(json.loads(reloaded.pending_path.read_text(encoding="utf-8")), [])

    def test_wayback_legacy_negative_cache_retries_and_caches_confirmed_absence(self):
        with tempfile.TemporaryDirectory() as directory:
            checker = converter.LinkChecker(Path(directory), True)
            checker.skip_links = False
            key = "archive:2005-01-02:http://example.com"
            checker.cache[key] = {"url": None}
            with patch("wordpress_sql_md.urlopen") as request:
                request.return_value.__enter__.return_value.read.return_value = (
                    b'{"archived_snapshots":{}}'
                )
                self.assertIsNone(checker.archive("http://example.com", "2005-01-02"))
                self.assertEqual(checker.cache[key], {"url": None, "checked": True})
                self.assertIsNone(checker.archive("http://example.com", "2005-01-02"))
                request.assert_called_once()

    def test_wayback_cooldown_expiry_only_allows_new_lookups(self):
        with tempfile.TemporaryDirectory() as directory:
            checker = converter.LinkChecker(Path(directory), True)
            checker.skip_links = False
            error = HTTPError("http://archive.org", 429, "rate limit", {}, None)
            with patch("wordpress_sql_md.time.monotonic", return_value=100), \
                 patch("wordpress_sql_md.urlopen", side_effect=error), \
                 self.assertLogs(level="WARNING"):
                checker.archive("http://example.com", "2005-01-02")
            with patch("wordpress_sql_md.time.monotonic", return_value=106), \
                 patch("wordpress_sql_md.time.sleep") as sleep, \
                 patch("wordpress_sql_md.urlopen") as request:
                request.return_value.__enter__.return_value.read.return_value = (
                    b'{"archived_snapshots":{}}'
                )
                checker.archive("http://example.com", "2005-01-02")
                request.assert_not_called()
                checker.archive("http://example.com/other", "2005-01-02")
                request.assert_called_once()
                sleep.assert_not_called()

    def test_wayback_retry_after_date_and_invalid_header_do_not_sleep(self):
        for header, delay in (
            ("Thu, 01 Jan 1970 00:01:50 GMT", 10),
            ("invalid", 5),
        ):
            with self.subTest(header=header), tempfile.TemporaryDirectory() as directory:
                checker = converter.LinkChecker(Path(directory), True)
                checker.skip_links = False
                error = HTTPError("http://archive.org", 429, "rate limit",
                                  {"Retry-After": header}, None)
                with patch("wordpress_sql_md.time.monotonic", return_value=100), \
                     patch("wordpress_sql_md.time.time", return_value=100), \
                     patch("wordpress_sql_md.time.sleep") as sleep, \
                     patch("wordpress_sql_md.urlopen", side_effect=error) as request, \
                     self.assertLogs(level="WARNING"):
                    checker.archive("http://example.com", "2005-01-02")
                    request.assert_called_once()
                    sleep.assert_not_called()
                self.assertEqual(checker.wayback_rate_limit_until, 100 + delay)

    def test_wayback_normal_pacing_and_confirmed_absence_clear_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            checker = converter.LinkChecker(Path(directory), True)
            checker.skip_links = False
            checker._defer_archive("http://example.com", "2005-01-02", "HTTP 429")
            with patch("wordpress_sql_md.shutil.which", return_value="curl"):
                reloaded = converter.LinkChecker(Path(directory), False)
            with patch("wordpress_sql_md.time.monotonic", return_value=100), \
                 patch("wordpress_sql_md.time.sleep") as sleep, \
                 patch("wordpress_sql_md.urlopen") as request:
                request.return_value.__enter__.return_value.read.return_value = (
                    b'{"archived_snapshots":{}}'
                )
                reloaded.archive("http://example.com", "2005-01-02")
                sleep.assert_not_called()
                reloaded.archive("http://example.com/other", "2005-01-02")
                sleep.assert_called_once_with(1)
                self.assertEqual(request.call_count, 2)
            reloaded.save()
            self.assertEqual(json.loads(reloaded.pending_path.read_text(encoding="utf-8")), [])

    def test_offline_leaves_wayback_pending_files_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pending_path = root / "wordpress_sql_md_wayback_pending.json"
            cache_path = root / "wordpress_sql_md_link_cache.json"
            pending_path.write_text("unchanged pending report", encoding="utf-8")
            cache_path.write_text("unchanged cache", encoding="utf-8")
            checker = converter.LinkChecker(root, True)
            with patch("wordpress_sql_md.urlopen") as request:
                self.assertIsNone(checker.archive("http://example.com", "2005-01-02"))
                checker.save()
                request.assert_not_called()
            self.assertEqual(pending_path.read_text(encoding="utf-8"), "unchanged pending report")
            self.assertEqual(cache_path.read_text(encoding="utf-8"), "unchanged cache")

    def test_dns_failure_is_remembered_without_retry_across_urls_and_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("wordpress_sql_md.shutil.which", return_value="curl"):
                checker = converter.LinkChecker(Path(directory), False)
            with patch("wordpress_sql_md.subprocess.run") as run, \
                 patch("wordpress_sql_md.logging.warning") as warning:
                run.return_value = subprocess.CompletedProcess([], 6, "", "Could not resolve host")
                self.assertFalse(checker.check("http://nakama.ca/first")["live"])
                self.assertFalse(checker.check("https://NAKAMA.ca:443/second")["live"])
                run.assert_called_once()
                warning.assert_called_once()
            with patch("wordpress_sql_md.shutil.which", return_value="curl"):
                reloaded = converter.LinkChecker(Path(directory), False)
            with patch.object(reloaded, "_curl", side_effect=AssertionError("DNS cache ignored")):
                self.assertFalse(reloaded.check("http://nakama.ca/third")["live"])
            with patch.object(reloaded, "_curl", return_value=(
                200, "image/jpeg", "http://pics.nakama.ca/photo.jpg", ""
            )) as curl:
                self.assertTrue(reloaded.check("http://pics.nakama.ca/photo.jpg")["live"])
                curl.assert_called_once()
            with patch("wordpress_sql_md.urlopen") as request:
                request.return_value.__enter__.return_value.read.return_value = b'{"archived_snapshots":{}}'
                self.assertIsNone(reloaded.archive("http://nakama.ca/third", "2005-01-02"))
                request.assert_called_once()

    def test_redirect_dns_failure_does_not_mark_origin_host_dead(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("wordpress_sql_md.shutil.which", return_value="curl"):
                checker = converter.LinkChecker(Path(directory), False)
            with patch.object(checker, "_curl", side_effect=[
                (301, "", "https://live.example/", "https://dead.example/"),
                converter.HostResolutionError("https://dead.example/", "DNS failure"),
            ]):
                self.assertFalse(checker.check("https://live.example/redirect")["live"])
            self.assertIn("dns:dead.example", checker.cache)
            self.assertNotIn("dns:live.example", checker.cache)
            with patch.object(checker, "_curl", return_value=(
                200, "text/html", "https://live.example/other", ""
            )):
                self.assertTrue(checker.check("https://live.example/other")["live"])

    def test_connection_errors_do_not_poison_host_and_offline_ignores_dns_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("wordpress_sql_md.shutil.which", return_value="curl"):
                checker = converter.LinkChecker(Path(directory), False)
            with patch("wordpress_sql_md.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess([], 7, "", "Connection failed")
                with self.assertLogs(level="WARNING"):
                    self.assertFalse(checker.check("http://example.com/")["live"])
                self.assertEqual(run.call_count, 2)
            self.assertNotIn("dns:example.com", checker.cache)
            checker.cache["dns:example.com"] = {"unresolved": True}
            checker.save()
            offline = converter.LinkChecker(Path(directory), True)
            self.assertEqual(offline.cache, {})
            self.assertTrue(offline.check("http://example.com/")["live"])

    def test_cleanup_preserves_functional_parameters_encoding_and_fragments(self):
        self.assertEqual(
            converter.clean_url(
                "https://example.com/Product.aspx?language=en-CA&product=4418965"
                "&MSCSProfile=junk&UTM_Source=mail&ASPSESSIONIDABC=token"
                "&unknown=a%2Bb&unknown=second#details"
            ),
            "https://example.com/Product.aspx?language=en-CA&product=4418965"
            "&unknown=a%2Bb&unknown=second#details",
        )
        self.assertEqual(
            converter.clean_url("https://youtube.com/watch?v=abc&list=xyz&t=30&fbclid=junk"),
            "https://youtube.com/watch?v=abc&list=xyz&t=30",
        )

    def test_curl_uses_three_second_limit_for_head_and_get(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("wordpress_sql_md.shutil.which", return_value="curl"):
                checker = converter.LinkChecker(Path(directory), False)
            for method in ("HEAD", "GET"):
                with patch("wordpress_sql_md.subprocess.run") as run:
                    run.return_value = subprocess.CompletedProcess(
                        [], 0, "\n200\ttext/html\thttps://example.com/\t", ""
                    )
                    checker._curl("https://example.com/?utm_source=junk", method, False)
                    command = run.call_args.args[0]
                    self.assertEqual(command[command.index("--max-time") + 1], "3")
                    self.assertEqual(command[-1], "https://example.com/")
                    self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_validation_cleans_redirects_and_cache_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("wordpress_sql_md.shutil.which", return_value="curl"):
                checker = converter.LinkChecker(Path(directory), False)
            with patch.object(checker, "_curl", side_effect=[
                (301, "", "https://example.com/", "https://example.com/watch?v=abc&utm_source=mail"),
                (200, "text/html", "https://example.com/watch?v=abc", ""),
            ]) as curl:
                result = checker.check("https://example.com/?MSCSProfile=junk")
            self.assertEqual(curl.call_args_list[1].args[0], "https://example.com/watch?v=abc")
            self.assertEqual(result["final_url"], "https://example.com/watch?v=abc")
            self.assertIn("https://example.com/", checker.cache)
            self.assertNotIn("https://example.com/?MSCSProfile=junk", checker.cache)

    def test_one_retry_and_get_fallback_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("wordpress_sql_md.shutil.which", return_value="curl"):
                checker = converter.LinkChecker(Path(directory), False)
            with patch.object(checker, "_curl", side_effect=[
                RuntimeError("timeout"),
                (405, "", "https://example.com/", ""),
                (200, "text/html", "https://example.com/", ""),
            ]) as curl:
                result = checker.check("https://example.com/")
            self.assertTrue(result["live"])
            self.assertEqual([call.args[1] for call in curl.call_args_list], ["HEAD", "HEAD", "GET"])

    def test_offline_cleanup_and_local_links_do_not_access_network(self):
        post = converter.Post("23", "Post", "post-name", "2005-03-14", "09:00", "")
        with tempfile.TemporaryDirectory() as directory:
            checker = converter.LinkChecker(Path(directory), True)
            media = converter.MediaStore(Path(directory), "media", checker)
            renderer = converter.MarkdownRenderer(
                checker, media, "https://example.com/blog", 450, True, [post]
            )
            with patch.object(checker, "check", side_effect=AssertionError("network")), \
                 patch.object(checker, "archive", side_effect=AssertionError("network")):
                output = renderer.html_to_markdown(
                    '<a href="?p=23&amp;utm_source=mail#notes">Local</a>'
                    '<a href="https://other.com/blog/?p=23">External</a>'
                    '<a href="?p=999">Unconverted</a>'
                    '<a href="https://youtube.com/watch?v=abc&amp;t=30&amp;gclid=junk">Video</a>'
                    '<img src="/photo.jpg?size=large&amp;MSCSProfile=junk" alt="Photo">',
                    "2005-03-14",
                )
            self.assertIn("[[2005-03-14 Post#notes|Local]]", output)
            self.assertIn("[External](https://other.com/blog/?p=23)", output)
            self.assertIn("[Unconverted](?p=999)", output)
            self.assertIn("[Video](https://youtube.com/watch?v=abc&t=30)", output)
            self.assertIn("![Photo](/photo.jpg?size=large)", output)


class SqlParserTests(unittest.TestCase):
    def test_dump_uses_declared_column_order_instead_of_legacy_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            dump = Path(directory) / "dump.sql"
            dump.write_text(
                "CREATE TABLE `wp_posts` (\n"
                " `ID` bigint(20),\n"
                " `post_status` varchar(20),\n"
                " `comment_status` varchar(20)\n"
                ");\n"
                "CREATE TABLE `wp_comments` (\n"
                " `comment_date_gmt` datetime,\n"
                " `comment_content` text,\n"
                " `comment_approved` varchar(20)\n"
                ");\n"
                "CREATE TABLE `wp_options` (\n"
                " `option_name` varchar(64),\n"
                " `option_type` int(11),\n"
                " `option_value` text\n"
                ");\n"
                "INSERT INTO `wp_posts` VALUES (1,'publish','open');\n"
                "INSERT INTO `wp_comments` VALUES ('2005-01-01 12:00:00','Hello','1');\n"
                "INSERT INTO `wp_options` VALUES ('siteurl',1,'https://example.com');",
                encoding="cp1252",
            )
            tables = converter.parse_dump(dump)
        self.assertEqual(tables["wp_posts"][0]["post_status"], "publish")
        self.assertEqual(tables["wp_comments"][0]["comment_content"], "Hello")
        self.assertEqual(tables["wp_comments"][0]["comment_approved"], "1")
        self.assertEqual(
            converter._option_value(tables["wp_options"], "siteurl"),
            "https://example.com",
        )

    def test_insert_explicit_columns_override_schema_and_reject_wrong_lengths(self):
        schemas = {"wp_posts": ["ID", "post_status", "comment_status"]}
        parsed = converter.parse_insert(
            "INSERT INTO `wp_posts` (`post_status`,`ID`) VALUES ('publish',1);",
            schemas,
        )
        self.assertEqual(parsed[1][0]["post_status"], "publish")
        with self.assertRaisesRegex(ValueError, "2 values.*3 columns"):
            converter.parse_insert("INSERT INTO `wp_posts` VALUES (1,'publish');", schemas)

    def test_parse_insert_decodes_mysql_escapes_and_multiline_values(self):
        parsed = converter.parse_insert(
            "INSERT INTO `wp_posts` (`ID`, `post_title`, `post_content`) VALUES "
            "(1,'It''s \\\\ a title','line one\\r\\nline two'),"
            "(2,'Second','<!--more-->');"
        )
        self.assertIsNotNone(parsed)
        table, rows = parsed
        self.assertEqual(table, "wp_posts")
        self.assertEqual(rows[0]["post_title"], "It's \\ a title")
        self.assertEqual(rows[0]["post_content"], "line one\r\nline two")
        self.assertEqual(rows[1]["post_title"], "Second")

    def test_parse_dump_handles_semicolons_inside_content(self):
        with tempfile.TemporaryDirectory() as directory:
            dump = Path(directory) / "dump.sql"
            dump.write_bytes(
                "INSERT INTO `wp_posts` VALUES "
                "(1,1,'2004-10-02 13:45:00','2004-10-02 13:45:00',"
                "'text; still content','Title',0,'','', '', 'publish',"
                "'open','open','','slug','','','2004-10-02 13:45:00',"
                "'2004-10-02 13:45:00','','0','','0','');".encode("cp1252")
            )
            tables = converter.parse_dump(dump)
        self.assertEqual(len(tables["wp_posts"]), 1)
        self.assertEqual(tables["wp_posts"][0]["post_content"], "text; still content")

    def test_parse_dump_replaces_undefined_cp1252_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            dump = Path(directory) / "dump.sql"
            dump.write_bytes(
                b"INSERT INTO `wp_posts` (`ID`,`post_title`) VALUES (1,'before "
                b"\x8f after');"
            )
            tables = converter.parse_dump(dump)
        self.assertEqual(tables["wp_posts"][0]["post_title"], "before \ufffd after")

    def test_parse_dump_preserves_utf8_with_or_without_bom(self):
        title = "Caf\u00e9 \u2014 \u65e5\u672c \U0001f600 \ufffd"
        sql = f"INSERT INTO `wp_posts` (`ID`,`post_title`) VALUES (1,'{title}');"
        for encoding in ("utf-8", "utf-8-sig"):
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as directory:
                dump = Path(directory) / "dump.sql"
                dump.write_bytes(sql.encode(encoding))
                with patch("wordpress_sql_md.logging.warning") as warning:
                    tables = converter.parse_dump(dump)
                self.assertEqual(tables["wp_posts"][0]["post_title"], title)
                warning.assert_not_called()

    def test_parse_dump_falls_back_to_cp1252_without_garbling_accents(self):
        with tempfile.TemporaryDirectory() as directory:
            dump = Path(directory) / "dump.sql"
            dump.write_bytes(
                "INSERT INTO `wp_posts` (`ID`,`post_title`) VALUES "
                "(1,'Caf\u00e9 \u2014 \u20ac');".encode("cp1252")
            )
            tables = converter.parse_dump(dump)
        self.assertEqual(tables["wp_posts"][0]["post_title"], "Caf\u00e9 \u2014 \u20ac")

    def test_parse_dump_reads_modern_taxonomy_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            dump = Path(directory) / "modern.sql"
            dump.write_text(
                "CREATE TABLE `wp_terms` (\n"
                " `term_id` bigint(20),\n"
                " `name` varchar(200),\n"
                " `slug` varchar(200),\n"
                " `term_group` bigint(10)\n"
                ");\n"
                "CREATE TABLE `wp_term_taxonomy` (\n"
                " `term_taxonomy_id` bigint(20),\n"
                " `term_id` bigint(20),\n"
                " `taxonomy` varchar(32),\n"
                " `description` longtext,\n"
                " `parent` bigint(20),\n"
                " `count` bigint(20)\n"
                ");\n"
                "CREATE TABLE `wp_term_relationships` (\n"
                " `object_id` bigint(20),\n"
                " `term_taxonomy_id` bigint(20),\n"
                " `term_order` int(11)\n"
                ");\n"
                "INSERT INTO `wp_terms` VALUES (4,'News','news',0);\n"
                "INSERT INTO `wp_term_taxonomy` VALUES (9,4,'category','',0,1);\n"
                "INSERT INTO `wp_term_relationships` VALUES (7,9,0);",
                encoding="utf-8",
            )
            tables = converter.parse_dump(dump)
        self.assertEqual(tables["wp_terms"][0]["slug"], "news")
        self.assertEqual(tables["wp_term_taxonomy"][0]["taxonomy"], "category")
        self.assertEqual(tables["wp_term_relationships"][0]["object_id"], "7")


class ConversionTests(unittest.TestCase):
    def test_title_filenames_preserve_case_spaces_and_remove_invalid_characters(self):
        post = converter.Post("23", 'MyCamelCase Title: <A/B> "Test"?*|\\\x01. ',
                              "old-slug", "2005-03-14", "09:00", "")
        self.assertEqual(
            converter.post_file_targets([post]),
            {"23": "2005-03-14 MyCamelCase Title AB Test"},
        )
        duplicate = converter.Post("24", post.title.lower(), "", post.date, post.time, "")
        with self.assertRaisesRegex(ValueError, "duplicate filename"):
            converter.post_file_targets([post, duplicate])
        post.title = ':/?*'
        with self.assertRaisesRegex(ValueError, "no usable filename"):
            converter.post_file_targets([post])

    def test_yaml_strings_are_unquoted_only_when_safe(self):
        for value in ("bernie", "post", "personal", "wordpress", "https://example.com/?p=19"):
            with self.subTest(value=value):
                self.assertEqual(converter._yaml_string(value), value)
        for value in (
            "", "null", "true", "YES", "on", "123", "2005-01-02", "23:47",
            "label: value", "label # comment", "[items]", " trailing ", "label:",
        ):
            with self.subTest(value=value):
                encoded = converter._yaml_string(value)
                self.assertTrue(encoded.startswith('"'))
                self.assertEqual(json.loads(encoded), value)

    def test_selected_posts_and_approved_comments_are_ordered(self):
        tables = {
            "wp_categories": [{"cat_ID": "4", "category_nicename": "news"}],
            "wp_post2cat": [{"post_id": "7", "category_id": "4"}],
            "wp_posts": [
                {
                    "ID": "7", "post_status": "publish",
                    "post_date": "2005-03-04 09:08:59",
                    "post_title": "An update", "post_name": "an-update",
                    "post_content": "First line\nSecond line",
                },
                {
                    "ID": "8", "post_status": "draft",
                    "post_date": "2005-03-04 09:08:59",
                    "post_title": "Draft", "post_name": "draft",
                },
            ],
            "wp_comments": [
                {
                    "comment_post_ID": "7", "comment_author": "Jane Doe",
                    "comment_date": "2005-03-04 10:00:00",
                    "comment_content": "Later", "comment_approved": "1",
                    "comment_type": "",
                },
                {
                    "comment_post_ID": "7", "comment_author": "Jane Doe",
                    "comment_date": "2005-03-04 09:00:00",
                    "comment_content": "Earlier", "comment_approved": "1",
                    "comment_type": "",
                },
                {
                    "comment_post_ID": "7", "comment_author": "Trackback",
                    "comment_approved": "1", "comment_type": "pingback",
                },
            ],
        }
        posts = converter._make_posts(tables)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].categories, ["news"])
        self.assertEqual([item.content for item in posts[0].comments], ["Earlier", "Later"])

    def test_modern_taxonomy_tags_and_post_type_filtering(self):
        tables = {
            "wp_terms": [
                {"term_id": "4", "slug": "news"},
                {"term_id": "5", "slug": "python"},
                {"term_id": "6", "slug": "main-menu"},
            ],
            "wp_term_taxonomy": [
                {"term_taxonomy_id": "9", "term_id": "4", "taxonomy": "category"},
                {"term_taxonomy_id": "10", "term_id": "5", "taxonomy": "post_tag"},
                {"term_taxonomy_id": "11", "term_id": "6", "taxonomy": "nav_menu"},
            ],
            "wp_term_relationships": [
                {"object_id": "7", "term_taxonomy_id": "9"},
                {"object_id": "7", "term_taxonomy_id": "10"},
                {"object_id": "7", "term_taxonomy_id": "11"},
            ],
            "wp_posts": [
                {
                    "ID": "7", "post_status": "publish", "post_type": "post",
                    "post_date": "2018-03-30 14:20:00", "post_title": "A post",
                },
                {
                    "ID": "8", "post_status": "publish", "post_type": "nav_menu_item",
                    "post_date": "2018-03-30 14:20:00", "post_title": "",
                },
            ],
        }
        posts = converter._make_posts(tables)
        self.assertEqual([post.id for post in posts], ["7"])
        self.assertEqual(posts[0].categories, ["news", "python"])
        with self.assertRaisesRegex(ValueError, "Unsupported WordPress schema version"):
            converter._make_posts(tables, "unsupported")

    def test_person_matches_full_names_aliases_and_ignore(self):
        people = [
            converter.PersonName("jane-doe", "Jane", "Doe", aliases=["JD"]),
            converter.PersonName("ignored", "Ignored", "Person", ignore=True),
        ]
        matched = converter.matching_people(people, "Hello JD and Jane Doe.")
        self.assertEqual([person.slug for person in matched], ["jane-doe"])

    def test_commenter_email_matching_and_name_fallback(self):
        person = converter._person_from_json({
            "slug": "bernie", "emails": [" jansen@example.com "],
        })
        people = [
            person,
            converter.PersonName("bob", profile_full_name="Bob"),
            converter.PersonName("ignored", emails=["ignored@example.com"], ignore=True),
        ]
        for email, expected in (
            (" JANSEN@example.com ", ["bernie"]),
            ("unknown@example.com", ["bob"]),
            ("", ["bob"]),
            ("ignored@example.com", ["bob"]),
            ("notjansen@example.com", ["bob"]),
        ):
            comment = converter.Comment("Bob", email, "", "2005-01-02", "Hello")
            self.assertEqual(
                [p.slug for p in converter.matching_commenters(people, comment)], expected
            )
        for emails in ("address@example.com", [123]):
            with self.assertRaisesRegex(ValueError, "emails must be a list of strings"):
                converter._person_from_json({"slug": "invalid", "emails": emails})

    def test_email_match_controls_output_and_missing_commenters(self):
        people = [
            converter.PersonName("bernie", emails=["jansen@example.com"]),
            converter.PersonName("bob", profile_full_name="Bob"),
            converter.PersonName("friend", emails=["friend@example.com"]),
        ]
        post = converter.Post("19", "A title", "a-title", "2005-01-02", "23:47", "Body")
        post.comments = [
            converter.Comment("Bob", "jansen@example.com", "", post.date, "Hello\n\nNext"),
            converter.Comment("Nickname", "friend@example.com", "", post.date, "Hi"),
        ]
        self.assertEqual(converter._post_people(post, people, "bernie"), ["bernie", "friend"])
        self.assertEqual(converter._missing_commenters([post], people, "bernie"), [])
        with tempfile.TemporaryDirectory() as directory:
            checker = converter.LinkChecker(Path(directory), skip_links=True)
            media = converter.MediaStore(Path(directory), "media", checker)
            renderer = converter.MarkdownRenderer(checker, media, "https://example.com", 450, True)
            markdown = converter._render_post(post, people, "bernie", "https://example.com", renderer)
        self.assertIn("1. **[[bernie]]** - 2005-01-02", markdown)
        self.assertIn("\n  > Hello\n  >\n  > Next\n", markdown)
        self.assertIn("\n  > Hi\n", markdown)
        self.assertIn("**[[friend]]**", markdown)
        self.assertNotIn("[[bob]]", markdown)
        self.assertNotIn("@example.com", markdown)

    def test_markdown_rendering_applies_autop_and_removes_more(self):
        with tempfile.TemporaryDirectory() as directory:
            checker = converter.LinkChecker(Path(directory), skip_links=True)
            media = converter.MediaStore(Path(directory), "media", checker)
            renderer = converter.MarkdownRenderer(checker, media, "https://example.com", 450, True)
            result = renderer.html_to_markdown("First line\nSecond line\n\nNext<!--more-->", "2005-01-02")
        self.assertIn("First line  \nSecond line", result)
        self.assertIn("Next", result)
        self.assertNotIn("more", result)

    def test_paragraphs_have_only_one_blank_line(self):
        with tempfile.TemporaryDirectory() as directory:
            checker = converter.LinkChecker(Path(directory), skip_links=True)
            media = converter.MediaStore(Path(directory), "media", checker)
            renderer = converter.MarkdownRenderer(checker, media, "https://example.com", 450, True)
            for separator in ("\n\n\n\n", "\n \n\t\n", "\r\n \r\n\t\r\n"):
                with self.subTest(separator=separator):
                    result = renderer.html_to_markdown(
                        f"<p>First<br>Second</p>{separator}<p>Next</p>", "2005-01-02"
                    )
                    self.assertEqual(result, "First  \nSecond\n\nNext")

    def test_dead_image_uses_wayback_snapshot_when_available(self):
        class DeadChecker:
            skip_links = False

            def check(self, url):
                return {
                    "live": False, "status": 404, "final_url": url,
                    "content_type": "text/html",
                }

            def archive(self, url, date):
                return "https://web.archive.org/web/20050102/https://example.com/missing.jpg"

        with tempfile.TemporaryDirectory() as directory:
            media = converter.MediaStore(Path(directory), "media", DeadChecker())
            renderer = converter.MarkdownRenderer(
                DeadChecker(), media, "https://example.com", 450, True
            )
            result = renderer.html_to_markdown(
                '<img src="/missing.jpg" alt="Old image">', "2005-01-02"
            )
        self.assertEqual(
            result,
            "![Old image](https://web.archive.org/web/20050102/https://example.com/missing.jpg)",
        )

    def test_dead_image_without_snapshot_is_backup_embed_not_nested_link(self):
        image_url = "http://pics.nakama.ca/a0/2a/4ba5ba3555ee2aa0_196.jpg"
        with tempfile.TemporaryDirectory() as directory:
            checker = converter.LinkChecker(Path(directory), True)
            checker.skip_links = False
            media = converter.MediaStore(Path(directory), "media", checker)
            renderer = converter.MarkdownRenderer(
                checker, media, "http://nakama.ca", 450, True, me_slug="bernie"
            )
            with patch.object(checker, "check", return_value={
                "live": False, "status": 404, "content_type": "", "final_url": image_url,
            }) as check, patch.object(checker, "archive", return_value=None):
                for source in (
                    f'<img src="{image_url}" alt="4MY5KIDS">',
                    f'<a href="http://nakama.ca/gallery/jansen/4ba5ba3555ee2aa0">'
                    f'<img src="{image_url}" alt="4MY5KIDS"></a>',
                ):
                    result = renderer.html_to_markdown(source, "2006-12-07")
                    self.assertEqual(result, "![[bernie/media/4ba5ba3555ee2aa0_196.jpg]]")
                self.assertTrue(all(call.args[0] == image_url for call in check.call_args_list))

    def test_failed_image_download_uses_original_backup_filename(self):
        url = "http://www.whoyouknow.ca/images/MixAndBurn2.JPG?size=large"
        with tempfile.TemporaryDirectory() as directory:
            checker = converter.LinkChecker(Path(directory), True)
            checker.skip_links = False
            media = converter.MediaStore(Path(directory), "photos", checker)
            renderer = converter.MarkdownRenderer(
                checker, media, "http://www.whoyouknow.ca", 450, False, me_slug="bernie"
            )
            with patch.object(checker, "check", return_value={
                "live": True, "content_type": "image/jpeg", "final_url": "https://cdn.example/new.jpg",
            }), patch.object(media, "download", side_effect=RuntimeError("download failed")), \
                 self.assertLogs(level="WARNING"):
                result = renderer.html_to_markdown(
                    f'<a href="/gallery"><img src="{url}" alt="Photo"></a>', "2007-01-02"
                )
            self.assertEqual(result, "![[bernie/photos/MixAndBurn2.JPG]]")
            self.assertFalse(media.folder.exists())
        self.assertEqual(converter.MediaStore.filename_for("https://example.com/A%20B.JPG"), "A B.JPG")

    def test_anchor_wrapping_same_image_emits_only_local_embed(self):
        class LiveChecker:
            skip_links = False

            def check(self, url):
                return {
                    "live": True, "status": 200, "final_url": url,
                    "content_type": "image/jpeg",
                }

            def archive(self, url, date):
                self.fail("A live image should not need a Wayback lookup")

        with tempfile.TemporaryDirectory() as directory:
            checker = LiveChecker()
            media = converter.MediaStore(Path(directory), "media", checker)
            renderer = converter.MarkdownRenderer(
                checker, media, "https://example.com", 450, True
            )
            with patch.object(media, "download", return_value="media/image.jpg") as download:
                result = renderer.html_to_markdown(
                    '<a href="/wp-content/uploads/image.jpg">'
                    '<img src="https://example.com/wp-content/uploads/image.jpg" alt="Photo">'
                    "</a>",
                    "2005-01-02",
                )
        self.assertEqual(result, "![[media/image.jpg|450]]")
        download.assert_called_once()

    def test_frontmatter_includes_wall_clock_date_and_time(self):
        post = converter.Post(
            id="19",
            title="A title",
            slug="a-title",
            date="2005-01-02",
            time="23:47",
            content="<p>Body</p>",
            categories=["uncategorized", "Uncategorized", "personal"],
        )
        with tempfile.TemporaryDirectory() as directory:
            checker = converter.LinkChecker(Path(directory), skip_links=True)
            media = converter.MediaStore(Path(directory), "media", checker)
            renderer = converter.MarkdownRenderer(checker, media, "https://example.com", 450, True)
            markdown = converter._render_post(
                post, [], "me", "https://example.com", renderer
            )
        self.assertIn('date: "2005-01-02"', markdown)
        self.assertIn('time: "23:47"', markdown)
        self.assertIn("url: https://example.com/?p=19", markdown)
        self.assertIn("people:\n  - me\n", markdown)
        self.assertNotIn("[[me]]", markdown)
        self.assertNotIn("uncategorized", markdown.casefold())
        self.assertIn("  - post", markdown)
        self.assertIn("  - personal", markdown)
        self.assertIn("service: wordpress", markdown)

    def test_cli_converts_dump_offline_without_creating_a_link_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config"
            config.mkdir()
            (config / "People.json").write_text(
                json.dumps([{"slug": "me", "first_name": "Blog", "last_name": "Author"}]),
                encoding="utf-8",
            )
            dump = root / "blog.sql"
            dump.write_text(
                "INSERT INTO `wp_options` (`option_name`,`option_value`) VALUES "
                "('siteurl','https://example.com');\n"
                "INSERT INTO `wp_posts` "
                "(`ID`,`post_status`,`post_date`,`post_title`,`post_name`,`post_content`) "
                "VALUES ('23','publish','2006-07-08 09:10:11','Test post','test-post',"
                "'Blog says hello\\nSecond line<!--more-->');\n",
                encoding="cp1252",
            )
            output = root / "output"
            console = io.StringIO()
            with contextlib.redirect_stdout(console):
                result = converter.main([
                    str(dump), "-c", str(config), "-o", str(output), "-m", "me",
                    "--skip-links",
                ])
            self.assertEqual(console.getvalue(), "")
            markdown = (output / "People" / "me" / "2006-07-08 Test post.md").read_text(encoding="utf-8")
            self.assertFalse((output / "2006-07-08 Test post.md").exists())
            (config / "settings.json").write_text(
                json.dumps({"people-subfolder": "Contacts"}), encoding="utf-8"
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(converter.main([
                    str(dump), "-c", str(config), "-o", str(output), "-m", "me",
                    "--skip-links",
                ]), 0)
            self.assertTrue((output / "Contacts" / "me" / "2006-07-08 Test post.md").exists())
            self.assertEqual(result, 0)
            self.assertIn('time: "09:10"', markdown)
            self.assertIn("# Test post", markdown)
            self.assertNotIn("<!--more-->", markdown)
            self.assertFalse((output / "wordpress_sql_md_link_cache.json").exists())


if __name__ == "__main__":
    unittest.main()
