"""Optional localhost GROBID header parser (no cloud requirement).

Uses consolidateHeader=0 so GROBID does not call Crossref on our behalf.
Fallback to PyMuPDF metadata/rules when the service is not available.
"""
from __future__ import annotations

import re
import urllib.request
from pathlib import Path
from typing import Any, Dict
from xml.etree import ElementTree


def header_metadata(source: Path, server: str) -> Dict[str, Any]:
    if source.suffix.lower() != ".pdf" or not server:
        return {}
    # Do not permit arbitrary SSRF targets supplied via local settings.
    url = server.rstrip("/")
    if url not in {"http://127.0.0.1:8070", "http://localhost:8070"}:
        return {}
    boundary = "rw-grobid-file"
    payload = source.read_bytes()
    if len(payload) > 50 * 1024 * 1024:
        return {}
    body = (
        ("--" + boundary + "\r\nContent-Disposition: form-data; name=\"consolidateHeader\"\r\n\r\n0\r\n"
         "--" + boundary + "\r\nContent-Disposition: form-data; name=\"input\"; filename=\"paper.pdf\"\r\n"
         "Content-Type: application/pdf\r\n\r\n").encode()
        + payload + ("\r\n--" + boundary + "--\r\n").encode()
    )
    request = urllib.request.Request(
        url + "/api/processHeaderDocument", data=body, method="POST",
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary,
                 "Accept": "application/xml"},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            xml = response.read(2 * 1024 * 1024 + 1)
        if len(xml) > 2 * 1024 * 1024:
            return {}
        root = ElementTree.fromstring(xml)
    except (OSError, ValueError, ElementTree.ParseError):
        return {}
    ns = {"t": "http://www.tei-c.org/ns/1.0"}

    def compact(node: Any) -> str:
        return re.sub(r"\s+", " ", " ".join(node.itertext())).strip() if node is not None else ""

    title = root.find(".//t:titleStmt/t:title", ns)
    if title is None:
        title = root.find(".//t:analytic/t:title", ns)
    names = []
    for person in root.findall(".//t:titleStmt/t:author/t:persName", ns):
        first = [compact(x) for x in person.findall("t:forename", ns)]
        last = [compact(x) for x in person.findall("t:surname", ns)]
        display = " ".join(x for x in first + last if x)
        if display and display not in names:
            names.append(display)
    abstract = root.find(".//t:profileDesc/t:abstract", ns)
    keywords = [compact(item) for item in root.findall(".//t:profileDesc/t:textClass/t:keywords/t:term", ns)]
    keywords = [item for item in keywords if item]
    return {"title": compact(title), "authors": names, "abstract": compact(abstract),
            "keywords": keywords, "parser": "grobid_local_header"}
