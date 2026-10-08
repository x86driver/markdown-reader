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

    def test_details_render_markdown_body_and_outline_without_blank_lines(self):
        document = self.render(
            '<details>\n<summary>展開 **說明** `code`</summary>\n'
            '## 內部章節\n- 清單\n\n'
            '| A | B |\n|---|---|\n| 1 | 2 |\n\n'
            '```python\nprint("ok")\n```\n</details>\n## 外部章節\n'
        )
        parsed = HTMLDocument(document.html)
        self.assertEqual(parsed.attributes("details"), [{}])
        self.assertEqual(parsed.attributes("summary"), [{}])
        self.assertIn('<summary>展開 <strong>說明</strong> <code>code</code></summary>', document.html)
        self.assertEqual([heading.title for heading in document.headings], ["內部章節", "外部章節"])
        self.assertEqual(len(parsed.attributes("ul")), 1)
        self.assertEqual(len(parsed.attributes("table")), 1)
        self.assertEqual(len(parsed.attributes("pre")), 1)
        self.assertIn('</details><h2 id="外部章節">', document.html)

    def test_nested_compact_and_multiline_summaries(self):
        document = self.render(
            '<details OPEN="false">\n<summary>\n多行 **摘要**\n</summary>\n'
            '<details><summary>內層</summary>*內容*</details>\n</details>\n'
            '<DETAILS><SUMMARY>另一區塊</SUMMARY>內容</DETAILS>\n'
        )
        parsed = HTMLDocument(document.html)
        self.assertEqual(parsed.attributes("details"), [{"open": None}, {}, {}])
        self.assertEqual(len(parsed.attributes("summary")), 3)
        self.assertIn('<summary>\n多行 <strong>摘要</strong>\n</summary>', document.html)
        self.assertIn('<details><summary>內層</summary><em>內容</em></details>', document.html)
        self.assertIn('<details><summary>另一區塊</summary>內容</details>', document.html)

    def test_details_inside_blockquotes_lists_and_reference_links(self):
        document = self.render(
            '> <details>\n> <summary>[摘要][link]</summary>\n>\n> 引用內容\n> </details>\n\n'
            '- <details>\n  <summary>清單摘要</summary>\n\n  清單內容\n  </details>\n\n'
            '[link]: https://example.com\n'
        )
        parsed = HTMLDocument(document.html)
        self.assertEqual(len(parsed.attributes("details")), 2)
        self.assertIn('<blockquote><details><summary><a href="https://example.com">摘要</a></summary>', document.html)
        self.assertIn('<li><details><summary>清單摘要</summary>', document.html)

    def test_disclosure_attributes_are_stripped_and_other_html_stays_escaped(self):
        document = self.render(
            '<details open onclick="alert(1)" id="fn1" style="display:none" name="group">\n'
            '<summary onmouseover="alert(2)"><img src="x" onerror="alert(3)">摘要</summary>\n'
            '<script>alert(4)</script>\n<iframe src="https://example.com"></iframe>\n'
            '[bad](javascript:alert%281%29)\n</details>\n'
            '<details-other>text</details-other>\n<summary-other>text</summary-other>\n'
        )
        parsed = HTMLDocument(document.html)
        self.assertEqual(parsed.attributes("details"), [{"open": None}])
        self.assertEqual(parsed.attributes("summary"), [{}])
        for tag in ("img", "script", "iframe", "a", "details-other", "summary-other"):
            self.assertFalse(parsed.attributes(tag))
        self.assertIn('&lt;img src=', document.html)
        self.assertIn('&lt;script&gt;', document.html)

    def test_disclosure_tags_in_code_and_escaped_text_stay_literal(self):
        document = self.render(
            '```html\n<details><summary>範例</summary></details>\n```\n\n'
            '    <details>\n    <summary>縮排範例</summary>\n    </details>\n\n'
            '`<details><summary>行內範例</summary></details>`\n\n'
            r'\<details>\<summary>跳脫範例\</summary>\</details>' + '\n'
        )
        parsed = HTMLDocument(document.html)
        self.assertFalse(parsed.attributes("details"))
        self.assertFalse(parsed.attributes("summary"))
        self.assertEqual(len(parsed.attributes("pre")), 2)

    def test_comments_hide_single_line_inline_and_multiline_markdown(self):
        document = self.render(
            '# 文件 <!-- 標題備註 -->\n\n'
            '前文 <!-- 行內 **隱藏文字** --> 後文\n\n'
            '<!-- 單行隱藏文字 -->\n'
            '<!--\n\n# 隱藏的一級標題\n## 重複\n'
            '**多行隱藏文字**\n\n'
            '<details open><summary>隱藏摘要</summary>隱藏折疊</details>\n'
            '<script>alert(1)</script>\n![隱藏圖片](hidden.png)\n'
            '```\n隱藏程式碼\n```\n-->\n'
            '## 重複\n\n前文 <!-- 跨行隱藏\n另一行隱藏 --> 後文\n'
        )
        self.assertEqual(document.title, '文件')
        self.assertEqual([(heading.title, heading.anchor) for heading in document.headings],
                         [('文件', '文件'), ('重複', '重複')])
        self.assertIn('<p>前文  後文</p>', document.html)
        for hidden in ('備註', '隱藏', 'hidden.png', 'alert(1)', '&lt;!--', '--&gt;'):
            self.assertNotIn(hidden, document.html)
        parsed = HTMLDocument(document.html)
        for tag in ('details', 'summary', 'script', 'img', 'pre', 'strong'):
            self.assertFalse(parsed.attributes(tag))

    def test_comment_contents_do_not_define_links_or_footnotes(self):
        document = self.render(
            '<!--\n[link]: https://hidden.invalid\n[^note]: 隱藏註腳\n-->\n'
            '[link] and missing[^note]\n\n'
            '<!-- [safe]: https://hidden.invalid -->\n'
            '[safe]\n\n[safe]: https://example.com\n'
        )
        parsed = HTMLDocument(document.html)
        self.assertEqual([link['href'] for link in parsed.attributes('a')], ['https://example.com'])
        self.assertIn('[link] and missing[^note]', document.html)
        self.assertNotIn('隱藏註腳', document.html)
        self.assertNotIn('hidden.invalid', document.html)
        self.assertNotIn('footnote-ref', document.html.split('<main class="markdown-body">', 1)[1])

    def test_comments_in_quotes_lists_tables_and_disclosures(self):
        document = self.render(
            '> <!--\n> ## 引用隱藏標題\n> 隱藏引用\n> -->\n> 引用內容\n\n'
            '- <!--\n  ## 清單隱藏標題\n  隱藏清單\n  -->\n  清單內容\n\n'
            '| 名稱 | 值 |\n|---|---|\n| A <!-- 隱藏表格 --> | 1 |\n\n'
            '<details>\n<summary>摘要 <!-- 隱藏摘要 --> **文字**</summary>\n'
            '<!--\n## 折疊隱藏標題\n隱藏折疊\n-->\n## 折疊章節\n</details>\n'
        )
        self.assertNotIn('隱藏', document.html)
        self.assertEqual([heading.title for heading in document.headings], ['折疊章節'])
        parsed = HTMLDocument(document.html)
        for tag in ('blockquote', 'ul', 'table', 'details', 'summary', 'strong'):
            self.assertEqual(len(parsed.attributes(tag)), 1)
        self.assertIn('<summary>摘要  <strong>文字</strong></summary>', document.html)
        self.assertIn('引用內容', document.html)
        self.assertIn('清單內容', document.html)

    def test_comment_block_preserves_suffixes_and_interrupts_paragraphs(self):
        document = self.render(
            '前文\n<!-- 隱藏第一段 -->**後文** <!-- 隱藏第二段 -->\n'
            '<!--\n隱藏多行\n-->[連結][link]\n'
            '<!-- 隱藏備註 --><details><summary>摘要</summary>內容</details>\n'
            '<!-- 隱藏備註 --><script>alert(1)</script>\n'
            '## 最後章節\n\n[link]: https://example.com\n'
        )
        self.assertNotIn('隱藏', document.html)
        self.assertIn('<p>前文</p>', document.html)
        self.assertIn('<strong>後文</strong>', document.html)
        self.assertIn('<details><summary>摘要</summary>內容</details>', document.html)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', document.html)
        self.assertEqual([heading.title for heading in document.headings], ['最後章節'])
        parsed = HTMLDocument(document.html)
        self.assertEqual([link['href'] for link in parsed.attributes('a')], ['https://example.com'])
        self.assertFalse(parsed.attributes('script'))

    def test_multiline_summary_comments_do_not_end_summary_or_emit_hidden_tags(self):
        source = (
            '<details>\n<summary>摘要 <!--\n</summary>\n'
            '<details>隱藏折疊</details>\n\n## 隱藏標題\n'
            '--> **尾摘要**\n</summary>\n## 內容\n</details>\n'
        )
        for prefix in ('', '> '):
            with self.subTest(prefix=prefix):
                document = self.render(''.join(prefix + line for line in source.splitlines(keepends=True)))
                self.assertNotIn('隱藏', document.html)
                self.assertNotIn('&lt;!--', document.html)
                self.assertEqual([heading.title for heading in document.headings], ['內容'])
                parsed = HTMLDocument(document.html)
                self.assertEqual(parsed.attributes('details'), [{}])
                self.assertEqual(parsed.attributes('summary'), [{}])
                self.assertIn('<summary>摘要  <strong>尾摘要</strong>\n</summary>', document.html)

    def test_comment_examples_in_code_escapes_and_entities_stay_literal(self):
        document = self.render(
            '```\n<!-- 圍欄範例\n## 程式碼標題\n-->\n```\n\n'
            '    <!-- 縮排範例 -->\n\n'
            '`<!-- 行內範例 -->`\n\n'
            r'\<!-- 跳脫範例 -->' + '\n\n'
            '&lt;!-- 實體範例 --&gt;\n'
        )
        self.assertFalse(document.headings)
        self.assertEqual(len(HTMLDocument(document.html).attributes('pre')), 2)
        for example in ('圍欄範例', '縮排範例', '行內範例', '跳脫範例', '實體範例'):
            self.assertIn('&lt;!-- ' + example, document.html)
        self.assertIn('## 程式碼標題', document.html)

    def test_unclosed_comments_respect_block_boundaries_and_inline_literals(self):
        document = self.render(
            '# 文件\n\n> <!--\n> ## 引用隱藏\n\n'
            '## 引用之外\n\n'
            '- <!--\n  ## 清單隱藏\n\n'
            '## 清單之外\n\n前文 <!-- 未閉合行內\n\n'
            '<!-- 未閉合區塊\n\n## 文件尾隱藏\n'
        )
        self.assertEqual([heading.title for heading in document.headings],
                         ['文件', '引用之外', '清單之外'])
        self.assertNotIn('隱藏', document.html)
        self.assertNotIn('未閉合區塊', document.html)
        self.assertIn('前文 &lt;!-- 未閉合行內', document.html)

    def test_empty_and_adjacent_comments_preserve_surrounding_text(self):
        document = self.render(
            '# A<!---->B <!-- 備註 --> C\n\n'
            'A<!----><!-- 備註 -->B<!-->C<!--->D\n\n'
            '<!----><!---->尾文\n'
        )
        self.assertEqual(document.title, 'AB  C')
        self.assertEqual(document.headings[0].anchor, 'ab-c')
        self.assertIn('<p>ABCD</p>', document.html)
        self.assertIn('尾文', document.html)
        self.assertNotIn('備註', document.html)
        self.assertNotIn('&lt;!--', document.html)

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
