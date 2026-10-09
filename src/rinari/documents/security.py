"""Inspección de contenedores antes de que un parser abra el archivo.

El formato se decide por el contenido, no por la extensión: un `.pptx` que en
realidad es un `.pptm` con macros, o un OOXML cifrado (que es un archivo
compuesto OLE, no un ZIP), se reconoce aquí. También se aplican los límites
del ZIP (miembros, tamaño expandido, ratio, rutas) y se inventarían los
riesgos que condicionan la edición: macros, vínculos externos, objetos
incrustados, firmas.
"""

from __future__ import annotations

import posixpath
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rinari.documents.contracts import DocumentError, DocumentErrorCode

MAX_MEMBERS = 20_000
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_RATIO = 200

_CFB = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

_MAIN_TYPES = {
    "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml": "pptx",
    "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml": "pptm",
    "application/vnd.openxmlformats-officedocument.presentationml.template.main+xml": "potx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml": "xlsx",
    "application/vnd.ms-excel.sheet.macroEnabled.main+xml": "xlsm",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml": "xltx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml": "docx",
    "application/vnd.ms-word.document.macroEnabled.main+xml": "docm",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml": "dotx",
}


@dataclass(slots=True)
class ContainerReport:
    kind: str
    parts: list[dict[str, Any]] = field(default_factory=list)
    expanded_bytes: int = 0
    risks: list[dict[str, Any]] = field(default_factory=list)
    encrypted: bool = False

    def flag(self, code: str, detail: str, **extra: Any) -> None:
        self.risks.append({"code": code, "detail": detail, **extra})

    def has(self, code: str) -> bool:
        return any(risk["code"] == code for risk in self.risks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "part_count": len(self.parts),
            "expanded_bytes": self.expanded_bytes,
            "encrypted": self.encrypted,
            "risks": self.risks,
        }


def sniff(path: Path) -> str:
    """Formato real por cabecera y contenido: pptx/xlsx/docx/pdf o su variante."""
    with path.open("rb") as stream:
        head = stream.read(8)
    if head.startswith(b"%PDF"):
        return "pdf"
    if head == _CFB:
        # OLE: binario antiguo (.ppt/.xls/.doc) u OOXML cifrado.
        raise DocumentError(
            DocumentErrorCode.ENCRYPTED_INPUT
            if _ole_has_encryption(path)
            else DocumentErrorCode.UNSUPPORTED_FORMAT,
            "The file is password-protected"
            if _ole_has_encryption(path)
            else "Legacy binary Office files (.ppt/.xls/.doc) need a converter",
            action="Save it as .pptx/.xlsx/.docx without a password and attach it again",
        )
    if not head.startswith(b"PK"):
        raise DocumentError(DocumentErrorCode.UNSUPPORTED_FORMAT, "Not an Office or PDF file")
    try:
        with zipfile.ZipFile(path) as archive:
            raw = archive.read("[Content_Types].xml")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise DocumentError(
            DocumentErrorCode.UNSUPPORTED_FORMAT, "Not an Office Open XML package"
        ) from exc
    from defusedxml import ElementTree

    root = ElementTree.fromstring(raw)
    for node in root:
        kind = _MAIN_TYPES.get(node.attrib.get("ContentType", ""))
        if kind:
            return kind
    raise DocumentError(DocumentErrorCode.UNSUPPORTED_FORMAT, "Unknown Office package type")


def _ole_has_encryption(path: Path) -> bool:
    # El directorio OLE guarda los nombres en UTF-16; basta con buscarlos.
    with path.open("rb") as stream:
        blob = stream.read(512 * 1024)
    return "EncryptionInfo".encode("utf-16-le") in blob


def inspect_container(path: Path) -> ContainerReport:
    """Límites del ZIP e inventario de riesgos de un paquete OOXML."""
    kind = sniff(path)
    report = ContainerReport(kind=kind)
    if kind == "pdf":
        _inspect_pdf(path, report)
        return report
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise DocumentError(DocumentErrorCode.UNSUPPORTED_FORMAT, "Damaged Office package") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS:
            raise DocumentError(
                DocumentErrorCode.ZIP_LIMIT_EXCEEDED, f"Package has more than {MAX_MEMBERS} parts"
            )
        for info in infos:
            name = info.filename
            normalized = posixpath.normpath(name)
            if name.startswith(("/", "\\")) or normalized.startswith("..") or ":" in name:
                raise DocumentError(
                    DocumentErrorCode.UNSAFE_EXTERNAL_RESOURCE, f"Unsafe part path: {name}"
                )
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise DocumentError(
                    DocumentErrorCode.UNSAFE_EXTERNAL_RESOURCE, f"Symbolic link in package: {name}"
                )
            if info.file_size > MAX_MEMBER_BYTES:
                raise DocumentError(
                    DocumentErrorCode.ZIP_LIMIT_EXCEEDED, f"Part {name} expands beyond the limit"
                )
            if info.compress_size and info.file_size / info.compress_size > MAX_RATIO:
                raise DocumentError(
                    DocumentErrorCode.ZIP_LIMIT_EXCEEDED, f"Part {name} has an unsafe ratio"
                )
            report.expanded_bytes += info.file_size
            if report.expanded_bytes > MAX_EXPANDED_BYTES:
                raise DocumentError(
                    DocumentErrorCode.ZIP_LIMIT_EXCEEDED, "Package expands beyond the limit"
                )
            report.parts.append({"name": name, "size": info.file_size, "crc": info.CRC})
        names = [part["name"] for part in report.parts]
        if kind.endswith("m") or any(n.endswith("vbaProject.bin") for n in names):
            report.flag(
                "macros",
                "Contains VBA macros; they are never run",
            )
        # Un gráfico nativo guarda sus datos como un .xlsx incrustado: eso es
        # el gráfico, no un objeto OLE ajeno.
        chart_data = any("/charts/" in n for n in names)
        embedded = [
            n
            for n in names
            if "/embeddings/" in n and not (chart_data and n.lower().endswith(".xlsx"))
        ]
        if embedded:
            report.flag(
                "embedded_objects", "Contains embedded objects (OLE/packages)", count=len(embedded)
            )
        if any("/activeX/" in n for n in names):
            report.flag("activex", "Contains ActiveX controls")
        if any(n.startswith("_xmlsignatures/") for n in names):
            report.flag("signed", "Digitally signed; an edit invalidates the signature")
        if any("/externalLinks/" in n for n in names):
            report.flag("external_links", "Links to other workbooks; they are not refreshed")
        if any("/connections.xml" in n or n.endswith("connections.xml") for n in names):
            report.flag("data_connections", "Has data connections; they are not refreshed")
        if any("/pivotTables/" in n or "/pivotCache/" in n for n in names):
            report.flag("pivot_tables", "Contains PivotTables")
        if any("/diagrams/" in n for n in names):
            report.flag("smartart", "Contains SmartArt diagrams")
        if any(n.endswith(".rels") for n in names):
            external = _external_targets(archive, [n for n in names if n.endswith(".rels")])
            if external:
                report.flag(
                    "external_targets",
                    "References external resources; they are never fetched",
                    count=len(external),
                )
    return report


def _external_targets(archive: zipfile.ZipFile, rels: list[str]) -> list[str]:
    from defusedxml import ElementTree

    found: list[str] = []
    for name in rels[:2000]:
        try:
            root = ElementTree.fromstring(archive.read(name))
        except Exception:
            continue
        for node in root:
            if node.attrib.get("TargetMode") == "External":
                kind = node.attrib.get("Type", "").rsplit("/", 1)[-1]
                # Los hipervínculos son contenido normal; lo que el render
                # podría cargar (imágenes, plantillas, vínculos) es un riesgo.
                if kind != "hyperlink":
                    found.append(node.attrib.get("Target", ""))
    return found


def _inspect_pdf(path: Path, report: ContainerReport) -> None:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(str(path), strict=False)
    except PdfReadError as exc:
        raise DocumentError(DocumentErrorCode.UNSUPPORTED_FORMAT, "Damaged PDF") from exc
    if reader.is_encrypted:
        report.encrypted = True
        raise DocumentError(
            DocumentErrorCode.PASSWORD_REQUIRED,
            "The PDF is encrypted",
            action="Provide an unencrypted copy",
        )
    root = reader.trailer.get("/Root", {})
    if "/AcroForm" in root:
        report.flag("forms", "Has form fields")
    if "/OpenAction" in root or "/AA" in root:
        report.flag("actions", "Has document actions; they are never run")
    names = root.get("/Names", {}) if hasattr(root, "get") else {}
    if hasattr(names, "get") and names.get("/EmbeddedFiles"):
        report.flag("attachments", "Has embedded files")
    if hasattr(names, "get") and names.get("/JavaScript"):
        report.flag("javascript", "Has JavaScript; it is never run")
    report.parts.append({"name": "pages", "size": len(reader.pages)})
