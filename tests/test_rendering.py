from __future__ import annotations

from dataclasses import FrozenInstanceError
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest

from markdown_finder.rendering import render_document, render_markdown


class HTMLDocument(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def attributes(self, tag):
        return [attrs for name, attrs in self.tags if name == tag]


class RenderingTests(unittest.TestCase):
    def render(self, source, path=Path("/tmp/notes/README.md")):
        return render_markdown(source, path=path)

    def test_heading_outline_extracts_visible_unicode_text_and_unique_ids(self):
        document = self.render(
            "# 中文 **標題** `code` [連結](other.md) ![圖片](image.png)\n\n"
            "## Repeat\n\n## Repeat\n\n## Repeat-1\n\n## !!!\n\n## ???\n"
        )
        self.assertEqual(document.title, "中文 標題 code 連結 圖片")
        self.assertEqual(document.headings[0].anchor, "中文-標題-code-連結-圖片")
        self.assertEqual([heading.anchor for heading in document.headings[1:]],
                         ["repeat", "repeat-1", "repeat-1-1", "section", "section-1"])
        self.assertEqual([heading.level for heading in document.headings], [1, 2, 2, 2, 2, 2])
        parsed = HTMLDocument(document.html)
        self.assertEqual(parsed.attributes("h1")[0]["id"], document.headings[0].anchor)
        with self.assertRaises(FrozenInstanceError):
            document.title = "Changed"

    def test_read_bom_utf8_and_filename_title_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "筆記.md"
            path.write_text("\ufeff沒有一級標題\n\n## 第二級", encoding="utf-8")
            document = render_document(path)
            self.assertEqual(document.path, path)
            self.assertEqual(document.title, "筆記.md")
            self.assertNotIn("\ufeff", document.html)
            self.assertIn("沒有一級標題", document.html)

    def test_file_and_decode_errors_propagate(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.md"
            with self.assertRaises(FileNotFoundError):
                render_document(path)
            path.write_bytes(b"\xff\xfe")
            with self.assertRaises(UnicodeDecodeError):
                render_document(path)

    def test_relative_resources_use_percent_encoded_original_directory(self):
        path = Path('/tmp/閱讀 筆記 & "folder"/README.md')
        document = self.render('![照片](<images/圖 1.png>)\n\n[另一份](<next note.md#段落>)', path)
        parsed = HTMLDocument(document.html)
        self.assertEqual(parsed.attributes("base")[0]["href"], path.parent.as_uri() + "/")
        self.assertEqual(parsed.attributes("img")[0]["src"], "images/%E5%9C%96%201.png")
        self.assertEqual(parsed.attributes("a")[0]["href"], "next%20note.md#%E6%AE%B5%E8%90%BD")

    def test_symlink_open_preserves_logical_resource_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            actual = root / "original"
            actual.mkdir()
            (actual / "note.md").write_text("# Note", encoding="utf-8")
            logical = root / "note.md"
            logical.symlink_to(actual / "note.md")
            document = render_document(logical)
            self.assertEqual(document.path, logical)
            self.assertEqual(HTMLDocument(document.html).attributes("base")[0]["href"], root.as_uri() + "/")

    def test_tables_tasks_footnotes_strikethrough_and_blockquotes(self):
        document = self.render(
            "| 名稱 | Value |\n| :--- | ---: |\n| CPU | 64 |\n\n"
            "- [x] 完成\n- [ ] 未完成\n\n> 引用 **內容**\n\n"
            "~~舊文字~~ 註記[^note]\n\n[^note]: 註解內容\n"
        )
        parsed = HTMLDocument(document.html)
        self.assertEqual(len(parsed.attributes("table")), 1)
        boxes = parsed.attributes("input")
        self.assertEqual(len(boxes), 2)
        self.assertTrue(all("disabled" in box for box in boxes))
        self.assertIn("checked", boxes[0])
        self.assertNotIn("checked", boxes[1])
        self.assertIn("<blockquote>", document.html)
        self.assertIn("<s>舊文字</s>", document.html)
        self.assertIn('id="fn1"', document.html)
        self.assertIn("註解內容", document.html)

    def test_fenced_highlight_and_unknown_language_escaped_fallback(self):
        document = self.render("```python\nif x < 2:\n    print('ok')\n```\n\n"
                               "```not-a-real-language\n<script>alert(1)</script> &\n```\n")
        self.assertIn('class="language-python"', document.html)
        self.assertIn('class="k">if</span>', document.html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; &amp;", document.html)
        self.assertFalse(HTMLDocument(document.html).attributes("script"))

    def test_raw_html_and_unsafe_links_do_not_become_active_elements(self):
        document = self.render(
            '<script>alert(1)</script>\n\n<img src="x" onerror="alert(1)">\n\n'
            '<iframe src="https://example.com"></iframe>\n\n'
            '[bad](javascript:alert%281%29)\n\n'
            '[encoded](jav&#x61;script:alert%281%29)\n\n'
            '[data](data:text/html;base64,AAAA)\n\n'
            '[safe](https://example.com)\n'
        )
        parsed = HTMLDocument(document.html)
        self.assertFalse(parsed.attributes("script"))
        self.assertFalse(parsed.attributes("img"))
        self.assertFalse(parsed.attributes("iframe"))
        self.assertEqual([link["href"] for link in parsed.attributes("a")], ["https://example.com"])
        self.assertIn("&lt;script&gt;", document.html)

    def test_csp_blocks_remote_subresources_and_document_scripts(self):
        document = self.render("![Remote](https://example.com/tracker.png)")
        parsed = HTMLDocument(document.html)
        policy = next(meta["content"] for meta in parsed.attributes("meta")
                      if meta.get("http-equiv") == "Content-Security-Policy")
        self.assertIn("default-src 'none'", policy)
        self.assertIn("script-src 'none'", policy)
        self.assertIn("img-src file: data:", policy)
        self.assertNotIn("https:", policy)
        self.assertFalse(parsed.attributes("script"))
        self.assertFalse(parsed.attributes("link"))

    def test_document_larger_than_webengine_sethtml_limit_is_not_truncated(self):
        source = "# Large document\n\n" + ("這是一段長文件文字。" * 80 + "\n\n") * 1000 + "## The end\n"
        document = self.render(source)
        self.assertGreater(len(document.html.encode("utf-8")), 2 * 1024 * 1024)
        self.assertEqual(document.headings[-1].title, "The end")
        self.assertIn('<h2 id="the-end">The end</h2>', document.html)
        self.assertTrue(document.html.endswith("</html>\n"))


if __name__ == "__main__":
    unittest.main()
