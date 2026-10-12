from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

MAX_MEMBER_BYTES = 30 * 1024 * 1024
MAX_TOTAL_BYTES = 300 * 1024 * 1024
MIN_CHAPTER_CHARS = 30

BLOCK_TAGS = {"p", "div", "li", "blockquote", "tr", "br", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd", "pre"}
SKIP_TAGS = {"script", "style", "head", "rt", "rp", "svg"}
HEADING_TAGS = {"h1", "h2", "h3"}


class EpubError(ValueError):
    pass


@dataclass
class EpubChapter:
    title: str
    paragraphs: list[str] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return sum(len(p) for p in self.paragraphs)


@dataclass
class EpubBook:
    title: str
    author: str
    chapters: list[EpubChapter]


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[str] = []
        self.heading = ""
        self._buffer: list[str] = []
        self._skip_depth = 0
        self._heading_depth = 0
        self._heading_buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in SKIP_TAGS:
            self._skip_depth += 1
        if tag in BLOCK_TAGS:
            self._flush()
        if tag in HEADING_TAGS and not self.heading:
            self._heading_depth += 1

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag in BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if tag in BLOCK_TAGS:
            self._flush()
        if tag in HEADING_TAGS and self._heading_depth:
            self._heading_depth -= 1
            if not self._heading_depth:
                self.heading = _clean(" ".join(self._heading_buffer))
                self._heading_buffer = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self._buffer.append(data)
        if self._heading_depth:
            self._heading_buffer.append(data)

    def _flush(self) -> None:
        text = _clean("".join(self._buffer))
        self._buffer = []
        if text and re.search(r"\w", text):
            self.paragraphs.append(text)

    def close(self) -> None:
        super().close()
        self._flush()


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _read_member(zf: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = zf.getinfo(name)
    except KeyError as exc:
        raise EpubError(f"EPUB에 {name} 파일이 없습니다.") from exc
    if info.file_size > MAX_MEMBER_BYTES:
        raise EpubError(f"{name} 파일이 너무 큽니다.")
    return zf.read(info)


def _parse_xml(data: bytes, label: str) -> ET.Element:
    if b"<!ENTITY" in data:
        raise EpubError(f"{label}에 허용되지 않는 XML entity 선언이 있습니다.")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise EpubError(f"{label}을(를) 읽을 수 없습니다: {exc}") from exc


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _resolve(base_dir: str, href: str) -> str:
    return posixpath.normpath(posixpath.join(base_dir, unquote(href.split("#", 1)[0])))


def _toc_titles(zf: zipfile.ZipFile, opf_dir: str, manifest: dict[str, dict], spine_toc_id: str | None) -> dict[str, str]:
    titles: dict[str, str] = {}
    nav = next((item for item in manifest.values() if "nav" in item["properties"].split()), None)
    if nav:
        try:
            nav_dir = posixpath.dirname(nav["path"])
            parser = _NavParser(nav_dir)
            parser.feed(_decode(_read_member(zf, nav["path"])))
            titles.update({path: label for path, label in parser.entries if label})
        except EpubError:
            pass
    ncx = manifest.get(spine_toc_id or "")
    if ncx and not titles:
        try:
            root = _parse_xml(_read_member(zf, ncx["path"]), "toc.ncx")
            ncx_dir = posixpath.dirname(ncx["path"])
            for point in root.iter():
                if _local(point.tag) != "navPoint":
                    continue
                label = next((_clean("".join(t.itertext())) for t in point.iter() if _local(t.tag) == "text"), "")
                content = next((c for c in point if _local(c.tag) == "content"), None)
                if content is not None and label and content.get("src"):
                    titles.setdefault(_resolve(ncx_dir, content.get("src")), label)
        except EpubError:
            pass
    return titles


class _NavParser(HTMLParser):
    def __init__(self, base_dir: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_dir = base_dir
        self.entries: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []
        self._in_toc = False
        self._nav_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        if tag == "nav":
            self._nav_depth += 1
            kind = attributes.get("epub:type") or attributes.get("type") or ""
            if "toc" in kind.split():
                self._in_toc = True
        elif tag == "a" and self._in_toc and attributes.get("href"):
            self._href = attributes["href"]
            self._text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.entries.append((_resolve(self.base_dir, self._href), _clean("".join(self._text))))
            self._href = None
        elif tag == "nav":
            self._nav_depth -= 1
            if not self._nav_depth:
                self._in_toc = False

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)


def parse_epub(path: Path) -> EpubBook:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise EpubError("올바른 EPUB 파일이 아닙니다.") from exc
    with zf:
        if sum(info.file_size for info in zf.infolist()) > MAX_TOTAL_BYTES:
            raise EpubError("EPUB 압축 해제 크기가 너무 큽니다.")
        container = _parse_xml(_read_member(zf, "META-INF/container.xml"), "container.xml")
        rootfile = next((el.get("full-path") for el in container.iter() if _local(el.tag) == "rootfile" and el.get("full-path")), None)
        if not rootfile:
            raise EpubError("EPUB의 OPF 경로를 찾을 수 없습니다.")
        opf = _parse_xml(_read_member(zf, rootfile), "OPF")
        opf_dir = posixpath.dirname(rootfile)

        title = author = ""
        manifest: dict[str, dict] = {}
        spine: list[str] = []
        spine_toc_id: str | None = None
        for el in opf.iter():
            name = _local(el.tag)
            if name == "title" and not title:
                title = _clean("".join(el.itertext()))
            elif name == "creator" and not author:
                author = _clean("".join(el.itertext()))
            elif name == "item" and el.get("id") and el.get("href"):
                manifest[el.get("id")] = {
                    "path": _resolve(opf_dir, el.get("href")),
                    "type": el.get("media-type", ""),
                    "properties": el.get("properties", ""),
                }
            elif name == "spine":
                spine_toc_id = el.get("toc")
            elif name == "itemref" and el.get("idref"):
                spine.append(el.get("idref"))
        if not spine:
            raise EpubError("EPUB 본문(spine)이 비어 있습니다.")

        toc_titles = _toc_titles(zf, opf_dir, manifest, spine_toc_id)
        chapters: list[EpubChapter] = []
        for idref in spine:
            item = manifest.get(idref)
            if not item or "html" not in item["type"] or "nav" in item["properties"].split():
                continue
            extractor = _TextExtractor()
            extractor.feed(_decode(_read_member(zf, item["path"])))
            extractor.close()
            paragraphs = extractor.paragraphs
            if sum(len(p) for p in paragraphs) < MIN_CHAPTER_CHARS:
                continue
            toc_title = toc_titles.get(item["path"])
            if toc_titles and not toc_title and chapters:
                chapters[-1].paragraphs.extend(paragraphs)
                continue
            chapters.append(EpubChapter(toc_title or extractor.heading or f"{len(chapters) + 1}장", paragraphs))
        if not chapters:
            raise EpubError("EPUB에서 읽을 수 있는 본문을 찾지 못했습니다.")
        return EpubBook(title or path.stem, author, chapters)
