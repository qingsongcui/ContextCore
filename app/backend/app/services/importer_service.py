import hashlib
import io
import json
import os
import subprocess
import tempfile
import zipfile
from typing import Any
from fastapi import HTTPException
from app.services.content_service import create_content


def _limit_from_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


MAX_UPLOAD_BYTES = _limit_from_env("CONTEXTCORE_IMPORT_MAX_UPLOAD_BYTES", 32 * 1024 * 1024)
MAX_TEXT_INPUT_BYTES = _limit_from_env("CONTEXTCORE_IMPORT_MAX_TEXT_INPUT_BYTES", 8 * 1024 * 1024)
MAX_EXTRACTED_TEXT_BYTES = _limit_from_env("CONTEXTCORE_IMPORT_MAX_EXTRACTED_BYTES", 8 * 1024 * 1024)
MAX_JSON_TOKENS = _limit_from_env("CONTEXTCORE_IMPORT_MAX_JSON_TOKENS", 100_000)
MAX_JSON_DEPTH = _limit_from_env("CONTEXTCORE_IMPORT_MAX_JSON_DEPTH", 64)
MAX_DOCX_EXPANDED_BYTES = _limit_from_env("CONTEXTCORE_IMPORT_MAX_DOCX_EXPANDED_BYTES", 32 * 1024 * 1024)
MAX_DOCX_MEMBERS = _limit_from_env("CONTEXTCORE_IMPORT_MAX_DOCX_MEMBERS", 4096)
MAX_PDF_PAGES = _limit_from_env("CONTEXTCORE_IMPORT_MAX_PDF_PAGES", 500)
MAX_IMPORT_CONCURRENCY = min(8, _limit_from_env("CONTEXTCORE_IMPORT_CONCURRENCY", 2))


def _limit_label(byte_count: int) -> str:
    mib = 1024 * 1024
    return f"{byte_count // mib} MiB" if byte_count % mib == 0 else f"{byte_count} bytes"


def _decode_text(file_bytes: bytes) -> str:
    for encoding in ("utf-8", "gb18030", "gbk", "utf-16", "latin-1"):
        try:
            return file_bytes.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return file_bytes.decode("utf-8", errors="replace")


def _preflight_json(raw_text: str, *, max_tokens: int = MAX_JSON_TOKENS, max_depth: int = MAX_JSON_DEPTH) -> None:
    """Bound JSON parser work before json.loads allocates a Python object graph.

    This lexical pass counts containers and value atoms without retaining
    tokens. Object keys are excluded. Syntax validation remains json.loads'
    responsibility when the input is within the resource budget.
    """
    token_count = 0
    depth = 0
    index = 0
    size = len(raw_text)

    def add_token() -> None:
        nonlocal token_count
        token_count += 1
        if token_count > max_tokens:
            raise HTTPException(
                status_code=413,
                detail=f"JSON document exceeds the {max_tokens}-value import limit",
            )

    while index < size:
        char = raw_text[index]
        if char == '"':
            index += 1
            escaped = False
            while index < size:
                current = raw_text[index]
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    break
                index += 1
            # Object keys are followed (possibly after whitespace) by a colon.
            lookahead = index + 1
            while lookahead < size and raw_text[lookahead].isspace():
                lookahead += 1
            if lookahead >= size or raw_text[lookahead] != ":":
                add_token()
            index += 1
            continue
        if char in "[{":
            depth += 1
            if depth > max_depth:
                raise HTTPException(
                    status_code=413,
                    detail=f"JSON nesting exceeds the {max_depth}-level import limit",
                )
            add_token()
            index += 1
            continue
        if char in "]}":
            depth = max(0, depth - 1)
            index += 1
            continue
        if char.isspace() or char in ",:":
            index += 1
            continue

        # A primitive extends until the next JSON structural delimiter or
        # whitespace. Invalid primitives are left for json.loads to diagnose.
        add_token()
        index += 1
        while index < size and not raw_text[index].isspace() and raw_text[index] not in ",:[]{}\"":
            index += 1


def parse_pdf(file_bytes: bytes | bytearray, *, max_extracted_bytes: int = MAX_EXTRACTED_TEXT_BYTES) -> str:
    try:
        import pypdf
    except ImportError:
        raise HTTPException(status_code=422, detail="PDF parser (pypdf) is not installed")

    try:
        configure = getattr(pypdf, "apply_configuration", None)
        if configure is None:
            raise HTTPException(
                status_code=422,
                detail="PDF import requires a pypdf release with bounded decompression support",
            )
        with configure(
            maximum_declared_stream_length=MAX_UPLOAD_BYTES,
            array_based_stream_maximum_output_length=max_extracted_bytes,
            jbig2_maximum_output_length=max_extracted_bytes,
            lzw_maximum_output_length=max_extracted_bytes,
            run_length_maximum_output_length=max_extracted_bytes,
            zlib_maximum_output_length=max_extracted_bytes,
            brotli_maximum_output_length=max_extracted_bytes,
            stream_decoding_work_maximum_length=max_extracted_bytes * 4,
            stream_parsing_work_maximum_count=250_000,
            stream_filters_maximum_length=8,
            image_maximum_buffer_size=max_extracted_bytes,
            xmp_maximum_input_length=max_extracted_bytes,
            page_tree_maximum_entries=MAX_PDF_PAGES,
            array_maximum_element_count=20_000,
            dictionary_maximum_element_count=20_000,
            form_fields_maximum_entries=10_000,
            outline_maximum_entries=10_000,
        ):
            stream = io.BytesIO(file_bytes)
            reader = pypdf.PdfReader(stream)
            if reader.is_encrypted:
                raise HTTPException(status_code=422, detail="PDF is encrypted and cannot be parsed")

            page_count = len(reader.pages)
            if page_count > MAX_PDF_PAGES:
                raise HTTPException(status_code=413, detail=f"PDF page count exceeds the {MAX_PDF_PAGES}-page import limit")
            pages_text = []
            extracted_bytes = 0
            for page in reader.pages:
                text = page.extract_text() or ""
                if text.strip():
                    clean = text.strip()
                    extracted_bytes += len(clean.encode("utf-8")) + (2 if pages_text else 0)
                    if extracted_bytes > max_extracted_bytes:
                        raise HTTPException(status_code=413, detail="Extracted PDF text exceeds the configured import limit")
                    pages_text.append(clean)

            extracted = "\n\n".join(pages_text).strip()
            if not extracted:
                raise HTTPException(status_code=422, detail="PDF contains no extractable text")
            return extracted
    except HTTPException:
        raise
    except Exception as e:
        if type(e).__name__ == "LimitReachedError":
            raise HTTPException(status_code=413, detail="PDF decompressed data exceeds the configured import limit") from e
        raise HTTPException(status_code=422, detail=f"Failed to parse PDF: {str(e)}")


def parse_docx(
    file_bytes: bytes | bytearray, *, max_extracted_bytes: int = MAX_EXTRACTED_TEXT_BYTES,
    max_expanded_bytes: int = MAX_DOCX_EXPANDED_BYTES,
) -> str:
    try:
        import docx
    except ImportError:
        raise HTTPException(status_code=422, detail="DOCX parser (python-docx) is not installed")

    try:
        # Inspect every member before python-docx loads the XML tree. Metadata
        # checks reject obvious bombs early; bounded streaming also enforces the
        # actual expanded byte budget if central-directory sizes are false.
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            members = archive.infolist()
            if len(members) > MAX_DOCX_MEMBERS:
                raise HTTPException(status_code=413, detail="DOCX archive contains too many files")
            declared_total = sum(member.file_size for member in members)
            if declared_total > max_expanded_bytes or any(member.file_size > max_expanded_bytes for member in members):
                raise HTTPException(status_code=413, detail="DOCX expanded archive exceeds the configured import limit")
            expanded_total = 0
            for member in members:
                with archive.open(member) as source:
                    while True:
                        chunk = source.read(min(64 * 1024, max_expanded_bytes - expanded_total + 1))
                        if not chunk:
                            break
                        expanded_total += len(chunk)
                        if expanded_total > max_expanded_bytes:
                            raise HTTPException(status_code=413, detail="DOCX expanded archive exceeds the configured import limit")

        stream = io.BytesIO(file_bytes)
        doc = docx.Document(stream)
        lines = []
        extracted_bytes = 0

        def append_line(value: str) -> None:
            nonlocal extracted_bytes
            clean = value.strip()
            if not clean:
                return
            extracted_bytes += len(clean.encode("utf-8")) + (2 if lines else 0)
            if extracted_bytes > max_extracted_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"Extracted DOCX text exceeds the {_limit_label(max_extracted_bytes)} limit",
                )
            lines.append(clean)

        if hasattr(doc, "iter_inner_content"):
            from docx.text.paragraph import Paragraph
            from docx.table import Table
            for block in doc.iter_inner_content():
                if isinstance(block, Paragraph):
                    append_line(block.text)
                elif isinstance(block, Table):
                    for row in block.rows:
                        append_line(" | ".join(cell.text.strip() for cell in row.cells if cell.text.strip()))
        else:
            for p in doc.paragraphs:
                append_line(p.text)
            for table in doc.tables:
                for row in table.rows:
                    append_line(" | ".join(cell.text.strip() for cell in row.cells if cell.text.strip()))
        extracted = "\n\n".join(lines).strip()
        if not extracted:
            raise HTTPException(status_code=422, detail="DOCX contains no extractable text")
        return extracted
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Failed to parse DOCX: {str(e)}")


def parse_doc(file_bytes: bytes | bytearray, *, max_extracted_bytes: int = MAX_EXTRACTED_TEXT_BYTES) -> str:
    textutil_bin = "/usr/bin/textutil"
    if not os.path.exists(textutil_bin):
        raise HTTPException(
            status_code=422,
            detail="Legacy .doc format requires macOS textutil converter, which is not available on this system",
        )

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".doc", delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name

        # Route stdout to a bounded temporary file rather than keeping an
        # unbounded subprocess result in Python memory.
        with tempfile.TemporaryFile() as output:
            proc = subprocess.Popen(
                [textutil_bin, "-convert", "txt", "-stdout", tmp_path],
                stdout=output,
                stderr=subprocess.DEVNULL,
            )
            try:
                return_code = proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise HTTPException(status_code=422, detail="textutil command timed out while parsing .doc")
            if output.tell() > max_extracted_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"Extracted DOC text exceeds the {_limit_label(max_extracted_bytes)} limit",
                )
            if return_code != 0:
                raise HTTPException(status_code=422, detail="textutil failed to parse .doc file")
            output.seek(0)
            text = output.read(max_extracted_bytes + 1).decode("utf-8", errors="replace").strip()
        if not text:
            raise HTTPException(status_code=422, detail="DOC contains no extractable text")
        return text
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=422, detail="textutil command timed out while parsing .doc")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def import_document(
    file_bytes: bytes | bytearray, filename: str, project_id: str | None = None,
    *, max_upload_bytes: int = MAX_UPLOAD_BYTES,
    max_extracted_bytes: int = MAX_EXTRACTED_TEXT_BYTES,
) -> dict[str, Any]:
    if not file_bytes:
        raise HTTPException(status_code=422, detail="Document file is empty")
    if len(file_bytes) > max_upload_bytes:
        raise HTTPException(status_code=413, detail=f"Upload exceeds the {_limit_label(max_upload_bytes)} limit")

    lower = filename.lower()
    if lower.endswith((".md", ".markdown", ".txt")):
        if len(file_bytes) > MAX_TEXT_INPUT_BYTES:
            raise HTTPException(status_code=413, detail=f"Text upload exceeds the {_limit_label(MAX_TEXT_INPUT_BYTES)} limit")
        extracted_text = _decode_text(file_bytes).strip()
        if not extracted_text:
            raise HTTPException(status_code=422, detail="Text document is empty")
    elif lower.endswith(".json"):
        if len(file_bytes) > MAX_TEXT_INPUT_BYTES:
            raise HTTPException(status_code=413, detail=f"JSON upload exceeds the {_limit_label(MAX_TEXT_INPUT_BYTES)} limit")
        raw_text = _decode_text(file_bytes)
        if not raw_text.strip():
            raise HTTPException(status_code=422, detail="JSON document is empty")
        if len(raw_text.encode("utf-8")) > max_extracted_bytes:
            raise HTTPException(status_code=413, detail=f"JSON text exceeds the {_limit_label(max_extracted_bytes)} extracted-text limit")
        _preflight_json(raw_text)
        try:
            json.loads(raw_text)
            # Keep the validated input verbatim. Pretty-printing would build a
            # second, much larger string and can expand compact arrays manyfold.
            extracted_text = raw_text
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=422, detail=f"Invalid JSON document: {str(e)}")
    elif lower.endswith(".pdf"):
        extracted_text = parse_pdf(file_bytes, max_extracted_bytes=max_extracted_bytes)
    elif lower.endswith(".docx"):
        extracted_text = parse_docx(file_bytes, max_extracted_bytes=max_extracted_bytes)
    elif lower.endswith(".doc"):
        extracted_text = parse_doc(file_bytes, max_extracted_bytes=max_extracted_bytes)
    else:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported document format: '{filename}'. Supported: .md, .txt, .json, .pdf, .docx, .doc",
        )

    if len(extracted_text.encode("utf-8")) > max_extracted_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Extracted document text exceeds the {_limit_label(max_extracted_bytes)} limit",
        )

    # Save as content with kind='document', category='knowledge'
    return create_content(
        title=filename,
        content=extracted_text,
        project_id=project_id,
        kind="document",
        category="knowledge",
        agent="import",
        _deduplicate_import=True,
    )
