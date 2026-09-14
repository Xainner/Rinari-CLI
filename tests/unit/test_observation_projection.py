import json

from rinari.tools.definition import ArtifactRef, ToolResult
from rinari.tools.observations import project_result


def project(result, budget=65536, tool="fs.read"):
    saved = {}

    def spill(suffix, text):
        uri = "artifact://session/runtime/" + suffix + ".txt"
        saved[uri] = text
        return ArtifactRef(uri=uri, name=suffix, kind="tool-output")

    output = project_result(result, tool=tool, budget=budget, spill=spill)
    return output, saved


def test_ten_kib_read_is_delivered_without_serialization_loss():
    source = ToolResult(ok=True, data={"text": "x" * 10240})
    output, saved = project(source)
    assert json.loads(output.to_model_text())["data"] == source.data
    assert not saved


def test_six_large_files_have_fair_excerpts_and_full_recovery():
    files = [
        {"path": f"file-{i}", "ok": True, "data": {"text": (f"line-{i} ñ\r\n" * 10000)}}
        for i in range(6)
    ]
    output, saved = project(ToolResult(ok=True, data={"files": files}), 12000)
    assert len(output.to_model_text("fs.read").encode("utf-8")) <= 12000
    rows = output.data["files"]
    assert len(rows) == 6
    for original, row in zip(files, rows, strict=True):
        assert row["text"] and original["data"]["text"].startswith(row["text"])
        assert json.loads(saved[row["result_ref"]]) == original
    assert json.loads(saved[output.data["result_ref"]])["data"]["files"] == files


def test_artifact_repage_unicode_advances_without_spilling():
    original = 'ñ"\\\r\n' * 5000
    data = {
        "uri": "artifact://session/runtime/full.txt",
        "start_byte": 0,
        "end_byte": len(original.encode("utf-8")),
        "size_bytes": len(original.encode("utf-8")),
        "text": original,
    }
    recovered = ""
    while data["text"]:
        output, saved = project(ToolResult(ok=True, data=data), 2000, "artifact.read")
        assert not saved
        assert len(output.to_model_text("artifact.read").encode("utf-8")) <= 2000
        assert output.ok and output.data["text"]
        recovered += output.data["text"]
        end = output.data["end_byte"]
        assert end == len(recovered.encode("utf-8"))
        data = {**data, "start_byte": end, "text": original.encode("utf-8")[end:].decode("utf-8")}
    assert recovered == original


def test_partial_source_is_distinct_from_delivery():
    output, _ = project(ToolResult(ok=True, truncated=True, data={"text": "x" * 10000}), 2000)
    assert output.data["source_partial"] and output.data["delivery_partial"]


def test_storage_failure_never_returns_fake_reference():
    import pytest

    def fail(*args):
        raise OSError("disk full")

    with pytest.raises(OSError):
        project_result(
            ToolResult(ok=True, data="x" * 10000), tool="fs.read", budget=2000, spill=fail
        )


def test_round_redistributes_small_results_without_exceeding_caps():
    from rinari.tools.observations import allocate_budgets

    budgets = allocate_budgets([1000, 1000, 60000, 60000], 65536, 100000)
    assert budgets == [1000, 1000, 49000, 49000]
    assert sum(budgets) == 100000
    assert allocate_budgets([500000, 1], 65536, 262144) == [65536, 1]
