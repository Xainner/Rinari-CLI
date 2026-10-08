"""Entrada del proceso hijo de un trabajo documental.

`python -m rinari.documents.worker <workdir>` lee `request.json`, corre la
operación registrada y deja `result.json` con el resultado y los archivos
producidos (rutas relativas a `workdir`). Nunca recibe código: solo el nombre
de una operación cerrada y su solicitud validada.
"""

from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rinari.documents.contracts import DocumentError, DocumentErrorCode

Operation = Callable[[dict[str, Any], Path], tuple[dict[str, Any], dict[str, bytes]]]


def _operations() -> dict[str, Operation]:
    from rinari.documents import operations

    return operations.REGISTRY


def run(workdir: Path) -> int:
    request = json.loads((workdir / "request.json").read_text(encoding="utf-8"))
    name = request.get("operation")
    try:
        operation = _operations().get(name)
        if operation is None:
            raise DocumentError(DocumentErrorCode.UNSUPPORTED_FEATURE, f"Unknown operation {name}")
        out = workdir / "out"
        out.mkdir(exist_ok=True)
        result, files = operation(request.get("request") or {}, out)
        written: dict[str, str] = {}
        for index, (key, data) in enumerate(files.items()):
            if isinstance(data, Path):
                # Salidas grandes (un dataset, un CSV) ya están en `out`: no pasan por memoria.
                written[key] = str(data.resolve().relative_to(workdir.resolve()))
                continue
            target = out / f"file-{index}"
            target.write_bytes(data)
            written[key] = str(target.relative_to(workdir))
        payload: dict[str, Any] = {"result": result, "files": written}
    except DocumentError as exc:
        payload = {"error": exc.to_dict()}
    except Exception as exc:
        payload = {
            "error": {
                "code": DocumentErrorCode.VALIDATION_FAILED.value,
                "message": f"{exc.__class__.__name__}: {exc}",
                "details": {"trace": traceback.format_exc()[-1500:]},
            }
        }
    (workdir / "result.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(run(Path(sys.argv[1])))
